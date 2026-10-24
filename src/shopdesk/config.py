from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "staging", "prod"] = "dev"
    brand_name: str = "Northwind Store"

    llm_provider: Literal["openai", "anthropic"] = "openai"
    fallback_provider: Literal["openai", "anthropic", "none"] = "anthropic"
    agent_effort: Literal["low", "medium", "high"] = "medium"
    max_agent_steps: int = 6
    llm_attempts: int = 3
    retry_initial_interval: float = 1.0
    llm_timeout_seconds: float = 90

    openai_api_key: SecretStr | None = None
    # EU data residency projects use https://eu.api.openai.com/v1
    openai_base_url: str = "https://api.openai.com/v1"
    openai_agent_model: str = "gpt-5.6-sol"
    openai_guard_model: str | None = "gpt-5.4-mini"
    openai_embedding_model: str = "text-embedding-3-large"

    anthropic_api_key: SecretStr | None = None
    anthropic_agent_model: str = "claude-opus-5-5"
    anthropic_guard_model: str = "claude-haiku-5-5"
    anthropic_refusal_fallback: bool = True

    qdrant_url: str = ":memory:"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "kb_chunks"
    embedding_provider: Literal["openai", "local"] = "openai"
    embedding_dimensions: int = 1024
    local_dense_model: str = "BAAI/bge-small-en-v1.5"
    sparse_model: str = "Qdrant/bm25"
    # Off by default: in evals the English-only MiniLM reranker pushed the right chunk out
    # of the top 5 for German questions, while RRF over multilingual embeddings + BM25 got
    # it first. "BAAI/bge-reranker-v2-m3-int8" is multilingual but adds ~2 s per query on CPU.
    rerank_model: str | None = None
    retrieval_candidates: int = 24
    retrieval_top_k: int = 5
    # Cosine floor for "clearly unrelated". Calibrated for text-embedding-3-large @1024:
    # off-topic questions scored 0.10-0.14, the weakest genuine query 0.23. Re-tune per model.
    retrieval_min_similarity: float = 0.2
    chunk_max_tokens: int = 350
    chunk_overlap_tokens: int = 50

    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"

    zendesk_subdomain: str | None = None
    # Only for a sandbox, an egress proxy, or the e2e stand-in; normally derived from subdomain.
    zendesk_base_url: str | None = None
    zendesk_auth_mode: Literal["oauth", "api_token"] = "oauth"
    zendesk_client_id: str | None = None
    zendesk_client_secret: SecretStr | None = None
    zendesk_redirect_uri: str = "http://localhost:8000/v1/zendesk/oauth/callback"
    # Least privilege: create/update tickets and their requesters, nothing else.
    zendesk_scopes: str = "tickets:read tickets:write users:read users:write"
    zendesk_email: str | None = None
    zendesk_api_token: SecretStr | None = None
    zendesk_access_token_ttl: int = 2 * 24 * 3600
    zendesk_refresh_token_ttl: int = 30 * 24 * 3600
    zendesk_webhook_secret: SecretStr | None = None
    handoff_attempts: int = 4
    # Safety net for the solved-ticket webhook, and the only handback path where Zendesk
    # can't reach us (local dev, private networks). 0 disables polling.
    zendesk_handback_poll_seconds: float = 0
    support_email: str = "support@example.com"

    commerce_api_url: str | None = None
    commerce_api_key: SecretStr | None = None

    admin_token: SecretStr | None = None
    pseudonym_key: SecretStr | None = None
    widget_jwt_secret: SecretStr | None = None
    widget_jwt_audience: str = "shopdesk-widget"
    token_encryption_key: SecretStr | None = None
    database_url: SecretStr | None = None
    conversation_ttl_days: int = 30
    cors_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_secrets_outside_dev(self) -> Self:
        if self.env == "dev":
            return self
        missing = [
            name
            for name in (
                f"{self.llm_provider}_api_key",
                "widget_jwt_secret",
                "pseudonym_key",
                "token_encryption_key",
                "admin_token",
                "database_url",
            )
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(f"missing required settings for {self.env}: {', '.join(missing)}")
        return self

    @property
    def zendesk_url(self) -> str:
        if self.zendesk_base_url:
            return self.zendesk_base_url.rstrip("/")
        return f"https://{self.zendesk_subdomain}.zendesk.com"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
