from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JWKError, JWTError

from nexus_backend.auth.models import CognitoUser
from nexus_backend.config import settings
from nexus_backend.errors import NotAuthenticated

_JWKS_TTL_SECONDS = 6 * 3600


@dataclass
class _JwksCache:
    keys_by_kid: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetched_at: float = 0.0

    def is_fresh(self) -> bool:
        return bool(self.keys_by_kid) and (time.monotonic() - self.fetched_at) < _JWKS_TTL_SECONDS


_cache = _JwksCache()


def is_jwks_ready() -> bool:
    """Return True if the JWKS cache is primed or dev mode is active."""
    from nexus_backend.config import settings as _s

    return _s.auth_mode == "dev" or _cache.is_fresh()


async def prime_jwks() -> None:
    """Populate the JWKS cache. Called from the app lifespan (prod only)."""
    if settings.auth_mode != "prod":
        return
    await _refresh_jwks()


async def _refresh_jwks() -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(settings.cognito_jwks_url)
        resp.raise_for_status()
        payload = resp.json()
    _cache.keys_by_kid = {k["kid"]: k for k in payload.get("keys", [])}
    _cache.fetched_at = time.monotonic()


async def _key_for(kid: str) -> dict[str, Any]:
    if not _cache.is_fresh() or kid not in _cache.keys_by_kid:
        await _refresh_jwks()
    key = _cache.keys_by_kid.get(kid)
    if key is None:
        raise NotAuthenticated("unknown signing key")
    return key


async def validate_token(token: str) -> CognitoUser:
    """Validate a raw bearer token and return a ``CognitoUser``.

    In dev mode, accepts HS256-signed tokens using ``DEV_JWT_SECRET``.
    In prod mode, validates RS256 signatures against the Cognito JWKS.
    """
    if settings.auth_mode == "dev":
        return _validate_dev_token(token)
    return await _validate_prod_token(token)


def _validate_dev_token(token: str) -> CognitoUser:
    try:
        claims = jwt.decode(
            token,
            settings.dev_jwt_secret,
            algorithms=["HS256"],
            options={"verify_aud": False, "verify_iss": False},
        )
    except JWTError as exc:
        raise NotAuthenticated(f"invalid dev jwt: {exc}") from exc

    if claims.get("token_use", "id") != "id":
        raise NotAuthenticated("token_use must be 'id'")

    return CognitoUser.model_validate(claims)


async def _validate_prod_token(token: str) -> CognitoUser:
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise NotAuthenticated(f"malformed jwt header: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise NotAuthenticated("jwt missing kid")

    try:
        key = await _key_for(kid)
    except JWKError as exc:
        raise NotAuthenticated(f"jwks error: {exc}") from exc

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.cognito_app_client_id,
            issuer=settings.cognito_issuer,
        )
    except JWTError as exc:
        raise NotAuthenticated(f"invalid jwt: {exc}") from exc

    if claims.get("token_use") != "id":
        raise NotAuthenticated("only id tokens are accepted (not access tokens)")

    return CognitoUser.model_validate(claims)
