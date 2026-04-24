"""Entry point: one thread per watched collection + a flush-tick driver thread."""
from __future__ import annotations

import signal
import sys
import threading
import time

from pymongo import MongoClient

from nexus_cdc.batcher import S3Batcher
from nexus_cdc.checkpoint import OffsetStore
from nexus_cdc.config import settings
from nexus_cdc.listener import CollectionListener
from nexus_cdc.observability import get_logger, setup_logging


def main() -> int:
    setup_logging(settings.log_level)
    log = get_logger("nexus_cdc.main")
    log.info(
        "startup",
        collections=settings.collections,
        bucket=settings.s3_bucket,
        ddb=settings.ddb_offsets_table,
    )

    client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=10000)
    # Validate connection before spawning threads.
    client.admin.command("ping")
    log.info("mongo_ping_ok")

    offsets = OffsetStore(
        settings.ddb_offsets_table, settings.aws_region, settings.aws_endpoint_url
    )

    batchers: dict[str, S3Batcher] = {}
    listeners: list[CollectionListener] = []
    for coll in settings.collections:
        batcher = S3Batcher(
            bucket=settings.s3_bucket,
            collection=coll,
            region=settings.aws_region,
            max_events=settings.batch_max_events,
            max_seconds=settings.batch_max_seconds,
            endpoint_url=settings.aws_endpoint_url,
        )
        batchers[coll] = batcher
        listeners.append(
            CollectionListener(
                mongo=client,
                db_name=settings.mongodb_db,
                collection=coll,
                batcher=batcher,
                offsets=offsets,
                idle_flush_seconds=settings.idle_flush_seconds,
                bootstrap_on_empty_offset=settings.bootstrap_on_empty_offset,
            )
        )

    stop = threading.Event()

    def _on_signal(signum, _frame):
        log.info("signal_received", signum=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    threads = []
    for listener in listeners:
        t = threading.Thread(
            target=listener.run_forever,
            name=f"cdc-{listener._collection_name}",
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Driver thread: flushes time-based batches every second. Avoids each
    # listener needing its own timer.
    def _tick_driver():
        while not stop.is_set():
            for batcher in batchers.values():
                try:
                    batcher.tick()
                except Exception:
                    log.exception("tick_error")
            time.sleep(1)

    tick = threading.Thread(target=_tick_driver, name="cdc-tick", daemon=True)
    tick.start()

    try:
        while not stop.is_set():
            time.sleep(1)
    finally:
        log.info("shutdown_flushing")
        for batcher in batchers.values():
            try:
                batcher.flush(reason="shutdown")
            except Exception:
                log.exception("shutdown_flush_failed", collection=batcher.collection)
        log.info("shutdown_complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
