"""Request and response models for the chat API.

Kept separate from internal domain types so the public contract is a
deliberate choice rather than whatever the agent's state happens to look like.
`AgentState` can be refactored freely without breaking a client.

Each model carries `json_schema_extra` examples, which FastAPI renders into
the OpenAPI docs at `/docs`. Since the interactive docs are the primary API
documentation for this project, a realistic example is worth more than a
paragraph of prose.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """A message from the traveller."""

    message: str = Field(
        min_length=1,
        max_length=4000,
        description="What the traveller wants, in any language.",
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Conversation to continue. Omit to start a new one - the id is "
            "returned in the response and should be reused for follow-ups so "
            "the agent keeps context."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "message": "Plan me 2 relaxed days in Kyoto. I'm vegetarian.",
                    "session_id": None,
                },
                {
                    "message": "Make day two lighter, we'll be tired.",
                    "session_id": "3f2a...",
                },
            ]
        }
    }


class ToolCallSummary(BaseModel):
    """One tool invocation, as exposed to clients."""

    tool: str
    status: Literal["ok", "degraded", "invalid"]
    source: str
    latency_ms: int


class PlanStepSummary(BaseModel):
    """One planned step, as exposed to clients."""

    description: str
    kind: str


class ChatResponse(BaseModel):
    """The agent's reply, with enough trace detail to be inspectable.

    The trace fields exist so a client can *show its work* - which tools ran,
    what the plan was. That is the difference between an assistant users
    trust and a black box, and it is also what makes the demo verifiable.
    """

    session_id: str = Field(description="Pass this back on the next message.")
    run_id: str = Field(description="Look up the full execution trace with this.")
    response: str = Field(description="The reply, in the traveller's language.")
    status: Literal["completed", "clarifying", "partial", "failed"]
    needs_clarification: bool = Field(
        description="True when the reply is a question rather than an itinerary."
    )
    detected_language: str
    destination: str | None = None
    plan: list[PlanStepSummary] = Field(default_factory=list)
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    steps_executed: int = 0
    replan_count: int = 0
    latency_ms: int = 0

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "session_id": "3f2a9c1e-...",
                    "run_id": "8b7d4e2f-...",
                    "response": "Here's a relaxed two days in Kyoto...",
                    "status": "completed",
                    "needs_clarification": False,
                    "detected_language": "en",
                    "destination": "Kyoto",
                    "plan": [
                        {
                            "description": "Research Kyoto districts suiting a slow pace",
                            "kind": "research",
                        },
                        {"description": "Find vegetarian dining options", "kind": "research"},
                        {"description": "Compose the itinerary", "kind": "compose"},
                    ],
                    "tool_calls": [
                        {
                            "tool": "search_travel_guide",
                            "status": "ok",
                            "source": "wikivoyage",
                            "latency_ms": 2410,
                        },
                        {
                            "tool": "find_places",
                            "status": "ok",
                            "source": "geoapify",
                            "latency_ms": 380,
                        },
                    ],
                    "steps_executed": 3,
                    "replan_count": 0,
                    "latency_ms": 11840,
                }
            ]
        }
    }


class SessionSummary(BaseModel):
    """A conversation in a list view."""

    id: str
    title: str | None = None
    destination: str | None = None
    language: str | None = None
    message_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MessageOut(BaseModel):
    """One stored message."""

    id: str
    role: Literal["user", "assistant", "system"]
    content: str
    language: str | None = None
    created_at: datetime | None = None


class SessionDetail(BaseModel):
    """A conversation with its messages."""

    session: SessionSummary
    messages: list[MessageOut] = Field(default_factory=list)


class MemoryOut(BaseModel):
    """One stored long-term memory, as shown to its owner.

    `mention_count` and `confidence` are exposed deliberately: a user asking
    "why does it think that?" deserves an answer, and reinforcement count is
    the most legible part of it.
    """

    id: str
    memory_type: str
    subject: str
    content: str
    confidence: float
    mention_count: int
    status: str = "active"
    source_lang: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class FeedbackRequest(BaseModel):
    """Thumbs up or down on a reply."""

    message_id: str
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=1000)


class ErrorResponse(BaseModel):
    """The error envelope every failure returns."""

    error: dict[str, Any] = Field(
        description="Contains `code`, `message`, `details` and `retryable`.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests. Please wait a moment.",
                        "details": {"retry_after_seconds": 12},
                        "retryable": True,
                    }
                }
            ]
        }
    }
