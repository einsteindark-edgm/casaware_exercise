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
        "Úsala para preguntas difusas o temáticas — p. ej. 'recibos sobre "
        "viajes', 'gastos relacionados con comida saludable'. No sirve para "
        "totales exactos ni filtros por monto/fecha: para eso usa "
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
                "description": "Número de resultados",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

# Backwards-compat alias — remove once all imports migrate.
SEARCH_EXPENSES_TOOL = SEARCH_EXPENSES_SEMANTIC_TOOL
