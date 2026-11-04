from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    environment: str = "development"
    database_url: str
    # Separate database used by the test suite; it is reset by every test session.
    test_database_url: str | None = None
    # Must match the selected embedding model; pgvector HNSW indexes support up to 2000 dims.
    embedding_dim: int = Field(default=768, gt=0, le=2000)

    # Authentication (spec §9, §12).
    # Encrypts TOTP secrets at rest; must be set, stable, and kept out of the database.
    secret_key: SecretStr | None = None
    public_base_url: str = "http://localhost:3000"  # for invitation links
    cookie_secure: bool = True  # set false only for plain-http local development
    session_ttl_hours: int = Field(default=12, gt=0)
    session_idle_minutes: int = Field(default=60, gt=0)
    api_token_ttl_days: int = Field(default=30, gt=0)
    invite_ttl_hours: int = Field(default=72, gt=0)

    # Outgoing email for invitations (app/services/email.py). Off by default: admins then share
    # the link shown on screen.
    email_backend: Literal["disabled", "console", "smtp", "memory"] = "disabled"
    email_from: str | None = None
    email_from_name: str = "Women on Boards Awards"
    email_reply_to: str | None = None
    email_site_name: str = "the Women on Boards Awards evaluation system"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    login_max_failures: int = Field(default=5, gt=0)
    lockout_minutes: int = Field(default=15, gt=0)
    password_min_length: int = Field(default=8, ge=8)
    mfa_required_roles: list[str] = ["admin", "committee"]

    # File storage, encrypted at rest (spec §3, §12): "id:base64key,..." of 32-byte keys, kept
    # outside the database and storage. New files use the active key; older ids still decrypt.
    storage_dir: Path = BACKEND_DIR / "var" / "storage"
    file_encryption_keys: SecretStr | None = None
    file_encryption_active_key: str | None = None
    # Only for throwaway local experiments; never in a deployment.
    allow_unencrypted_storage: bool = False
    max_file_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_files_per_upload: int = Field(default=50, gt=0)
    # Whether evidence can be uploaded only after the finalist's consent to assessment (part A)
    # is recorded. Off: recording consent is optional, but an explicit withdrawal still blocks
    # uploads. The data protection review (§4.1) recommends turning it on for real nominations.
    upload_requires_consent: bool = False
    max_archive_members: int = Field(default=200, gt=0)
    max_archive_total_bytes: int = Field(default=500 * 1024 * 1024, gt=0)

    # Ingestion worker (spec §5).
    scanner: Literal["clamav", "disabled"] = "clamav"
    clamd_host: str = "127.0.0.1"
    clamd_port: int = 3310
    ocr_languages: str = "eng"
    embedding_provider: Literal["local", "voyage", "fake"] = "local"
    embedding_model: str = "nomic-ai/nomic-embed-text-v1.5"
    voyage_api_key: SecretStr | None = None
    model_cache_dir: Path | None = None
    chunk_target_tokens: int = Field(default=700, gt=0)
    chunk_max_tokens: int = Field(default=900, gt=0)
    chunk_overlap_tokens: int = Field(default=100, ge=0)
    # Retrieval (spec §5.3).
    reranker_provider: Literal["local", "voyage", "none", "fake"] = "local"
    reranker_model: str = "jinaai/jina-reranker-v1-turbo-en"
    retrieval_vector_candidates: int = Field(default=20, gt=0)
    retrieval_lexical_candidates: int = Field(default=20, gt=0)
    retrieval_rrf_k: int = Field(default=60, gt=0)
    retrieval_top_k: int = Field(default=10, gt=0, le=50)
    # Below this reranker score the top passage is flagged as low relevance. Model-specific:
    # leave unset until calibrated on the pilot benchmark (FR-15).
    rerank_min_score: float | None = None

    # Claim extraction (FR-06). Sending evidence to a hosted LLM is cross-border processing of
    # personal data (spec §12): it stays off until the data protection review approves it.
    llm_provider: Literal["anthropic", "openrouter", "fake", "disabled"] = "disabled"
    external_ai_processing_approved: bool = False
    anthropic_api_key: SecretStr | None = None  # else the SDK's own credential resolution
    claim_model: str = "claude-opus-5-5"
    # OpenRouter's Anthropic-compatible endpoint. Requests are pinned to the providers listed
    # (Anthropic itself by default) with no fallback to others, and to providers that don't
    # train on or retain prompts: OpenRouter otherwise routes Claude via Bedrock, Vertex, etc.
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api"
    openrouter_model: str = "anthropic/claude-opus-5.5"
    openrouter_providers: list[str] = ["anthropic"]
    claim_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    claim_max_output_tokens: int = Field(default=64000, gt=0)
    # Passage tokens per extraction request; larger evidence sets are split into batches.
    claim_batch_tokens: int = Field(default=120_000, gt=1000)
    # Jury decision (23 Oct 2026): claims a supporting document supports are verified on
    # extraction. Turning it off stops new automatic verifications; earlier ones stand.
    jury_verifies_supporting_documents: bool = True

    # External research (FR-13): web search about the nominee via Claude's server-side tools.
    # Needs the same approval as claim extraction, plus its own switch.
    research_enabled: bool = False
    # Research can call a different provider than claims and assessment. OpenRouter doesn't
    # accept web_fetch results sent back after a paused turn, so research fails there; set
    # RESEARCH_PROVIDER=anthropic with ANTHROPIC_API_KEY. Unset means LLM_PROVIDER.
    research_provider: Literal["anthropic", "openrouter"] | None = None
    # Research reads many web pages, so it runs on a cheaper model and effort than claims and
    # assessment. Quotes are still checked against the fetched pages and reviewers accept each
    # finding, so a weaker model costs rejected findings, not wrong evidence.
    research_model: str = "claude-haiku-5-5"
    research_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    research_max_claims: int = Field(default=25, gt=0)
    research_max_searches: int = Field(default=10, gt=0)
    research_max_fetches: int = Field(default=10, gt=0)
    # Page text kept per fetch. Fetched pages make up most of a run's cost.
    research_max_fetch_tokens: int = Field(default=10_000, gt=0)
    research_max_continuations: int = Field(default=8, gt=0)
    # Optional ISO country hint for web search results. Unset by default: the search tool
    # rejects countries it doesn't support, including UG.
    research_country: str | None = None

    @field_validator("research_provider", "research_country", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    # User-generated and social platforms are not credible sources for verification (spec §6).
    research_blocked_domains: list[str] = [
        "facebook.com",
        "instagram.com",
        "x.com",
        "twitter.com",
        "tiktok.com",
        "reddit.com",
        "quora.com",
        "pinterest.com",
        "medium.com",
        "youtube.com",
        "linkedin.com",
    ]

    worker_poll_seconds: float = Field(default=3.0, gt=0)
    max_processing_attempts: int = Field(default=5, gt=0)
    # A document stuck mid-processing this long (worker crashed) is picked up again.
    processing_timeout_minutes: int = Field(default=30, gt=0)

    @property
    def llm_model(self) -> str:
        """The model recorded on runs: the hosted model's id, or the test double's."""
        if self.llm_provider == "openrouter":
            return self.openrouter_model
        if self.llm_provider == "anthropic":
            return self.claim_model
        return "fake:scripted"


@lru_cache
def get_settings() -> Settings:
    return Settings()
