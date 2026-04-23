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

    # Fake mode: stub external providers (Textract / Bedrock / Vector Search)
    fake_providers: bool = True

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
    bedrock_model_id: str = "anthropic.claude-sonnet-4-6-20250514-v1:0"
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-4-6"

    log_level: str = "INFO"


settings = Settings()
