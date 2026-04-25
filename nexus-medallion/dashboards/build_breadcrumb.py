"""Generate `nexus-breadcrumb.lvdash.json` (Phase E.5).

Standalone Databricks dashboard that joins the AWS X-Ray breadcrumb with
the long-running expense_events timeline by `trace_id`. Two widgets:

  1. Filter — single-select on `trace_id` (catalog of distinct trace_ids
     from the last 24h, ordered most-recent first).
  2. Timeline — every event for the picked trace_id, plus the latency
     between consecutive events (`stage_duration_sec`).

The dashboard reuses the same five-bug-tolerant scaffolding from
`build_trace.py`. Run: `python build_breadcrumb.py` — writes the JSON
next to this file. Import it into Databricks via the UI or the
`databricks dashboards import` CLI.
"""
from __future__ import annotations

import json
from pathlib import Path

CATALOG = "nexus_dev"
OUT_PATH = Path(__file__).parent / "nexus-breadcrumb.lvdash.json"


# ── Queries ──────────────────────────────────────────────────────────

# Distinct trace_ids in the last 24h, with the expense they came from.
SQL_TRACE_OPTIONS = f"""
SELECT
  metadata.trace_id AS trace_id,
  MIN(created_at)   AS first_seen,
  MAX(created_at)   AS last_seen,
  COUNT(*)          AS event_count,
  ANY_VALUE(expense_id) AS expense_id
FROM {CATALOG}.silver.expense_events
WHERE metadata.trace_id IS NOT NULL
  AND created_at >= current_timestamp() - INTERVAL 24 HOURS
GROUP BY metadata.trace_id
ORDER BY last_seen DESC
LIMIT 200
""".strip()

# Timeline for the picked trace_id, with stage latency.
SQL_BREADCRUMB = f"""
WITH events AS (
  SELECT
    expense_id,
    event_type,
    actor.type   AS actor_type,
    actor.id     AS actor_id,
    workflow_id,
    metadata.trace_id AS trace_id,
    created_at,
    LAG(created_at) OVER (
      PARTITION BY expense_id ORDER BY created_at
    ) AS prev_at
  FROM {CATALOG}.silver.expense_events
  WHERE metadata.trace_id = :trace_id
)
SELECT
  created_at,
  event_type,
  actor_type,
  actor_id,
  workflow_id,
  expense_id,
  CASE
    WHEN prev_at IS NULL THEN NULL
    ELSE (UNIX_TIMESTAMP(created_at) - UNIX_TIMESTAMP(prev_at))
  END AS stage_duration_sec
FROM events
ORDER BY created_at ASC
""".strip()


# ── Lakeview JSON helpers (proven to work — see build_trace.py header) ──

def _col(field_name, title, order, type_, display_as, extra=None):
    spec = {
        "type": "column",
        "fieldName": field_name,
        "title": title,
        "order": order,
        "type_": type_,
        "displayAs": display_as,
    }
    if extra:
        spec.update(extra)
    return spec


def ds(name, display_name, sql, parameters=None):
    out = {
        "name": name,
        "displayName": display_name,
        "queryLines": [sql],
    }
    if parameters:
        out["parameters"] = parameters
    return out


def widget_query(dataset_name, field_names):
    return {
        "name": f"main_query_{dataset_name}",
        "query": {
            "datasetName": dataset_name,
            "fields": [{"name": f, "expression": f"`{f}`"} for f in field_names],
            "disaggregated": True,
        },
    }


# ── Dashboard structure ──────────────────────────────────────────────

def build():
    dataset_options = ds(
        "trace_options",
        "Available trace IDs (24h)",
        SQL_TRACE_OPTIONS,
    )
    dataset_breadcrumb = ds(
        "breadcrumb",
        "Breadcrumb timeline (filtered by trace_id)",
        SQL_BREADCRUMB,
        parameters=[{"keyword": "trace_id", "displayName": "trace_id", "dataType": "STRING"}],
    )

    # Filter widget — single-select on trace_id.
    filter_widget = {
        "name": "filter_trace_id",
        "queries": [
            {
                "name": "param_q_trace_options",
                "query": {
                    "datasetName": "trace_options",
                    "fields": [
                        {"name": "trace_id", "expression": "`trace_id`"},
                        {"name": "last_seen", "expression": "`last_seen`"},
                        {"name": "expense_id", "expression": "`expense_id`"},
                    ],
                    "disaggregated": True,
                },
            }
        ],
        "spec": {
            "version": 2,
            "widgetType": "filter-single-select",
            "encodings": {
                "fields": [
                    {"parameterName": "trace_id", "queryName": "param_q_trace_options"},
                    {"fieldName": "trace_id", "queryName": "param_q_trace_options"},
                ]
            },
            "frame": {"showTitle": True, "title": "Pick a trace_id"},
        },
        "position": {"x": 0, "y": 0, "width": 6, "height": 2},
    }

    # Timeline widget — table.
    timeline_widget = {
        "name": "widget_breadcrumb_timeline",
        "queries": [widget_query("breadcrumb", ["created_at", "event_type", "actor_type", "actor_id", "workflow_id", "expense_id", "stage_duration_sec"])],
        "spec": {
            "version": 1,
            "widgetType": "table",
            "encodings": {
                "columns": [
                    _col("created_at", "Timestamp (UTC)", 0, "datetime", "datetime"),
                    _col("event_type", "Event", 1, "string", "string"),
                    _col("actor_type", "Actor type", 2, "string", "string"),
                    _col("actor_id", "Actor", 3, "string", "string"),
                    _col("workflow_id", "Workflow ID", 4, "string", "string"),
                    _col("expense_id", "Expense", 5, "string", "string"),
                    _col("stage_duration_sec", "Δ vs prev (sec)", 6, "decimal", "number"),
                ]
            },
            "frame": {"showTitle": True, "title": "Breadcrumb timeline (event sequence + latency between stages)"},
        },
        "position": {"x": 0, "y": 2, "width": 12, "height": 12},
    }

    # Markdown header — instructions.
    header = {
        "name": "header_md",
        "spec": {
            "version": 1,
            "widgetType": "markdown",
            "encodings": {},
            "frame": {"showTitle": False},
            "markdown": (
                "# Nexus Breadcrumb (audit timeline)\n\n"
                "Pick a `trace_id` from the dropdown to see every step the "
                "request took across backend, worker, OCR, RAG and HITL. "
                "The `Δ vs prev (sec)` column shows how long each stage took. "
                "For the live (sub-minute) view, open CloudWatch ServiceLens."
            ),
        },
        "position": {"x": 6, "y": 0, "width": 6, "height": 2},
    }

    pages = [
        {
            "name": "page_breadcrumb",
            "displayName": "Breadcrumb",
            "layout": [
                {"widget": header},
                {"widget": filter_widget},
                {"widget": timeline_widget},
            ],
        }
    ]

    dashboard = {
        "datasets": [dataset_options, dataset_breadcrumb],
        "pages": pages,
    }
    return dashboard


def main():
    OUT_PATH.write_text(json.dumps(build(), indent=2))
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
