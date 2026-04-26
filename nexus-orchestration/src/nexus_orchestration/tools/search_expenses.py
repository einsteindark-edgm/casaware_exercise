"""Tool schema for the SEMANTIC leg of the hybrid RAG.

Exposed to the LLM as `search_expenses_semantic`. Databricks Vector Search on
`gold.expense_chunks_index`. Shape matches Anthropic / Bedrock Converse tool
spec.
"""
from __future__ import annotations

SEARCH_EXPENSES_SEMANTIC_TOOL: dict = {
    "name": "search_expenses_semantic",
    "description": (
        "Semantic (vector) search over the user's approved receipts. Use it "
        "by DEFAULT for any question that does NOT explicitly contain a "
        "vendor name, a date, an amount, a currency, an exact category, or "
        "ask for totals/counts. Ideal for thematic or exploratory questions "
        "— 'receipts about transportation', 'expenses related to a client', "
        "'what unusual expenses do I have', 'things related to marketing'. "
        "Not suitable for exact totals or amount/date filters: for that, "
        "use `search_expenses_structured`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query",
            },
            "k": {
                "type": "integer",
                "description": (
                    "Number of candidates to retrieve before filtering by "
                    "minimum score. 10 is a good default; raise to 20 if "
                    "the first search returned few rows."
                ),
                "default": 10,
            },
        },
        "required": ["query"],
    },
}

# Backwards-compat alias — remove once all imports migrate.
SEARCH_EXPENSES_TOOL = SEARCH_EXPENSES_SEMANTIC_TOOL
