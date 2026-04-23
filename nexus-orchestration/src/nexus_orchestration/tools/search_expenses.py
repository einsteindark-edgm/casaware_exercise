"""Tool schema exposed to the LLM in the RAG workflow.

The shape matches the Anthropic / Bedrock Converse tool spec.
"""
from __future__ import annotations

SEARCH_EXPENSES_TOOL: dict = {
    "name": "search_expenses",
    "description": (
        "Busca en los gastos del usuario por similitud semántica. "
        "Usa esta herramienta para responder preguntas sobre gastos pasados."
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
