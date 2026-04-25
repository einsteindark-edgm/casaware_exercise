"""Regenerate nexus-expense-trace.lvdash.json.

Five bugs to avoid (each cost an iteration to learn):

1. Data widgets must NOT repeat the `parameters` binding in their query.
   Only the *dataset* declaration and the *filter widget's* queries carry
   the binding; the engine propagates the parameter value automatically
   to any query whose datasetName is a parameterized dataset. Repeating
   it on the data widget's query silently breaks the trigger — the
   dependent widgets never re-query when the user picks a new value.

2. Table-column `type` must be one of the values Lakeview actually
   accepts (`string`, `decimal`, `datetime`). Unsupported values like
   `integer`, `float`, `date` cause the column spec to fail validation
   silently — the widget renders, but with no rows. This manifests as
   "everything looks fine but tables are empty even for IDs that exist".
   Known-good pairings:
       type=string   displayAs=string    (text)
       type=decimal  displayAs=number    (int/float)
       type=datetime displayAs=datetime  (dates & timestamps)

3. The filter-single-select widget's `encodings.fields` must contain
   exactly ONE entry with `parameterName` and ONE with `fieldName` —
   not one parameterName per dataset. Multiple parameterName entries
   for the same parameter confuse Lakeview and the click on the
   dropdown won't propagate. The 8 `param_q_*` queries still live in
   the widget's `queries` array (the parameter system re-runs them on
   value change), but only one is referenced in encodings.

4. Each widget's main query MUST have a unique `name` across the
   dashboard. Using "main_query" for all 9 data widgets generates
   duplicate HTML form ids (browser DevTools warns: "Duplicate form
   field id in the same form, 10 resources"). The form HTML loses
   tracking of the filter widget's selection — clicks on the dropdown
   register internally but get cleared on the next render because the
   filter sees collateral state from a colliding widget. Use
   `main_query_<widget_name>` (or any unique string).

5. The filter widget's `defaultSelection.values.values` MUST be an
   empty array `[]`, not `[{"value": ""}]`. The non-empty array with
   an empty-string element makes Lakeview think "the user has already
   picked the empty string", so it re-applies that default on each
   render and the user's actual click bounces off. Apply the same to
   each parameterized dataset's `parameters[0].defaultSelection`.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "nexus-expense-trace.lvdash.json"

PARAM_NAME = "p_expense_id"
# Bug 5: defaultSelection.values.values must be `[]`, not `[{"value": ""}]`.
PARAM_DECL = [
    {
        "displayName": "expense_id",
        "keyword": PARAM_NAME,
        "dataType": "STRING",
        "defaultSelection": {
            "values": {
                "dataType": "STRING",
                "values": [],
            }
        },
    }
]


# ── Column helpers — FULL spec the Lakeview table renderer expects.
# Third bug learned: the minimal-looking column spec (fieldName + type +
# displayAs + order + visible + title + displayName) renders the table
# frame but NO ROWS. The renderer silently ignores rows unless the
# column dict contains the full set of formatting / search / link /
# image defaults. Verified against the published LakeFlow sample.
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


def _col(field_name, title, order, type_, displayAs, extra=None):
    out = dict(_BASE_COL)
    out.update({
        "fieldName": field_name,
        "type": type_,
        "displayAs": displayAs,
        "order": order,
        "title": title,
        "displayName": title,
    })
    if extra:
        out.update(extra)
    return out


def col_text(field_name, title, order):
    return _col(field_name, title, order, "string", "string")


def col_number(field_name, title, order):
    # The sample stores numbers as type=decimal + displayAs=number and
    # provides an explicit `numberFormat` to avoid default scientific
    # notation on small numbers.
    return _col(field_name, title, order, "decimal", "number",
                extra={"numberFormat": "0,0.00"})


def col_datetime(field_name, title, order):
    return _col(field_name, title, order, "datetime", "datetime",
                extra={"dateTimeFormat": "ll LTS"})


# ── Dataset + widget helpers
def ds(name, display_name, sql, parameterized=True):
    d = {
        "name": name,
        "displayName": display_name,
        "queryLines": [line + "\n" for line in sql.strip().splitlines()],
    }
    if parameterized:
        d["parameters"] = PARAM_DECL
    return d


def widget_query(dataset_name, field_names):
    # NO `parameters` here — the dataset declaration + filter widget is
    # what drives the parameter value through.
    return {
        "datasetName": dataset_name,
        "fields": [{"name": n, "expression": f"`{n}`"} for n in field_names],
        "disaggregated": True,
    }


def table_widget(name, title, dataset_name, columns, x, y, w, h):
    return {
        "widget": {
            "name": name,
            "queries": [
                {
                    # Bug 4: query name MUST be unique across widgets.
                    # If 2+ widgets share "main_query" the rendered form
                    # inputs collide on id (browser warns "Duplicate form
                    # field id"), and the single-select filter loses its
                    # selection on re-render. Use widget-suffixed name.
                    "name": f"main_query_{name}",
                    "query": widget_query(
                        dataset_name, [c["fieldName"] for c in columns]
                    ),
                }
            ],
            "spec": {
                "version": 1,
                "widgetType": "table",
                # Spec-level defaults the renderer needs — without these
                # the rows don't paint (same bug class as the missing
                # per-column fields).
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
                    "name": f"main_query_{name}",  # see Bug 4 in table_widget
                    "query": widget_query(dataset_name, [x_field, y_field]),
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


# ── Datasets ────────────────────────────────────────────────────────────
PARAMETERIZED_DATASETS = [
    ("d_presence", "Presence per layer", """
SELECT 'bronze' AS layer, COUNT(*) AS rows, 1 AS sort_order
FROM   nexus_dev.bronze.mongodb_cdc_expenses
WHERE  expense_id = :p_expense_id
UNION ALL
SELECT 'silver', COUNT(*), 2 FROM nexus_dev.silver.expenses     WHERE expense_id = :p_expense_id
UNION ALL
SELECT 'gold',   COUNT(*), 3 FROM nexus_dev.gold.expense_audit  WHERE expense_id = :p_expense_id
ORDER BY sort_order
"""),
    ("d_bronze_cdc_expense", "Bronze CDC (expenses)", """
SELECT __source_ts_ms, __op, __deleted, tenant_id, status, amount, currency, vendor,
       date, final_amount, final_vendor, final_date, created_at, updated_at, _cdc_ingestion_ts
FROM   nexus_dev.bronze.mongodb_cdc_expenses
WHERE  expense_id = :p_expense_id
ORDER BY __source_ts_ms
"""),
    ("d_silver_expense", "Silver expense (current state)", """
SELECT expense_id, tenant_id, user_id, receipt_id, workflow_id,
       status, amount, currency, vendor, date, category,
       final_amount, final_currency, final_vendor, final_date,
       approved_at, created_at, updated_at
FROM   nexus_dev.silver.expenses
WHERE  expense_id = :p_expense_id
"""),
    ("d_silver_ocr", "Silver OCR", """
SELECT expense_id, tenant_id, user_id,
       ocr_total, ocr_total_confidence,
       ocr_vendor, ocr_vendor_confidence,
       ocr_date, ocr_date_confidence,
       ocr_currency, avg_confidence, textract_raw_s3_key, extracted_at
FROM   nexus_dev.silver.ocr_extractions
WHERE  expense_id = :p_expense_id
"""),
    ("d_silver_timeline", "Events timeline (silver)", """
SELECT created_at, event_type, actor, workflow_id, event_id
FROM   nexus_dev.silver.expense_events
WHERE  expense_id = :p_expense_id
ORDER BY created_at
"""),
    ("d_silver_hitl", "HITL tasks", """
SELECT task_id, status, decision, resolved_by, resolved_at, created_at, workflow_id
FROM   nexus_dev.silver.hitl_events
WHERE  expense_id = :p_expense_id
ORDER BY created_at DESC
"""),
    ("d_gold_audit", "Gold audit", """
SELECT tenant_id, expense_id, user_id, receipt_id,
       final_amount, final_currency, final_vendor, final_date,
       category, approved_at
FROM   nexus_dev.gold.expense_audit
WHERE  expense_id = :p_expense_id
"""),
    ("d_bronze_receipt", "Bronze receipt", """
SELECT __source_ts_ms, __op, receipt_id, s3_key, content_type, size_bytes, uploaded_at
FROM   nexus_dev.bronze.mongodb_cdc_receipts
WHERE  expense_id = :p_expense_id
ORDER BY __source_ts_ms
"""),
]

datasets = [ds(n, d, s) for (n, d, s) in PARAMETERIZED_DATASETS]

# Dropdown options source (no parameter — must be full list)
datasets.append(ds(
    "d_all_ids", "All expense_ids (dropdown source)", """
SELECT expense_id, MAX(created_at) AS last_seen
FROM   nexus_dev.silver.expenses
GROUP BY expense_id
ORDER BY last_seen DESC
LIMIT 500
""", parameterized=False))

# Latest 20 (no parameter — always-on reference table)
datasets.append(ds(
    "d_recent", "Latest 20 expenses", """
SELECT expense_id, tenant_id, status, amount, currency, vendor, created_at
FROM   nexus_dev.silver.expenses
ORDER BY created_at DESC
LIMIT 20
""", parameterized=False))


# ── Filter widget ───────────────────────────────────────────────────────
#
# Structure (verified against Databricks' published LakeFlow System
# Tables Dashboard — filter-single-select spec):
#   queries  = [one per parameterized dataset (binds :p_expense_id)]
#              + [one options-source query (reads distinct expense_ids)]
#   encodings.fields = matching pairs:
#              [{"parameterName": ..., "queryName": ...}] × N  for each
#              binding query, then
#              {"fieldName": "expense_id", "queryName": ...}   once for
#              the options-source query.
filter_param_queries = [
    {
        "name": f"param_q_{ds_name}",
        "query": {
            "datasetName": ds_name,
            "parameters": [{"name": PARAM_NAME, "keyword": PARAM_NAME}],
            "disaggregated": False,
        },
    }
    for (ds_name, _, _) in PARAMETERIZED_DATASETS
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

filter_widget = {
    "widget": {
        "name": "f_expense_id",
        "queries": filter_param_queries + [filter_options_query],
        "spec": {
            "version": 2,
            "widgetType": "filter-single-select",
            "encodings": {
                # Bug 3: only ONE parameterName entry (referencing any of the
                # param_q_* queries — the parameter system propagates to all
                # datasets with parameters block). Multiple parameterName for
                # the same parameter confuses Lakeview and click stops
                # propagating.
                "fields": [
                    {"parameterName": PARAM_NAME,
                     "queryName": filter_param_queries[0]["name"]},
                    {"fieldName": "expense_id",
                     "displayName": "expense_id",
                     "queryName": "options_q_d_all_ids"},
                ]
            },
            # Bug 5: empty array means "no default selection" — required so
            # the user's click sticks. With [{"value": ""}] Lakeview re-applies
            # the empty-string default on each render and selection bounces.
            "selection": {
                "defaultSelection": {
                    "values": {"dataType": "STRING", "values": []}
                }
            },
            "frame": {"title": "Select an expense_id", "showTitle": True},
        },
    },
    "position": {"x": 0, "y": 0, "width": 6, "height": 2},
}


# ── Data widgets ────────────────────────────────────────────────────────
widgets = [filter_widget]

# Latest table — not parameterized, lets you pick an ID visually
widgets.append(
    table_widget(
        "w_recent",
        "Latest expenses (pick an ID from the dropdown above to trace it)",
        "d_recent",
        [
            col_text("expense_id", "expense_id", 0),
            col_text("tenant_id", "tenant", 1),
            col_text("status", "status", 2),
            col_number("amount", "amount", 3),
            col_text("currency", "ccy", 4),
            col_text("vendor", "vendor", 5),
            col_datetime("created_at", "created_at", 6),
        ],
        x=0, y=2, w=6, h=8,
    )
)

widgets.append(
    bar_widget(
        "w_presence",
        "Does this ID exist in each layer?",
        "d_presence",
        x_field="layer",
        y_field="rows",
        x=0, y=10, w=3, h=6,
        x_sort=["bronze", "silver", "gold"],
    )
)

widgets.append(
    table_widget(
        "w_silver_expense", "Silver — current state", "d_silver_expense",
        [
            col_text("expense_id", "expense_id", 0),
            col_text("tenant_id", "tenant", 1),
            col_text("status", "status", 2),
            col_number("amount", "user amount", 3),
            col_text("vendor", "user vendor", 4),
            col_datetime("date", "user date", 5),
            col_number("final_amount", "final amount", 6),
            col_text("final_vendor", "final vendor", 7),
            col_datetime("final_date", "final date", 8),
            col_datetime("approved_at", "approved_at", 9),
            col_datetime("created_at", "created_at", 10),
            col_datetime("updated_at", "updated_at", 11),
            col_text("workflow_id", "workflow_id", 12),
        ],
        x=3, y=10, w=3, h=6,
    )
)

widgets.append(
    table_widget(
        "w_silver_ocr", "OCR extraction (silver)", "d_silver_ocr",
        [
            col_number("ocr_total", "total", 0),
            col_number("ocr_total_confidence", "total conf %", 1),
            col_text("ocr_vendor", "vendor", 2),
            col_number("ocr_vendor_confidence", "vendor conf %", 3),
            col_datetime("ocr_date", "date", 4),
            col_number("ocr_date_confidence", "date conf %", 5),
            col_number("avg_confidence", "avg conf %", 6),
            col_text("textract_raw_s3_key", "textract s3_key", 7),
            col_datetime("extracted_at", "extracted_at", 8),
        ],
        x=0, y=16, w=3, h=6,
    )
)

widgets.append(
    table_widget(
        "w_silver_hitl", "HITL tasks", "d_silver_hitl",
        [
            col_text("task_id", "task_id", 0),
            col_text("status", "status", 1),
            col_text("decision", "decision", 2),
            col_text("resolved_by", "resolved_by", 3),
            col_datetime("resolved_at", "resolved_at", 4),
            col_datetime("created_at", "created_at", 5),
            col_text("workflow_id", "workflow_id", 6),
        ],
        x=3, y=16, w=3, h=6,
    )
)

widgets.append(
    table_widget(
        "w_silver_timeline", "Events timeline", "d_silver_timeline",
        [
            col_datetime("created_at", "t", 0),
            col_text("event_type", "event_type", 1),
            col_text("actor", "actor", 2),
            col_text("workflow_id", "workflow_id", 3),
            col_text("event_id", "event_id", 4),
        ],
        x=0, y=22, w=6, h=8,
    )
)

widgets.append(
    table_widget(
        "w_gold", "Gold — expense_audit", "d_gold_audit",
        [
            col_text("tenant_id", "tenant", 0),
            col_text("expense_id", "expense_id", 1),
            col_number("final_amount", "amount", 2),
            col_text("final_currency", "ccy", 3),
            col_text("final_vendor", "vendor", 4),
            col_datetime("final_date", "date", 5),
            col_text("category", "category", 6),
            col_datetime("approved_at", "approved_at", 7),
        ],
        x=0, y=30, w=6, h=5,
    )
)

widgets.append(
    table_widget(
        "w_bronze_cdc", "Bronze CDC (all change events)", "d_bronze_cdc_expense",
        [
            col_number("__source_ts_ms", "src_ts_ms", 0),
            col_text("__op", "op", 1),
            col_text("status", "status", 2),
            col_number("amount", "amount", 3),
            col_text("vendor", "vendor", 4),
            col_number("final_amount", "final_amount", 5),
            col_text("final_vendor", "final_vendor", 6),
            col_datetime("_cdc_ingestion_ts", "ingest_ts", 7),
        ],
        x=0, y=35, w=6, h=8,
    )
)

widgets.append(
    table_widget(
        "w_bronze_receipt", "S3 receipt", "d_bronze_receipt",
        [
            col_text("receipt_id", "receipt_id", 0),
            col_text("s3_key", "s3_key", 1),
            col_text("content_type", "mime", 2),
            col_number("size_bytes", "bytes", 3),
            col_datetime("uploaded_at", "uploaded_at", 4),
        ],
        x=0, y=43, w=6, h=4,
    )
)


dashboard = {
    "datasets": datasets,
    "pages": [
        {
            "name": "main",
            "displayName": "Expense trace",
            "layout": widgets,
            "pageType": "PAGE_TYPE_CANVAS",
        }
    ],
    "uiSettings": {"theme": {"widgetHeaderAlignment": "ALIGNMENT_UNSPECIFIED"}},
}

OUT.write_text(json.dumps(dashboard, indent=2))
print(f"wrote {OUT} ({len(widgets)} widgets, {len(datasets)} datasets)")
