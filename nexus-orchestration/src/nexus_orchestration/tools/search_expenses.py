"""Tool schema for the SEMANTIC leg of the hybrid RAG.

Exposed to the LLM as `search_expenses_semantic`. Databricks Vector Search on
`gold.expense_chunks_index`. Shape matches Anthropic / Bedrock Converse tool
spec.
"""
from __future__ import annotations

SEARCH_EXPENSES_SEMANTIC_TOOL: dict = {
    "name": "search_expenses_semantic",
    "description": (
        "Búsqueda semántica (vector) sobre los recibos aprobados del usuario. "
        "Úsala por DEFAULT para cualquier pregunta que NO contenga "
        "explícitamente un vendor con nombre, una fecha, un monto, una "
        "moneda, una categoría exacta, ni pida totales/conteos. Ideal para "
        "preguntas temáticas o exploratorias — 'recibos sobre transporte', "
        "'gastos relacionados con un cliente', 'qué gastos extraños tengo', "
        "'cosas relacionadas con marketing'. No sirve para totales exactos "
        "ni filtros por monto/fecha: para eso usa "
        "`search_expenses_structured`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Consulta en lenguaje natural",
            },
            "k": {
                "type": "integer",
                "description": (
                    "Número de candidatos a recuperar antes de filtrar por "
                    "score mínimo. 10 es un buen default; sube a 20 si la "
                    "primera búsqueda devolvió pocas filas."
                ),
                "default": 10,
            },
        },
        "required": ["query"],
    },
}

# Backwards-compat alias — remove once all imports migrate.
SEARCH_EXPENSES_TOOL = SEARCH_EXPENSES_SEMANTIC_TOOL
