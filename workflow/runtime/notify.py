"""Best-effort run/gate notifications (Phase 2 TASK 1).

``notify()`` is the single hook the driver calls when a run parks at a gate
or reaches a terminal status. It is deliberately defensive: a notification
failure (no target configured, the messaging subsystem unavailable, a send
error) must NEVER raise and must NEVER be allowed to break a workflow run.
Every code path returns a small result dict; the outcome is always also
recorded to the run's event log (``workflow.runtime.events.notify_event``)
so a notification attempt is auditable even when delivery didn't happen.

Delivery reuses the existing cross-channel send path
(``tools.send_message_tool.send_message_tool``) rather than growing a new
messaging integration — see AGENTS.md's footprint ladder ("extend existing
code" is the first rung).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import events

__all__ = ["Notification", "notify", "default_notifier", "set_notifier"]

logger = logging.getLogger(__name__)

_DEFAULT_NOTIFY_ON = ("failed", "partial", "awaiting_gate")


@dataclass
class Notification:
    """One notification-worthy occurrence: a gate park, or a terminal run
    status (succeeded/failed/partial/paused)."""

    run_id: str
    workflow_id: str
    status: str
    gate_id: Optional[str] = None
    succeeded: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    cost_usd: float = 0.0
    detail: str = ""

    def summary_text(self) -> str:
        base = f"[hermes workflow] run {self.run_id} ({self.workflow_id}) -> {self.status}"
        if self.status == "awaiting_gate" and self.gate_id:
            cmd = f"hermes workflow gate {self.run_id} {self.gate_id} --decide approve"
            line = f"{base} at gate '{self.gate_id}' | approve with: {cmd}"
        else:
            ok = len(self.succeeded)
            fail = len(self.failed)
            skip = len(self.skipped)
            line = f"{base} | ok={ok} fail={fail} skip={skip} | ${self.cost_usd:.4f}"
        if self.detail:
            line += f" | {self.detail}"
        return line


def _record(n: Notification, result: Dict[str, Any]) -> Dict[str, Any]:
    """Write the audit event (best-effort) and return the result unchanged."""
    payload: Dict[str, Any] = dict(result)
    payload["run_id"] = n.run_id
    payload["workflow_id"] = n.workflow_id
    payload["status"] = n.status
    if n.gate_id:
        payload["gate_id"] = n.gate_id
    try:
        events.notify_event(n.run_id, payload)
    except Exception:
        logger.debug("notify: failed to write audit event", exc_info=True)
    return result


def notify(
    n: Notification,
    *,
    workflow_notify: Any = None,
    gate_notify: Any = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Best-effort notification hook. Never raises.

    ``workflow_notify`` is the IR's run-level ``notify:`` block: ``None``
    (unset -> defer to config), ``True`` (use config defaults), ``False``
    (explicitly suppressed), or a dict ``{"on": [...], "target": "..."}``.

    ``gate_notify`` is the gate's own notify setting for a gate event: a
    bool (``Gate.notify``) or a dict with an optional ``target`` override.
    It only applies when ``n.status == "awaiting_gate"``.

    Target resolution order: ``gate_notify.target`` (if a dict) ->
    ``workflow_notify["target"]`` -> ``config["notify_target"]``.
    Status-filter resolution order: ``workflow_notify["on"]`` ->
    ``config["notify_on"]`` -> ``["failed", "partial", "awaiting_gate"]``.
    """
    try:
        if config is None:
            try:
                from ..config import load_workflow_config

                config = load_workflow_config()
            except Exception as exc:
                logger.info("notify: could not load workflow config: %s", exc)
                config = {}
        config = config or {}

        # A gate's own notify:false suppresses only ITS park notification.
        if n.status == "awaiting_gate" and gate_notify is not None:
            gate_enabled = (
                gate_notify.get("notify", True) if isinstance(gate_notify, dict) else bool(gate_notify)
            )
            if not gate_enabled:
                return _record(n, {"delivered": False, "reason": "filtered"})

        # workflow_notify is False -> suppressed regardless of status.
        if workflow_notify is False:
            return _record(n, {"delivered": False, "reason": "filtered"})

        wf_dict = workflow_notify if isinstance(workflow_notify, dict) else {}

        status_filter = wf_dict.get("on") or config.get("notify_on") or list(_DEFAULT_NOTIFY_ON)
        if n.status not in status_filter:
            return _record(n, {"delivered": False, "reason": "filtered"})

        target = None
        if isinstance(gate_notify, dict):
            target = gate_notify.get("target")
        if not target:
            target = wf_dict.get("target")
        if not target:
            target = config.get("notify_target")

        if not target:
            logger.info("notify: no notify_target configured for run %s; skipping delivery", n.run_id)
            return _record(n, {"delivered": False, "reason": "no_target"})

        try:
            from tools.send_message_tool import send_message_tool
        except Exception as exc:
            logger.warning("notify: send_message_tool unavailable: %s", exc)
            return _record(n, {"delivered": False, "reason": f"import_error: {exc}"})

        try:
            raw = send_message_tool({"action": "send", "target": target, "message": n.summary_text()})
            result = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as exc:
            logger.warning("notify: send_message_tool raised: %s", exc)
            return _record(n, {"delivered": False, "reason": f"send_error: {exc}"})

        if isinstance(result, dict) and result.get("error"):
            logger.warning("notify: delivery reported an error: %s", result.get("error"))
            return _record(n, {"delivered": False, "reason": str(result.get("error"))})

        return _record(n, {"delivered": True, "target": target})
    except Exception as exc:  # belt and braces: notify() must never raise.
        logger.warning("notify: unexpected failure: %s", exc)
        try:
            events.notify_event(n.run_id, {"delivered": False, "reason": f"internal_error: {exc}"})
        except Exception:
            pass
        return {"delivered": False, "reason": f"internal_error: {exc}"}


_notifier: Callable[..., Dict[str, Any]] = notify


def set_notifier(fn: Optional[Callable[..., Dict[str, Any]]]) -> None:
    """Override the module-level default notifier (tests inject a recording
    fake here instead of monkeypatching imports). ``fn=None`` restores the
    real ``notify``."""
    global _notifier
    _notifier = fn or notify


def default_notifier() -> Callable[..., Dict[str, Any]]:
    """Return the current default notifier (resolved lazily so a
    ``set_notifier()`` call made after a Driver is constructed still takes
    effect on the next notification)."""
    return _notifier
