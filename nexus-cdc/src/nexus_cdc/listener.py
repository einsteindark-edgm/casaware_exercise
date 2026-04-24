"""Single-collection change stream listener.

Runs in its own thread. Owns a pymongo Collection watch() cursor, feeds
events into an S3Batcher, and persists the resume_token after each flush.

Restart semantics:
 - Cold start with offset present → resume with `resumeAfter`.
 - Cold start with no offset + bootstrap_on_empty_offset=True → full snapshot
   first (emits `__op='r'` events), then switch to watch() from the cluster
   time recorded at bootstrap_end.
 - Crash between S3 flush and DDB PutItem → next start replays from previous
   resume_token → duplicates in S3 → silver applies SCD1 merge by
   (key, __source_ts_ms) which is idempotent. At-least-once is the guarantee.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

from nexus_cdc.batcher import S3Batcher
from nexus_cdc.checkpoint import OffsetStore
from nexus_cdc.mappers import envelope_from_change, envelope_from_snapshot
from nexus_cdc.observability import get_logger

log = get_logger("nexus_cdc.listener")


class CollectionListener:
    def __init__(
        self,
        *,
        mongo: MongoClient,
        db_name: str,
        collection: str,
        batcher: S3Batcher,
        offsets: OffsetStore,
        idle_flush_seconds: int,
        bootstrap_on_empty_offset: bool,
    ) -> None:
        self._mongo = mongo
        self._db_name = db_name
        self._collection_name = collection
        self._batcher = batcher
        self._offsets = offsets
        self._idle_flush_seconds = idle_flush_seconds
        self._bootstrap = bootstrap_on_empty_offset
        self._last_activity = time.monotonic()

    @property
    def _col(self) -> Collection:
        return self._mongo[self._db_name][self._collection_name]

    def run_forever(self) -> None:
        """Main loop — recovers from transient errors with exponential backoff."""
        backoff = 1
        while True:
            try:
                self._run_once()
                backoff = 1  # successful restart (shouldn't exit normally)
            except PyMongoError as e:
                log.error(
                    "mongo_error",
                    collection=self._collection_name,
                    error=str(e),
                    backoff=backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:
                log.exception("listener_crashed", collection=self._collection_name)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _run_once(self) -> None:
        offset = self._offsets.get(self._collection_name)
        resume_token = (offset or {}).get("resume_token")

        if not resume_token and self._bootstrap and not (offset or {}).get("bootstrap_completed_at"):
            log.info("bootstrap_start", collection=self._collection_name)
            cluster_time_ms = self._bootstrap_full_sync()
            self._offsets.mark_bootstrap_complete(self._collection_name, cluster_time_ms)
            log.info(
                "bootstrap_complete",
                collection=self._collection_name,
                cluster_time_ms=cluster_time_ms,
            )
            offset = self._offsets.get(self._collection_name)
            resume_token = None

        self._watch(resume_token=resume_token, start_at_ms=(offset or {}).get("last_ts_ms"))

    def _bootstrap_full_sync(self) -> int:
        """Iterate the collection, emit __op=r events, return now-ish cluster_time in ms."""
        cluster_time_ms = int(time.time() * 1000)
        cursor = self._col.find({}, no_cursor_timeout=False)
        count = 0
        for doc in cursor:
            try:
                event = envelope_from_snapshot(doc)
            except Exception as e:
                self._batcher.dlq({"_snapshot": True, "_id": str(doc.get("_id"))}, str(e))
                continue
            self._batcher.add(event)
            count += 1
        self._batcher.flush(reason="bootstrap_end")
        log.info("bootstrap_snapshot", collection=self._collection_name, docs=count)
        return cluster_time_ms

    def _watch(self, *, resume_token: dict[str, Any] | None, start_at_ms: int | None) -> None:
        pipeline: Iterable[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "full_document": "updateLookup",  # get full doc on updates
            "max_await_time_ms": 1000,        # wake every second to flush on idle
        }
        if resume_token:
            kwargs["resume_after"] = resume_token
        elif start_at_ms is not None:
            # BSON Timestamp(seconds, inc)
            from bson.timestamp import Timestamp

            kwargs["start_at_operation_time"] = Timestamp(int(start_at_ms // 1000), 1)

        log.info(
            "watch_start",
            collection=self._collection_name,
            resume=bool(resume_token),
            start_at_ms=start_at_ms,
        )
        with self._col.watch(pipeline, **kwargs) as stream:
            while stream.alive:
                change = stream.try_next()
                if change is None:
                    # No event available within max_await_time_ms — tick the batcher
                    self._batcher.tick()
                    continue
                try:
                    envelope = envelope_from_change(change)
                except Exception as e:
                    self._batcher.dlq(change.get("documentKey", {}), f"mapper_failed: {e}")
                    continue
                if envelope is None:
                    continue
                self._batcher.add(envelope)

                # Persist resume token after each event so crashes lose at most a batch.
                # DDB PutItem is cheap (~5ms) but we piggyback it to flush boundaries
                # to avoid a write per event.
                resume_token = stream.resume_token
                self._last_activity = time.monotonic()

                # Flush-by-size handled inside add(); also persist token every N events.
                if len(self._batcher._buffer) == 0:  # add() flushed
                    self._offsets.put(
                        self._collection_name,
                        resume_token,
                        envelope.get("__source_ts_ms", 0),
                    )

            # Stream died (network blip, cursor timeout) — persist last known token.
            if resume_token:
                self._offsets.put(
                    self._collection_name, resume_token, int(time.time() * 1000)
                )
