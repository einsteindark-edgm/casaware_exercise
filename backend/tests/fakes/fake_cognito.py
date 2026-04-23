from __future__ import annotations

import time

from jose import jwt

from nexus_backend.config import settings


def mint_dev_jwt(
    *,
    sub: str,
    tenant_id: str,
    email: str,
    role: str = "user",
    groups: list[str] | None = None,
    ttl_seconds: int = 3600,
    secret: str | None = None,
) -> str:
    """Mint an HS256-signed JWT accepted by ``AUTH_MODE=dev``."""
    now = int(time.time())
    claims = {
        "sub": sub,
        "email": email,
        "custom:tenant_id": tenant_id,
        "custom:role": role,
        "cognito:groups": groups or ["nexus-users"],
        "token_use": "id",
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(claims, secret or settings.dev_jwt_secret, algorithm="HS256")
