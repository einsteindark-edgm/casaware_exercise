"""Pure comparison activity — no I/O, no LLM, deterministic.

Compares user-reported expense fields against OCR-extracted fields with
configurable tolerances. Returns the list of conflicts (empty when aligned).
"""
from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz
from temporalio import activity


@activity.defn(name="compare_fields")
async def compare_fields(inp: dict[str, Any]) -> dict[str, Any]:
    user = inp["user_reported"]
    ocr = inp["ocr_extracted"]
    tol = inp["tolerance"]

    conflicts: list[dict[str, Any]] = []

    # amount — percentage tolerance
    user_amount = float(user.get("amount") or 0)
    ocr_amount_field = ocr.get("ocr_total", {}) or {}
    ocr_amount = float(ocr_amount_field.get("value") or 0)
    if user_amount and ocr_amount:
        diff_pct = abs(user_amount - ocr_amount) / max(user_amount, ocr_amount)
        if diff_pct > tol["amount_pct"]:
            conflicts.append(
                {
                    "field": "amount",
                    "user_value": user_amount,
                    "ocr_value": ocr_amount,
                    "confidence": ocr_amount_field.get("confidence"),
                }
            )

    # vendor — Levenshtein similarity
    user_vendor = (user.get("vendor") or "").strip().lower()
    ocr_vendor_field = ocr.get("ocr_vendor", {}) or {}
    ocr_vendor = (ocr_vendor_field.get("value") or "").strip().lower()
    if user_vendor and ocr_vendor:
        similarity = fuzz.ratio(user_vendor, ocr_vendor) / 100
        if similarity < tol["vendor_similarity_min"]:
            conflicts.append(
                {
                    "field": "vendor",
                    "user_value": user_vendor,
                    "ocr_value": ocr_vendor,
                    "confidence": ocr_vendor_field.get("confidence"),
                    "similarity": similarity,
                }
            )

    # date — exact equality
    ocr_date_field = ocr.get("ocr_date", {}) or {}
    if user.get("date") and ocr_date_field.get("value"):
        if user["date"] != ocr_date_field["value"]:
            conflicts.append(
                {
                    "field": "date",
                    "user_value": user.get("date"),
                    "ocr_value": ocr_date_field.get("value"),
                    "confidence": ocr_date_field.get("confidence"),
                }
            )

    return {"fields_in_conflict": conflicts}
