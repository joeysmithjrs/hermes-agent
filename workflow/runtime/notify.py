"""Best-effort run/gate notifications (Phase 2 TASK 1; presets/templates
Phase 3 TASK 1).

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

Status-filter resolution order (documented in full on ``notify()``):
explicit ``on:`` list > ``preset:`` (workflow's own, else config's
``notify_preset``) > config ``notify_on`` > the built-in default. See
``NOTIFY_PRESETS`` for the shipped preset names.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import events

__all__ = [
    "Notification",
    "notify",
    "default_notifier",
    "set_notifier",
    "NOTIFY_PRESETS",
]

logger = logging.getLogger(__name__)

_DEFAULT_NOTIFY_ON = ("failed", "partial", "awaiting_gate")

# All statuses notify() ever fires for (gate park + every terminal status).
_ALL_NOTIFIABLE_STATUSES = ("succeeded", "failed", "partial", "paused", "awaiting_gate")

# Named shorthand for ``notify: {on: [...]}`` so an operator writes
# ``notify: {preset: gates_and_failures}`` instead of hand-listing statuses.
# Keep additions here in sync with any docs that enumerate preset names.
NOTIFY_PRESETS: Dict[str, Tuple[str, ...]] = {
    "all": _ALL_NOTIFIABLE_STATUSES,
    "failures": ("failed", "partial"),
    "gates": ("awaiting_gate",),
    "budget": ("paused",),
    "gates_and_failures": ("awaiting_gate", "failed", "partial"),
    "success": ("succeeded",),
}


class _DefaultingMap(dict):
    """``str.format_map`` mapping where an unknown placeholder renders as an
    empty string instead of raising ``KeyError``. A typo'd field name in an
    operator's custom ``template:`` must degrade the message, not the run."""

    def __missing__(self, key: str) -> str:
        return ""


@dataclass
class Notification:
    """One notification-worthy occurrence: a gate park, or a terminal run
    status (succeeded/failed/partial/paused).

    ``pause_reason`` / ``max_budget_usd`` are optional Phase 3 additions for
    the budget-pause message polish (below) — both default to ``None`` so
    the driver's existing construction call (which doesn't set them) keeps
    working unchanged. When the driver does thread them through for a
    ``status="paused"`` occurrence, ``effective_detail()`` fills in the
    cost/cap/resume-command sentence automatically unless ``detail`` was
    already set explicitly.
    """

    run_id: str
    workflow_id: str
    status: str
    gate_id: Optional[str] = None
    succeeded: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    cost_usd: float = 0.0
    detail: str = ""
    pause_reason: Optional[str] = None
    max_budget_usd: Optional[float] = None

    def effective_detail(self) -> str:
        """``detail`` if the caller set one explicitly; otherwise, for a
        budget pause, an auto-built sentence stating the cost, the cap (when
        known), and the exact resume command. Empty string for every other
        unset-detail case (unchanged Phase 2 behavior)."""
        if self.detail:
            return self.detail
        if self.status == "paused" and self.pause_reason == "BUDGET":
            resume_cmd = f"hermes workflow run --resume {self.run_id} --max-budget-usd <higher>"
            if self.max_budget_usd is not None:
                return (
                    f"cost ${self.cost_usd:.4f} exceeded the ${self.max_budget_usd:.4f} "
                    f"budget cap; resume with a higher cap: {resume_cmd}"
                )
            return (
                f"cost ${self.cost_usd:.4f} exceeded the configured budget cap; "
                f"resume with a higher cap: {resume_cmd}"
            )
        return ""

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
        detail = self.effective_detail()
        if detail:
            line += f" | {detail}"
        return line

    def render(self, template: Optional[str]) -> str:
        """Render ``template`` (an operator-supplied ``notify: {template:
        ...}`` string) against a fixed field set: ``{run_id} {workflow_id}
        {status} {gate_id} {ok} {fail} {skip} {cost_usd} {detail}``. No
        template (``None``/empty) reproduces ``summary_text()`` verbatim —
        existing behavior is unchanged unless an operator opts in.

        Uses ``str.format_map`` with a defaulting mapping so a typo'd or
        future field name renders empty instead of raising ``KeyError``.
        Any other formatting error (e.g. a bad format spec) is caught and
        falls back to ``summary_text()`` -- a broken custom template must
        degrade the message, never the run.
        """
        if not template:
            return self.summary_text()
        fields = _DefaultingMap(
            run_id=self.run_id,
            workflow_id=self.workflow_id,
            status=self.status,
            gate_id=self.gate_id or "",
            ok=len(self.succeeded),
            fail=len(self.failed),
            skip=len(self.skipped),
            cost_usd=f"{self.cost_usd:.4f}",
            detail=self.effective_detail(),
        )
        try:
            return template.format_map(fields)
        except Exception as exc:
            logger.warning("notify: template render failed (%s); using summary_text", exc)
            return self.summary_text()


def _resolve_status_filter(wf_dict: Dict[str, Any], config: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    """Resolve which statuses trigger delivery.

    Precedence: explicit workflow ``on:`` list > ``preset:`` (the
    workflow's own, else config's ``notify_preset``) > config
    ``notify_on`` > the built-in default (``_DEFAULT_NOTIFY_ON``).

    An unknown preset name never silently sends everything or nothing: it
    logs a warning and falls back to the next tier down (config
    ``notify_on``, else the built-in default). The fallback is recorded in
    the returned meta dict (``{"preset_unknown": name}``) so the caller can
    fold it into the audit event; a successfully-resolved preset instead
    records ``{"preset": name}``.
    """
    meta: Dict[str, Any] = {}

    explicit_on = wf_dict.get("on")
    if explicit_on:
        return list(explicit_on), meta

    preset_name = wf_dict.get("preset") or config.get("notify_preset")
    if preset_name:
        resolved = NOTIFY_PRESETS.get(preset_name)
        if resolved is not None:
            meta["preset"] = preset_name
            return list(resolved), meta
        logger.warning("notify: unknown preset '%s' -- falling back to notify_on/default", preset_name)
        meta["preset_unknown"] = preset_name

    config_on = config.get("notify_on")
    if config_on:
        return list(config_on), meta

    return list(_DEFAULT_NOTIFY_ON), meta


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

    Status-filter resolution order (see ``_resolve_status_filter``):
    ``workflow_notify["on"]`` (explicit list) -> ``workflow_notify["preset"]``
    or ``config["notify_preset"]`` (looked up in ``NOTIFY_PRESETS``) ->
    ``config["notify_on"]`` -> the built-in default
    ``["failed", "partial", "awaiting_gate"]``. An unknown preset name is
    never treated as "send everything" or "send nothing" -- it warns and
    falls back to the next tier, and the fallback is recorded on the audit
    event as ``preset_unknown``.

    ``workflow_notify["template"]`` (optional) renders the message via
    ``Notification.render()`` instead of the default ``summary_text()`` --
    see that method for the fixed field set and defaulting behavior.
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

        status_filter, filter_meta = _resolve_status_filter(wf_dict, config)
        if n.status not in status_filter:
            return _record(n, {"delivered": False, "reason": "filtered", **filter_meta})

        target = None
        if isinstance(gate_notify, dict):
            target = gate_notify.get("target")
        if not target:
            target = wf_dict.get("target")
        if not target:
            target = config.get("notify_target")

        if not target:
            logger.info("notify: no notify_target configured for run %s; skipping delivery", n.run_id)
            return _record(n, {"delivered": False, "reason": "no_target", **filter_meta})

        message = n.render(wf_dict.get("template"))

        try:
            from tools.send_message_tool import send_message_tool
        except Exception as exc:
            logger.warning("notify: send_message_tool unavailable: %s", exc)
            return _record(n, {"delivered": False, "reason": f"import_error: {exc}", **filter_meta})

        try:
            raw = send_message_tool({"action": "send", "target": target, "message": message})
            result = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as exc:
            logger.warning("notify: send_message_tool raised: %s", exc)
            return _record(n, {"delivered": False, "reason": f"send_error: {exc}", **filter_meta})

        if isinstance(result, dict) and result.get("error"):
            logger.warning("notify: delivery reported an error: %s", result.get("error"))
            return _record(n, {"delivered": False, "reason": str(result.get("error")), **filter_meta})

        return _record(n, {"delivered": True, "target": target, **filter_meta})
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
