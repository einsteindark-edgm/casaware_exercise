from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Collections we replicate. Order matters only for bootstrap full-sync
# (expenses before receipts/hitl so foreign keys resolve cleanly in silver).
DEFAULT_COLLECTIONS: tuple[str, ...] = (
    "expenses",
    "receipts",
    "hitl_tasks",
    "ocr_extractions",
    "expense_events",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongodb_uri: str = Field(..., description="mongodb+srv://... connection string")
    mongodb_db: str = "nexus_dev"

    # AWS
    aws_region: str = "us-east-1"
    s3_bucket: str = Field(..., description="Bucket for CDC events (e.g. nexus-dev-edgm-cdc)")
    ddb_offsets_table: str = Field(..., description="DynamoDB table for resume tokens")
    aws_endpoint_url: str | None = None  # for LocalStack in dev

    # Batcher
    batch_max_events: int = 200
    batch_max_seconds: int = 30
    idle_flush_seconds: int = 300  # don't emit empty files; just skip

    # What to watch
    collections: list[str] = list(DEFAULT_COLLECTIONS)

    # Behaviour
    log_level: str = "INFO"
    bootstrap_on_empty_offset: bool = True  # initial full-sync when DDB empty


settings = Settings()  # type: ignore[call-arg]
