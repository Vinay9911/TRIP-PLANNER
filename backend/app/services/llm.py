"""Language model access.

Every LLM call in the system goes through this module, which exists to make
one decision explicit: **not every step of an agent needs the same model.**

A single plan-and-execute turn issues somewhere between six and twelve model
calls - plan, tool-selection rounds, replan checks, memory extraction,
language detection, final composition. The Groq free tier allows roughly
1,000 requests per day. Spending a frontier-class call on "which language is
this message written in?" is how you exhaust that budget before lunch.

So calls are routed to one of three roles:

===========  ==========================  ===================================
Role         Default model               Used for
===========  ==========================  ===================================
``planner``  ``openai/gpt-oss-120b``     Decomposing goals, replanning,
                                         deciding whether to clarify.
``executor`` ``llama-3.3-70b-versatile`` Tool selection and final prose.
``utility``  ``llama-3.1-8b-instant``    Language detection, memory
                                         extraction, contradiction checks.
===========  ==========================  ===================================

The split is quality-driven as much as cost-driven: planning is the step
where a weaker model visibly degrades output, while classification and
extraction are tasks an 8B model does well when given a strict output schema.

The models are configuration, not code. Swapping the whole system onto
OpenAI or Anthropic means changing `.env` and one factory function here.

**Keys rotate.** `GROQ_API_KEY` accepts a comma-separated list, and every call
draws from a `KeyPool` (see `app/services/keys.py`). When a key reports a rate
limit the call retries **immediately on the next key** rather than backing off,
because the limit belongs to the key rather than to the service - sleeping
would waste time while a perfectly usable key sits idle. Backoff resumes only
once every key in the pool is cooling down.
"""

from __future__ import annotations

import asyncio
import random
from enum import StrEnum
from functools import lru_cache
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from pydantic import BaseModel
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError, RateLimitError
from app.core.logging import get_logger
from app.services.keys import AllKeysExhausted, KeyPool, get_pool

logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class ModelRole(StrEnum):
    """Which class of work a model call belongs to.

    Callers name the *role* rather than the model, so the mapping from work to
    model stays in one place and can be retuned without touching call sites.
    """

    PLANNER = "planner"
    EXECUTOR = "executor"
    UTILITY = "utility"


@lru_cache(maxsize=8)
def _build_model(
    role: ModelRole,
    model_name: str,
    temperature: float,
    timeout: int,
    api_key: str,
) -> BaseChatModel:
    """Construct and cache a chat model client.

    Cached because instantiating a client per call would discard the
    underlying HTTP connection pool and add a TLS handshake to every request.
    The cache key includes all parameters, so differently-configured models
    do not collide.

    Args:
        role: The role this client serves. Part of the cache key.
        model_name: Provider model identifier.
        temperature: Sampling temperature.
        timeout: Per-request timeout in seconds.
        api_key: Provider credential drawn from the pool. Part of the cache
            key, so each key gets its own client and connection pool.

    Returns:
        A configured chat model.
    """
    from langchain_groq import ChatGroq

    # `max_retries=0` because retries are handled by `invoke_with_retry`
    # below, which distinguishes rate limits from other failures and logs
    # each attempt. Leaving the client's own retry loop enabled would produce
    # silent, unlogged retries nested inside ours.
    return ChatGroq(
        model=model_name,
        temperature=temperature,
        timeout=timeout,
        max_retries=0,
        api_key=api_key,  # type: ignore[arg-type]
    )


def get_llm_pool(settings: Settings | None = None) -> KeyPool:
    """Return the shared Groq key pool.

    Args:
        settings: Settings override, for tests.

    Returns:
        The process-wide pool built from `GROQ_API_KEY`.

    Raises:
        ConfigurationError: If no key is configured.
    """
    cfg = settings or get_settings()
    return get_pool("groq", cfg.groq_api_key.get_secret_value())


def _resolve_role(role: ModelRole, cfg: Settings, temperature: float | None) -> tuple[str, float]:
    """Map a role onto its model name and sampling temperature.

    Args:
        role: Which class of work the call belongs to.
        cfg: Application settings.
        temperature: Explicit override, or None for the role default.

    Returns:
        A tuple of (model name, temperature).
    """
    model_by_role = {
        ModelRole.PLANNER: cfg.llm_planner_model,
        ModelRole.EXECUTOR: cfg.llm_executor_model,
        ModelRole.UTILITY: cfg.llm_utility_model,
    }
    default_temperature_by_role = {
        # Planning is near-deterministic: the same goal should decompose the
        # same way twice, otherwise debugging a bad plan is guesswork.
        ModelRole.PLANNER: cfg.llm_planner_temperature,
        ModelRole.EXECUTOR: cfg.llm_responder_temperature,
        # Extraction and classification want the single most likely answer.
        ModelRole.UTILITY: 0.0,
    }

    return (
        model_by_role[role],
        temperature if temperature is not None else default_temperature_by_role[role],
    )


def get_model(
    role: ModelRole, *, settings: Settings | None = None, temperature: float | None = None
) -> BaseChatModel:
    """Return a chat model for a role, bound to one key from the pool.

    Used where a model object must be handed to something that owns its own
    call loop - `create_agent` in the executor node. Because the key is fixed
    for that object's lifetime, rotation cannot happen *inside* such a call;
    the executor instead catches a rate limit and asks for a fresh model, which
    draws the next key.

    For ordinary calls prefer `call_model` or `structured_call`, which rotate
    per attempt.

    Args:
        role: Which class of work the call belongs to.
        settings: Settings override, for tests.
        temperature: Sampling override.

    Returns:
        A ready-to-use chat model.

    Raises:
        AllKeysExhausted: If every key is currently cooling down.
    """
    cfg = settings or get_settings()
    model_name, resolved_temperature = _resolve_role(role, cfg, temperature)

    return _build_model(
        role=role,
        model_name=model_name,
        temperature=resolved_temperature,
        timeout=cfg.llm_timeout_seconds,
        api_key=get_llm_pool(cfg).acquire(),
    )


def _classify_provider_error(exc: Exception) -> Exception:
    """Translate a provider exception into this application's error taxonomy.

    Providers signal quota exhaustion in several shapes - a typed exception, a
    429 status attribute, or just a string. Normalising here means the retry
    policy and the admin dashboard both see one consistent error type.

    Args:
        exc: The exception raised by the provider client.

    Returns:
        A `RateLimitError` or `ExternalServiceError` wrapping the original.
    """
    text = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)

    if status == 429 or "rate limit" in text or "too many requests" in text:
        retry_after = getattr(exc, "retry_after", None)
        return RateLimitError(
            "The language model provider rejected the request for exceeding a rate limit.",
            service="groq",
            retry_after_seconds=float(retry_after) if retry_after else None,
            details={"original": str(exc)[:500]},
        )

    return ExternalServiceError(
        "The language model provider could not complete the request.",
        service="groq",
        details={"original": str(exc)[:500], "status": status},
    )


async def _invoke_rotating(
    role: ModelRole,
    messages: list[BaseMessage],
    *,
    purpose: str,
    cfg: Settings,
    temperature: float | None = None,
    schema: type[BaseModel] | None = None,
    max_attempts: int | None = None,
) -> Any:
    """Invoke a model, rotating keys on rate limits and backing off on faults.

    The core of the failover behaviour. Each attempt draws a fresh key from the
    pool, so a rate-limited key is skipped rather than waited on.

    The two failure paths are handled differently on purpose:

    * **Rate limited** - the key is rested and the loop retries *immediately*
      with the next key. No sleep: the quota belongs to the key, and another
      key is not exhausted. Sleeping here is the mistake a single-key retry
      policy makes, and with a pool it is pure wasted latency.
    * **Any other failure** - likely transient and provider-wide (a network
      blip, a 503), so the key is rested briefly and the loop *does* back off
      with jitter before trying again.

    Args:
        role: Which model tier to use.
        messages: Conversation to send.
        purpose: Short label for logs.
        cfg: Application settings.
        temperature: Sampling override.
        schema: When given, the call is made with structured output and the
            parsed model is returned instead of a message.
        max_attempts: Attempt ceiling. Defaults to one attempt per key plus
            two, so every key gets a turn before the call is abandoned.

    Returns:
        The model's reply, or an instance of `schema`.

    Raises:
        AllKeysExhausted: If every key is cooling down for longer than one
            request should absorb.
        ExternalServiceError: If every attempt failed for another reason.
    """
    pool = get_llm_pool(cfg)
    model_name, resolved_temperature = _resolve_role(role, cfg, temperature)
    attempts = max_attempts or (pool.size + 2)

    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            api_key = pool.acquire()
        except AllKeysExhausted as exhausted:
            last_error = exhausted
            # Every key is resting. Wait only if the pause is short enough to
            # be worth absorbing inside one request; otherwise fail fast so the
            # agent can degrade rather than hang.
            if attempt < attempts and exhausted.retry_after_seconds <= 20:
                await asyncio.sleep(exhausted.retry_after_seconds + 0.5)
                continue
            raise

        model: Any = _build_model(
            role=role,
            model_name=model_name,
            temperature=resolved_temperature,
            timeout=cfg.llm_timeout_seconds,
            api_key=api_key,
        )
        if schema is not None:
            model = model.with_structured_output(schema)

        try:
            response = await model.ainvoke(messages)
        except Exception as exc:  # noqa: BLE001
            # Broad by necessity: provider SDKs raise their own hierarchies and
            # occasionally bare exceptions. Everything is normalised into this
            # application's taxonomy on the next line, and the loop decides
            # from the type whether to rotate keys or back off.
            error = _classify_provider_error(exc)
            last_error = error

            if isinstance(error, RateLimitError):
                pool.report_rate_limited(api_key, error.retry_after_seconds)
                logger.info(
                    "llm.key_rotated",
                    purpose=purpose,
                    attempt=attempt,
                    keys_available=pool.available_count(),
                )
                # Straight to the next key - deliberately no sleep.
                continue

            pool.report_error(api_key)
            logger.warning(
                "llm.attempt_failed",
                purpose=purpose,
                attempt=attempt,
                max_attempts=attempts,
                error_code=getattr(error, "code", "unknown"),
                error=str(exc)[:300],
            )
            if attempt >= attempts:
                break
            # Jittered, so concurrent requests do not resynchronise into a
            # burst that re-trips whatever caused the failure. S311: this is
            # retry timing, not a security decision - `random` is correct here.
            jitter = 0.5 + random.random() * 0.5  # noqa: S311
            await asyncio.sleep(min(2.0 ** (attempt - 1), 8.0) * jitter)
            continue

        pool.report_success(api_key)

        if schema is not None:
            # `with_structured_output` may hand back a plain dict depending on
            # the provider's tool-calling implementation. Validate, never trust.
            if not isinstance(response, schema):
                try:
                    response = schema.model_validate(response)
                except Exception as exc:
                    raise ExternalServiceError(
                        "Model returned a reply that does not satisfy the "
                        f"{schema.__name__} schema.",
                        service="groq",
                        details={"purpose": purpose, "received": str(response)[:300]},
                    ) from exc
            logger.info("llm.structured_completed", purpose=purpose, schema=schema.__name__)
            return response

        usage = getattr(response, "usage_metadata", None) or {}
        logger.info(
            "llm.completed",
            purpose=purpose,
            attempt=attempt,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        return response

    raise last_error or ExternalServiceError(
        "The language model provider could not complete the request.", service="groq"
    )


async def call_model(
    role: ModelRole,
    messages: list[BaseMessage],
    *,
    purpose: str,
    settings: Settings | None = None,
    temperature: float | None = None,
) -> BaseMessage:
    """Call a model for free-text output, rotating keys as needed.

    Args:
        role: Which model tier to use.
        messages: Conversation to send.
        purpose: Short label for logs, e.g. `"compose_response"`. Makes it
            possible to see which stage of a run burned the quota.
        settings: Settings override, for tests.
        temperature: Sampling override.

    Returns:
        The model's reply.

    Raises:
        ExternalServiceError: If every attempt fails.
    """
    cfg = settings or get_settings()
    result: BaseMessage = await _invoke_rotating(
        role, messages, purpose=purpose, cfg=cfg, temperature=temperature
    )
    return result


async def invoke_with_retry(
    model: BaseChatModel,
    messages: list[BaseMessage],
    *,
    purpose: str,
    max_attempts: int = 3,
) -> BaseMessage:
    """Invoke a pre-built model, retrying transient failures with backoff.

    Retained for callers that already hold a model object. It cannot rotate
    keys, because the key was fixed when the model was constructed - prefer
    `call_model`, which does.

    Args:
        model: The chat model to call.
        messages: Conversation to send.
        purpose: Short label for logs.
        max_attempts: Total attempts including the first.

    Returns:
        The model's reply.

    Raises:
        ExternalServiceError: If every attempt fails.
    """
    attempt_number = 0

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=1.0, max=20.0),
        retry=retry_if_exception_type((ExternalServiceError, RateLimitError)),
        reraise=True,
    ):
        with attempt:
            attempt_number += 1
            try:
                response = await model.ainvoke(messages)
            except Exception as exc:
                error = _classify_provider_error(exc)
                logger.warning(
                    "llm.attempt_failed",
                    purpose=purpose,
                    attempt=attempt_number,
                    error=str(exc)[:300],
                )
                raise error from exc

            usage = getattr(response, "usage_metadata", None) or {}
            logger.info(
                "llm.completed",
                purpose=purpose,
                attempt=attempt_number,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
            return response

    raise ExternalServiceError(  # pragma: no cover - unreachable with reraise=True
        "Retry loop exited without a result.", service="groq"
    )


async def structured_call(
    role: ModelRole,
    messages: list[BaseMessage],
    schema: type[SchemaT],
    *,
    purpose: str,
    settings: Settings | None = None,
    temperature: float | None = None,
) -> SchemaT:
    """Call a model and parse its reply into a Pydantic model.

    Structured output is used for every internal decision the agent makes -
    the plan, memory candidates, the clarification verdict - rather than
    parsing prose. Free-text parsing is where agent pipelines rot: a model that
    one day writes "Step 1:" instead of "1." silently breaks a regex, whereas a
    schema violation fails loudly and immediately.

    Rotates keys on rate limits exactly as `call_model` does.

    Args:
        role: Which model tier to use.
        messages: Conversation to send.
        schema: Pydantic model describing the expected reply.
        purpose: Short label for logs.
        settings: Settings override, for tests.
        temperature: Sampling override.

    Returns:
        An instance of `schema`.

    Raises:
        ExternalServiceError: If the provider fails, or returns something that
            cannot be coerced into the schema.
    """
    cfg = settings or get_settings()
    result: SchemaT = await _invoke_rotating(
        role, messages, purpose=purpose, cfg=cfg, temperature=temperature, schema=schema
    )
    return result


def extract_usage(response: Any) -> dict[str, int]:
    """Pull token counts off a model response.

    Args:
        response: A message or result returned by a chat model.

    Returns:
        A dict with `input_tokens` and `output_tokens`, zeroed when the
        provider did not report usage.
    """
    usage = getattr(response, "usage_metadata", None) or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }
