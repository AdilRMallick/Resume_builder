"""Central configuration. Everything tunable lives here, nothing is hardcoded downstream."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return value
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix=""
    )

    # datastores
    database_url: str = Field(
        default="postgresql+psycopg://jme:jme@localhost:5433/jme", alias="JME_DATABASE_URL"
    )
    redis_url: str = Field(default="redis://localhost:6380/0", alias="JME_REDIS_URL")

    # feed
    feed_url: str = Field(
        default="https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
        alias="JME_FEED_URL",
    )

    # fetching etiquette
    user_agent: str = Field(
        default="job-match-engine/0.1 (+https://github.com/your-handle/job-match-engine)",
        alias="JME_USER_AGENT",
    )
    host_rate_per_sec: float = Field(default=0.5, alias="JME_HOST_RATE_PER_SEC")
    host_burst: int = Field(default=1, alias="JME_HOST_BURST")
    fetch_timeout_sec: int = Field(default=20, alias="JME_FETCH_TIMEOUT_SEC")
    respect_robots: bool = Field(default=True, alias="JME_RESPECT_ROBOTS")

    # llm
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    kimi_api_key: str | None = Field(default=None, alias="MOONSHOT_API_KEY")
    resume_openai_model: str = Field(
        default="gpt-5.6-terra", alias="JME_RESUME_OPENAI_MODEL"
    )
    resume_anthropic_model: str = Field(
        default="claude-sonnet-5", alias="JME_RESUME_ANTHROPIC_MODEL"
    )
    resume_gemini_model: str = Field(
        default="gemini-3.6-flash", alias="JME_RESUME_GEMINI_MODEL"
    )
    resume_kimi_model: str = Field(default="kimi-k2.6", alias="JME_RESUME_KIMI_MODEL")
    # Local Claude Code CLI. Uses the signed-in Claude subscription, never an API key.
    resume_claude_code_enabled: bool = Field(
        default=True, alias="JME_RESUME_CLAUDE_CODE_ENABLED"
    )
    claude_code_cli_path: str | None = Field(default=None, alias="JME_CLAUDE_CODE_CLI_PATH")
    resume_claude_code_model: str = Field(
        default="sonnet", alias="JME_RESUME_CLAUDE_CODE_MODEL"
    )
    # Measured on this bullet bank: low ~30s but few rewrites survive validation,
    # medium ~85s and most do, the CLI default is slower still with no gain.
    resume_claude_code_effort: str = Field(
        default="medium", alias="JME_RESUME_CLAUDE_CODE_EFFORT"
    )
    # The CLI boots a whole agent runtime and its latency has a long tail, so it needs
    # far more headroom than an HTTP call.
    resume_claude_code_timeout_sec: int = Field(
        default=300, alias="JME_RESUME_CLAUDE_CODE_TIMEOUT_SEC"
    )
    resume_ai_timeout_sec: int = Field(default=90, alias="JME_RESUME_AI_TIMEOUT_SEC")
    resume_tectonic_path: str | None = Field(default=None, alias="JME_TECTONIC_PATH")
    resume_pdf_timeout_sec: int = Field(default=120, alias="JME_RESUME_PDF_TIMEOUT_SEC")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-opus-5", alias="JME_ANTHROPIC_MODEL")
    anthropic_effort: str = Field(default="medium", alias="JME_ANTHROPIC_EFFORT")
    extraction_prompt_version: str = Field(default="v1", alias="JME_EXTRACTION_PROMPT_VERSION")
    match_prompt_version: str = Field(default="v1", alias="JME_MATCH_PROMPT_VERSION")
    max_cost_per_match_usd: float = Field(default=0.05, alias="JME_MAX_COST_PER_MATCH_USD")

    # embeddings
    embedding_provider: str = Field(default="hash", alias="JME_EMBEDDING_PROVIDER")
    embedding_model: str = Field(default="voyage-3", alias="JME_EMBEDDING_MODEL")
    embedding_dim: int = Field(default=1536, alias="JME_EMBEDDING_DIM")
    voyage_api_key: str | None = Field(default=None, alias="VOYAGE_API_KEY")

    # eligibility
    grad_date: dt.date = Field(default=dt.date(2027, 5, 1), alias="JME_GRAD_DATE")
    location_allowlist: list[str] = Field(
        default_factory=lambda: ["Remote", "Detroit", "Ann Arbor", "Michigan", "Chicago"],
        alias="JME_LOCATION_ALLOWLIST",
    )
    location_boost: list[str] = Field(
        default_factory=lambda: ["Detroit", "Ann Arbor", "Michigan", "Chicago"],
        alias="JME_LOCATION_BOOST",
    )
    role_types: list[str] = Field(default_factory=lambda: ["swe"], alias="JME_ROLE_TYPES")
    allow_sponsorship_required: bool = Field(
        default=False, alias="JME_ALLOW_SPONSORSHIP_REQUIRED"
    )
    shortlist_size: int = Field(default=20, alias="JME_SHORTLIST_SIZE")

    # evidence
    evidence_dir: str = Field(default="./evidence", alias="JME_EVIDENCE_DIR")
    evidence_repos: list[str] = Field(default_factory=list, alias="JME_EVIDENCE_REPOS")
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")

    @field_validator(
        "location_allowlist", "location_boost", "role_types", "evidence_repos", mode="before"
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return _csv(v)
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


# ---- Redis stream / group names. Shared contract with the Go fetcher. ----
STREAM_FETCH = "jme:fetch"
STREAM_ENRICH = "jme:enrich"
STREAM_FETCH_DEAD = "jme:fetch:dead"
STREAM_ENRICH_DEAD = "jme:enrich:dead"
GROUP_FETCH = "fetchers"
GROUP_ENRICH = "enrichers"
