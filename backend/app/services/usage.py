"""Per-run token accounting.

Every model call adds its usage here, so a finished run can report what it
actually cost. Without this the `prompt_tokens` columns on `agent_runs` stay
zero and the only way to learn why requests fail is to infer it from rate-limit
errors - which is how the free-tier ceiling stayed invisible for so long.

Context-local, matching the tool recorder: concurrent requests each meter their
own usage with no shared state and no parameter threaded through every call.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class TokenUsage:
    """Tokens consumed during one agent run, broken down by stage."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    #: Per-purpose totals, e.g. {"create_plan": (1, 2400, 180)}.
    by_stage: dict[str, tuple[int, int, int]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """Prompt plus completion."""
        return self.prompt_tokens + self.completion_tokens

    def add(self, purpose: str, prompt: int, completion: int) -> None:
        """Record one model call.

        Args:
            purpose: Stage label, e.g. `"create_plan"`.
            prompt: Input tokens.
            completion: Output tokens.
        """
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.calls += 1

        calls, prompts, completions = self.by_stage.get(purpose, (0, 0, 0))
        self.by_stage[purpose] = (calls + 1, prompts + prompt, completions + completion)

    def summary(self) -> list[dict[str, object]]:
        """Render the per-stage breakdown, most expensive first."""
        rows = [
            {
                "stage": stage,
                "calls": calls,
                "prompt_tokens": prompts,
                "completion_tokens": completions,
                "total_tokens": prompts + completions,
            }
            for stage, (calls, prompts, completions) in self.by_stage.items()
        ]
        rows.sort(key=lambda row: row["total_tokens"], reverse=True)  # type: ignore[arg-type,return-value]
        return rows


_meter: ContextVar[TokenUsage | None] = ContextVar("token_usage", default=None)

# Separate from TokenUsage because it is written from a different layer - the
# RAG retriever, which has no reason to know about token accounting - and
# read back by the runner alongside it. `rag_hops` was declared on AgentState
# and on the agent_runs table from the start but nothing ever wrote to it, so
# every run reported zero hops regardless of how much retrieval actually
# happened; this is what closes that gap.
_hop_counter: ContextVar[list[int] | None] = ContextVar("rag_hop_counter", default=None)


def start_metering() -> TokenUsage:
    """Begin metering token usage for the current context.

    Returns:
        The meter the caller reads once the run finishes.
    """
    meter = TokenUsage()
    _meter.set(meter)
    _hop_counter.set([0])
    return meter


def stop_metering() -> None:
    """Stop metering for the current context."""
    _meter.set(None)
    _hop_counter.set(None)


def record(purpose: str, prompt: int, completion: int) -> None:
    """Add one call's usage to the active meter, if any.

    Args:
        purpose: Stage label.
        prompt: Input tokens.
        completion: Output tokens.
    """
    meter = _meter.get()
    if meter is not None:
        meter.add(purpose, prompt, completion)


def record_rag_hops(count: int) -> None:
    """Add to the current run's multi-hop retrieval count.

    Called once per `search_travel_guide` invocation with however many hops
    that retrieval actually took, so a run touching the tool several times
    reports the true total rather than the count from a single call.

    Args:
        count: Hops performed by one retrieval.
    """
    counter = _hop_counter.get()
    if counter is not None:
        counter[0] += count


def total_rag_hops() -> int:
    """Return the current run's accumulated hop count."""
    counter = _hop_counter.get()
    return counter[0] if counter is not None else 0
