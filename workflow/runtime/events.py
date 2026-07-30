"""Append-only events.jsonl helper (design §9). Tool names only, no arg secrets."""

from __future__ import annotations

import json
import time
from typing import Any, Dict

from ..store import fs

__all__ = ["emit", "start_event", "end_event", "tool_event", "cost_event"]


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def emit(run_id: str, node_run_id: str, event: Dict[str, Any]) -> None:
    """Append a tool-names-only event (no arg secrets) to the node's events.jsonl."""
    event = dict(event)
    event.setdefault("ts", _ts())
    fs.append_event(run_id, node_run_id, event)


def start_event(run_id: str, node_run_id: str, node_id: str, kind: str) -> None:
    emit(run_id, node_run_id, {"event": "start", "node_id": node_id, "kind": kind})


def end_event(run_id: str, node_run_id: str, node_id: str, status: str) -> None:
    emit(run_id, node_run_id, {"event": "end", "node_id": node_id, "status": status})


def tool_event(run_id: str, node_run_id: str, tool_name: str) -> None:
    """Record a tool invocation by NAME ONLY (design §9 — no arg secrets)."""
    emit(run_id, node_run_id, {"event": "tool", "name": tool_name})


def cost_event(run_id: str, node_run_id: str, cost_usd: float) -> None:
    emit(run_id, node_run_id, {"event": "cost", "cost_usd": cost_usd})