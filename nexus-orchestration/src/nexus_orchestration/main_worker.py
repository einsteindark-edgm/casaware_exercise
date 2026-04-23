"""Worker entry point.

Reads TASK_QUEUE from settings. Supported values:

  nexus-orchestrator-tq  — hosts ExpenseAuditWorkflow + its activities
  nexus-ocr-tq           — hosts OCRExtractionWorkflow + Textract activities
  nexus-databricks-tq    — hosts AuditValidationWorkflow + auditor activities
  nexus-rag-tq           — hosts RAGQueryWorkflow + LLM + vector search
  all                    — registers EVERY workflow and activity on a single
                           worker (useful in dev)

Each process is a Temporal Worker; scale out by running multiple processes /
containers per task queue.
"""
from __future__ import annotations

import asyncio

from temporalio.client import Client, TLSConfig
from temporalio.worker import Worker

from nexus_orchestration.activities.comparison import compare_fields
from nexus_orchestration.activities.llm import bedrock_converse, load_rag_system_prompt
from nexus_orchestration.activities.mongodb_writes import (
    create_hitl_task,
    emit_expense_event,
    load_chat_history,
    save_chat_turn,
    update_expense_status,
    update_expense_to_approved,
    update_expense_to_rejected,
    upsert_ocr_extraction,
)
from nexus_orchestration.activities.query_expense import query_expense_for_validation
from nexus_orchestration.activities.redis_events import publish_event
from nexus_orchestration.activities.textract import (
    textract_analyze_document_queries,
    textract_analyze_expense,
)
from nexus_orchestration.activities.vector_search import vector_similarity_search
from nexus_orchestration.config import settings
from nexus_orchestration.observability.logging import configure_logging, get_logger
from nexus_orchestration.workflows.audit_validation import AuditValidationWorkflow
from nexus_orchestration.workflows.expense_audit import ExpenseAuditWorkflow
from nexus_orchestration.workflows.ocr_extraction import OCRExtractionWorkflow
from nexus_orchestration.workflows.rag_query import RAGQueryWorkflow

log = get_logger(__name__)


# ── Registration groups ─────────────────────────────────────────────────────

# Activities used by ANY workflow — always registered on the worker that owns
# the parent workflow so it can invoke them directly if needed.
_COMMON_ACTIVITIES = [
    publish_event,
    emit_expense_event,
    update_expense_status,
    update_expense_to_approved,
    update_expense_to_rejected,
    create_hitl_task,
]

_OCR_ACTIVITIES = [
    textract_analyze_expense,
    textract_analyze_document_queries,
    upsert_ocr_extraction,
    emit_expense_event,
    publish_event,
]

_AUDIT_ACTIVITIES = [
    query_expense_for_validation,
    compare_fields,
    create_hitl_task,
    emit_expense_event,
    publish_event,
]

_RAG_ACTIVITIES = [
    load_rag_system_prompt,
    bedrock_converse,
    vector_similarity_search,
    save_chat_turn,
    load_chat_history,
    publish_event,
]


def _registration_for(task_queue: str) -> tuple[list, list]:
    """Returns (workflows, activities) to register for the given task queue."""
    if task_queue == "all":
        return (
            [
                ExpenseAuditWorkflow,
                OCRExtractionWorkflow,
                AuditValidationWorkflow,
                RAGQueryWorkflow,
            ],
            _dedupe(
                _COMMON_ACTIVITIES
                + _OCR_ACTIVITIES
                + _AUDIT_ACTIVITIES
                + _RAG_ACTIVITIES
            ),
        )
    if task_queue == "nexus-orchestrator-tq":
        return ([ExpenseAuditWorkflow], _dedupe(_COMMON_ACTIVITIES))
    if task_queue == "nexus-ocr-tq":
        return ([OCRExtractionWorkflow], _dedupe(_OCR_ACTIVITIES))
    if task_queue == "nexus-databricks-tq":
        return ([AuditValidationWorkflow], _dedupe(_AUDIT_ACTIVITIES))
    if task_queue == "nexus-rag-tq":
        return ([RAGQueryWorkflow], _dedupe(_RAG_ACTIVITIES))
    raise ValueError(f"unknown task queue: {task_queue}")


def _dedupe(items: list) -> list:
    seen = set()
    out = []
    for item in items:
        key = id(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _tls_config() -> TLSConfig | None:
    cert = settings.temporal_tls_cert_path
    key = settings.temporal_tls_key_path
    if not (cert and key):
        return None
    with open(cert, "rb") as f:
        cert_bytes = f.read()
    with open(key, "rb") as f:
        key_bytes = f.read()
    return TLSConfig(client_cert=cert_bytes, client_private_key=key_bytes)


async def run_worker(task_queue: str) -> None:
    configure_logging(settings.log_level)
    workflows, activities = _registration_for(task_queue)

    log.info(
        "worker.starting",
        task_queue=task_queue,
        temporal_host=settings.temporal_host,
        namespace=settings.temporal_namespace,
        workflows=[getattr(w, "__name__", str(w)) for w in workflows],
        activities_count=len(activities),
        fake_providers=settings.fake_providers,
    )

    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
        tls=_tls_config(),
    )

    # When TASK_QUEUE=all we need one Worker per queue so child workflows find
    # the right hosts.
    if task_queue == "all":
        queues = [
            "nexus-orchestrator-tq",
            "nexus-ocr-tq",
            "nexus-databricks-tq",
            "nexus-rag-tq",
        ]
        workers = [
            Worker(
                client,
                task_queue=q,
                workflows=workflows,
                activities=activities,
                max_concurrent_activities=settings.worker_max_concurrent_activities,
                max_concurrent_workflow_tasks=settings.worker_max_concurrent_workflow_tasks,
            )
            for q in queues
        ]
        log.info("worker.ready", task_queues=queues)
        await asyncio.gather(*(w.run() for w in workers))
        return

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=activities,
        max_concurrent_activities=settings.worker_max_concurrent_activities,
        max_concurrent_workflow_tasks=settings.worker_max_concurrent_workflow_tasks,
    )
    log.info("worker.ready", task_queue=task_queue)
    await worker.run()


def main() -> None:
    task_queue = settings.task_queue
    asyncio.run(run_worker(task_queue))


if __name__ == "__main__":
    main()
