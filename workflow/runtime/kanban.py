"""Best-effort kanban projection of run/node status (Phase 3 TASK 2).

THE KANBAN IS NEVER AN EXECUTION BUS. ``project_run()`` is a one-way,
best-effort mirror for human visibility only: the driver's control flow
(readiness, gating, retries, budget circuit-breaking, resume) must never
read from -- or depend on -- kanban state, and a kanban failure must never
affect a run. Every code path here returns a result dict and never raises,
mirroring ``workflow.runtime.notify``'s defensive contract exactly. Off by
default: with ``workflow.kanban_projection`` unset, ``project_run()`` is a
same-shape no-op that touches nothing.

## Why this ships as a documented stub, not a live board write

``tools/kanban_tools.py`` (the existing kanban tool surface -- see AGENTS.md's
footprint ladder: extending it is preferred over a new integration) only
exposes kanban mutation through *dispatchable work-item* semantics, not a
passive "card" concept:

* ``kanban_create`` requires an ``assignee`` and defaults
  ``initial_status="running"``: creating a card immediately queues it for
  the kanban dispatcher to spawn a live agent for that profile. That IS the
  kanban becoming an execution bus -- exactly what this module must never
  cause. Pinning ``initial_status="blocked"`` avoids the immediate spawn,
  but then the natural status-update path is closed: both ``kanban_complete``
  and ``kanban_block`` call into ``hermes_cli.kanban_db`` functions that
  validate the task actually has a live dispatcher run under it (e.g.
  ``kb.complete_task(..., expected_run_id=...)``) -- they close out a
  worker's OWN dispatched run, not relabel an external, never-dispatched
  card. A permanently-blocked card can only ever grow ``kanban_comment``
  text; its ``status`` field could never honestly reflect
  "succeeded"/"failed" for a workflow run.
* So there is no lever, through the existing registered tool handlers
  (``tools.registry.registry.dispatch("kanban_create"/"kanban_complete"/
  "kanban_block", ...)``), to drive a faithful one-card-per-run status
  mirror without either (a) actually letting the dispatcher execute a real
  agent for every workflow node transition (real spend, real side effects,
  exactly the "execution bus" outcome forbidden above), or (b) adding a new
  passive card/task kind to ``hermes_cli/kanban_db.py`` -- out of this
  task's file ownership ("sacred core").

Until a passive card kind exists upstream, ``project_run()`` is an honest,
``NotImplementedError``-free no-op: it returns
``{"projected": False, "reason": "kanban_tasks_are_dispatchable_work_items"}``
and never touches the real kanban board. It does NOT fake success.

Unit-testable without a live kanban via the same injection seam as
``notify.set_notifier()``: ``set_projector()`` lets a test swap in a
recording fake and assert the projection payload, independent of whether a
real projector ever ships.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from . import events

__all__ = [
    "project_run",
    "projection_enabled",
    "default_projector",
    "set_projector",
]

logger = logging.getLogger(__name__)


def _stub_projector(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Honest no-op projector -- see the module docstring for exactly what
    upstream capability is missing and what it would take to go live."""
    return {"projected": False, "reason": "kanban_tasks_are_dispatchable_work_items"}


_projector: Callable[[Dict[str, Any]], Dict[str, Any]] = _stub_projector


def set_projector(fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]) -> None:
    """Override the module-level projector (tests inject a recording fake
    here instead of monkeypatching imports). ``fn=None`` restores the
    honest stub."""
    global _projector
    _projector = fn or _stub_projector


def default_projector() -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Return the current default projector, resolved lazily (mirrors
    ``notify.default_notifier()``)."""
    return _projector


def projection_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """True when ``workflow.kanban_projection`` is on. Never raises -- a
    config-load failure reads as disabled (fail closed: silence, not a
    fabricated board write)."""
    try:
        if config is None:
            from ..config import load_workflow_config

            config = load_workflow_config()
        return bool((config or {}).get("kanban_projection", False))
    except Exception:
        logger.debug("kanban: projection_enabled config load failed", exc_info=True)
        return False


def _record(run_id: str, workflow_id: str, status: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Write the audit event (best-effort) and return the result unchanged.
    Mirrors ``notify._record`` -- every projection attempt is auditable via
    the existing run-level event log, delivered or not."""
    payload: Dict[str, Any] = dict(result)
    payload["event"] = "kanban"
    payload["run_id"] = run_id
    payload["workflow_id"] = workflow_id
    payload["status"] = status
    try:
        events.run_event(run_id, payload)
    except Exception:
        logger.debug("kanban: failed to write audit event", exc_info=True)
    return result


def project_run(
    run_id: str,
    workflow_id: str,
    status: str,
    *,
    succeeded: Optional[List[str]] = None,
    failed: Optional[List[str]] = None,
    skipped: Optional[List[str]] = None,
    cost_usd: float = 0.0,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Best-effort kanban projection hook. Never raises; never affects a
    run (see module docstring -- the kanban is NEVER an execution bus).

    Default OFF: with ``workflow.kanban_projection`` unset/false, this
    touches nothing and returns ``{"projected": False, "reason": "disabled"}``
    (still audited, so "did we mean to project and didn't" is visible in
    the event log same as an unconfigured notify target).

    When enabled, delegates the actual board write to the injected
    projector (``set_projector()`` / ``default_projector()``) with a plain
    dict payload -- see ``_stub_projector`` for the real-projector status.
    """
    try:
        if config is None:
            try:
                from ..config import load_workflow_config

                config = load_workflow_config()
            except Exception as exc:
                logger.info("kanban: could not load workflow config: %s", exc)
                config = {}
        config = config or {}

        if not bool(config.get("kanban_projection", False)):
            return _record(run_id, workflow_id, status, {"projected": False, "reason": "disabled"})

        payload = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "status": status,
            "succeeded": list(succeeded or []),
            "failed": list(failed or []),
            "skipped": list(skipped or []),
            "cost_usd": cost_usd,
            "board": config.get("kanban_board"),
        }

        try:
            result = _projector(payload)
        except Exception as exc:
            logger.warning("kanban: projector raised: %s", exc)
            return _record(run_id, workflow_id, status, {"projected": False, "reason": f"projector_error: {exc}"})

        if not isinstance(result, dict):
            result = {"projected": False, "reason": "invalid_projector_result"}
        return _record(run_id, workflow_id, status, result)
    except Exception as exc:  # belt and braces: project_run() must never raise.
        logger.warning("kanban: unexpected failure: %s", exc)
        try:
            events.run_event(
                run_id,
                {
                    "event": "kanban",
                    "workflow_id": workflow_id,
                    "status": status,
                    "projected": False,
                    "reason": f"internal_error: {exc}",
                },
            )
        except Exception:
            pass
        return {"projected": False, "reason": f"internal_error: {exc}"}
