from __future__ import annotations

import datetime as dt
from decimal import Decimal

from bson import ObjectId
from bson.decimal128 import Decimal128
from bson.timestamp import Timestamp

from nexus_cdc.mappers import envelope_from_change, envelope_from_snapshot


def test_snapshot_basic_types():
    doc = {
        "_id": ObjectId("507f1f77bcf86cd799439011"),
        "expense_id": "exp_abc",
        "tenant_id": "t_alpha",
        "amount": Decimal("100.50"),
        "currency": "USD",
        "created_at": dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.UTC),
        "updated_at": dt.datetime(2026, 4, 2, 12, 0, 0, tzinfo=dt.UTC),
    }
    out = envelope_from_snapshot(doc)

    assert out["__op"] == "r"
    assert out["__deleted"] is False
    assert out["__source_ts_ms"] == int(
        dt.datetime(2026, 4, 2, 12, 0, 0, tzinfo=dt.UTC).timestamp() * 1000
    )
    assert out["expense_id"] == "exp_abc"
    assert out["amount"] == 100.5
    assert out["created_at"] == "2026-04-01T12:00:00+00:00"
    # internal Mongo _id must be dropped to keep bronze schema stable
    assert "_id" not in out


def test_snapshot_handles_decimal128():
    doc = {
        "expense_id": "exp_1",
        "tenant_id": "t",
        "amount": Decimal128("42.00"),
        "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    }
    out = envelope_from_snapshot(doc)
    assert out["amount"] == 42.0


def test_snapshot_naive_datetime_treated_as_utc():
    naive = dt.datetime(2026, 4, 1, 12, 0, 0)  # no tzinfo
    doc = {"expense_id": "x", "tenant_id": "t", "created_at": naive}
    out = envelope_from_snapshot(doc)
    ts = out["__source_ts_ms"]
    assert ts == int(naive.replace(tzinfo=dt.UTC).timestamp() * 1000)


def test_snapshot_date_fields_become_iso_strings():
    doc = {
        "expense_id": "x",
        "tenant_id": "t",
        "date": dt.date(2026, 4, 22),
        "created_at": dt.datetime(2026, 4, 22, tzinfo=dt.UTC),
    }
    out = envelope_from_snapshot(doc)
    assert out["date"] == "2026-04-22"


def test_change_insert():
    change = {
        "operationType": "insert",
        "clusterTime": Timestamp(1745331000, 1),
        "documentKey": {"_id": ObjectId("507f1f77bcf86cd799439011")},
        "fullDocument": {
            "_id": ObjectId("507f1f77bcf86cd799439011"),
            "expense_id": "exp_xyz",
            "tenant_id": "t_beta",
        },
    }
    out = envelope_from_change(change)
    assert out is not None
    assert out["__op"] == "c"
    assert out["__deleted"] is False
    assert out["__source_ts_ms"] == 1745331000 * 1000 + 1
    assert out["expense_id"] == "exp_xyz"


def test_change_update_uses_full_document():
    change = {
        "operationType": "update",
        "clusterTime": Timestamp(1745331005, 2),
        "documentKey": {"_id": ObjectId("507f1f77bcf86cd799439011")},
        "fullDocument": {
            "_id": ObjectId("507f1f77bcf86cd799439011"),
            "expense_id": "exp_xyz",
            "tenant_id": "t_beta",
            "status": "approved",
        },
    }
    out = envelope_from_change(change)
    assert out is not None
    assert out["__op"] == "u"
    assert out["status"] == "approved"


def test_change_delete_emits_tombstone():
    change = {
        "operationType": "delete",
        "clusterTime": Timestamp(1745331010, 0),
        "documentKey": {"_id": ObjectId("507f1f77bcf86cd799439011"), "expense_id": "exp_xyz"},
    }
    out = envelope_from_change(change)
    assert out is not None
    assert out["__op"] == "d"
    assert out["__deleted"] is True
    # documentKey business field should survive
    assert out["expense_id"] == "exp_xyz"


def test_change_invalidate_returns_none():
    change = {"operationType": "invalidate"}
    assert envelope_from_change(change) is None


def test_resolved_fields_map_survives():
    doc = {
        "task_id": "hitl_1",
        "tenant_id": "t",
        "expense_id": "exp_1",
        "resolved_fields": {"amount": "150.00", "vendor": "Starbucks"},
        "created_at": dt.datetime(2026, 4, 1, tzinfo=dt.UTC),
    }
    out = envelope_from_snapshot(doc)
    assert out["resolved_fields"] == {"amount": "150.00", "vendor": "Starbucks"}
