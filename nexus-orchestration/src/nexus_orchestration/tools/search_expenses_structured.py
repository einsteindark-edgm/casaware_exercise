"""Tool schema for the STRUCTURED (SQL) leg of the hybrid RAG.

Exposed to the LLM as `search_expenses_structured`. Databricks SQL over
`gold.expense_audit`. Shape matches Anthropic / Bedrock Converse tool spec.
"""
from __future__ import annotations

SEARCH_EXPENSES_STRUCTURED_TOOL: dict = {
    "name": "search_expenses_structured",
    "description": (
        "Busca gastos por filtros EXACTOS (vendor, categoría, rango de monto, "
        "rango de fechas, moneda). Úsala cuando la pregunta tiene datos "
        "concretos ('cuánto gasté en Uber', 'recibos entre $100 y $500 en "
        "marzo'). Devuelve el total agregado (aggregate=sum|count) o hasta "
        "50 filas (aggregate=list) con expense_id para citar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vendor": {
                "type": "string",
                "description": "Match case-insensitive sobre el vendor final del recibo.",
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
                    "sum/count devuelven un único total. list devuelve hasta "
                    "`limit` filas."
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
