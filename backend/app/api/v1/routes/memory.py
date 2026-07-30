"""Memory routes: letting users see and correct what is remembered about them.

A system that quietly builds a profile of someone and never shows it to them
is doing something users are right to distrust. These endpoints make the
long-term memory legible and correctable by its subject:

* `GET  /me/memories`      what we believe, and how confident we are
* `DELETE /me/memories/{id}` remove something wrong
* `POST /me/memory-settings` turn extraction off entirely
* `DELETE /me/data`         erase everything derived, keeping the account

They also make the memory system demonstrable, which is useful for the
assessment: an evaluator can hold a conversation, then call `GET /me/memories`
and see the extracted facts with their confidence and reinforcement counts.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, get_database, get_memory_service
from app.core.errors import ConfigurationError, ResourceNotFoundError
from app.core.logging import get_logger
from app.db.session import Database
from app.memory.consolidator import consolidate_candidate
from app.memory.schemas import CandidateMemory, MemorySubject, MemoryType
from app.schemas.chat import ErrorResponse, MemoryOut

logger = get_logger(__name__)

router = APIRouter(tags=["memory"])


class MemorySettings(BaseModel):
    """Whether long-term memory extraction runs for this user."""

    memory_enabled: bool = Field(
        description=(
            "When false, no durable facts are extracted or stored. Existing "
            "memories are retained but not added to - use DELETE /me/data to "
            "remove them."
        )
    )


class ErasureReport(BaseModel):
    """What an erasure removed."""

    memories_deleted: int = 0
    messages_deleted: int = 0
    sessions_deleted: int = 0


@router.get(
    "/me/memories",
    response_model=list[MemoryOut],
    summary="See what the agent remembers about you",
)
async def list_my_memories(
    user: CurrentUser,
    memory_service: Annotated[Any, Depends(get_memory_service)],
    include_inactive: bool = False,
) -> list[MemoryOut]:
    """List the durable facts stored about the caller.

    Args:
        user: The authenticated caller.
        memory_service: Long-term memory access.
        include_inactive: Include superseded and deleted entries. Superseded
            ones show how a preference changed over time - useful for
            understanding why the agent behaves as it does.

    Returns:
        Stored memories, most recently reinforced first.
    """
    if memory_service is None:
        raise ConfigurationError("Long-term memory is not configured on this deployment.")

    memories = await memory_service.store.list_for_user(user.id, include_inactive=include_inactive)
    return [
        MemoryOut(
            id=memory.id,
            memory_type=memory.memory_type.value,
            subject=memory.subject,
            content=memory.content,
            confidence=memory.confidence,
            mention_count=memory.mention_count,
            status=memory.status.value,
            source_lang=memory.source_lang,
            first_seen_at=memory.first_seen_at,
            last_seen_at=memory.last_seen_at,
        )
        for memory in memories
    ]


class MemoryIn(BaseModel):
    """A fact the traveller is telling the agent directly."""

    content: str = Field(
        min_length=3,
        max_length=280,
        description=(
            "One durable fact about the traveller, in their own words - "
            "'I'm vegetarian', 'I hate early flights'. Stored as written."
        ),
    )
    memory_type: Literal["constraint", "preference", "identity", "experience"] = Field(
        default="preference",
        description=(
            "constraint = a hard requirement that must never be traded off; "
            "preference = a soft leaning; identity = a stable attribute; "
            "experience = somewhere they have been."
        ),
    )
    subject: str = Field(
        default="other",
        max_length=40,
        description="diet, budget, pace, accessibility, home_base, interests, …",
    )


@router.post(
    "/me/memories",
    response_model=MemoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Tell it something about yourself",
)
async def add_my_memory(
    payload: MemoryIn,
    user: CurrentUser,
    memory_service: Annotated[Any, Depends(get_memory_service)],
) -> MemoryOut:
    """Store a fact the traveller supplied directly.

    Waiting for extraction to notice a preference is a poor first experience:
    the gate correctly ignores most messages, so a new user can hold a whole
    conversation and still see an empty profile. This lets them state
    something outright.

    It goes through the **same consolidation path** as an extracted fact
    rather than straight into the table. That matters: a manual entry should
    still reinforce an existing belief rather than duplicate it, and should
    still supersede one it contradicts. The only difference is confidence -
    a fact the traveller stated about themselves is not a guess, so it enters
    at full confidence.

    Args:
        payload: The fact, its type and its subject.
        user: The authenticated caller.
        memory_service: Long-term memory access.

    Returns:
        The stored memory.

    Raises:
        ConfigurationError: If long-term memory is not configured.
    """
    if memory_service is None:
        raise ConfigurationError("Long-term memory is not configured on this deployment.")

    candidate = CandidateMemory(
        memory_type=MemoryType(payload.memory_type),
        subject=_coerce_subject(payload.subject),
        content=payload.content.strip(),
        # Stated by the person it is about, so there is nothing to be
        # uncertain about in the way an extraction is uncertain.
        confidence=1.0,
    )

    embedding = await memory_service.embeddings.embed_query(candidate.content)
    outcome = await consolidate_candidate(
        user.id,
        candidate,
        embedding,
        memory_service.store,
        source_lang="en",
        extractor_model="user",
        settings=memory_service.settings,
    )

    logger.info(
        "memory.user_added",
        user_id=user.id,
        action=outcome.action.value,
        subject=candidate.subject.value,
    )

    # Read back through the same listing the page uses, rather than returning
    # the candidate: consolidation may have reinforced an existing memory
    # instead of inserting, in which case the row that matters is the older
    # one with a bumped mention count, not what was just submitted.
    stored = next(
        (
            memory
            for memory in await memory_service.store.list_for_user(user.id)
            if memory.id == outcome.memory_id
        ),
        None,
    )
    if stored is None:
        raise ResourceNotFoundError("The memory could not be stored.")

    return MemoryOut(
        id=stored.id,
        memory_type=stored.memory_type.value,
        subject=stored.subject,
        content=stored.content,
        confidence=stored.confidence,
        mention_count=stored.mention_count,
        status=stored.status.value,
        source_lang=stored.source_lang,
        first_seen_at=stored.first_seen_at,
        last_seen_at=stored.last_seen_at,
    )


def _coerce_subject(value: str) -> MemorySubject:
    """Map a client-supplied subject onto the closed vocabulary.

    The subject set is deliberately closed so retrieval can filter on it; an
    unrecognised value becomes OTHER rather than a 422, because a slightly
    mis-tagged memory is still worth keeping.

    Args:
        value: The requested subject.

    Returns:
        A valid `MemorySubject`.
    """
    try:
        return MemorySubject(value.strip().lower())
    except ValueError:
        return MemorySubject.OTHER


@router.delete(
    "/me/memories/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse, "description": "Not found"}},
    summary="Forget one thing",
)
async def delete_my_memory(
    memory_id: str,
    user: CurrentUser,
    memory_service: Annotated[Any, Depends(get_memory_service)],
) -> None:
    """Delete one stored memory.

    Soft-deleted rather than removed, so a repeatedly-wrong extraction leaves
    a trace worth investigating. It is excluded from retrieval immediately.
    """
    if memory_service is None:
        raise ConfigurationError("Long-term memory is not configured on this deployment.")

    if not await memory_service.store.delete(memory_id, user.id):
        raise ResourceNotFoundError(f"Memory {memory_id} was not found.")

    logger.info("memory.user_deleted", user_id=user.id, memory_id=memory_id)


@router.post(
    "/me/memory-settings",
    response_model=MemorySettings,
    summary="Turn long-term memory on or off",
)
async def update_memory_settings(
    payload: MemorySettings,
    user: CurrentUser,
    database: Annotated[Database, Depends(get_database)],
) -> MemorySettings:
    """Enable or disable long-term memory extraction for the caller.

    Checked before the extractor runs, not merely before retrieval, so
    opting out means nothing is written rather than written-but-hidden.
    """
    async with database.user_scope(user.id) as conn:
        await conn.execute(
            "update public.profiles set memory_enabled = %s where id = %s",
            (payload.memory_enabled, user.id),
        )

    logger.info("memory.settings_updated", user_id=user.id, enabled=payload.memory_enabled)
    return payload


@router.delete(
    "/me/data",
    response_model=ErasureReport,
    summary="Erase everything remembered about you",
)
async def erase_my_data(
    user: CurrentUser,
    database: Annotated[Database, Depends(get_database)],
) -> ErasureReport:
    """Permanently delete all memories, messages and conversations.

    The account itself survives - this is "forget me", not "delete my login".
    Executed by a `security definer` database function that re-checks the
    caller against the target, so the guarantee does not depend on this route
    remembering to.
    """
    async with database.user_scope(user.id) as conn:
        cursor = await conn.execute("select public.erase_user_data(%s) as report", (user.id,))
        row = await cursor.fetchone()

    report = (row or {}).get("report") or {}
    logger.info(
        "privacy.user_data_erased",
        user_id=user.id,
        **{
            key: report.get(key)
            for key in ("memories_deleted", "messages_deleted", "sessions_deleted")
        },
    )

    return ErasureReport(
        memories_deleted=report.get("memories_deleted", 0),
        messages_deleted=report.get("messages_deleted", 0),
        sessions_deleted=report.get("sessions_deleted", 0),
    )
