from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CognitoUser(BaseModel):
    """Subset of the Cognito ID Token claims that the rest of the backend relies on."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    sub: str
    email: str
    tenant_id: str = Field(alias="custom:tenant_id")
    role: str = Field(default="user", alias="custom:role")
    groups: list[str] = Field(default_factory=list, alias="cognito:groups")


class TenantContext(BaseModel):
    tenant_id: str
    user_id: str
