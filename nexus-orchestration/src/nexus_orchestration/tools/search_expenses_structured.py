"""Tool schema for the STRUCTURED (SQL) leg of the hybrid RAG.

Exposed to the LLM as `search_expenses_structured`. Databricks SQL over
`gold.expense_audit`. Shape matches Anthropic / Bedrock Converse tool spec.
"""
from __future__ import annotations

SEARCH_EXPENSES_STRUCTURED_TOOL: dict = {
    "name": "search_expenses_structured",
    "description": (
        "Exact SQL search. Use it ONLY when the user's question explicitly "
        "mentions at least one of: a vendor with a concrete name ('Uber'), "
        "a monetary value or range ('more than $100'), a date or range "
        "('March', 'between the 1st and the 15th'), a currency ('dollars', "
        "'pesos'), an exact category ('food', 'travel'), or asks for a "
        "total/sum/count ('how much did I spend'). For everything else "
        "—fuzzy topics, intents, exploration— use `search_expenses_semantic`. "
        "If in doubt, prefer semantic. Returns the aggregate "
        "(aggregate=sum|count) or up to 50 rows (aggregate=list) with "
        "expense_id to cite."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vendor": {
                "type": "string",
                "description": "Case-insensitive match against the receipt's final vendor.",
            },
            "category": {
                "type": "string",
                "description": "travel | food | lodging | office | other",
            },
            "amount_min": {"type": "number"},
            "amount_max": {"type": "number"},
            "date_from": {
                "type": "string",
                "description": "YYYY-MM-DD inclusive",
            },
            "date_to": {
                "type": "string",
                "description": "YYYY-MM-DD inclusive",
            },
            "currency": {
                "type": "string",
                "description": "COP | USD | EUR",
            },
            "aggregate": {
                "type": "string",
                "enum": ["sum", "count", "list"],
                "default": "list",
                "description": (
                    "Use 'list' (default) for questions like 'do I have any "
                    "X expenses?' or 'show me my expenses'. Use 'sum' ONLY "
                    "when the user asks for an explicit monetary total "
                    "('how much did I spend'). Use 'count' ONLY when they "
                    "ask for an explicit number of receipts ('how many "
                    "receipts'). Both sum and count also return up to 10 "
                    "rows in `rows` with real expense_id values to cite."
                ),
            },
            "limit": {
                "type": "integer",
                "default": 20,
                "maximum": 50,
            },
        },
        "required": [],
    },
}
