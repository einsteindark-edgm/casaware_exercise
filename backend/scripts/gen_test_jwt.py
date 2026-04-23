"""Generate a dev JWT accepted by ``AUTH_MODE=dev``.

Example:
    uv run python scripts/gen_test_jwt.py \\
        --sub a3f4-user-1 \\
        --tenant t_01HQ_ACME \\
        --email alice@example.com
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running via "python scripts/gen_test_jwt.py" without installing the package.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from tests.fakes.fake_cognito import mint_dev_jwt  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Generate a Nexus dev JWT (HS256)")
    p.add_argument("--sub", required=True, help="User sub (Cognito user id)")
    p.add_argument("--tenant", required=True, help="tenant_id (custom:tenant_id claim)")
    p.add_argument("--email", required=True)
    p.add_argument("--role", default="user", choices=["user", "auditor", "admin"])
    p.add_argument("--ttl", type=int, default=3600, help="Token TTL in seconds")
    p.add_argument("--secret", default=None, help="Override DEV_JWT_SECRET")
    args = p.parse_args()

    token = mint_dev_jwt(
        sub=args.sub,
        tenant_id=args.tenant,
        email=args.email,
        role=args.role,
        ttl_seconds=args.ttl,
        secret=args.secret,
    )
    print(token)


if __name__ == "__main__":
    main()
