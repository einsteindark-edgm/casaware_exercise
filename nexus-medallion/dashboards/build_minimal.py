"""Regenerate nexus-medallion-trace.lvdash.json — full medallion traceability.

WHAT THIS DASHBOARD DOES
========================
Pick an `expense_id` in the top filter; every widget below shows what
happened to that expense in each medallion layer:

  - bronze.mongodb_cdc_expenses           (every CDC change event)
  - bronze.mongodb_cdc_receipts           (S3 receipt metadata)
  - bronze.mongodb_cdc_expense_events     (workflow event stream)
  - bronze.mongodb_cdc_ocr_extractions    (OCR raw)
  - bronze.mongodb_cdc_hitl_tasks         (HITL raw)
  - silver.expenses                       (current state)
  - silver.expense_events                 (timeline)
  - silver.ocr_extractions                (OCR clean)
  - silver.hitl_events                    (HITL clean)
  - gold.expense_audit                    (final audit row)

WORKING CASCADING-FILTER PATTERN (verified against the official
Databricks LakeFlow System Tables Dashboard sample)
=============================================================
1. PARAMETERIZED DATASET
     - declare `parameters: [{keyword, displayName, dataType,
       defaultSelection: {values: {dataType, values: [{value: "__none__"}]}}}]`
     - the SQL uses `:keyword` directly
     - WHERE clause uses the show-all-or-filter pattern:
         WHERE :p_expense_id = '__none__' OR expense_id = :p_expense_id
       so the table renders something on initial load (when no id
       is selected yet) instead of staying empty.

2. FILTER WIDGET (`filter-single-select`)
     - one `param_q_<dataset>` query per parameterized dataset, body:
         {datasetName, parameters: [{name, keyword}], disaggregated: false}
     - one `options_q_<options_dataset>` query, body:
         {datasetName, fields: [<column>, <column>_associativity], disaggregated: false}
     - encodings.fields = N parameterName entries (one per param_q query)
       + 1 fieldName entry (referencing the options query)
     - spec.selection.defaultSelection.values.values = [{"value": "__none__"}]
       (NOT empty []. The empty form makes Lakeview never seed the
       parameter, and the propagation pipeline never fires.)

3. DATA WIDGET (table / bar)
     - main query: {datasetName, fields, disaggregated}
     - DO NOT add a `parameters` block to this query — propagation
       comes from the dataset declaration + the filter's param_q
       query, not from the data widget itself. Adding parameters
       here breaks the trigger.

4. SQL WAREHOUSE MUST BE RUNNING. A stopped warehouse silently fails
   every query — widgets show empty, no error visible. This is the
   single most common cause of "data not loading" on freshly imported
   dashboards.

5. Every data widget's query name MUST be exactly `main_query`. The
   LakeFlow sample uses this for every widget — uniqueness comes from
   the widget's own `name` field, not from the query name. Suffixing
   to `main_query_<widget>` silently breaks parameter propagation:
   the dataset's parameter value never reaches the widget. (This was
   the original 2-widget cascade working, then 13 widgets failing —
   the only diff was the query-name suffix.)
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "nexus-medallion-trace.lvdash.json"

PARAM_NAME = "p_expense_id"
SENTINEL = "__none__"

PARAM_DECL = [
    {
        "displayName": "expense_id",
        "keyword": PARAM_NAME,
        "dataType": "STRING",
        "defaultSelection": {
            "values": {
                "dataType": "STRING",
                "values": [{"value": SENTINEL}],
            }
        },
    }
]


# ── Column helpers ──────────────────────────────────────────────────────
_BASE_COL = {
    "booleanValues": ["false", "true"],
    "imageUrlTemplate":   "{{ @ }}",
    "imageTitleTemplate": "{{ @ }}",
    "imageWidth":  "",
    "imageHeight": "",
    "linkUrlTemplate":   "{{ @ }}",
    "linkTextTemplate":  "{{ @ }}",
    "linkTitleTemplate": "{{ @ }}",
    "linkOpenInNewTab": True,
    "visible": True,
    "allowSearch":         False,
    "alignContent":        "left",
    "allowHTML":           True,
    "highlightLinks":      False,
    "useMonospaceFont":    False,
    "preserveWhitespace":  False,
}


def _col(field, title, order, type_, displayAs, extra=None):
    out = dict(_BASE_COL)
    out.update({
        "fieldName": field,
        "type": type_,
        "displayAs": displayAs,
        "order": order,
        "title": title,
        "displayName": title,
    })
    if extra:
        out.update(extra)
    return out


def col_text(f, t, o):  return _col(f, t, o, "string", "string")
def col_num(f, t, o):   return _col(f, t, o, "decimal", "number", {"numberFormat": "0,0.00"})
def col_int(f, t, o):   return _col(f, t, o, "decimal", "number", {"numberFormat": "0"})
def col_dt(f, t, o):    return _col(f, t, o, "datetime", "datetime", {"dateTimeFormat": "ll LTS"})


# ── Datasets ────────────────────────────────────────────────────────────
def _filter_clause():
    return f"WHERE :{PARAM_NAME} = '{SENTINEL}' OR expense_id = :{PARAM_NAME}"


PARAMETERIZED = [
    # name, displayName, sql
    ("d_trace", "Per-layer presence", f"""
SELECT 'bronze.expenses' AS layer, COUNT(*) AS row_count, 1 AS sort_order
FROM   nexus_dev.bronze.mongodb_cdc_expenses
{_filter_clause()}
UNION ALL
SELECT 'bronze.receipts', COUNT(*), 2
FROM   nexus_dev.bronze.mongodb_cdc_receipts
{_filter_clause()}
UNION ALL
SELECT 'bronze.events', COUNT(*), 3
FROM   nexus_dev.bronze.mongodb_cdc_expense_events
{_filter_clause()}
UNION ALL
SELECT 'bronze.ocr', COUNT(*), 4
FROM   nexus_dev.bronze.mongodb_cdc_ocr_extractions
{_filter_clause()}
UNION ALL
SELECT 'bronze.hitl', COUNT(*), 5
FROM   nexus_dev.bronze.mongodb_cdc_hitl_tasks
{_filter_clause()}
UNION ALL
SELECT 'silver.expenses', COUNT(*), 6
FROM   nexus_dev.silver.expenses
{_filter_clause()}
UNION ALL
SELECT 'silver.events', COUNT(*), 7
FROM   nexus_dev.silver.expense_events
{_filter_clause()}
UNION ALL
SELECT 'silver.ocr', COUNT(*), 8
FROM   nexus_dev.silver.ocr_extractions
{_filter_clause()}
UNION ALL
SELECT 'silver.hitl', COUNT(*), 9
FROM   nexus_dev.silver.hitl_events
{_filter_clause()}
UNION ALL
SELECT 'gold.expense_audit', COUNT(*), 10
FROM   nexus_dev.gold.expense_audit
{_filter_clause()}
ORDER BY sort_order
"""),
    ("d_silver_expense", "Silver — current state", f"""
SELECT expense_id, tenant_id, user_id, receipt_id, workflow_id,
       status, amount, currency, vendor, date, category,
       final_amount, final_currency, final_vendor, final_date,
       approved_at, created_at, updated_at
FROM   nexus_dev.silver.expenses
{_filter_clause()}
"""),
    ("d_silver_timeline", "Silver — events timeline", f"""
SELECT created_at, event_type, actor, workflow_id, event_id
FROM   nexus_dev.silver.expense_events
{_filter_clause()}
ORDER BY created_at
"""),
    ("d_silver_ocr", "Silver — OCR", f"""
SELECT expense_id, tenant_id, user_id,
       ocr_total, ocr_total_confidence,
       ocr_vendor, ocr_vendor_confidence,
       ocr_date, ocr_date_confidence,
       ocr_currency, avg_confidence, textract_raw_s3_key, extracted_at
FROM   nexus_dev.silver.ocr_extractions
{_filter_clause()}
"""),
    ("d_silver_hitl", "Silver — HITL events", f"""
SELECT task_id, status, decision, resolved_by, resolved_at,
       created_at, workflow_id, discrepancy_fields, resolved_fields
FROM   nexus_dev.silver.hitl_events
{_filter_clause()}
ORDER BY created_at DESC
"""),
    ("d_gold_audit", "Gold — expense_audit", f"""
SELECT tenant_id, expense_id, user_id, receipt_id,
       final_amount, final_currency, final_vendor, final_date,
       category, approved_at, had_hitl, hitl_decision,
       hitl_resolved_by, hitl_resolved_at
FROM   nexus_dev.gold.expense_audit
{_filter_clause()}
"""),
    ("d_bronze_cdc_expense", "Bronze CDC — expenses (every change event)", f"""
SELECT __source_ts_ms, __op, __deleted, status, amount, currency, vendor,
       date, final_amount, final_vendor, final_date,
       created_at, updated_at, _cdc_ingestion_ts
FROM   nexus_dev.bronze.mongodb_cdc_expenses
{_filter_clause()}
ORDER BY __source_ts_ms
"""),
    ("d_bronze_receipt", "Bronze CDC — receipt", f"""
SELECT __source_ts_ms, __op, receipt_id, s3_key, content_type, size_bytes, uploaded_at
FROM   nexus_dev.bronze.mongodb_cdc_receipts
{_filter_clause()}
ORDER BY __source_ts_ms
"""),
    ("d_bronze_events", "Bronze CDC — workflow events", f"""
SELECT __source_ts_ms, __op, event_type, actor, workflow_id, event_id, created_at
FROM   nexus_dev.bronze.mongodb_cdc_expense_events
{_filter_clause()}
ORDER BY __source_ts_ms
"""),
    ("d_bronze_ocr", "Bronze CDC — OCR raw", f"""
SELECT __source_ts_ms, __op, ocr_total, ocr_vendor, ocr_date,
       ocr_currency, avg_confidence, textract_raw_s3_key, extracted_at
FROM   nexus_dev.bronze.mongodb_cdc_ocr_extractions
{_filter_clause()}
ORDER BY __source_ts_ms
"""),
    ("d_bronze_hitl", "Bronze CDC — HITL raw", f"""
SELECT __source_ts_ms, __op, task_id, status, decision,
       resolved_by, resolved_at, created_at, workflow_id
FROM   nexus_dev.bronze.mongodb_cdc_hitl_tasks
{_filter_clause()}
ORDER BY __source_ts_ms
"""),
]

# Always-on reference tables (no parameter)
NON_PARAMETERIZED = [
    ("d_all_ids", "All expense_ids (dropdown source)", """
SELECT expense_id, MAX(created_at) AS last_seen
FROM   nexus_dev.silver.expenses
GROUP BY expense_id
ORDER BY last_seen DESC
LIMIT 500
"""),
    ("d_recent", "Latest 20 expenses", """
SELECT expense_id, tenant_id, status, amount, currency, vendor, created_at
FROM   nexus_dev.silver.expenses
ORDER BY created_at DESC
LIMIT 20
"""),
]


def ds(name, display_name, sql, parameterized):
    d = {
        "name": name,
        "displayName": display_name,
        "queryLines": [line + "\n" for line in sql.strip().splitlines()],
    }
    if parameterized:
        d["parameters"] = PARAM_DECL
    return d


datasets = [ds(n, dn, sql, True) for (n, dn, sql) in PARAMETERIZED]
datasets += [ds(n, dn, sql, False) for (n, dn, sql) in NON_PARAMETERIZED]


# ── Filter widget ───────────────────────────────────────────────────────
filter_param_queries = [
    {
        "name": f"param_q_{name}",
        "query": {
            "datasetName": name,
            "parameters": [{"name": PARAM_NAME, "keyword": PARAM_NAME}],
            "disaggregated": False,
        },
    }
    for (name, _, _) in PARAMETERIZED
]
filter_options_query = {
    "name": "options_q_d_all_ids",
    "query": {
        "datasetName": "d_all_ids",
        "fields": [
            {"name": "expense_id", "expression": "`expense_id`"},
            {"name": "expense_id_associativity",
             "expression": "COUNT_IF(`associative_filter_predicate_group`)"},
        ],
        "disaggregated": False,
    },
}

filter_encodings = [
    {"parameterName": PARAM_NAME, "queryName": q["name"]}
    for q in filter_param_queries
] + [
    {"fieldName": "expense_id",
     "displayName": "expense_id",
     "queryName": filter_options_query["name"]},
]

filter_widget = {
    "widget": {
        "name": "f_expense_id",
        "queries": filter_param_queries + [filter_options_query],
        "spec": {
            "version": 2,
            "widgetType": "filter-single-select",
            "encodings": {"fields": filter_encodings},
            "selection": {
                "defaultSelection": {
                    "values": {
                        "dataType": "STRING",
                        "values": [{"value": SENTINEL}],
                    }
                }
            },
            "frame": {"title": "Select an expense_id", "showTitle": True},
        },
    },
    "position": {"x": 0, "y": 0, "width": 6, "height": 2},
}


# ── Widget builders ─────────────────────────────────────────────────────
def table_widget(name, title, dataset_name, columns, x, y, w, h):
    return {
        "widget": {
            "name": name,
            "queries": [
                {
                    "name": "main_query",
                    "query": {
                        "datasetName": dataset_name,
                        "fields": [
                            {"name": c["fieldName"],
                             "expression": f"`{c['fieldName']}`"}
                            for c in columns
                        ],
                        "disaggregated": True,
                    },
                }
            ],
            "spec": {
                "version": 1,
                "widgetType": "table",
                "allowHTMLByDefault": False,
                "condensed": True,
                "invisibleColumns": [],
                "itemsPerPage": 25,
                "paginationSize": "default",
                "withRowNumber": False,
                "encodings": {"columns": columns},
                "frame": {"title": title, "showTitle": True},
            },
        },
        "position": {"x": x, "y": y, "width": w, "height": h},
    }


def bar_widget(name, title, dataset_name, x_field, y_field, x, y, w, h,
               x_scale="categorical", x_sort=None):
    x_enc = {"fieldName": x_field, "scale": {"type": x_scale},
             "displayName": x_field}
    if x_sort:
        x_enc["scale"]["sort"] = {"by": "manual", "values": x_sort}
    return {
        "widget": {
            "name": name,
            "queries": [
                {
                    "name": "main_query",
                    "query": {
                        "datasetName": dataset_name,
                        "fields": [
                            {"name": x_field,
                             "expression": f"`{x_field}`"},
                            {"name": y_field,
                             "expression": f"`{y_field}`"},
                        ],
                        "disaggregated": True,
                    },
                }
            ],
            "spec": {
                "version": 3,
                "widgetType": "bar",
                "encodings": {
                    "x": x_enc,
                    "y": {"fieldName": y_field,
                          "scale": {"type": "quantitative"},
                          "displayName": y_field},
                },
                "frame": {"title": title, "showTitle": True},
            },
        },
        "position": {"x": x, "y": y, "width": w, "height": h},
    }


# ── Layout (6-column grid) ──────────────────────────────────────────────
widgets = [filter_widget]

# Row 1 — recent table (helps user pick) + presence bar
widgets.append(
    table_widget(
        "w_recent",
        "Latest expenses (use these IDs in the filter above)",
        "d_recent",
        [
            col_text("expense_id", "expense_id", 0),
            col_text("tenant_id", "tenant", 1),
            col_text("status", "status", 2),
            col_num("amount", "amount", 3),
            col_text("currency", "ccy", 4),
            col_text("vendor", "vendor", 5),
            col_dt("created_at", "created_at", 6),
        ],
        x=0, y=2, w=6, h=6,
    )
)

widgets.append(
    bar_widget(
        "w_presence",
        "Presence of selected expense in each layer/table",
        "d_trace",
        x_field="layer",
        y_field="row_count",
        x=0, y=8, w=6, h=6,
        x_sort=[
            "bronze.expenses", "bronze.receipts", "bronze.events",
            "bronze.ocr", "bronze.hitl",
            "silver.expenses", "silver.events", "silver.ocr", "silver.hitl",
            "gold.expense_audit",
        ],
    )
)

# Row 2 — silver expense + gold audit (final state vs auditable record)
widgets.append(
    table_widget(
        "w_silver_expense", "Silver — current state", "d_silver_expense",
        [
            col_text("expense_id", "expense_id", 0),
            col_text("status", "status", 1),
            col_num ("amount", "user amount", 2),
            col_text("vendor", "user vendor", 3),
            col_dt  ("date", "user date", 4),
            col_num ("final_amount", "final amount", 5),
            col_text("final_currency", "final ccy", 6),
            col_text("final_vendor", "final vendor", 7),
            col_dt  ("final_date", "final date", 8),
            col_text("category", "category", 9),
            col_dt  ("approved_at", "approved_at", 10),
            col_dt  ("created_at", "created_at", 11),
            col_dt  ("updated_at", "updated_at", 12),
            col_text("workflow_id", "workflow_id", 13),
        ],
        x=0, y=14, w=3, h=8,
    )
)

widgets.append(
    table_widget(
        "w_gold", "Gold — expense_audit (final auditable record)", "d_gold_audit",
        [
            col_text("expense_id", "expense_id", 0),
            col_text("tenant_id", "tenant", 1),
            col_num ("final_amount", "amount", 2),
            col_text("final_currency", "ccy", 3),
            col_text("final_vendor", "vendor", 4),
            col_dt  ("final_date", "date", 5),
            col_text("category", "category", 6),
            col_text("had_hitl", "had_hitl", 7),
            col_text("hitl_decision", "hitl_decision", 8),
            col_text("hitl_resolved_by", "hitl_by", 9),
            col_dt  ("hitl_resolved_at", "hitl_at", 10),
            col_dt  ("approved_at", "approved_at", 11),
        ],
        x=3, y=14, w=3, h=8,
    )
)

# Row 3 — silver timeline (full width)
widgets.append(
    table_widget(
        "w_silver_timeline", "Silver — events timeline (chronological)",
        "d_silver_timeline",
        [
            col_dt  ("created_at", "t", 0),
            col_text("event_type", "event_type", 1),
            col_text("actor", "actor", 2),
            col_text("workflow_id", "workflow_id", 3),
            col_text("event_id", "event_id", 4),
        ],
        x=0, y=22, w=6, h=6,
    )
)

# Row 4 — silver OCR + silver HITL
widgets.append(
    table_widget(
        "w_silver_ocr", "Silver — OCR extraction", "d_silver_ocr",
        [
            col_num("ocr_total", "total", 0),
            col_num("ocr_total_confidence", "total conf", 1),
            col_text("ocr_vendor", "vendor", 2),
            col_num("ocr_vendor_confidence", "vendor conf", 3),
            col_dt ("ocr_date", "date", 4),
            col_num("ocr_date_confidence", "date conf", 5),
            col_num("avg_confidence", "avg conf", 6),
            col_text("textract_raw_s3_key", "textract s3_key", 7),
            col_dt ("extracted_at", "extracted_at", 8),
        ],
        x=0, y=28, w=3, h=6,
    )
)

widgets.append(
    table_widget(
        "w_silver_hitl", "Silver — HITL events", "d_silver_hitl",
        [
            col_text("task_id", "task_id", 0),
            col_text("status", "status", 1),
            col_text("decision", "decision", 2),
            col_text("resolved_by", "resolved_by", 3),
            col_dt  ("resolved_at", "resolved_at", 4),
            col_dt  ("created_at", "created_at", 5),
            col_text("workflow_id", "workflow_id", 6),
            col_text("discrepancy_fields", "discrepancy_fields", 7),
            col_text("resolved_fields", "resolved_fields", 8),
        ],
        x=3, y=28, w=3, h=6,
    )
)

# Row 5 — bronze CDC expense (every change event = full audit trail)
widgets.append(
    table_widget(
        "w_bronze_cdc_expense",
        "Bronze CDC — every change event for this expense",
        "d_bronze_cdc_expense",
        [
            col_int ("__source_ts_ms", "src_ts_ms", 0),
            col_text("__op", "op", 1),
            col_text("__deleted", "deleted", 2),
            col_text("status", "status", 3),
            col_num ("amount", "amount", 4),
            col_text("currency", "ccy", 5),
            col_text("vendor", "vendor", 6),
            col_num ("final_amount", "final_amount", 7),
            col_text("final_vendor", "final_vendor", 8),
            col_dt  ("created_at", "created_at", 9),
            col_dt  ("updated_at", "updated_at", 10),
            col_dt  ("_cdc_ingestion_ts", "ingest_ts", 11),
        ],
        x=0, y=34, w=6, h=8,
    )
)

# Row 6 — bronze receipt + bronze events
widgets.append(
    table_widget(
        "w_bronze_receipt", "Bronze CDC — S3 receipt", "d_bronze_receipt",
        [
            col_int ("__source_ts_ms", "src_ts_ms", 0),
            col_text("__op", "op", 1),
            col_text("receipt_id", "receipt_id", 2),
            col_text("s3_key", "s3_key", 3),
            col_text("content_type", "mime", 4),
            col_int ("size_bytes", "bytes", 5),
            col_dt  ("uploaded_at", "uploaded_at", 6),
        ],
        x=0, y=42, w=3, h=6,
    )
)

widgets.append(
    table_widget(
        "w_bronze_events", "Bronze CDC — workflow events", "d_bronze_events",
        [
            col_int ("__source_ts_ms", "src_ts_ms", 0),
            col_text("__op", "op", 1),
            col_text("event_type", "event_type", 2),
            col_text("actor", "actor", 3),
            col_text("workflow_id", "workflow_id", 4),
            col_text("event_id", "event_id", 5),
            col_dt  ("created_at", "created_at", 6),
        ],
        x=3, y=42, w=3, h=6,
    )
)

# Row 7 — bronze OCR + bronze HITL
widgets.append(
    table_widget(
        "w_bronze_ocr", "Bronze CDC — OCR raw", "d_bronze_ocr",
        [
            col_int ("__source_ts_ms", "src_ts_ms", 0),
            col_text("__op", "op", 1),
            col_num ("ocr_total", "total", 2),
            col_text("ocr_vendor", "vendor", 3),
            col_dt  ("ocr_date", "date", 4),
            col_text("ocr_currency", "ccy", 5),
            col_num ("avg_confidence", "avg_conf", 6),
            col_text("textract_raw_s3_key", "s3_key", 7),
            col_dt  ("extracted_at", "extracted_at", 8),
        ],
        x=0, y=48, w=3, h=6,
    )
)

widgets.append(
    table_widget(
        "w_bronze_hitl", "Bronze CDC — HITL raw", "d_bronze_hitl",
        [
            col_int ("__source_ts_ms", "src_ts_ms", 0),
            col_text("__op", "op", 1),
            col_text("task_id", "task_id", 2),
            col_text("status", "status", 3),
            col_text("decision", "decision", 4),
            col_text("resolved_by", "resolved_by", 5),
            col_dt  ("resolved_at", "resolved_at", 6),
            col_dt  ("created_at", "created_at", 7),
            col_text("workflow_id", "workflow_id", 8),
        ],
        x=3, y=48, w=3, h=6,
    )
)


dashboard = {
    "datasets": datasets,
    "pages": [
        {
            "name": "main",
            "displayName": "Medallion trace",
            "layout": widgets,
            "pageType": "PAGE_TYPE_CANVAS",
        }
    ],
    "uiSettings": {"theme": {"widgetHeaderAlignment": "ALIGNMENT_UNSPECIFIED"}},
}

OUT.write_text(json.dumps(dashboard, indent=2))
print(f"wrote {OUT} ({len(widgets)} widgets, {len(datasets)} datasets)")
