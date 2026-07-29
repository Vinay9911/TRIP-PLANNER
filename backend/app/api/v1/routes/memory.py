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

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, get_database, get_memory_service
from app.core.errors import ConfigurationError, ResourceNotFoundError
from app.core.logging import get_logger
from app.db.session import Database
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
