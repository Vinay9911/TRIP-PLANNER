"""Application configuration.

Every externally-tunable value in the system is declared here as a typed
field and sourced from the environment. Nothing else in the codebase reads
`os.environ` directly, which means:

* `.env.example` and this module are the single, greppable inventory of
  configuration - there are no hidden knobs buried in function defaults;
* configuration errors surface at import time as Pydantic validation errors
  with useful messages, rather than as a `None` propagating into an HTTP
  client three layers down;
* tests can build a `Settings` instance with explicit overrides instead of
  mutating global process state.

The settings object is cached (`get_settings`) so the `.env` file is parsed
once per process, and so FastAPI can use it as a dependency without cost.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "production"]
LogFormat = Literal["console", "json"]
FlightProviderName = Literal["mock", "duffel"]


class Settings(BaseSettings):
    """Typed, validated view of the process environment.

    Field names map to upper-case environment variables (Pydantic settings is
    case-insensitive). Secrets use `SecretStr` so that an accidental `repr()`
    of the settings object in a log line does not leak credentials.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application ------------------------------------------------------
    environment: Environment = "development"
    log_level: str = "INFO"
    log_format: LogFormat = "console"
    cors_origins: str = "http://localhost:3000"
    rate_limit_requests_per_minute: int = Field(default=10, ge=1)

    # -- LLM (Groq) -------------------------------------------------------
    # Accepts ONE key or several comma-separated. Multiple keys are pooled
    # and rotated, so a rate limit on one moves straight to the next rather
    # than stalling the request. See app/services/keys.py.
    groq_api_key: SecretStr = SecretStr("")
    llm_planner_model: str = "openai/gpt-oss-120b"
    llm_executor_model: str = "llama-3.3-70b-versatile"
    llm_utility_model: str = "llama-3.1-8b-instant"
    llm_planner_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    llm_responder_temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    llm_timeout_seconds: int = Field(default=60, ge=5)

    # -- LLM (local, via Ollama) ------------------------------------------
    # "groq" is cloud only; "local" never leaves the machine; "auto" prefers
    # Groq and falls back to Ollama once the daily token budget is spent,
    # which is the only failure a second cloud key cannot fix.
    #
    # Model choice here is measured, not assumed. On a 4GB laptop GPU
    # `llama3.2:3b` ran at 47.8 tok/s and emitted correct tool calls;
    # `llama3.1:8b` was correct but 6x slower because it does not fit in
    # VRAM; `qwen3:4b` produced no tool calls at all and is unusable as an
    # executor whatever its other merits.
    llm_provider: Literal["groq", "local", "auto"] = "auto"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_planner_model: str = "llama3.2:3b"
    ollama_executor_model: str = "llama3.2:3b"
    ollama_utility_model: str = "llama3.2:3b"

    # -- Embeddings (Gemini) ----------------------------------------------
    # Also accepts a comma-separated list, pooled the same way.
    gemini_api_key: SecretStr = SecretStr("")
    embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = Field(default=768, ge=128, le=3072)

    # -- Supabase ---------------------------------------------------------
    supabase_url: str = ""
    supabase_anon_key: SecretStr = SecretStr("")
    supabase_service_role_key: SecretStr = SecretStr("")
    database_url: SecretStr = SecretStr("")
    supabase_jwks_url: str = ""
    supabase_jwt_audience: str = "authenticated"

    # -- Tools ------------------------------------------------------------
    tavily_api_key: SecretStr = SecretStr("")
    geoapify_api_key: SecretStr = SecretStr("")
    open_meteo_base_url: str = "https://api.open-meteo.com/v1"
    open_meteo_geocoding_url: str = "https://geocoding-api.open-meteo.com/v1"
    wikivoyage_api_base_url: str = "https://en.wikivoyage.org/w/api.php"

    # Wikimedia's robot policy requires a User-Agent naming the client and a
    # way to contact its operator; requests without one are answered with a
    # 403 and a link to the policy. Verified against the live API - a generic
    # agent really is rejected, so this is a hard requirement rather than
    # etiquette. Format: "Name/version (contact-url; contact-email)".
    http_user_agent: str = (
        "TripPlannerAgent/0.1 (https://github.com/example/trip-planner; trip-planner@example.com)"
    )
    flight_provider: FlightProviderName = "mock"
    duffel_api_token: SecretStr = SecretStr("")

    # -- Agent guardrails -------------------------------------------------
    agent_max_plan_steps: int = Field(default=8, ge=1, le=20)
    agent_max_replan_cycles: int = Field(default=3, ge=0, le=10)
    agent_max_tool_calls_per_step: int = Field(default=4, ge=1, le=10)
    # How many independent plan steps may run at once. Capped rather than
    # unbounded: each concurrent step is its own tool-calling loop, and a wide
    # fan-out on a five-step plan would open more sockets and burn more of the
    # per-minute token allowance in one burst than it saves in wall clock.
    # Three covers the research steps that are actually independent.
    agent_max_parallel_steps: int = Field(default=3, ge=1, le=8)
    agent_step_timeout_seconds: int = Field(default=90, ge=10)

    # -- RAG --------------------------------------------------------------
    rag_max_hops: int = Field(default=4, ge=1, le=8)
    rag_chunk_tokens: int = Field(default=512, ge=128, le=2048)
    rag_top_k: int = Field(default=6, ge=1, le=50)
    rag_cache_ttl_days: int = Field(default=30, ge=1)

    # -- Long-term memory -------------------------------------------------
    memory_dedupe_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    memory_conflict_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    memory_retrieval_top_k: int = Field(default=8, ge=1, le=50)
    memory_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)

    # -- Validators -------------------------------------------------------

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        """Upper-case the log level and reject values `logging` cannot parse."""
        normalised = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalised not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return normalised

    @field_validator("memory_conflict_threshold")
    @classmethod
    def _conflict_below_dedupe(cls, value: float, info: object) -> float:
        """Ensure the conflict band sits strictly below the dedupe threshold.

        The two thresholds define three bands of cosine similarity against an
        existing memory: *unrelated* (< conflict), *related, possibly
        contradictory* (conflict..dedupe), and *duplicate* (>= dedupe). If the
        bounds were inverted the middle band would vanish and contradiction
        arbitration would never run.
        """
        data = getattr(info, "data", {}) or {}
        dedupe = data.get("memory_dedupe_threshold")
        if dedupe is not None and value >= dedupe:
            raise ValueError(
                "memory_conflict_threshold must be strictly less than "
                f"memory_dedupe_threshold ({value} >= {dedupe})"
            )
        return value

    # -- Derived helpers --------------------------------------------------

    @property
    def is_production(self) -> bool:
        """True when running with production defaults and stricter checks."""
        return self.environment == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins parsed from the comma-separated environment value."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def has_database(self) -> bool:
        """True when a Postgres URL is configured.

        When false the application degrades to in-process memory: an
        in-memory LangGraph checkpointer and an in-memory vector store. That
        keeps the test suite and a first-run developer experience working
        without any cloud credentials.
        """
        return bool(self.database_url.get_secret_value())

    @property
    def has_embeddings(self) -> bool:
        """True when an embeddings provider is configured."""
        return bool(self.gemini_api_key.get_secret_value())

    @property
    def groq_key_count(self) -> int:
        """How many Groq keys are configured, for startup logging."""
        from app.services.keys import parse_keys

        return len(parse_keys(self.groq_api_key.get_secret_value()))

    @property
    def gemini_key_count(self) -> int:
        """How many Gemini keys are configured, for startup logging."""
        from app.services.keys import parse_keys

        return len(parse_keys(self.gemini_api_key.get_secret_value()))

    def missing_production_secrets(self) -> list[str]:
        """Names of secrets that must be present before serving real traffic.

        Called at startup rather than at import so that a misconfigured
        deployment fails loudly with a complete list, instead of failing on
        the first request that happens to touch a missing key.
        """
        required = {
            "GROQ_API_KEY": self.groq_api_key.get_secret_value(),
            "GEMINI_API_KEY": self.gemini_api_key.get_secret_value(),
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key.get_secret_value(),
            "DATABASE_URL": self.database_url.get_secret_value(),
            "SUPABASE_JWKS_URL": self.supabase_jwks_url,
        }
        return sorted(name for name, value in required.items() if not value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that repeated FastAPI dependency resolution is free and the
    `.env` file is read exactly once. Tests that need different values should
    construct `Settings(...)` directly rather than clearing this cache.
    """
    return Settings()
