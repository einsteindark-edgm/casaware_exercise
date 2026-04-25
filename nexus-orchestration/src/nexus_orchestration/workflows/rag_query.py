"""RAGQueryWorkflow — hybrid agentic loop using AWS Bedrock (or fake).

Runs in `nexus-rag-tq`. The workflow streams tokens via Redis, decides whether
to call `search_expenses_semantic` (vector) or `search_expenses_structured`
(SQL) — or both in the same turn — and persists the turn in `chat_turns` so
subsequent turns can be context-aware.

Input (from backend/src/nexus_backend/api/v1/chat.py):
  {session_id, turn, tenant_id, user_id, message}

Note: `previous_turns` is not in the input — we load it from Mongo because the
backend only writes session metadata, not turn history.

Security invariants:
- tenant_filter ALWAYS comes from the workflow input, never from LLM args.
- `max_iterations` caps runaway loops.
- bedrock_converse RetryPolicy=1 while streaming (re-stream duplicates tokens).
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nexus_orchestration.tools.search_expenses import (
        SEARCH_EXPENSES_SEMANTIC_TOOL,
    )
    from nexus_orchestration.tools.search_expenses_structured import (
        SEARCH_EXPENSES_STRUCTURED_TOOL,
    )


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

        tool_calls_log: list[dict[str, Any]] = []
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
                    "tools": [
                        SEARCH_EXPENSES_SEMANTIC_TOOL,
                        SEARCH_EXPENSES_STRUCTURED_TOOL,
                    ],
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
                allowed_ids = _allowed_expense_ids(messages)
                final_text, hallucinated = _strip_hallucinated_expense_links(
                    final_text, allowed_ids
                )
                if hallucinated:
                    workflow.logger.warning(
                        "stripped hallucinated expense links",
                        extra={"workflow_id": workflow_id, "ids": list(hallucinated)},
                    )

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
                            "tool_calls": tool_calls_log,
                        },
                    },
                    start_to_close_timeout=timedelta(seconds=5),
                )
                self._state = "completed"
                return {
                    "status": "completed",
                    "text": final_text,
                    "citations": citations,
                    "tool_calls": tool_calls_log,
                }

            # Tool-use branch. Each tool block may be dispatched independently.
            # Bedrock Converse format: assistant blocks look like
            # {"toolUse": {"toolUseId": ..., "name": ..., "input": {...}}}
            # and we reply with a user message whose content is a list of
            # {"toolResult": {"toolUseId": ..., "content": [{"json": {...}}]}}
            tool_results: list[dict[str, Any]] = []
            for block in response["content"]:
                if not isinstance(block, dict):
                    continue
                tool_use = block.get("toolUse")
                if not isinstance(tool_use, dict):
                    continue

                tool_name = tool_use.get("name")
                tool_use_id = tool_use.get("toolUseId")
                tool_input = tool_use.get("input") or {}

                if tool_name == "search_expenses_semantic":
                    result = await workflow.execute_activity(
                        "vector_similarity_search",
                        {
                            "query": tool_input.get("query", ""),
                            "k": int(tool_input.get("k", 5)),
                            # CRITICAL: tenant_filter comes from the workflow
                            # input, NEVER from LLM-provided arguments.
                            "tenant_filter": tenant_id,
                        },
                        start_to_close_timeout=timedelta(seconds=15),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                    tool_calls_log.append(
                        {
                            "tool": "search_expenses_semantic",
                            "row_count": len(result) if isinstance(result, list) else 0,
                        }
                    )
                    # Bedrock requires toolResult.content[*].json to be a JSON
                    # object, not an array — wrap the list of rows in a dict.
                    semantic_payload = {
                        "rows": result if isinstance(result, list) else [],
                        "row_count": len(result) if isinstance(result, list) else 0,
                    }
                    tool_results.append(
                        {
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "content": [{"json": semantic_payload}],
                            }
                        }
                    )

                elif tool_name == "search_expenses_structured":
                    result = await workflow.execute_activity(
                        "search_expenses_structured",
                        {**tool_input, "tenant_filter": tenant_id},
                        start_to_close_timeout=timedelta(seconds=15),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                    tool_calls_log.append(
                        {
                            "tool": "search_expenses_structured",
                            "row_count": (
                                result.get("row_count_total", 0)
                                if isinstance(result, dict)
                                else 0
                            ),
                            "error": (
                                result.get("error")
                                if isinstance(result, dict)
                                else None
                            ),
                        }
                    )
                    structured_payload = (
                        result
                        if isinstance(result, dict)
                        else {"rows": result if isinstance(result, list) else []}
                    )
                    tool_results.append(
                        {
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "content": [{"json": structured_payload}],
                            }
                        }
                    )

                else:
                    tool_results.append(
                        {
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "content": [{"text": f"Tool {tool_name} not available"}],
                                "status": "error",
                            }
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
                    "tool_calls": tool_calls_log,
                },
            },
            start_to_close_timeout=timedelta(seconds=5),
        )
        self._state = "failed"
        return {"status": "max_iterations_exceeded", "tool_calls": tool_calls_log}

    @workflow.signal(name="cancel_chat")
    async def cancel_chat(self, payload: dict[str, Any]) -> None:
        self._cancelled = True

    @workflow.query(name="get_status")
    def get_status(self) -> dict[str, Any]:
        return {"state": self._state, "current_step": self._state}


# ── Citation extraction ─────────────────────────────────────────────────────

def normalize_citation(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one row from either tool into the wire shape the frontend
    consumes. Returns None if the row lacks an expense_id (nothing to cite).
    """
    eid = raw.get("expense_id")
    if not eid:
        return None
    amount = raw.get("amount")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    return {
        "expense_id": eid,
        "vendor": raw.get("vendor"),
        "amount": amount,
        "currency": raw.get("currency"),
        "date": raw.get("date"),
        "category": raw.get("category"),
        "link": raw.get("link") or f"/expenses/{eid}",
        "source": raw.get("_source") or raw.get("source") or "unknown",
    }


def _iter_tool_result_rows(tool_result: Any):
    """Yield row dicts regardless of whether the tool returned a list (vector)
    or a dict with a `rows` key (SQL) or an aggregate payload (SQL sum/count).
    """
    if isinstance(tool_result, list):
        for r in tool_result:
            if isinstance(r, dict):
                yield r
        return
    if isinstance(tool_result, dict):
        if tool_result.get("error"):
            return
        rows = tool_result.get("rows")
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict):
                    yield r


def _extract_citations_from_history(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Walk the toolResult blocks in order; normalize + dedupe by expense_id,
    preserving the first source seen. Cap at 10 citations.

    Reads Bedrock-Converse-shaped blocks: {"toolResult": {"content": [...]}}.
    """
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            tool_result = block.get("toolResult")
            if not isinstance(tool_result, dict):
                continue
            for item in tool_result.get("content", []) or []:
                if not isinstance(item, dict):
                    continue
                payload = item.get("json")
                if payload is None:
                    continue
                for raw in _iter_tool_result_rows(payload):
                    cit = normalize_citation(raw)
                    if cit is None:
                        continue
                    if cit["expense_id"] in seen:
                        continue
                    seen.add(cit["expense_id"])
                    citations.append(cit)
                    if len(citations) >= 10:
                        return citations
    return citations


def _extract_final_text(content: list[dict[str, Any]]) -> str:
    for block in content:
        if isinstance(block, dict) and block.get("text"):
            return block["text"]
    return ""


# ── Hallucination guard ─────────────────────────────────────────────────────

# Matches any markdown link to /expenses/<id>. The id is captured so we can
# verify it against the set of expense_ids actually returned by tools.
_EXPENSE_LINK_RE = re.compile(
    r"\[([^\]]*)\]\(\s*/expenses/([A-Za-z0-9_\-]+)\s*\)"
)


def _allowed_expense_ids(messages: list[dict[str, Any]]) -> set[str]:
    """Set of expense_ids that actually appear in tool_result rows of this run.

    These are the ONLY ids the assistant is allowed to cite. Anything else in
    the final text is a hallucination and must be stripped before we publish
    the message to the user.
    """
    allowed: set[str] = set()
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            tool_result = block.get("toolResult")
            if not isinstance(tool_result, dict):
                continue
            for item in tool_result.get("content", []) or []:
                if not isinstance(item, dict):
                    continue
                payload = item.get("json")
                if payload is None:
                    continue
                for raw in _iter_tool_result_rows(payload):
                    eid = raw.get("expense_id")
                    if isinstance(eid, str) and eid:
                        allowed.add(eid)
    return allowed


def _strip_hallucinated_expense_links(
    final_text: str, allowed_ids: set[str]
) -> tuple[str, set[str]]:
    """Remove any /expenses/<id> markdown link whose id isn't in allowed_ids.

    Returns (sanitized_text, set_of_stripped_ids). The link text itself is
    dropped along with the URL — keeping the text would still leave a dangling
    reference to a fabricated receipt. We collapse runs of resulting blank
    lines so the output stays tidy.
    """
    stripped: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        eid = match.group(2)
        if eid in allowed_ids:
            return match.group(0)
        stripped.add(eid)
        return ""

    cleaned = _EXPENSE_LINK_RE.sub(_replace, final_text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, stripped
