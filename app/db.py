"""SQLite storage. One row per channel per minute (the minute's average watts).

Channel names:
  grid            net watts at the service entrance, + import / - export
  solar           watts produced by the panels (>= 0)
  battery         watts, + charging / - discharging
  circuit:<id>    a branch circuit from the energy monitor
  device:<id>     a single device, e.g. a smart plug
Whole-home load is derived: grid + solar - battery.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples_1m (
    ts_min  INTEGER NOT NULL,
    channel TEXT    NOT NULL,
    avg_w   REAL    NOT NULL,
    n       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (ts_min, channel)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_samples_channel ON samples_1m (channel, ts_min);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.latest: dict[str, tuple[int, float]] = {}
        # Recent raw readings per channel for the live graph: (epoch_seconds, watts), about 15 min at 1 Hz.
        self.recent: dict[str, deque] = {}
        self._load_latest()

    # ---- settings -------------------------------------------------------
    def get_setting(self, key: str, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self.conn.commit()

    # ---- samples --------------------------------------------------------
    def ingest(self, samples: list[tuple[int, str, float]]) -> int:
        """samples: (epoch_seconds, channel, watts). Averages within a minute."""
        rows = [(int(ts) // 60, ch, float(w)) for ts, ch, w in samples]
        with self.lock:
            self.conn.executemany(
                "INSERT INTO samples_1m(ts_min, channel, avg_w, n) VALUES(?, ?, ?, 1) "
                "ON CONFLICT(ts_min, channel) DO UPDATE SET "
                "avg_w=(avg_w*n+excluded.avg_w)/(n+1), n=n+1",
                rows,
            )
            self.conn.commit()
        with self.lock:
            for ts, ch, w in samples:
                prev = self.latest.get(ch)
                if prev is None or int(ts) >= prev[0]:
                    self.latest[ch] = (int(ts), float(w))
                if ch in ("grid", "solar", "battery"):
                    q = self.recent.setdefault(ch, deque(maxlen=900))
                    if not q or int(ts) >= q[-1][0]:
                        q.append((int(ts), float(w)))
        return len(rows)

    def bulk_insert(self, rows: list[tuple[int, str, float]]) -> None:
        """Fast path for backfills: (ts_min, channel, avg_w); replaces existing rows."""
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO samples_1m(ts_min, channel, avg_w, n) VALUES(?, ?, ?, 1)",
                rows,
            )
            self.conn.commit()
        self._load_latest()

    def _load_latest(self) -> None:
        with self.lock:
            rows = self.conn.execute(
                "SELECT channel, MAX(ts_min) AS ts_min FROM samples_1m GROUP BY channel"
            ).fetchall()
            for row in rows:
                value = self.conn.execute(
                    "SELECT avg_w FROM samples_1m WHERE channel=? AND ts_min=?",
                    (row["channel"], row["ts_min"]),
                ).fetchone()
                self.latest[row["channel"]] = (row["ts_min"] * 60, value["avg_w"])

    def recent_points(self, since: int) -> dict[str, list[tuple[int, float]]]:
        with self.lock:
            return {ch: [p for p in q if p[0] > since] for ch, q in self.recent.items()}

    def count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) AS c FROM samples_1m").fetchone()["c"]

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def channels(self) -> list[dict]:
        rows = self.query(
            "SELECT channel, MIN(ts_min) AS first, MAX(ts_min) AS last, COUNT(*) AS rows "
            "FROM samples_1m GROUP BY channel ORDER BY channel"
        )
        return [
            {"channel": r["channel"], "first": r["first"] * 60, "last": r["last"] * 60, "rows": r["rows"]}
            for r in rows
        ]

    @staticmethod
    def now() -> int:
        return int(time.time())
