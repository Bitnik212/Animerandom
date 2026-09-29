"""Nightly `embed` + `train` inside the server, after the ingest window.

A Postgres advisory lock makes sure only one replica runs it."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from rec_engine import jobs
from rec_engine.db import Database

log = logging.getLogger(__name__)
LOCK_KEY = 0x72656330  # "rec0"


def next_run(at: str, now: datetime) -> datetime:
    hour, minute = (int(x) for x in at.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def run_once(db: Database) -> bool:
    """Run the nightly jobs unless another process holds the lock. True if it ran."""
    with db.engine.connect() as conn:
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            return False
        try:
            log.info("nightly embed: %s", jobs.embed_job(db))
            log.info("nightly train: %s", jobs.train_job(db))
        except Exception:
            log.exception("nightly jobs failed")
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            conn.commit()
    return True


class Nightly(threading.Thread):
    def __init__(self, db: Database, at: str) -> None:
        super().__init__(name="rec-nightly", daemon=True)
        self.db, self.at = db, at
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.is_set():
            wait = (next_run(self.at, datetime.now(UTC)) - datetime.now(UTC)).total_seconds()
            if self.stopped.wait(max(wait, 1)):
                return
            run_once(self.db)

    def stop(self) -> None:
        self.stopped.set()
