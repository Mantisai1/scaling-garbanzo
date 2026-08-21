import os


class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./mantis.db")
    # One-time bootstrap token used to create the first organization and admin key.
    bootstrap_token: str = os.getenv("MANTIS_BOOTSTRAP_TOKEN", "change-me-bootstrap")
    # Optional model providers for the LLM judge / fine-tune adapter.
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    judge_model: str = os.getenv("MANTIS_JUDGE_MODEL", "gpt-4o-mini")
    # Retention defaults (days) per data class. Audit retention can never be shorter than others.
    retention_traces_days: int = int(os.getenv("MANTIS_RETENTION_TRACES_DAYS", "30"))
    retention_payloads_days: int = int(os.getenv("MANTIS_RETENTION_PAYLOADS_DAYS", "30"))
    retention_audit_days: int = int(os.getenv("MANTIS_RETENTION_AUDIT_DAYS", "365"))
    schema_version: str = "1.0.0"
    genai_semconv_version: str = "1.37.0"  # pinned OpenTelemetry GenAI semantic conventions


settings = Settings()
assert settings.retention_audit_days >= max(
    settings.retention_traces_days, settings.retention_payloads_days
), "Audit retention must be >= all other data classes"
