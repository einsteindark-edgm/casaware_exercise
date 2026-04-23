from typing import Literal

from pydantic import Field
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

    auth_mode: Literal["dev", "prod"] = "dev"
    dev_jwt_secret: str = "dev-secret-change-me"

    cognito_user_pool_id: str = "us-east-1_DEV"
    cognito_app_client_id: str = "dev-client"
    cognito_jwks_url: str = "http://localhost/unused"

    temporal_mode: Literal["real", "fake"] = "real"
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_tls_cert_path: str | None = None
    temporal_tls_key_path: str | None = None

    redis_url: str = "redis://localhost:6379/0"
    redis_tls: bool = False

    mongodb_uri: str = "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true"
    mongodb_db: str = "nexus_dev"

    s3_receipts_bucket: str = "nexus-receipts-dev"
    s3_textract_output_bucket: str = "nexus-textract-output-dev"
    aws_endpoint_url: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    rate_limit_default: str = "100/minute"

    xray_enabled: bool = False
    log_level: str = "INFO"

    expense_history_cache: bool = False

    @property
    def cognito_issuer(self) -> str:
        return f"https://cognito-idp.{self.aws_region}.amazonaws.com/{self.cognito_user_pool_id}"


settings = Settings()
