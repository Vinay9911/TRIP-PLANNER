"""Turn orchestration.

The graph handles reasoning. This module handles everything around one turn:
binding request context, persisting messages, running the graph, writing the
trace, and scheduling background memory extraction.

The ordering matters and is the main thing worth reading here.

**Memory extraction happens after the response is returned, never before.**
Extraction costs a model call plus embedding calls plus consolidation queries -
easily two seconds. Putting that on the critical path would make every turn
slower to serve a benefit the user does not experience until their *next*
session. So `run_turn` returns the answer, and FastAPI's background tasks run
the write path afterwards.

**Trace persistence never fails a request.** If the database is unreachable
when we go to write the trace, the traveller has already received a working
itinerary; turning that into a 500 would be absurd. Trace failures are logged
and swallowed.

**Tool context is bound and unbound around the run.** Identity lives in a
context variable rather than in tool arguments, so the model cannot address
another user's data. The `finally` block is what keeps that true under
concurrency - a leaked context would let the next request inherit the previous
user's identity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph import AgentDependencies, compile_graph, recursion_limit_for
from app.agent.state import AgentState, initial_state
from app.core.config import Settings, get_settings
from app.core.logging import bind_request_context, get_logger
from app.db.repositories import SessionRepository, TraceRepository
from app.memory.service import MemoryService
from app.rag.retriever import MultiHopRetriever
from app.services.usage import TokenUsage, start_metering, stop_metering, total_rag_hops
from app.tools.base import ToolCallRecord, start_recording, stop_recording
from app.tools.context import ToolContext, reset_tool_context, set_tool_context

logger = get_logger(__name__)


@dataclass
class TurnResult:
    """Everything one turn produced.

    Attributes:
        run_id: Unique id for this execution, for looking up the trace.
        session_id: Conversation this belonged to.
        response: The text shown to the traveller.
        status: How the run ended.
        detected_language: Language the reply was written in.
        destination: Destination resolved during the turn, if any.
        needs_clarification: True when the response is a question, not an answer.
        plan: The plan as first produced.
        steps_executed: How many steps ran.
        replan_count: How many times the plan was revised.
        tool_calls: Every tool invocation made.
        latency_ms: Wall-clock duration.
        user_message_id: Stored id of the traveller's message.
        assistant_message_id: Stored id of the reply.
    """

    run_id: str
    session_id: str
    response: str
    status: str
    detected_language: str = "en"
    destination: str | None = None
    needs_clarification: bool = False
    plan: list[Any] = None  # type: ignore[assignment]
    steps_executed: int = 0
    replan_count: int = 0
    tool_calls: list[ToolCallRecord] = None  # type: ignore[assignment]
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    token_breakdown: list[dict[str, object]] = None  # type: ignore[assignment]
    rag_hops: int = 0
    user_message_id: str | None = None
    assistant_message_id: str | None = None

    def __post_init__(self) -> None:
        """Normalise mutable defaults."""
        if self.plan is None:
            self.plan = []
        if self.tool_calls is None:
            self.tool_calls = []
        if self.token_breakdown is None:
            self.token_breakdown = []


class AgentRunner:
    """Runs one conversational turn end to end.

    Attributes:
        settings: Application settings.
        memory_service: Long-term memory, or None to run without it.
        retriever: Multi-hop RAG retriever, or None.
        sessions: Conversation persistence, or None for stateless runs.
        traces: Trace persistence, or None.
        checkpointer: LangGraph checkpointer providing short-term memory.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        memory_service: MemoryService | None = None,
        retriever: MultiHopRetriever | None = None,
        sessions: SessionRepository | None = None,
        traces: TraceRepository | None = None,
        checkpointer: Any = None,
    ) -> None:
        """Initialise the runner."""
        self.settings = settings or get_settings()
        self.memory_service = memory_service
        self.retriever = retriever
        self.sessions = sessions
        self.traces = traces
        self.checkpointer = checkpointer

    async def run_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> TurnResult:
        """Process one user message and produce a reply.

        Args:
            user_id: Authenticated user, from a verified JWT.
            session_id: Conversation thread. Doubles as the checkpointer's
                `thread_id`.
            message: What the traveller wrote.
            history: Prior turns, used when no checkpointer is configured.

        Returns:
            The turn's result, including the trace data needed to persist it.
        """
        run_id = str(uuid4())
        started = time.perf_counter()

        bind_request_context(user_id=user_id, session_id=session_id, run_id=run_id)

        # Persist the user's message first, so it exists even if the run then
        # fails - a conversation that loses the question but keeps the error
        # is impossible to debug.
        user_message_id: str | None = None
        if self.sessions is not None:
            try:
                row = await self.sessions.add_message(
                    session_id, user_id, role="user", content=message
                )
                user_message_id = row.get("id")
            except Exception:
                logger.warning("runner.persist_user_message_failed", exc_info=True)

        if self.traces is not None:
            try:
                await self.traces.start_run(
                    run_id=run_id,
                    session_id=session_id,
                    user_id=user_id,
                    request_message_id=user_message_id,
                )
            except Exception:
                logger.warning("runner.start_run_trace_failed", exc_info=True)

        records = start_recording()
        meter: TokenUsage = start_metering()
        context_token = set_tool_context(
            ToolContext(
                user_id=user_id,
                session_id=session_id,
                memory_service=self.memory_service,
                retriever=self.retriever,
            )
        )

        try:
            final_state = await self._invoke_graph(
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                message=message,
                history=history or [],
                records=records,
            )
        finally:
            # Read the hop count before stop_metering() clears the ContextVar
            # it lives in - reading it after returned 0 for every run, since
            # `total_rag_hops()` has nothing left to read once the context is
            # cleared. `meter` itself doesn't have this problem: it is a plain
            # object obtained from start_metering() and held by this local
            # variable, so stop_metering() clearing the *ContextVar binding*
            # doesn't affect the object it used to point to. rag_hops has no
            # equivalent object to hold onto, only this function call, so the
            # read has to happen before the clear rather than after.
            rag_hops = total_rag_hops()

            # Both must unwind even on failure: a leaked tool context would
            # let the next request inherit this user's identity.
            reset_tool_context(context_token)
            stop_recording()
            stop_metering()

        latency_ms = int((time.perf_counter() - started) * 1000)
        response = final_state.get("final_response") or (
            "I could not put together an answer for that. Could you rephrase it?"
        )

        assistant_message_id: str | None = None
        if self.sessions is not None:
            try:
                row = await self.sessions.add_message(
                    session_id,
                    user_id,
                    role="assistant",
                    content=response,
                    language=final_state.get("detected_language"),
                )
                assistant_message_id = row.get("id")

                await self.sessions.update_context(
                    session_id,
                    user_id,
                    title=message[:80],
                    destination=final_state.get("destination"),
                    language=final_state.get("detected_language"),
                )
            except Exception:
                logger.warning("runner.persist_reply_failed", exc_info=True)

        result = TurnResult(
            run_id=run_id,
            session_id=session_id,
            response=response,
            status=final_state.get("status", "completed"),
            detected_language=final_state.get("detected_language", "en"),
            destination=final_state.get("destination"),
            needs_clarification=bool(final_state.get("needs_clarification")),
            plan=final_state.get("initial_plan", []),
            steps_executed=len(final_state.get("completed_steps", [])),
            replan_count=final_state.get("replan_count", 0),
            tool_calls=list(records),
            latency_ms=latency_ms,
            prompt_tokens=meter.prompt_tokens,
            completion_tokens=meter.completion_tokens,
            token_breakdown=meter.summary(),
            rag_hops=rag_hops,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
        )

        await self._persist_trace(result, final_state)

        logger.info(
            "runner.turn_completed",
            status=result.status,
            steps=result.steps_executed,
            replans=result.replan_count,
            tool_calls=len(result.tool_calls),
            latency_ms=latency_ms,
            total_tokens=meter.total_tokens,
            llm_calls=meter.calls,
        )
        return result

    async def _invoke_graph(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        message: str,
        history: list[dict[str, str]],
        records: list[ToolCallRecord],
    ) -> AgentState:
        """Compile and invoke the agent graph for one turn.

        Returns:
            The final graph state, or a minimal failure state if the graph
            itself raised.
        """
        graph = compile_graph(
            AgentDependencies(memory_service=self.memory_service, settings=self.settings),
            checkpointer=self.checkpointer,
            tool_records=records,
        )

        messages: list[Any] = []
        if self.checkpointer is None:
            # Without a checkpointer the caller supplies history explicitly.
            for turn in history[-10:]:
                if turn.get("role") == "user":
                    messages.append(HumanMessage(content=turn["content"]))
                elif turn.get("role") == "assistant":
                    messages.append(AIMessage(content=turn["content"]))
        messages.append(HumanMessage(content=message))

        state = initial_state(
            user_id=user_id, session_id=session_id, run_id=run_id, messages=messages
        )

        config: dict[str, Any] = {
            "recursion_limit": recursion_limit_for(self.settings),
            "configurable": {"thread_id": session_id},
        }

        try:
            return await graph.ainvoke(state, config)  # type: ignore[no-any-return]
        except Exception as exc:
            logger.exception("runner.graph_failed", run_id=run_id)
            return AgentState(
                status="failed",
                final_response=(
                    "Something went wrong while planning your trip. Please try again - "
                    "if it keeps happening, try rephrasing your request."
                ),
                errors=[{"node": "graph", "error": str(exc)[:300]}],
                completed_steps=[],
            )

    async def _persist_trace(self, result: TurnResult, state: AgentState) -> None:
        """Write the execution trace.

        Never raises: the traveller already has their answer, and losing
        telemetry is not worth turning a successful turn into an error.
        """
        if self.traces is None:
            return

        try:
            await self.traces.finish_run(
                run_id=result.run_id,
                status=result.status,
                initial_plan=result.plan,
                replan_count=result.replan_count,
                injected_memory_ids=state.get("memory_ids", []),
                rag_hops=result.rag_hops,
                detected_language=result.detected_language,
                latency_ms=result.latency_ms,
                response_message_id=result.assistant_message_id,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )
            await self.traces.record_steps(result.run_id, state.get("completed_steps", []))
            await self.traces.record_tool_calls(result.run_id, result.tool_calls)
        except Exception:
            logger.warning("runner.trace_persist_failed", run_id=result.run_id, exc_info=True)

    async def extract_memories_background(
        self,
        *,
        user_id: str,
        session_id: str,
        user_message: str,
        assistant_reply: str,
        message_id: str | None,
    ) -> None:
        """Run the long-term memory write path after the response was sent.

        Scheduled as a FastAPI background task. Swallows every exception: by
        the time this runs the HTTP response is already on its way, so raising
        would only produce an unhandled error in the worker.

        Args:
            user_id: Whose memory to update.
            session_id: Conversation, for provenance.
            user_message: What the traveller said.
            assistant_reply: The reply, as disambiguation context.
            message_id: Message id, for provenance.
        """
        if self.memory_service is None:
            return

        bind_request_context(user_id=user_id, session_id=session_id)

        try:
            outcomes = await self.memory_service.remember(
                user_id,
                user_message,
                assistant_reply=assistant_reply,
                session_id=session_id,
                message_id=message_id,
            )
            if outcomes:
                logger.info(
                    "runner.memory_updated",
                    outcomes=[outcome.action.value for outcome in outcomes],
                )
        except Exception:
            logger.warning("runner.background_memory_failed", exc_info=True)
