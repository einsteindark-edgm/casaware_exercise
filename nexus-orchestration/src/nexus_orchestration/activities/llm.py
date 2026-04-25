"""LLM activities.

`bedrock_converse` covers both sync and streaming modes. When
`settings.fake_providers` is True the fake publishes tokens to Redis matching
the real streaming contract.
"""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis.asyncio as redis
from temporalio import activity
from ulid import ULID

from nexus_orchestration.activities._fakes import fake_bedrock_stream
from nexus_orchestration.config import settings
from nexus_orchestration.observability.logging import get_logger
from nexus_orchestration.observability.otel import current_trace_id_hex

_log = get_logger(__name__)


# ── Hallucinated-link sanitizer (streaming) ─────────────────────────────────
#
# When Bedrock streams text tokens, we want to drop any [...](/expenses/<id>)
# whose <id> wasn't returned by a real tool_result. We can't just regex the
# final text — the user is already seeing tokens via Redis. So we hold tokens
# that look like the start of an /expenses/ link until we can either close
# and validate them, or determine they're safe to release.

_FULL_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*/expenses/([\w\-]+)\s*\)")
# A buffer prefix that *might* still grow into an /expenses/ link. We hold
# back from emitting until we know it isn't one.
_PARTIAL_LINK_RE = re.compile(
    r"\[(?:[^\]]*(?:\]\(\s*(?:/(?:e(?:x(?:p(?:e(?:n(?:s(?:e(?:s(?:/[\w\-]*)?)?)?)?)?)?)?)?)?)?)?)?$"
)


class _LinkSanitizer:
    """Streaming filter: drops `[...](/expenses/<id>)` markdown links whose
    id is not in `allowed_ids`. Other text passes through unchanged.

    Tokens may arrive mid-link, so we keep a small tail buffer of any chars
    that could still be the prefix of an /expenses/ link and only emit them
    once we're sure (link closed, or pattern broken).
    """

    def __init__(self, allowed_ids: set[str]) -> None:
        self.allowed = allowed_ids
        self._buf = ""

    def feed(self, token: str) -> str:
        self._buf += token
        return self._drain(final=False)

    def flush(self) -> str:
        # End of stream: emit whatever's left, but drop any still-partial
        # /expenses/ link prefix (it can only be a fabrication that the
        # model never closed — common when it gets truncated mid-link).
        return self._drain(final=True)

    def _drain(self, final: bool) -> str:
        # Resolve every closed /expenses/ link first.
        def repl(m: re.Match[str]) -> str:
            return m.group(0) if m.group(2) in self.allowed else ""

        cleaned = _FULL_LINK_RE.sub(repl, self._buf)

        if final:
            # Any remaining partial /expenses/ prefix is a hallucination.
            cleaned = _PARTIAL_LINK_RE.sub("", cleaned)
            self._buf = ""
            return cleaned

        # Find the longest tail that might still grow into an /expenses/ link.
        m = _PARTIAL_LINK_RE.search(cleaned)
        if m and m.end() == len(cleaned):
            held_from = m.start()
            out = cleaned[:held_from]
            self._buf = cleaned[held_from:]
            return out

        self._buf = ""
        return cleaned


def _allowed_ids_from_messages(messages: list[dict[str, Any]]) -> set[str]:
    allowed: set[str] = set()
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            tr = block.get("toolResult")
            if not isinstance(tr, dict):
                continue
            for item in tr.get("content") or []:
                if not isinstance(item, dict):
                    continue
                payload = item.get("json")
                rows: list[Any] = []
                if isinstance(payload, list):
                    rows = payload
                elif isinstance(payload, dict):
                    if isinstance(payload.get("rows"), list):
                        rows = payload["rows"]
                for r in rows:
                    if isinstance(r, dict):
                        eid = r.get("expense_id")
                        if isinstance(eid, str) and eid:
                            allowed.add(eid)
    return allowed

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        kwargs: dict[str, Any] = {
            "encoding": "utf-8",
            "decode_responses": True,
            "health_check_interval": 30,
        }
        if settings.redis_tls:
            kwargs["ssl"] = True
        _redis_client = redis.from_url(settings.redis_url, **kwargs)
    return _redis_client


@activity.defn(name="load_rag_system_prompt")
async def load_rag_system_prompt() -> str:
    return (_PROMPTS_DIR / "rag_system.md").read_text(encoding="utf-8")


@activity.defn(name="bedrock_converse")
async def bedrock_converse(inp: dict[str, Any]) -> dict[str, Any]:
    """Calls Bedrock Converse (or the fake). Optionally streams tokens to Redis.

    Input shape:
      {
        system: str,
        messages: [...],
        tools: [ToolSpec],
        stream_to_redis: bool,
        tenant_id, user_id, workflow_id
      }
    """
    if settings.use_fake_bedrock:
        redis_client = _get_redis() if inp.get("stream_to_redis") else None
        return await fake_bedrock_stream(
            messages=inp["messages"],
            tenant_id=inp["tenant_id"],
            user_id=inp["user_id"],
            workflow_id=inp["workflow_id"],
            tools=inp.get("tools", []),
            redis_client=redis_client,
        )

    import boto3  # type: ignore[import-not-found]

    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    request = {
        "modelId": settings.bedrock_model_id,
        "messages": inp["messages"],
        "system": [{"text": inp["system"]}],
        "toolConfig": {
            "tools": [
                {
                    "toolSpec": {
                        "name": t["name"],
                        "description": t["description"],
                        "inputSchema": {"json": t["input_schema"]},
                    }
                }
                for t in inp.get("tools", [])
            ]
        }
        if inp.get("tools")
        else None,
        "inferenceConfig": {"maxTokens": 4096, "temperature": 0.2},
    }
    request = {k: v for k, v in request.items() if v is not None}

    # Phase E.6 — Bedrock decision log. Emits ONE structured log per
    # invocation correlating: trace_id ↔ modelId ↔ tools_offered ↔
    # tools_emitted_by_model ↔ stop_reason ↔ tokens. The Bedrock
    # Invocation Logs (account-level, /aws/bedrock/...) hold the full
    # prompt; this bridge log lets the dashboard JOIN them by timestamp
    # + workflow_id to a specific trace_id.
    _start = time.perf_counter()
    _trace_id = current_trace_id_hex()
    _tools_offered = [t["name"] for t in inp.get("tools", [])]

    if not inp.get("stream_to_redis"):
        response = client.converse(**request)
        content = response["output"]["message"]["content"]
        _emit_bedrock_decision_log(
            trace_id=_trace_id,
            workflow_id=inp.get("workflow_id"),
            tenant_id=inp.get("tenant_id"),
            model_id=settings.bedrock_model_id,
            mode="sync",
            tools_offered=_tools_offered,
            content=content,
            stop_reason=response["stopReason"],
            usage=response.get("usage") or {},
            latency_ms=(time.perf_counter() - _start) * 1000,
        )
        return {
            "content": content,
            "stop_reason": response["stopReason"],
        }

    # Streaming path. Emits BEDROCK-format content blocks so the workflow can
    # echo them back verbatim in the next turn without translation.
    response = client.converse_stream(**request)
    accumulated_content: list[dict[str, Any]] = []
    current_text = ""
    current_tool_use: dict[str, Any] | None = None
    current_tool_input_json = ""
    stop_reason = "end_turn"

    redis_client = _get_redis()
    tenant_id = inp["tenant_id"]
    user_id = inp["user_id"]
    workflow_id = inp["workflow_id"]

    # Hallucinated-link guard: gather expense_ids from prior tool_results in
    # this conversation and gate every text token through the sanitizer
    # before it reaches Redis subscribers.
    allowed_ids = _allowed_ids_from_messages(inp.get("messages") or [])
    sanitizer = _LinkSanitizer(allowed_ids)

    for event in response["stream"]:
        activity.heartbeat()

        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"]["delta"]
            if "text" in delta:
                token = delta["text"]
                safe = sanitizer.feed(token)
                if safe:
                    current_text += safe
                    await _publish_token(
                        redis_client, tenant_id, user_id, workflow_id, safe
                    )
            elif "toolUse" in delta and current_tool_use is not None:
                current_tool_input_json += delta["toolUse"].get("input", "") or ""
        elif "contentBlockStart" in event:
            start = event["contentBlockStart"]["start"]
            if "toolUse" in start:
                # Flush any text held in the sanitizer before switching block.
                tail = sanitizer.flush()
                if tail:
                    current_text += tail
                    await _publish_token(
                        redis_client, tenant_id, user_id, workflow_id, tail
                    )
                current_tool_use = {
                    "toolUse": {
                        "toolUseId": start["toolUse"]["toolUseId"],
                        "name": start["toolUse"]["name"],
                        "input": {},
                    }
                }
                current_tool_input_json = ""
        elif "contentBlockStop" in event:
            if current_tool_use is not None:
                try:
                    current_tool_use["toolUse"]["input"] = (
                        json.loads(current_tool_input_json) if current_tool_input_json else {}
                    )
                except Exception:
                    current_tool_use["toolUse"]["input"] = {}
                accumulated_content.append(current_tool_use)
                current_tool_use = None
                current_tool_input_json = ""
            else:
                tail = sanitizer.flush()
                if tail:
                    current_text += tail
                    await _publish_token(
                        redis_client, tenant_id, user_id, workflow_id, tail
                    )
                if current_text:
                    accumulated_content.append({"text": current_text})
                    current_text = ""
        elif "messageStop" in event:
            stop_reason = event["messageStop"]["stopReason"]

    # If the stream ended without a clean contentBlockStop, drain anything
    # the sanitizer is still holding.
    tail = sanitizer.flush()
    if tail:
        current_text += tail
        await _publish_token(redis_client, tenant_id, user_id, workflow_id, tail)
    if current_text:
        accumulated_content.append({"text": current_text})

    _emit_bedrock_decision_log(
        trace_id=_trace_id,
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        model_id=settings.bedrock_model_id,
        mode="stream",
        tools_offered=_tools_offered,
        content=accumulated_content,
        stop_reason=stop_reason,
        usage={},  # Bedrock streaming surface usage in metadata events; capture in a follow-up.
        latency_ms=(time.perf_counter() - _start) * 1000,
    )

    return {"content": accumulated_content, "stop_reason": stop_reason}


def _emit_bedrock_decision_log(
    *,
    trace_id: str | None,
    workflow_id: str | None,
    tenant_id: str | None,
    model_id: str,
    mode: str,
    tools_offered: list[str],
    content: list[dict[str, Any]],
    stop_reason: str,
    usage: dict[str, Any],
    latency_ms: float,
) -> None:
    """Write a single structured log line per Bedrock invocation.

    The log captures *what the model decided*: which tools it chose to call
    (with their argument names — not values, to keep the line small) and how
    much text it produced. Full prompts/responses live in the Bedrock
    Invocation Logs and are joined via timestamp + workflow_id.
    """
    tools_emitted: list[dict[str, Any]] = []
    text_blocks = 0
    text_chars = 0
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if "toolUse" in block:
            tu = block["toolUse"]
            tools_emitted.append(
                {
                    "name": tu.get("name"),
                    "input_keys": sorted(list((tu.get("input") or {}).keys())),
                }
            )
        elif "text" in block:
            text_blocks += 1
            text_chars += len(block["text"] or "")

    _log.info(
        "bedrock.invoke",
        trace_id=trace_id,
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        model_id=model_id,
        mode=mode,
        tools_offered=tools_offered,
        tools_emitted=tools_emitted,
        tools_emitted_count=len(tools_emitted),
        stop_reason=stop_reason,
        input_tokens=int(usage.get("inputTokens") or 0),
        output_tokens=int(usage.get("outputTokens") or 0),
        total_tokens=int(usage.get("totalTokens") or 0),
        text_blocks=text_blocks,
        text_chars=text_chars,
        latency_ms=round(latency_ms, 2),
    )


async def _publish_token(
    redis_client: redis.Redis,
    tenant_id: str,
    user_id: str,
    workflow_id: str,
    token: str,
) -> None:
    from nexus_orchestration.activities.redis_events import user_channel, workflow_channel
    from nexus_orchestration.schemas.events import EventEnvelope

    envelope = EventEnvelope(
        event_type="chat.token",
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        user_id=user_id,
        payload={"token": token},
    )
    payload = envelope.model_dump_json()
    for channel in (user_channel(tenant_id, user_id), workflow_channel(tenant_id, workflow_id)):
        async with redis_client.pipeline(transaction=False) as pipe:
            pipe.publish(channel, payload)
            pipe.zadd(f"{channel}:buffer", {payload: envelope.epoch_ms})
            pipe.zremrangebyrank(f"{channel}:buffer", 0, -51)
            pipe.expire(f"{channel}:buffer", 3600)
            await pipe.execute()


_ = datetime, UTC, ULID  # reserved for future timestamped variants
