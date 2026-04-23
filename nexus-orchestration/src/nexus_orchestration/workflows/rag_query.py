"""RAGQueryWorkflow — minimal agentic loop using AWS Bedrock (or fake).

Runs in `nexus-rag-tq`. The workflow streams tokens via Redis, decides whether
to call the `search_expenses` tool, and persists the turn in `chat_turns` so
subsequent turns can be context-aware.

Input (from backend/src/nexus_backend/api/v1/chat.py):
  {session_id, turn, tenant_id, user_id, message}

Note: `previous_turns` is not in the input — we load it from Mongo because the
backend only writes session metadata, not turn history.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nexus_orchestration.tools.search_expenses import SEARCH_EXPENSES_TOOL


@workflow.defn(name="RAGQueryWorkflow")
class RAGQueryWorkflow:
    def __init__(self) -> None:
        self._cancelled = False
        self._state: str = "running"

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        tenant_id = inp["tenant_id"]
        user_id = inp["user_id"]
        session_id = inp["session_id"]
        turn = int(inp.get("turn") or 1)
        user_message = inp["message"]
        workflow_id = workflow.info().workflow_id

        # 1) Load previous turns and the system prompt.
        system_prompt = await workflow.execute_activity(
            "load_rag_system_prompt",
            start_to_close_timeout=timedelta(seconds=5),
        )
        previous_turns = await workflow.execute_activity(
            "load_chat_history",
            {"session_id": session_id, "tenant_id": tenant_id, "max_turns": 10},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        messages: list[dict[str, Any]] = list(previous_turns) + [
            {"role": "user", "content": [{"text": user_message}]}
        ]

        max_iterations = 5
        for _ in range(max_iterations):
            if self._cancelled:
                self._state = "cancelled"
                return {"status": "cancelled"}

            response = await workflow.execute_activity(
                "bedrock_converse",
                {
                    "system": system_prompt,
                    "messages": messages,
                    "tools": [SEARCH_EXPENSES_TOOL],
                    "stream_to_redis": True,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "workflow_id": workflow_id,
                },
                start_to_close_timeout=timedelta(seconds=60),
                heartbeat_timeout=timedelta(seconds=10),
                # No retries while streaming — re-streaming duplicates tokens.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

            messages.append({"role": "assistant", "content": response["content"]})

            if response.get("stop_reason") != "tool_use":
                citations = _extract_citations_from_history(messages)
                final_text = _extract_final_text(response["content"])

                await workflow.execute_activity(
                    "save_chat_turn",
                    {
                        "session_id": session_id,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "turn": turn,
                        "user_message": user_message,
                        "assistant_message": final_text,
                        "citations": citations,
                    },
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )

                await workflow.execute_activity(
                    "publish_event",
                    {
                        "event_type": "chat.complete",
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "payload": {
                            "session_id": session_id,
                            "citations": citations,
                            "final_text": final_text,
                        },
                    },
                    start_to_close_timeout=timedelta(seconds=5),
                )
                self._state = "completed"
                return {
                    "status": "completed",
                    "text": final_text,
                    "citations": citations,
                }

            # Tool-use branch.
            tool_results = []
            for block in response["content"]:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block["name"] == "search_expenses":
                    search_result = await workflow.execute_activity(
                        "vector_similarity_search",
                        {
                            "query": block["input"].get("query", ""),
                            "k": int(block["input"].get("k", 5)),
                            # CRITICAL: tenant_filter comes from the workflow
                            # input, NOT from LLM-provided arguments.
                            "tenant_filter": tenant_id,
                        },
                        start_to_close_timeout=timedelta(seconds=10),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": [{"json": search_result}],
                        }
                    )
                else:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": [{"text": f"Tool {block['name']} not available"}],
                            "status": "error",
                        }
                    )
            messages.append({"role": "user", "content": tool_results})

        # Exceeded max_iterations.
        await workflow.execute_activity(
            "publish_event",
            {
                "event_type": "chat.complete",
                "tenant_id": tenant_id,
                "user_id": user_id,
                "payload": {
                    "session_id": session_id,
                    "error": "max_iterations_exceeded",
                },
            },
            start_to_close_timeout=timedelta(seconds=5),
        )
        self._state = "failed"
        return {"status": "max_iterations_exceeded"}

    @workflow.signal(name="cancel_chat")
    async def cancel_chat(self, payload: dict[str, Any]) -> None:
        self._cancelled = True

    @workflow.query(name="get_status")
    def get_status(self) -> dict[str, Any]:
        return {"state": self._state, "current_step": self._state}


def _extract_citations_from_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            for item in block.get("content", []):
                if isinstance(item, dict) and isinstance(item.get("json"), list):
                    citations.extend(item["json"])
    return citations[:10]


def _extract_final_text(content: list[dict[str, Any]]) -> str:
    for block in content:
        if isinstance(block, dict) and block.get("text"):
            return block["text"]
    return ""
