"""LLM activities.

`bedrock_converse` covers both sync and streaming modes. When
`settings.fake_providers` is True the fake publishes tokens to Redis matching
the real streaming contract.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis.asyncio as redis
from temporalio import activity
from ulid import ULID

from nexus_orchestration.activities._fakes import fake_bedrock_stream
from nexus_orchestration.config import settings

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
    if settings.fake_providers:
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

    if not inp.get("stream_to_redis"):
        response = client.converse(**request)
        return {
            "content": response["output"]["message"]["content"],
            "stop_reason": response["stopReason"],
        }

    # Streaming path.
    response = client.converse_stream(**request)
    accumulated_content: list[dict[str, Any]] = []
    current_text = ""
    current_tool_use: dict[str, Any] | None = None
    stop_reason = "end_turn"

    redis_client = _get_redis()
    tenant_id = inp["tenant_id"]
    user_id = inp["user_id"]
    workflow_id = inp["workflow_id"]

    for event in response["stream"]:
        activity.heartbeat()

        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"]["delta"]
            if "text" in delta:
                token = delta["text"]
                current_text += token
                await _publish_token(redis_client, tenant_id, user_id, workflow_id, token)
            elif "toolUse" in delta and current_tool_use is not None:
                current_tool_use["input_json"] = current_tool_use.get(
                    "input_json", ""
                ) + delta["toolUse"].get("input", "")
        elif "contentBlockStart" in event:
            start = event["contentBlockStart"]["start"]
            if "toolUse" in start:
                current_tool_use = {
                    "type": "tool_use",
                    "id": start["toolUse"]["toolUseId"],
                    "name": start["toolUse"]["name"],
                    "input_json": "",
                }
        elif "contentBlockStop" in event:
            if current_tool_use is not None:
                input_json = current_tool_use.pop("input_json", "") or "{}"
                try:
                    current_tool_use["input"] = json.loads(input_json)
                except Exception:
                    current_tool_use["input"] = {}
                accumulated_content.append(current_tool_use)
                current_tool_use = None
            elif current_text:
                accumulated_content.append({"type": "text", "text": current_text})
                current_text = ""
        elif "messageStop" in event:
            stop_reason = event["messageStop"]["stopReason"]

    return {"content": accumulated_content, "stop_reason": stop_reason}


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
