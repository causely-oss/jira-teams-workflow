"""Local state + per-Issue locking.

Jira is the source of truth for "does a ticket exist" (label search). SQLite caches the
mapping and remembers the last evidence fingerprint so we only comment on real change.
The asyncio lock serializes the Causely-triggered and alert-triggered paths for the
same Issue ID, which is what prevents duplicate tickets when both fire seconds apart.

Single-process only. For >1 replica, swap the lock for a Postgres advisory lock or Redis.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import defaultdict
from contextlib import asynccontextmanager


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickets (
              issue_id TEXT PRIMARY KEY, jira_key TEXT NOT NULL,
              fingerprint TEXT, severity TEXT, state TEXT, last_comment_at REAL
            );
            CREATE TABLE IF NOT EXISTS alerts_seen (fp TEXT PRIMARY KEY, seen_at REAL);
            """
        )
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def issue_lock(self, issue_id: str):
        async with self._locks[issue_id]:
            yield

    def get(self, issue_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT jira_key, fingerprint, severity, state, last_comment_at FROM tickets WHERE issue_id=?",
            (issue_id,),
        ).fetchone()
        if not row:
            return None
        return dict(zip(["jira_key", "fingerprint", "severity", "state", "last_comment_at"], row))

    def upsert(self, issue_id: str, jira_key: str, fingerprint: str, severity: str, state: str,
               commented: bool) -> None:
        prev = self.get(issue_id) or {}
        last = time.time() if commented else prev.get("last_comment_at")
        self.db.execute(
            "INSERT OR REPLACE INTO tickets VALUES (?,?,?,?,?,?)",
            (issue_id, jira_key, fingerprint, severity, state, last),
        )
        self.db.commit()

    def alert_is_duplicate(self, fp: str, window_s: int) -> bool:
        now = time.time()
        row = self.db.execute("SELECT seen_at FROM alerts_seen WHERE fp=?", (fp,)).fetchone()
        if row and now - row[0] < window_s:
            return True
        self.db.execute("INSERT OR REPLACE INTO alerts_seen VALUES (?,?)", (fp, now))
        self.db.commit()
        return False
