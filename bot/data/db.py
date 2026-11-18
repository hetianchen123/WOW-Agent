"""SQLite persistence: events, sessions, gold samples, item counts.

Thread-safe enough for our single control loop + background threads
(audio/watchdog) via a re-entrant lock.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class Database:
    def __init__(self, path: Path | str = "data/wowagnet.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    data TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
                CREATE TABLE IF NOT EXISTS sessions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    module TEXT NOT NULL,
                    status TEXT,
                    stats TEXT
                );
                CREATE TABLE IF NOT EXISTS gold_samples(
                    ts REAL PRIMARY KEY,
                    gold INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS item_counts(
                    ts REAL NOT NULL,
                    item_id INTEGER NOT NULL,
                    count INTEGER NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- events
    def log_event(self, kind: str, data: Dict[str, Any] | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events(ts, kind, data) VALUES (?,?,?)",
                (time.time(), kind, json.dumps(data, ensure_ascii=False) if data else None),
            )

    def recent_events(self, since: float = 0.0, limit: int = 200) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (since, limit),
            ).fetchall()

    def event_counts(self, since: float = 0.0) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) c FROM events WHERE ts >= ? GROUP BY kind ORDER BY c DESC",
                (since,),
            ).fetchall()
        return {r["kind"]: r["c"] for r in rows}

    # -------------------------------------------------------------- sessions
    def start_session(self, module: str, meta: Dict[str, Any] | None = None) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO sessions(started_at, module, status, stats) VALUES (?,?,?,?)",
                (time.time(), module, "running", json.dumps(meta or {}, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def end_session(self, session_id: int, status: str, stats: Dict[str, Any] | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET ended_at=?, status=?, stats=? WHERE id=?",
                (time.time(), status, json.dumps(stats or {}, ensure_ascii=False), session_id),
            )

    def list_sessions(self, limit: int = 50) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()

    # ----------------------------------------------------------- measurements
    def add_gold(self, gold: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO gold_samples(ts, gold) VALUES (?,?)", (time.time(), gold)
            )

    def add_item_count(self, item_id: int, count: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO item_counts(ts, item_id, count) VALUES (?,?,?)",
                (time.time(), item_id, count),
            )

    def gold_series(self, since: float = 0.0) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM gold_samples WHERE ts >= ? ORDER BY ts", (since,)
            ).fetchall()
