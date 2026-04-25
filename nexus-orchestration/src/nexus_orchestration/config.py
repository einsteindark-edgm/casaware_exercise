from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Literal["dev", "staging", "prod"] = "dev"
    aws_region: str = "us-east-1"

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_tls_cert_path: str | None = None
    temporal_tls_key_path: str | None = None
    task_queue: str = "all"

    # Concurrency knobs per worker
    worker_max_concurrent_activities: int = 20
    worker_max_concurrent_workflow_tasks: int = 50

    # Dependencies shared with the backend
    mongodb_uri: str = "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true"
    mongodb_db: str = "nexus_dev"
    redis_url: str = "redis://localhost:6379/0"
    redis_tls: bool = False

    # S3 (receipts bucket is the only one the worker needs)
    s3_receipts_bucket: str = "nexus-receipts-dev"
    s3_textract_output_bucket: str = "nexus-textract-output-dev"
    aws_endpoint_url: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    # Fake mode: stub external providers (Textract / Bedrock / Vector Search /
    # Databricks SQL). Default False — production path is real. Flip
    # FAKE_PROVIDERS=true for isolated unit-style runs, or flip individual
    # providers via FAKE_TEXTRACT / FAKE_BEDROCK / FAKE_VECTOR_SEARCH /
    # FAKE_SQL_SEARCH.
    fake_providers: bool = False

    # Per-provider overrides. When unset, each falls back to fake_providers.
    fake_textract: bool | None = None
    fake_bedrock: bool | None = None
    fake_vector_search: bool | None = None
    fake_sql_search: bool | None = None

    # When fake_providers=True, controls how fake Textract compares against the
    # user-reported data:
    #   - "auto"  : hash-derived small offset (may or may not trigger HITL)
    #   - "force" : guaranteed discrepancy > tolerance → always HITL
    #   - "never" : OCR exactly matches user data → always happy path (no HITL)
    fake_hitl_mode: Literal["auto", "force", "never"] = "auto"

    # Auditor source: "mongo" reads user-reported data from Mongo (dev),
    # "databricks" reads from silver.expenses (prod).
    audit_source: Literal["mongo", "databricks"] = "mongo"

    # Databricks (only required when fake_providers=False or audit_source=databricks)
    databricks_host: str | None = None
    databricks_token: str | None = None
    databricks_warehouse_id: str | None = None
    databricks_catalog: str = "nexus_dev"
    databricks_vs_endpoint: str | None = None
    databricks_vs_index: str | None = None

    # LLM
    # Cross-region inference profile. Default: Amazon Nova Pro (US geo).
    # Nova is first-party AWS so it has no access-request form — good default
    # for dev. Converse API + tool-use shape is identical to Claude's.
    # Flip to "us.anthropic.claude-sonnet-4-6" once the Anthropic FTU form
    # is approved in your account.
    bedrock_model_id: str = "us.amazon.nova-pro-v1:0"
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-4-6"

    log_level: str = "INFO"

    @property
    def use_fake_textract(self) -> bool:
        if self.fake_textract is None:
            return self.fake_providers
        return self.fake_textract

    @property
    def use_fake_bedrock(self) -> bool:
        if self.fake_bedrock is None:
            return self.fake_providers
        return self.fake_bedrock

    @property
    def use_fake_vector_search(self) -> bool:
        if self.fake_vector_search is None:
            return self.fake_providers
        return self.fake_vector_search

    @property
    def use_fake_sql_search(self) -> bool:
        if self.fake_sql_search is None:
            return self.fake_providers
        return self.fake_sql_search

    def validate_real_providers(self) -> list[str]:
        """Return a list of human-readable problems preventing the non-fake
        paths from running. Empty list → OK.

        We do NOT check AWS credentials programmatically because boto3 uses
        the full chain (env vars, shared config, instance roles, SSO) — any
        programmatic test would be both flaky and noisy. boto3 surfaces the
        real error on the first Bedrock call.
        """
        problems: list[str] = []
        if not self.use_fake_bedrock:
            if not self.bedrock_model_id:
                problems.append("BEDROCK_MODEL_ID is required when FAKE_BEDROCK=false")
            if not self.aws_region:
                problems.append("AWS_REGION is required when FAKE_BEDROCK=false")
        if not self.use_fake_vector_search:
            missing = [
                f
                for f in ("databricks_host", "databricks_token", "databricks_vs_endpoint", "databricks_vs_index")
                if not getattr(self, f)
            ]
            if missing:
                problems.append(
                    "Vector Search real mode requires: "
                    + ", ".join(m.upper() for m in missing)
                )
        if not self.use_fake_sql_search:
            missing = [
                f
                for f in ("databricks_host", "databricks_token", "databricks_warehouse_id")
                if not getattr(self, f)
            ]
            if missing:
                problems.append(
                    "SQL search real mode requires: "
                    + ", ".join(m.upper() for m in missing)
                )
        return problems


settings = Settings()
