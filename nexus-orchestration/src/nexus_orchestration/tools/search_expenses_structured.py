"""Tool schema for the STRUCTURED (SQL) leg of the hybrid RAG.

Exposed to the LLM as `search_expenses_structured`. Databricks SQL over
`gold.expense_audit`. Shape matches Anthropic / Bedrock Converse tool spec.
"""
from __future__ import annotations

SEARCH_EXPENSES_STRUCTURED_TOOL: dict = {
    "name": "search_expenses_structured",
    "description": (
        "Búsqueda SQL exacta. Úsala SOLO cuando la pregunta del usuario "
        "menciona explícitamente al menos uno de: un vendor con nombre "
        "concreto ('Uber'), un valor o rango monetario ('más de $100'), "
        "una fecha o rango ('marzo', 'entre el 1 y 15'), una moneda "
        "('dólares', 'pesos'), una categoría exacta ('comida', 'viaje'), "
        "o pide un total/suma/conteo ('cuánto gasté'). Para todo lo demás "
        "—temas difusos, intenciones, exploración— usa "
        "`search_expenses_semantic`. Si dudas, prefiere semantic. Devuelve "
        "el agregado (aggregate=sum|count) o hasta 50 filas (aggregate=list) "
        "con expense_id para citar."
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
                    "Usa 'list' (default) para preguntas tipo '¿tengo gastos "
                    "de X?' o 'muéstrame mis gastos'. Usa 'sum' SOLO cuando "
                    "el usuario pide un total monetario explícito ('cuánto "
                    "gasté'). Usa 'count' SOLO cuando pide un número de "
                    "recibos explícito ('cuántos recibos'). Tanto sum como "
                    "count devuelven también hasta 10 filas en `rows` con "
                    "expense_id reales para citar."
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
