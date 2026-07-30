"""workflow/chain.py — trigger-chain helpers (Phase 3 TASK 3).

Small, mostly-pure helpers that let one run's final envelope seed another
run's input. Shared verbatim by both `hermes workflow chain` and the inline
`hermes workflow run --from-run` form (workflow/cli.py) so there is exactly
one implementation of "read a source run, derive an input dict" — no
duplicated logic between the two CLI surfaces.

Only `resolve_chain_input` touches the filesystem (via `workflow.status()`);
`select_path` and `build_chain_input` are pure functions, easy to unit test
without a HERMES_HOME.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = [
    "select_path",
    "build_chain_input",
    "resolve_chain_input",
    "ChainSourceNotTerminal",
    "DONE_STATUSES",
]

# Statuses whose final envelope is truly final and will not change further.
# `paused` and `awaiting_gate` are checkpointed-but-resumable: a later
# `resume` can still add to succeeded/failed/cost, so chaining off them
# risks freezing a snapshot the source run itself considers incomplete. This
# is intentionally a *different* (stricter) set than the `watch` command's
# terminal set in cli.py, which stops polling on `paused`/`awaiting_gate`
# too but for a different reason (nothing happens without external action,
# not that the data is final).
DONE_STATUSES = {"succeeded", "failed", "partial", "cancelled"}


class ChainSourceNotTerminal(Exception):
    """Raised when chaining off a source run that has not finished."""


def select_path(obj: Any, path: Optional[str]) -> Any:
    """Extract a dotted path (e.g. ``"a.b.2.c"``) out of ``obj``.

    Never raises: a missing dict key, an out-of-range/non-integer list
    index, or indexing through a scalar all resolve to ``None`` rather than
    raising — a chain should degrade to an absent value, not crash the
    target run before it even starts. An empty/None ``path`` returns
    ``obj`` unchanged (the "whole final envelope" default).
    """
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            try:
                idx = int(part)
            except ValueError:
                return None
            if idx < 0 or idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            return None
    return cur


def build_chain_input(
    source_envelope: Dict[str, Any],
    *,
    select: Optional[str] = None,
    as_key: str = "from_run",
    extra_input: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the derived input dict for a chained run (pure; no I/O).

    Always carries ``{source_run_id, source_workflow_id, source_status}``
    for provenance, plus ``{as_key: select_path(source_envelope, select)}``.
    ``extra_input`` (the caller's explicit ``--input``) is merged last and
    wins on key collision — documented CLI behaviour, not an accident of
    dict ordering.
    """
    key = as_key or "from_run"
    derived: Dict[str, Any] = {
        key: select_path(source_envelope, select),
        "source_run_id": source_envelope.get("run_id"),
        "source_workflow_id": source_envelope.get("workflow_id"),
        "source_status": source_envelope.get("status"),
    }
    if extra_input:
        derived.update(extra_input)
    return derived


def resolve_chain_input(
    source_run_id: str,
    *,
    select: Optional[str] = None,
    as_key: str = "from_run",
    extra_input: Optional[Dict[str, Any]] = None,
    allow_incomplete: bool = False,
) -> Dict[str, Any]:
    """Read SOURCE_RUN_ID's envelope and build the input for a chained run.

    Raises ``ChainSourceNotTerminal`` unless the source run's status is in
    ``DONE_STATUSES`` (or ``allow_incomplete=True``) — chaining off a
    half-finished run silently is a data-integrity trap, not a convenience.
    Raises ``FileNotFoundError`` (propagated from ``status()``) if the
    source run does not exist.
    """
    from . import status as _status

    src = _status(source_run_id)
    if src.get("status") not in DONE_STATUSES and not allow_incomplete:
        raise ChainSourceNotTerminal(
            f"source run {source_run_id!r} is '{src.get('status')}', not one of "
            f"{sorted(DONE_STATUSES)}. Pass --allow-incomplete to chain off it anyway."
        )
    return build_chain_input(src, select=select, as_key=as_key, extra_input=extra_input)
