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
    "build_lineage",
    "resolve_chain_input",
    "resolve_chain",
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


def build_lineage(
    source_envelope: Dict[str, Any],
    *,
    select: Optional[str] = None,
    as_key: str = "from_run",
) -> Dict[str, Any]:
    """The lineage record a chained/looped-back run carries (pure; no I/O).

    Post-Phase-3 §6: a backward route is a NEW run, never a graph cycle — the
    graph stays acyclic, previous artifacts stay intact, checkpoint durability
    is untouched. What a cycle WOULD have given for free is the answer to "how
    did we get here", so the new run records it explicitly: source run, its
    workflow and terminal status, and exactly which slice of its envelope was
    fed forward. Without the `select`/`as` pair the lineage would say a run was
    seeded from another without saying with what, which is the half of the
    provenance a reader actually needs when the loop misbehaves.

    Distinct from `build_chain_input`, which builds what the new run RECEIVES;
    this is what the new run RECORDS.
    """
    return {
        "run_id": source_envelope.get("run_id"),
        "workflow_id": source_envelope.get("workflow_id"),
        "status": source_envelope.get("status"),
        "select": select,
        "as": as_key or "from_run",
    }


def resolve_chain(
    source_run_id: str,
    *,
    select: Optional[str] = None,
    as_key: str = "from_run",
    extra_input: Optional[Dict[str, Any]] = None,
    allow_incomplete: bool = False,
) -> Dict[str, Any]:
    """Read SOURCE_RUN_ID once and return ``{"input": ..., "lineage": ...}``.

    One read, both artifacts: `resolve_chain_input` reads the source envelope to
    build the derived input, and the lineage has to describe that SAME envelope.
    Reading twice would let a source run change status between the two (it can:
    `--allow-incomplete` chains off a still-running run) and leave the recorded
    lineage describing a state the input never came from.
    """
    from . import status as _status

    src = _status(source_run_id)
    if src.get("status") not in DONE_STATUSES and not allow_incomplete:
        raise ChainSourceNotTerminal(
            f"source run {source_run_id!r} is '{src.get('status')}', not one of "
            f"{sorted(DONE_STATUSES)}. Pass --allow-incomplete to chain off it anyway."
        )
    return {
        "input": build_chain_input(src, select=select, as_key=as_key, extra_input=extra_input),
        "lineage": build_lineage(src, select=select, as_key=as_key),
        "source": src,
    }


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

    Thin wrapper over ``resolve_chain`` (which also returns the lineage) so the
    long-standing single-purpose entry point keeps its exact signature and
    return shape.
    """
    return resolve_chain(
        source_run_id,
        select=select,
        as_key=as_key,
        extra_input=extra_input,
        allow_incomplete=allow_incomplete,
    )["input"]
