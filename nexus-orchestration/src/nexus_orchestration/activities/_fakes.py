"""Deterministic fakes used when settings.fake_providers is True.

The shape of each fake's return value mirrors its real counterpart so the
workflows don't branch on FAKE_PROVIDERS.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from nexus_orchestration.config import settings
from nexus_orchestration.schemas.events import EventEnvelope


def _hash_int(s: str, mod: int = 1000) -> int:
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % mod


def fake_textract_extract(
    s3_key: str,
    user_reported: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stubbed AnalyzeExpense-like result.

    Behavior is governed by settings.fake_hitl_mode:
      - "force": guaranteed amount + vendor discrepancy → HITL always
      - "never": exact match of user_reported → no HITL
      - "auto":  hash-derived small offset (legacy behavior)
    """
    user_reported = user_reported or {}
    user_amount = float(user_reported.get("amount") or 100.0)
    user_vendor = user_reported.get("vendor") or "Starbucks"
    user_date = user_reported.get("date") or "2026-04-22"

    mode = settings.fake_hitl_mode
    ocr_vendor: str = str(user_vendor)
    ocr_date: str = str(user_date)

    if mode == "force":
        # 15% over user amount → well above the 1% tolerance.
        ocr_amount = round(user_amount * 1.15 + 0.01, 2)
        # Swap vendor to a clearly different name to also trigger vendor conflict.
        ocr_vendor = "FAKE_VENDOR_MISMATCH"
    elif mode == "never":
        ocr_amount = round(user_amount, 2)
    else:  # "auto"
        # Hash-derived offset (1..5 units) + 0.5. On small amounts this triggers
        # HITL; on large amounts it may fall below the 1% tolerance.
        offset = (_hash_int(s3_key, mod=5) or 1) + 0.5
        ocr_amount = round(user_amount + offset, 2)

    fields = {
        "ocr_total": {"value": ocr_amount, "confidence": 99.1},
        "ocr_vendor": {"value": ocr_vendor, "confidence": 96.4},
        "ocr_date": {"value": ocr_date, "confidence": 91.2},
    }
    avg = sum(f["confidence"] for f in fields.values()) / len(fields)
    return {
        "raw_output_s3_key": f"fake/tenant/expense/{s3_key}.json",
        "fields": fields,
        "avg_confidence": round(avg, 2),
        "fields_summary": [
            {"field": "amount", "value": ocr_amount, "confidence": 99.1},
            {"field": "vendor", "value": ocr_vendor, "confidence": 96.4},
            {"field": "date", "value": ocr_date, "confidence": 91.2},
        ],
    }


def fake_vector_search(query: str, tenant_filter: str, k: int = 5) -> list[dict[str, Any]]:
    """Deterministic synthetic chunks derived from the tenant + query hash."""
    base_amount = 10.0 + (_hash_int(f"{tenant_filter}:{query}", mod=500) / 10)
    results = []
    for i in range(min(k, 3)):
        results.append(
            {
                "chunk_id": f"chunk_{tenant_filter}_{i}",
                "expense_id": f"exp_FAKE{i:03d}",
                "chunk_text": f"[FAKE] Gasto en {query.split()[0] if query.split() else 'cafeteria'} por ${base_amount + i} USD.",
                "date": "2026-03-15",
                "vendor": "Starbucks" if i % 2 == 0 else "McDonalds",
                "amount": base_amount + i,
            }
        )
    return results


async def fake_bedrock_stream(
    messages: list[dict[str, Any]],
    tenant_id: str,
    user_id: str,
    workflow_id: str,
    tools: list[dict[str, Any]] | None = None,
    redis_client: Any | None = None,
) -> dict[str, Any]:
    """Simulate a streaming Bedrock response.

    Mimics a two-turn tool-use cycle: first call requests `search_expenses`,
    second call returns a natural-language response with a citation. The
    caller (the `bedrock_converse` activity) publishes `chat.token` events to
    Redis for each word, matching the real streaming contract.
    """
    # If no tool results are in the message history, request the tool first.
    tools = tools or []
    wants_tool = not _already_has_tool_result(messages)
    if wants_tool and any(t.get("name") == "search_expenses" for t in tools):
        user_text = _last_user_text(messages)
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": f"toolu_fake_{_hash_int(user_text, mod=100000)}",
                    "name": "search_expenses",
                    "input": {"query": user_text, "k": 3},
                }
            ],
            "stop_reason": "tool_use",
        }

    # Second call: emit a natural-language response, streaming tokens.
    tool_result = _extract_first_tool_result(messages)
    vendor = "Starbucks"
    amount = 0.0
    if isinstance(tool_result, list) and tool_result:
        first = tool_result[0]
        vendor = first.get("vendor", vendor)
        amount = sum(float(r.get("amount") or 0) for r in tool_result)

    answer = (
        f"Según tus gastos, {vendor} suma aproximadamente ${amount:.2f} USD "
        f"en el periodo consultado."
    )
    tokens = [w + " " for w in answer.split(" ")]

    if redis_client is not None:
        from nexus_orchestration.activities.redis_events import (
            user_channel,
            workflow_channel,
        )

        async def _publish_token(token: str) -> None:
            envelope = EventEnvelope(
                event_type="chat.token",
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                user_id=user_id,
                payload={"token": token},
            )
            payload = envelope.model_dump_json()
            for channel in (
                user_channel(tenant_id, user_id),
                workflow_channel(tenant_id, workflow_id),
            ):
                async with redis_client.pipeline(transaction=False) as pipe:
                    pipe.publish(channel, payload)
                    pipe.zadd(f"{channel}:buffer", {payload: envelope.epoch_ms})
                    pipe.zremrangebyrank(f"{channel}:buffer", 0, -51)
                    pipe.expire(f"{channel}:buffer", 3600)
                    await pipe.execute()

        for tok in tokens:
            await _publish_token(tok)

    return {
        "content": [{"type": "text", "text": answer}],
        "stop_reason": "end_turn",
    }


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        return block["text"]
            if isinstance(content, str):
                return content
    return ""


def _already_has_tool_result(messages: list[dict[str, Any]]) -> bool:
    for msg in messages:
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
    return False


def _extract_first_tool_result(messages: list[dict[str, Any]]) -> Any:
    for msg in messages:
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    items = block.get("content", [])
                    for item in items:
                        if isinstance(item, dict) and "json" in item:
                            return item["json"]
                        if isinstance(item, dict) and "text" in item:
                            try:
                                return json.loads(item["text"])
                            except Exception:
                                return item["text"]
    return None


_ = datetime, UTC  # reserved for a future timestamp-aware fake
