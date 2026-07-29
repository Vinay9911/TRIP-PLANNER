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
"""

from __future__ import annotations

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
from app.core.errors import ConfigurationError, ExternalServiceError, RateLimitError
from app.core.logging import get_logger

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
        api_key: Provider credential.

    Returns:
        A configured chat model.

    Raises:
        ConfigurationError: If no API key is available.
    """
    if not api_key:
        raise ConfigurationError(
            "GROQ_API_KEY is not set. Get a free key at https://console.groq.com "
            "and add it to your .env file."
        )

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


def get_model(
    role: ModelRole, *, settings: Settings | None = None, temperature: float | None = None
) -> BaseChatModel:
    """Return the chat model configured for a role.

    Args:
        role: Which class of work the call belongs to.
        settings: Settings override, for tests.
        temperature: Sampling override. Defaults to the role's configured
            temperature.

    Returns:
        A ready-to-use chat model.
    """
    cfg = settings or get_settings()

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

    return _build_model(
        role=role,
        model_name=model_by_role[role],
        temperature=temperature if temperature is not None else default_temperature_by_role[role],
        timeout=cfg.llm_timeout_seconds,
        api_key=cfg.groq_api_key.get_secret_value(),
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


async def invoke_with_retry(
    model: BaseChatModel,
    messages: list[BaseMessage],
    *,
    purpose: str,
    max_attempts: int = 3,
) -> BaseMessage:
    """Invoke a chat model, retrying transient failures with backoff.

    Only `ExternalServiceError` and `RateLimitError` are retried. A malformed
    request or a missing credential is not transient, and retrying it wastes
    quota that the rest of the run needs.

    Args:
        model: The chat model to call.
        messages: Conversation to send.
        purpose: Short label for logs, e.g. `"plan"` or `"extract_memories"`.
            Makes it possible to see which stage of a run burned the quota.
        max_attempts: Total attempts including the first.

    Returns:
        The model's reply.

    Raises:
        ExternalServiceError: If every attempt fails.
        RateLimitError: If every attempt is rate limited.
    """
    attempt_number = 0

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        # Jittered backoff: a synchronised retry storm across concurrent
        # requests would re-trip the same per-minute limit that caused it.
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
                    max_attempts=max_attempts,
                    error_code=getattr(error, "code", "unknown"),
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
    parsing prose. Free-text parsing is where agent pipelines rot: a model
    that one day writes "Step 1:" instead of "1." silently breaks a regex,
    whereas a schema violation fails loudly and immediately.

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
    model = get_model(role, settings=settings, temperature=temperature)
    structured = model.with_structured_output(schema)

    try:
        result = await structured.ainvoke(messages)
    except Exception as exc:
        error = _classify_provider_error(exc)
        logger.warning("llm.structured_failed", purpose=purpose, error=str(exc)[:300])
        raise error from exc

    if not isinstance(result, schema):
        # `with_structured_output` can hand back a plain dict depending on the
        # provider's tool-calling implementation. Validate rather than trust.
        try:
            result = schema.model_validate(result)
        except Exception as exc:
            raise ExternalServiceError(
                f"Model returned a reply that does not satisfy the {schema.__name__} schema.",
                service="groq",
                details={"purpose": purpose, "received": str(result)[:300]},
            ) from exc

    logger.info("llm.structured_completed", purpose=purpose, schema=schema.__name__)
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
