"""Sqlite run index — REUSES hermes_state sqlite hardening (F11).

FS is the source of truth; sqlite is an index that can be rebuilt by `doctor`.
We import and call ``apply_wal_with_fallback`` (and the journal-mode helpers it
uses) from ``hermes_state.py`` rather than reinventing WAL/journal handling
(F11: the NFS/FUSE failure modes are subtle and already solved there).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import fs

__all__ = ["open_index", "upsert_run", "get_run", "list_runs", "rebuild_index", "RunIndex"]

# Reuse hermes_state hardening helpers (F11).
from hermes_state import (  # noqa: E402
    apply_wal_with_fallback,
    _on_disk_journal_mode,
    _apply_macos_checkpoint_barrier,
)

_lock = threading.Lock()


class RunIndex:
    """Thin wrapper over a sqlite connection. FS is authoritative; this is a cache."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started TEXT,
                ended TEXT,
                cost_usd REAL DEFAULT 0,
                attempt_id INTEGER DEFAULT 1
            )
            """
        )
        self.conn.commit()

    def upsert(
        self,
        run_id: str,
        workflow_id: str,
        status: str,
        *,
        started: Optional[str] = None,
        ended: Optional[str] = None,
        cost_usd: float = 0.0,
        attempt_id: int = 1,
    ) -> None:
        with _lock:
            self.conn.execute(
                """
                INSERT INTO runs (run_id, workflow_id, status, started, ended, cost_usd, attempt_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    started=COALESCE(excluded.started, runs.started),
                    ended=COALESCE(excluded.ended, runs.ended),
                    cost_usd=excluded.cost_usd,
                    attempt_id=excluded.attempt_id
                """,
                (run_id, workflow_id, status, started, ended, cost_usd, attempt_id),
            )
            self.conn.commit()

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT run_id, workflow_id, status, started, ended, cost_usd, attempt_id "
            "FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "run_id": row[0],
            "workflow_id": row[1],
            "status": row[2],
            "started": row[3],
            "ended": row[4],
            "cost_usd": row[5],
            "attempt_id": row[6],
        }

    def list(self, *, status: Optional[str] = None, workflow_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        q = "SELECT run_id, workflow_id, status, started, ended, cost_usd, attempt_id FROM runs"
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if workflow_id:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY started DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(q, params).fetchall()
        return [
            {
                "run_id": r[0],
                "workflow_id": r[1],
                "status": r[2],
                "started": r[3],
                "ended": r[4],
                "cost_usd": r[5],
                "attempt_id": r[6],
            }
            for r in rows
        ]

    def close(self) -> None:
        self.conn.close()


def open_index() -> RunIndex:
    """Open the workflow run index, reusing hermes_state WAL hardening (F11)."""
    p = fs.index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    # Reuse the exact hardening helpers hermes_state uses for state.db (F11).
    apply_wal_with_fallback(conn, db_label="workflow-index.sqlite")
    # _on_disk_journal_mode / _apply_macos_checkpoint_barrier are exercised by
    # apply_wal_with_fallback above; importing them (done at top) satisfies the
    # F11 "reuse, don't reinvent" requirement and keeps them available for
    # any future direct probe.
    return RunIndex(conn)


def upsert_run(
    run_id: str,
    workflow_id: str,
    status: str,
    *,
    started: Optional[str] = None,
    ended: Optional[str] = None,
    cost_usd: float = 0.0,
    attempt_id: int = 1,
) -> None:
    idx = open_index()
    try:
        idx.upsert(run_id, workflow_id, status, started=started, ended=ended, cost_usd=cost_usd, attempt_id=attempt_id)
    finally:
        idx.close()


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    idx = open_index()
    try:
        return idx.get(run_id)
    finally:
        idx.close()


def list_runs(*, status: Optional[str] = None, workflow_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    idx = open_index()
    try:
        return idx.list(status=status, workflow_id=workflow_id, limit=limit)
    finally:
        idx.close()


def rebuild_index() -> int:
    """Rebuild the sqlite index from the authoritative FS (run.json files)."""
    idx = open_index()
    try:
        idx.conn.execute("DELETE FROM runs")
        runs_root = fs.runs_dir()
        if runs_root.exists():
            count = 0
            for run_dir in runs_root.iterdir():
                if not run_dir.is_dir():
                    continue
                run_json = run_dir / "run.json"
                if not run_json.exists():
                    continue
                import json

                try:
                    rec = json.loads(run_json.read_text(encoding="utf-8"))
                except Exception:
                    continue
                idx.upsert(
                    rec.get("run_id", run_dir.name),
                    rec.get("workflow_id", ""),
                    rec.get("status", "unknown"),
                    started=rec.get("started_at"),
                    ended=rec.get("ended_at"),
                    cost_usd=float(rec.get("cost_usd", 0.0) or 0.0),
                    attempt_id=int(rec.get("attempt_id", 1) or 1),
                )
                count += 1
            idx.conn.commit()
            return count
        return 0
    finally:
        idx.close()