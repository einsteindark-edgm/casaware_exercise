from __future__ import annotations

import asyncio

import pytest

from nexus_orchestration.activities.comparison import compare_fields

_TOL = {"amount_pct": 0.01, "vendor_similarity_min": 0.85}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.mark.asyncio
async def test_within_tolerance_no_conflicts():
    result = await compare_fields(
        {
            "user_reported": {"amount": 100.0, "vendor": "Starbucks", "date": "2026-04-22"},
            "ocr_extracted": {
                "ocr_total": {"value": 100.5, "confidence": 99.0},
                "ocr_vendor": {"value": "Starbucks", "confidence": 96.0},
                "ocr_date": {"value": "2026-04-22", "confidence": 91.0},
            },
            "tolerance": _TOL,
        }
    )
    # 0.5% amount diff, exact vendor, exact date → no conflicts
    assert result["fields_in_conflict"] == []


@pytest.mark.asyncio
async def test_amount_over_tolerance():
    result = await compare_fields(
        {
            "user_reported": {"amount": 100.0, "vendor": "Starbucks", "date": "2026-04-22"},
            "ocr_extracted": {
                "ocr_total": {"value": 120.0, "confidence": 95.0},
                "ocr_vendor": {"value": "Starbucks", "confidence": 96.0},
                "ocr_date": {"value": "2026-04-22", "confidence": 91.0},
            },
            "tolerance": _TOL,
        }
    )
    conflicts = result["fields_in_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["field"] == "amount"
    assert conflicts[0]["user_value"] == 100.0
    assert conflicts[0]["ocr_value"] == 120.0


@pytest.mark.asyncio
async def test_vendor_low_similarity():
    result = await compare_fields(
        {
            "user_reported": {"amount": 100.0, "vendor": "Starbucks", "date": "2026-04-22"},
            "ocr_extracted": {
                "ocr_total": {"value": 100.5, "confidence": 99.0},
                "ocr_vendor": {"value": "McDonalds", "confidence": 94.0},
                "ocr_date": {"value": "2026-04-22", "confidence": 91.0},
            },
            "tolerance": _TOL,
        }
    )
    fields = [c["field"] for c in result["fields_in_conflict"]]
    assert "vendor" in fields


@pytest.mark.asyncio
async def test_vendor_typo_within_tolerance():
    result = await compare_fields(
        {
            "user_reported": {"amount": 100.0, "vendor": "Starbucks", "date": "2026-04-22"},
            "ocr_extracted": {
                "ocr_total": {"value": 100.5, "confidence": 99.0},
                # fuzzy match ~94%, should pass 85% threshold
                "ocr_vendor": {"value": "Starbuks", "confidence": 94.0},
                "ocr_date": {"value": "2026-04-22", "confidence": 91.0},
            },
            "tolerance": _TOL,
        }
    )
    fields = [c["field"] for c in result["fields_in_conflict"]]
    assert "vendor" not in fields


@pytest.mark.asyncio
async def test_date_mismatch():
    result = await compare_fields(
        {
            "user_reported": {"amount": 100.0, "vendor": "Starbucks", "date": "2026-04-22"},
            "ocr_extracted": {
                "ocr_total": {"value": 100.5, "confidence": 99.0},
                "ocr_vendor": {"value": "Starbucks", "confidence": 96.0},
                "ocr_date": {"value": "2026-04-23", "confidence": 91.0},
            },
            "tolerance": _TOL,
        }
    )
    fields = [c["field"] for c in result["fields_in_conflict"]]
    assert "date" in fields


@pytest.mark.asyncio
async def test_missing_ocr_amount_skipped():
    result = await compare_fields(
        {
            "user_reported": {"amount": 100.0, "vendor": "Starbucks", "date": "2026-04-22"},
            "ocr_extracted": {
                "ocr_total": {"value": 0, "confidence": 0.0},
                "ocr_vendor": {"value": "Starbucks", "confidence": 96.0},
                "ocr_date": {"value": "2026-04-22", "confidence": 91.0},
            },
            "tolerance": _TOL,
        }
    )
    # amount comparison skipped because ocr_amount == 0
    assert result["fields_in_conflict"] == []


@pytest.mark.asyncio
async def test_ocr_amount_currency_string_parsed():
    # Textract AnalyzeExpense returns TOTAL as a formatted string with
    # thousands separator + currency code (e.g. "24,395.00 COP"). Previously
    # this crashed compare_fields with ValueError: could not convert string to
    # float. The tolerant parser must handle it.
    result = await compare_fields(
        {
            "user_reported": {"amount": 24395.0, "vendor": "Starbucks", "date": "2026-04-22"},
            "ocr_extracted": {
                "ocr_total": {"value": "24,395.00 COP", "confidence": 99.9},
                "ocr_vendor": {"value": "Starbucks", "confidence": 96.0},
                "ocr_date": {"value": "2026-04-22", "confidence": 91.0},
            },
            "tolerance": _TOL,
        }
    )
    assert result["fields_in_conflict"] == []


@pytest.mark.asyncio
async def test_ocr_amount_european_format():
    # European format: "24.395,00 €" — comma as decimal, dot as thousands.
    result = await compare_fields(
        {
            "user_reported": {"amount": 24395.0, "vendor": "Starbucks", "date": "2026-04-22"},
            "ocr_extracted": {
                "ocr_total": {"value": "24.395,00 €", "confidence": 99.9},
                "ocr_vendor": {"value": "Starbucks", "confidence": 96.0},
                "ocr_date": {"value": "2026-04-22", "confidence": 91.0},
            },
            "tolerance": _TOL,
        }
    )
    assert result["fields_in_conflict"] == []


_ = _run  # referenced for conditional sync helpers (unused currently)
