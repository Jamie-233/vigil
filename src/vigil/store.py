"""SQLite sample store.

SQLite is enough here, and choosing it is a design decision rather than a
shortcut: one probe per target every few seconds is a few writes per second,
far below what a single file handles, and it means Vigil deploys as one
process with no database server to babysit.

WAL mode matters because the dashboard reads while probes write. In the
default rollback-journal mode a long read blocks writers, and blocked writers
would show up as gaps in the very data meant to prove the network was fine.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass


@dataclass
class Sample:
    target: str
    ts: float
    ok: bool
    latency_ms: float | None
    detail: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    target     TEXT    NOT NULL,
    ts         REAL    NOT NULL,
    ok         INTEGER NOT NULL,
    latency_ms REAL,
    detail     TEXT,
    extra      TEXT
);
-- Every query is "one target over a time range"; a composite index turns that
-- into a single range scan instead of filtering the whole table.
CREATE INDEX IF NOT EXISTS samples_target_ts ON samples (target, ts);

CREATE TABLE IF NOT EXISTS events (
    target TEXT NOT NULL,
    ts     REAL NOT NULL,
    old    TEXT NOT NULL,
    new    TEXT NOT NULL,
    reason TEXT,
    flapping INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False plus our own lock: writes come from the event
        # loop via a worker thread, reads from request handlers.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
                # NORMAL is durable under WAL except for the last transaction on
                # power loss — acceptable for monitoring samples, and much faster.
                self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)

    def add_sample(self, target: str, ok: bool, latency_ms: float | None, detail: str,
                   extra: dict | None = None, ts: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)",
                (target, ts or time.time(), int(ok), latency_ms, detail, json.dumps(extra or {})),
            )

    def add_event(self, target: str, old: str, new: str, reason: str, flapping: bool, ts: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (target, ts, old, new, reason, int(flapping)),
            )

    def samples(self, target: str, since: float, until: float | None = None) -> list[Sample]:
        with self._lock:
            rows = self._db.execute(
                "SELECT target, ts, ok, latency_ms, detail FROM samples "
                "WHERE target = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (target, since, until or time.time() + 1),
            ).fetchall()
        return [Sample(r[0], r[1], bool(r[2]), r[3], r[4]) for r in rows]

    def series(self, target: str, since: float, buckets: int = 120) -> list[dict]:
        """Downsample to fixed buckets in SQL.

        Shipping every raw sample to the browser for a 7-day view would be
        tens of thousands of points for a chart a few hundred pixels wide.
        Each bucket keeps max latency, not average, so short spikes survive
        downsampling instead of being smoothed into invisibility.
        """
        now = time.time()
        width = max((now - since) / buckets, 1e-6)
        with self._lock:
            rows = self._db.execute(
                """
                SELECT CAST((ts - :since) / :width AS INTEGER) AS b,
                       MIN(ts), MAX(latency_ms), AVG(ok), COUNT(*)
                FROM samples WHERE target = :t AND ts >= :since
                GROUP BY b ORDER BY b
                """,
                {"t": target, "since": since, "width": width},
            ).fetchall()
        return [
            {"ts": r[1], "latency_max": r[2], "availability": round(r[3] * 100, 2), "count": r[4]}
            for r in rows
        ]

    def events(self, since: float, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT target, ts, old, new, reason, flapping FROM events "
                "WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [
            {"target": r[0], "ts": r[1], "old": r[2], "new": r[3], "reason": r[4], "flapping": bool(r[5])}
            for r in rows
        ]

    def prune(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            cur = self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()
