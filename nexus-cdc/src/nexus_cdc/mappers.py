"""Mongo BSON document → bronze-shaped JSON envelope.

The envelope MUST match what `seed_bronze_from_mongo.py` writes today so that
the silver pipeline's `apply_changes(keys=..., sequence_by=__source_ts_ms)`
can merge seed rows with live CDC rows without schema drift.

Additions vs a vanilla dict:
 - __op:           'c' (create), 'u' (update), 'd' (delete), 'r' (read/snapshot)
 - __source_ts_ms: int — time the change was observed (Mongo wallTime or cluster_time)
 - __deleted:      bool — redundant with __op='d' but matches seed output

Normalizers:
 - Decimal   → float           (Autoloader schema casts back to DecimalType later)
 - datetime  → ISO-8601 string (Autoloader parses into TimestampType)
 - date      → ISO-8601 string (same)
 - ObjectId  → str             (we never index on _id anyway; we use *_id business IDs)
 - missing   → absent from JSON (not null) so Autoloader treats schema as sparse
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from bson import ObjectId
from bson.decimal128 import Decimal128


def _json_safe(v: Any) -> Any:
    """Recursively convert Mongo BSON types to JSON-serialisable ones."""
    if v is None:
        return None
    if isinstance(v, bool | int | float | str):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, Decimal128):
        return float(v.to_decimal())
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=dt.UTC)
        return v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items() if not k.startswith("_")
                or k in ("_id",)}  # drop Mongo's $clusterTime etc., keep _id for traceability
    if isinstance(v, list | tuple | set):
        return [_json_safe(x) for x in v]
    return str(v)


def _ts_ms(change: dict[str, Any] | None, fallback: dt.datetime | None = None) -> int:
    """Extract a monotonic timestamp for silver's sequence_by.

    Prefers change.clusterTime (BSON Timestamp), then wallTime, then the doc's
    updated_at/created_at, then now().
    """
    if change is not None:
        cluster = change.get("clusterTime")
        if cluster is not None:
            # bson.timestamp.Timestamp has .time (seconds) and .inc
            return int(cluster.time) * 1000 + int(getattr(cluster, "inc", 0))
        wall = change.get("wallTime")
        if isinstance(wall, dt.datetime):
            if wall.tzinfo is None:
                wall = wall.replace(tzinfo=dt.UTC)
            return int(wall.timestamp() * 1000)
    if fallback is not None:
        if fallback.tzinfo is None:
            fallback = fallback.replace(tzinfo=dt.UTC)
        return int(fallback.timestamp() * 1000)
    return int(dt.datetime.now(dt.UTC).timestamp() * 1000)


def envelope_from_snapshot(doc: dict[str, Any]) -> dict[str, Any]:
    """Full-sync / bootstrap row: op='r', ts pulled from updated_at|created_at|now."""
    out = _json_safe(doc)
    assert isinstance(out, dict)
    out.pop("_id", None)  # Mongo object id is internal — business _id columns remain
    ts_source = doc.get("updated_at") or doc.get("created_at")
    out["__op"] = "r"
    out["__source_ts_ms"] = _ts_ms(None, fallback=ts_source)
    out["__deleted"] = False
    return out


def envelope_from_change(change: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a Mongo change stream event to our bronze envelope.

    Returns None for operations we don't care about (e.g. `invalidate`,
    `drop`, `rename`). Delete events emit a tombstone with the key fields
    from `documentKey` so silver can apply_as_deletes.
    """
    op_type = change.get("operationType")
    doc_key = change.get("documentKey") or {}
    if op_type in ("insert",):
        mongo_op = "c"
        body = change.get("fullDocument") or {}
    elif op_type in ("update", "replace"):
        mongo_op = "u"
        # Prefer fullDocument (requires capture.mode=update_lookup or preAndPostImages).
        body = change.get("fullDocument") or change.get("updateDescription", {}).get("updatedFields") or {}
    elif op_type == "delete":
        mongo_op = "d"
        body = doc_key  # only _id / primary business key available
    else:
        return None

    ts = _ts_ms(change)
    out = _json_safe(body)
    assert isinstance(out, dict)
    out.pop("_id", None)
    out["__op"] = mongo_op
    out["__source_ts_ms"] = ts
    out["__deleted"] = mongo_op == "d"
    return out
