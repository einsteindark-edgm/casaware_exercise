"""Workflow-level tests for the hybrid RAG loop.

Uses Temporal's time-skipping environment + fake SQL + fake vector. Bedrock is
replaced by a scripted mock whose output matches the real Bedrock Converse
shape ({"toolUse": {...}}, {"text": "..."}).

Covers:
  1. Exact filters → structured tool called, citations from SQL.
  2. Fuzzy → semantic tool called, citations from vector.
  3. Tenant isolation: LLM tries to pass another tenant; workflow overrides.
  4. Tool error path: SQL without filters → {"error": ...}; workflow doesn't
     crash.
"""
from __future__ import annotations

from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from nexus_orchestration.activities._fakes import fake_sql_search, fake_vector_search
from nexus_orchestration.workflows.rag_query import RAGQueryWorkflow

_captured_tool_inputs: list[dict[str, Any]] = []
_captured_tool_results: list[dict[str, Any]] = []


def _reset_captured() -> None:
    _captured_tool_inputs.clear()
    _captured_tool_results.clear()


@activity.defn(name="load_rag_system_prompt")
async def mock_system_prompt() -> str:
    return "test prompt"


@activity.defn(name="load_chat_history")
async def mock_load_history(inp: dict[str, Any]) -> list[dict[str, Any]]:
    return []


@activity.defn(name="save_chat_turn")
async def mock_save_turn(inp: dict[str, Any]) -> None:
    return None


@activity.defn(name="publish_event")
async def mock_publish(inp: dict[str, Any]) -> None:
    return None


@activity.defn(name="vector_similarity_search")
async def mock_vector_search(inp: dict[str, Any]) -> list[dict[str, Any]]:
    _captured_tool_inputs.append(
        {"tool": "search_expenses_semantic", "input": dict(inp)}
    )
    rows = fake_vector_search(
        inp.get("query", ""), inp["tenant_filter"], int(inp.get("k", 5))
    )
    _captured_tool_results.append(
        {"tool": "search_expenses_semantic", "rows": list(rows)}
    )
    return rows


@activity.defn(name="search_expenses_structured")
async def mock_sql_search(inp: dict[str, Any]) -> dict[str, Any]:
    _captured_tool_inputs.append(
        {"tool": "search_expenses_structured", "input": dict(inp)}
    )
    tool_input = {
        k: v
        for k, v in inp.items()
        if k not in {"tenant_filter", "tenant_id", "tenantId", "tenant"}
    }
    if not any(
        tool_input.get(k)
        for k in (
            "vendor",
            "category",
            "amount_min",
            "amount_max",
            "date_from",
            "date_to",
            "currency",
        )
    ):
        return {"error": "at least one filter is required"}
    aggregate = str(tool_input.get("aggregate") or "list")
    result = fake_sql_search(
        tool_input=tool_input,
        tenant_filter=inp["tenant_filter"],
        aggregate_kind=aggregate,
    )
    for row in result.get("rows", []):
        eid = row.get("expense_id")
        if eid:
            row["link"] = f"/expenses/{eid}"
            row["_source"] = "sql"
    _captured_tool_results.append(
        {"tool": "search_expenses_structured", "rows": list(result.get("rows", []))}
    )
    return result


def _bedrock_mock(script: list[Any]):
    """Each script entry is either a Bedrock-shaped dict, or a callable
    `() -> dict` evaluated at activity-call time (so the final-text turn can
    cite expense_ids that came from an earlier tool call).
    """
    counter = {"i": 0}

    @activity.defn(name="bedrock_converse")
    async def mock_bedrock(inp: dict[str, Any]) -> dict[str, Any]:
        i = counter["i"]
        counter["i"] = i + 1
        if i < len(script):
            entry = script[i]
            return entry() if callable(entry) else entry
        return {"content": [{"text": "done"}], "stop_reason": "end_turn"}

    return mock_bedrock


ALL_COMMON = [
    mock_system_prompt,
    mock_load_history,
    mock_save_turn,
    mock_publish,
    mock_vector_search,
    mock_sql_search,
]


async def _run_workflow(script: list[dict[str, Any]], message: str):
    _reset_captured()
    mock_bedrock = _bedrock_mock(script)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="nexus-rag-tq",
            workflows=[RAGQueryWorkflow],
            activities=[*ALL_COMMON, mock_bedrock],
        ):
            handle = await env.client.start_workflow(
                "RAGQueryWorkflow",
                {
                    "session_id": "sess_TEST",
                    "turn": 1,
                    "tenant_id": "t_OWNER",
                    "user_id": "u_1",
                    "message": message,
                },
                id="rag-test-sess-1",
                task_queue="nexus-rag-tq",
            )
            return await handle.result()


def _bedrock_tool_use(name: str, args: dict[str, Any], tool_use_id: str = "t1"):
    return {
        "content": [
            {
                "toolUse": {
                    "toolUseId": tool_use_id,
                    "name": name,
                    "input": args,
                }
            }
        ],
        "stop_reason": "tool_use",
    }


def _bedrock_text(text: str):
    return {"content": [{"text": text}], "stop_reason": "end_turn"}


# ── Scenarios ──────────────────────────────────────────────────────────────

def _cite_first_row_after(tool_name: str):
    """Returns a callable that, at the time it's invoked, builds a final-text
    response citing the first expense_id from the most recent capture for
    `tool_name`. Used so test scripts can reference IDs the fakes produced.
    """

    def build() -> dict[str, Any]:
        for capture in reversed(_captured_tool_results):
            if capture["tool"] != tool_name:
                continue
            for row in capture["rows"]:
                eid = row.get("expense_id")
                if eid:
                    return _bedrock_text(
                        f"Aquí está. [ver recibo](/expenses/{eid})"
                    )
        return _bedrock_text("No encontré gastos que coincidan")

    return build


@pytest.mark.asyncio
async def test_structured_tool_path():
    script = [
        _bedrock_tool_use(
            "search_expenses_structured",
            {"vendor": "Uber", "aggregate": "list"},
        ),
        _cite_first_row_after("search_expenses_structured"),
    ]
    result = await _run_workflow(script, "¿cuánto gasté en Uber?")
    assert result["status"] == "completed"
    assert len(result["citations"]) == 1
    assert result["citations"][0]["link"].startswith("/expenses/")
    assert result["citations"][0]["source"] == "sql"


@pytest.mark.asyncio
async def test_semantic_tool_path():
    script = [
        _bedrock_tool_use(
            "search_expenses_semantic",
            {"query": "comida saludable", "k": 3},
        ),
        _cite_first_row_after("search_expenses_semantic"),
    ]
    result = await _run_workflow(script, "recibos relacionados con comida saludable")
    assert result["status"] == "completed"
    assert len(result["citations"]) == 1
    assert result["citations"][0]["source"] == "semantic"


@pytest.mark.asyncio
async def test_extra_tool_rows_are_not_attached_when_llm_cites_one():
    """Regression: LLM only cites one of N rows the tool returned. The chip
    list must mirror the answer, not dump every row.
    """
    script = [
        _bedrock_tool_use(
            "search_expenses_semantic",
            {"query": "Proveedor Logístico Global", "k": 5},
        ),
        _cite_first_row_after("search_expenses_semantic"),
    ]
    result = await _run_workflow(script, "dame la factura de Proveedor Logístico Global")
    assert result["status"] == "completed"
    # fake_vector_search returns 3 rows but the LLM cited only the first.
    assert len(result["citations"]) == 1


@pytest.mark.asyncio
async def test_tenant_isolation_cannot_be_overridden_by_llm():
    script = [
        _bedrock_tool_use(
            "search_expenses_structured",
            {
                "vendor": "Uber",
                "tenant_id": "t_ATTACKER",
                "tenant_filter": "t_ATTACKER",
            },
        ),
        _bedrock_text("Listo."),
    ]
    await _run_workflow(script, "dame mis Ubers")
    sql_calls = [c for c in _captured_tool_inputs if c["tool"] == "search_expenses_structured"]
    assert len(sql_calls) == 1
    assert sql_calls[0]["input"]["tenant_filter"] == "t_OWNER"


@pytest.mark.asyncio
async def test_error_payload_does_not_crash_workflow():
    script = [
        _bedrock_tool_use("search_expenses_structured", {}),
        _bedrock_text("No pude filtrar, intenta con un vendor."),
    ]
    result = await _run_workflow(script, "lo que sea")
    assert result["status"] == "completed"
    assert result["citations"] == []
