"""Trip Planner Agent - an AI travel planner with tool use, memory and RAG.

Package layout, in dependency order (each layer may import from those above
it, never the reverse):

    core/       Configuration, logging, error taxonomy. No business logic.
    services/   Thin clients for outside systems: LLM, embeddings, HTTP.
    db/         Postgres pool and repositories. Owns all SQL.
    providers/  Swappable implementations of a capability (e.g. flights).
    tools/      Functions the agent may call. The model's action surface.
    memory/     Short-term conversation state and long-term user knowledge.
    rag/        Multi-hop retrieval over the Wikivoyage corpus.
    agent/      The LangGraph plan-execute-replan loop that ties it together.
    api/        FastAPI routes. HTTP concerns only, no business logic.
    schemas/    Pydantic request and response models for the API.

The rule that keeps this honest: `agent/` may import `tools/`, but `tools/`
never imports `agent/`. A tool that knows about the planner is a tool that
cannot be tested on its own.
"""
