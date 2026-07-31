"""Chat routes: the main conversational endpoint.

`POST /api/v1/chat` is the endpoint the assignment's "conversational
interface" requirement refers to. It accepts a message, runs the agent, and
returns the reply plus enough trace detail for a client to show its work.

Two behaviours here are worth noting.

**Memory extraction is scheduled, not awaited.** It runs in a FastAPI
background task after the response is sent. Extraction costs a model call and
several embedding calls, and its benefit lands on the traveller's *next*
session - so making them wait for it would be paying latency now for value
later.

**Sessions are created implicitly.** Omitting `session_id` starts a new
conversation and returns its id. A client that always sends the id back gets
continuity for free, and one that never does gets stateless behaviour, without
either needing a separate "create session" call first.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, status

from app.api.deps import CurrentUser, Profiles, Sessions, get_runner
from app.core.errors import ResourceNotFoundError
from app.core.logging import bind_request_context, get_logger
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    ErrorResponse,
    MessageOut,
    PlanStepSummary,
    SessionDetail,
    SessionSummary,
    ToolCallSummary,
)

logger = get_logger(__name__)

router = APIRouter(tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        429: {"model": ErrorResponse, "description": "Rate limited"},
        502: {"model": ErrorResponse, "description": "An upstream service failed"},
    },
    summary="Send a message to the travel planning agent",
)
async def chat(
    payload: ChatRequest,
    user: CurrentUser,
    sessions: Sessions,
    profiles: Profiles,
    background: BackgroundTasks,
    runner: Annotated[object, Depends(get_runner)],
) -> ChatResponse:
    """Send a message and receive the agent's reply.

    The agent decides for itself which tools to use, plans multi-step work,
    applies preferences remembered from previous conversations, and replies in
    the language the message was written in.

    It runs in one of three gears, reported as `mode`: a request too vague to
    act on gets one short clarifying question (`clarify`); a named destination
    without a specified trip gets grounded suggestions, a draft outline and at
    most two questions (`advise`); a specified trip - or an explicit "just
    plan it" - runs the full plan-execute pipeline (`plan`). The conversation's
    gathered details are returned as `trip_state`, and the `focus` field lets
    a client switch flights / attractions / stays / restaurants on and off.

    Args:
        payload: The message and optional session id.
        user: The authenticated caller.
        sessions: Conversation repository.
        profiles: Profile repository, consulted for the memory opt-out.
        background: FastAPI background task queue.
        runner: The agent runner.

    Returns:
        The reply with plan and tool-call summaries.
    """
    # A supplied session id is validated against the caller's own sessions.
    # `get` runs under RLS, so another user's id raises not-found rather than
    # letting a caller append messages to someone else's conversation.
    if payload.session_id:
        session = await sessions.get(payload.session_id, user.id)
        session_id = session["id"]
    else:
        session = await sessions.create(user.id, title=payload.message[:80])
        session_id = session["id"]

    bind_request_context(user_id=user.id, session_id=session_id)

    # The slot ledger survives across turns in the session row; the focus
    # selection travels with each request when the user touches the toggles,
    # and falls back to what the conversation last used when they do not -
    # so a toggle set once holds for the whole conversation, on any device.
    trip_state = dict(session.get("trip_state") or {})
    focus = payload.focus if payload.focus is not None else trip_state.get("focus")
    if payload.focus is not None:
        trip_state["focus"] = list(payload.focus)

    result = await runner.run_turn(  # type: ignore[attr-defined]
        user_id=user.id,
        session_id=session_id,
        message=payload.message,
        trip_state=trip_state,
        focus=focus,
        local_only=payload.local_only,
    )

    # Respect the memory opt-out by not scheduling extraction at all, rather
    # than extracting and hiding the result.
    if await profiles.memory_enabled(user.id):
        background.add_task(
            runner.extract_memories_background,  # type: ignore[attr-defined]
            user_id=user.id,
            session_id=session_id,
            user_message=payload.message,
            assistant_reply=result.response,
            message_id=result.user_message_id,
        )

    background.add_task(profiles.touch_last_seen, user.id)

    return ChatResponse(
        session_id=result.session_id,
        run_id=result.run_id,
        response=result.response,
        status=result.status,  # type: ignore[arg-type]
        mode=result.mode,  # type: ignore[arg-type]
        trip_state=result.trip_state,
        llm_providers=result.llm_providers,
        suggested_options=result.suggested_options,
        itinerary=result.itinerary,
        suggested_actions=result.suggested_actions,
        needs_clarification=result.needs_clarification,
        detected_language=result.detected_language,
        destination=result.destination,
        plan=[
            PlanStepSummary(description=step.description, kind=step.kind) for step in result.plan
        ],
        tool_calls=[
            ToolCallSummary(
                tool=record.tool_name,
                status=record.status,  # type: ignore[arg-type]
                source=record.source,
                latency_ms=record.latency_ms,
            )
            for record in result.tool_calls
        ],
        steps_executed=result.steps_executed,
        replan_count=result.replan_count,
        latency_ms=result.latency_ms,
        total_tokens=result.prompt_tokens + result.completion_tokens,
    )


@router.get(
    "/sessions",
    response_model=list[SessionSummary],
    summary="List your conversations",
)
async def list_sessions(user: CurrentUser, sessions: Sessions) -> list[SessionSummary]:
    """List the caller's conversations, most recently updated first."""
    rows = await sessions.list_for_user(user.id)
    return [SessionSummary(**row) for row in rows]


@router.get(
    "/sessions/{session_id}",
    response_model=SessionDetail,
    responses={404: {"model": ErrorResponse, "description": "Not found"}},
    summary="Get one conversation with its messages",
)
async def get_session(session_id: str, user: CurrentUser, sessions: Sessions) -> SessionDetail:
    """Fetch one conversation and its full message history.

    Returns 404 for a conversation belonging to another user - the same
    response as one that does not exist, so the endpoint cannot be used to
    discover valid session ids.
    """
    session = await sessions.get(session_id, user.id)
    messages = await sessions.get_messages(session_id, user.id)

    return SessionDetail(
        session=SessionSummary(**session),
        messages=[MessageOut(**message) for message in messages],
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a conversation",
)
async def delete_session(session_id: str, user: CurrentUser, sessions: Sessions) -> None:
    """Archive a conversation so it no longer appears in listings.

    A soft delete. Permanently erasing derived data is a separate, explicit
    operation - see `DELETE /api/v1/me/data`.
    """
    if not await sessions.archive(session_id, user.id):
        raise ResourceNotFoundError(f"Session {session_id} was not found.")
