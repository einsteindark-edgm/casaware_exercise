"""Unit tests for citation extraction / normalization in RAGQueryWorkflow.

Tool-result blocks are in Bedrock Converse format:
    {"toolResult": {"toolUseId": ..., "content": [{"json": {...}}]}}
"""
from __future__ import annotations

from nexus_orchestration.workflows.rag_query import (
    _citations_for_cited_ids,
    _cited_expense_ids_in_order,
    _extract_citations_from_history,
    normalize_citation,
)


def _tool_result_block(tool_use_id: str, payload):
    return {
        "toolResult": {
            "toolUseId": tool_use_id,
            "content": [{"json": payload}],
        }
    }


def test_normalize_adds_link_and_source():
    cit = normalize_citation(
        {
            "expense_id": "exp_01KQABC",
            "vendor": "Uber",
            "amount": 42.0,
            "currency": "USD",
            "date": "2026-03-14",
            "_source": "sql",
        }
    )
    assert cit is not None
    assert cit["link"] == "/expenses/exp_01KQABC"
    assert cit["source"] == "sql"


def test_normalize_drops_row_without_expense_id():
    assert normalize_citation({"vendor": "Uber"}) is None


def test_dedupe_by_expense_id_preserves_first_source():
    # LLM first calls SQL, then semantic. Both return the same expense_id.
    # The dedupe should keep the first source ("sql").
    messages = [
        {
            "role": "user",
            "content": [
                _tool_result_block(
                    "t1",
                    {
                        "aggregate_kind": "list",
                        "rows": [
                            {
                                "expense_id": "exp_1",
                                "vendor": "Uber",
                                "amount": 42.0,
                                "currency": "USD",
                                "link": "/expenses/exp_1",
                                "_source": "sql",
                            }
                        ],
                    },
                )
            ],
        },
        {
            "role": "user",
            "content": [
                _tool_result_block(
                    "t2",
                    [
                        {
                            "expense_id": "exp_1",
                            "vendor": "Uber",
                            "amount": 42.0,
                            "chunk_text": "…",
                            "_source": "semantic",
                            "link": "/expenses/exp_1",
                        },
                        {
                            "expense_id": "exp_2",
                            "vendor": "Starbucks",
                            "amount": 5.5,
                            "_source": "semantic",
                            "link": "/expenses/exp_2",
                        },
                    ],
                )
            ],
        },
    ]
    cits = _extract_citations_from_history(messages)
    assert [c["expense_id"] for c in cits] == ["exp_1", "exp_2"]
    assert cits[0]["source"] == "sql"
    assert cits[1]["source"] == "semantic"


def test_sql_error_payload_yields_no_citations():
    messages = [
        {
            "role": "user",
            "content": [_tool_result_block("t1", {"error": "no filters provided"})],
        }
    ]
    assert _extract_citations_from_history(messages) == []


def test_cap_at_10():
    rows = [
        {
            "expense_id": f"exp_{i:03d}",
            "vendor": "V",
            "amount": 1.0,
            "link": f"/expenses/exp_{i:03d}",
            "_source": "semantic",
        }
        for i in range(25)
    ]
    messages = [
        {"role": "user", "content": [_tool_result_block("t1", rows)]}
    ]
    cits = _extract_citations_from_history(messages)
    assert len(cits) == 10


def test_cited_ids_in_order_dedupes_and_preserves_order():
    text = (
        "Mira [a](/expenses/exp_B) y luego [b](/expenses/exp_A) "
        "y de nuevo [c](/expenses/exp_B)."
    )
    assert _cited_expense_ids_in_order(text) == ["exp_B", "exp_A"]


def test_citations_for_cited_ids_filters_to_what_llm_cited():
    """Tool returned 3 rows, LLM cited only one — chips must follow the LLM."""
    rows = [
        {
            "expense_id": "exp_GOOD",
            "vendor": "TT",
            "amount": 5700.0,
            "currency": "COP",
            "_source": "semantic",
            "link": "/expenses/exp_GOOD",
        },
        {
            "expense_id": "exp_NOISE_1",
            "vendor": "Starbucks",
            "amount": 24395.0,
            "currency": "COP",
            "_source": "semantic",
            "link": "/expenses/exp_NOISE_1",
        },
        {
            "expense_id": "exp_NOISE_2",
            "vendor": "Starbucks",
            "amount": 298765.0,
            "currency": "COP",
            "_source": "semantic",
            "link": "/expenses/exp_NOISE_2",
        },
    ]
    messages = [
        {"role": "user", "content": [_tool_result_block("t1", rows)]},
    ]
    final_text = "Encontré la factura. [ver recibo](/expenses/exp_GOOD)"

    cits = _citations_for_cited_ids(messages, final_text)
    assert [c["expense_id"] for c in cits] == ["exp_GOOD"]
    assert cits[0]["vendor"] == "TT"


def test_citations_for_cited_ids_empty_when_text_has_no_links():
    rows = [
        {
            "expense_id": "exp_X",
            "vendor": "Uber",
            "amount": 10.0,
            "_source": "sql",
            "link": "/expenses/exp_X",
        }
    ]
    messages = [{"role": "user", "content": [_tool_result_block("t1", rows)]}]
    assert _citations_for_cited_ids(messages, "No encontré gastos que coincidan") == []


def test_aggregate_only_payload_yields_no_citations():
    messages = [
        {
            "role": "user",
            "content": [
                _tool_result_block(
                    "t1",
                    {
                        "aggregate_kind": "sum",
                        "aggregate_value": 100.0,
                        "currency": "USD",
                        "rows": [],
                    },
                )
            ],
        }
    ]
    assert _extract_citations_from_history(messages) == []
