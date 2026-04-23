from __future__ import annotations

from fastapi import Header

from nexus_backend.auth.cognito import validate_token
from nexus_backend.auth.models import CognitoUser
from nexus_backend.errors import NotAuthenticated
from nexus_backend.observability.logging import bind_request_context


async def get_current_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> CognitoUser:
    """Validate the ``Authorization: Bearer <token>`` header and return the user."""
    if not authorization:
        raise NotAuthenticated("missing Authorization header")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise NotAuthenticated("malformed Authorization header (expected 'Bearer <token>')")

    user = await validate_token(parts[1].strip())
    bind_request_context(tenant_id=user.tenant_id, user_id=user.sub)
    return user


async def get_tenant_id(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    user = await get_current_user(authorization=authorization)
    return user.tenant_id
