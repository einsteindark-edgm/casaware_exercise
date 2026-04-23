from __future__ import annotations

from ulid import ULID


def _ulid() -> str:
    return str(ULID())


def new_expense_id() -> str:
    return f"exp_{_ulid()}"


def new_receipt_id() -> str:
    return f"rcpt_{_ulid()}"


def new_hitl_id() -> str:
    return f"hitl_{_ulid()}"


def new_event_id() -> str:
    return f"evt_{_ulid()}"


def new_session_id() -> str:
    return f"sess_{_ulid()}"


def ulid_to_epoch_ms(ulid_str: str) -> int:
    raw = ulid_str.split("_", 1)[-1]
    return int(ULID.from_str(raw).timestamp * 1000)


def epoch_ms_now() -> int:
    from time import time

    return int(time() * 1000)
