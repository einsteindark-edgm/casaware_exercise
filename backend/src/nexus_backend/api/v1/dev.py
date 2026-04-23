from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from nexus_backend.config import settings

router = APIRouter(prefix="/dev", tags=["dev"], include_in_schema=False)


@router.get("/token")
async def mint_token(
    sub: str = Query(default="a3f4-dev-user"),
    tenant_id: str = Query(default="t_01HQ-dev"),
    email: str = Query(default="dev@nexus.local"),
    role: str = Query(default="user"),
    ttl: int = Query(default=28800, ge=60, le=86400),
) -> dict[str, str | int]:
    """Mint an HS256 JWT accepted by the dev auth path.

    Only active when ``AUTH_MODE=dev``. Returns 404 in production so the path
    does not exist for scanners.
    """
    if settings.auth_mode != "dev":
        raise HTTPException(status_code=404, detail="not found")

    from tests.fakes.fake_cognito import mint_dev_jwt

    token = mint_dev_jwt(
        sub=sub,
        tenant_id=tenant_id,
        email=email,
        role=role,
        ttl_seconds=ttl,
    )
    return {
        "id_token": token,
        "token_type": "Bearer",
        "expires_in": ttl,
        "sub": sub,
        "tenant_id": tenant_id,
        "email": email,
    }
