"""Append-only events.jsonl helper (design §9). Tool names only, no arg secrets."""

from __future__ import annotations

import time
from typing import Any, Dict

from ..store import fs

__all__ = [
    "emit",
    "start_event",
    "end_event",
    "tool_event",
    "cost_event",
    "run_event",
    "notify_event",
]

# Filesystem events.jsonl is keyed by node_run_id (design §9 / F1). Run-level
# events (a budget pause, a notification attempt) have no single owning
# node-run, so they are filed under this synthetic slot rather than growing a
# second on-disk event log.
_RUN_LEVEL_SLOT = "_run"


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def emit(run_id: str, node_run_id: str, event: Dict[str, Any]) -> None:
    """Append a tool-names-only event (no arg secrets) to the node's events.jsonl."""
    event = dict(event)
    event.setdefault("ts", _ts())
    fs.append_event(run_id, node_run_id, event)


def start_event(run_id: str, node_run_id: str, node_id: str, kind: str) -> None:
    emit(run_id, node_run_id, {"event": "start", "node_id": node_id, "kind": kind})


def end_event(run_id: str, node_run_id: str, node_id: str, status: str, **extra: Any) -> None:
    """End-of-node event. ``extra`` carries optional fields (e.g. Phase 2's
    ``effective_model`` / ``effective_provider``) — None-valued extras are
    dropped so a worker that doesn't report them (FakeWorker) is a silent
    no-op rather than polluting every event with null fields."""
    payload: Dict[str, Any] = {"event": "end", "node_id": node_id, "status": status}
    for k, v in extra.items():
        if v is not None:
            payload[k] = v
    emit(run_id, node_run_id, payload)


def tool_event(run_id: str, node_run_id: str, tool_name: str) -> None:
    """Record a tool invocation by NAME ONLY (design §9 — no arg secrets)."""
    emit(run_id, node_run_id, {"event": "tool", "name": tool_name})


def cost_event(run_id: str, node_run_id: str, cost_usd: float) -> None:
    emit(run_id, node_run_id, {"event": "cost", "cost_usd": cost_usd})


def run_event(run_id: str, payload: Dict[str, Any]) -> None:
    """Append a run-level event not tied to a single node_run_id (e.g. a
    budget-circuit-break pause). Filed under the synthetic ``_run`` slot."""
    event = dict(payload)
    event.setdefault("event", "run")
    emit(run_id, _RUN_LEVEL_SLOT, event)


def notify_event(run_id: str, payload: Dict[str, Any]) -> None:
    """Audit trail for a notification attempt, delivered or not, so
    notification attempts are auditable even when delivery is unavailable
    (Phase 2 TASK 1)."""
    event = dict(payload)
    event["event"] = "notify"
    run_event(run_id, event)
