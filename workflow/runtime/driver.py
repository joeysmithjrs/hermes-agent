"""Deterministic driver — ready-set walk over a VerifiedIR (design §4).

Control flow is code, not an LLM loop. The driver:
1. Loads verified IR + run record (or creates one).
2. Computes ready node-runs (upstreams succeeded, conditions satisfied).
3. Starts each ready node per kind (agent via worker, script via registry,
   fanout materializes branches, map materializes+runs+reduces branches in
   one node, join reduces, gate parks).
4. Writes envelope + event log, checkpoints, repeats until terminal.

Node bodies keyed by ``node_run_id`` (F1). Fanout/map branches each get a
unique node_run_id. side_effects: external nodes resume to failed
(INTERRUPTED), not requeued, and are never auto-retried by ``on_fail: retry``
(F6). Fanout/map over max_branches -> failed CARDINALITY, no overspawn (F5).

Phase 3 additions (§A): ``map`` = fanout + implicit join in one node (its own
output is the reduced value over its branches, see ``_run_fanout``); richer
``reduce:`` types ``first_k``/``majority``/``best`` (``workflow.runtime.
scripts``) alongside ``concat``/``top_k``, dispatched from one place
(``Driver._select_reducer``) shared by ``join`` and ``map``; per-node
``on_fail`` policies (``fail_run``/``skip_downstream``/``continue``/``retry``,
see ``Driver._apply_on_fail``); and real bounded concurrency for
``max_parallel_nodes > 1`` (``Driver._run_ready_batch``).
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..expr import TemplateError, eval_condition
from ..ir import DEFAULT_ON_FAIL, Node, VerifiedIR, WorkflowIR
from ..store import fs
from ..store import checkpoint as ckpt
from . import events
from . import scripts as script_registry
from .worker import FakeWorker, Worker

__all__ = ["Driver", "RunState", "run", "resume", "GATE_PARKED"]


GATE_PARKED = "awaiting_gate"


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_run_id() -> str:
    """run_id = uuid4-based (F17)."""
    return "wf_" + uuid.uuid4().hex[:12]


def _default_driver_worker() -> Worker:
    """Worker used when a caller constructs a Driver with ``worker=None``.

    Phase 2 invariant — **no silent FakeWorker**. Phase 1 defaulted to
    ``FakeWorker()`` here unconditionally, which meant any caller that forgot
    to pass a worker got canned "successes" for agent nodes that never ran an
    LLM, reported as ``status: succeeded`` and distinguishable from a real run
    only by squinting at the output text.

    This module's ``run()``/``resume()`` are the raw engine API: they are
    below the ``workflow.run()``/``resume()`` authorization boundary and do
    NOT check ``workflow.enabled``. So the two safe defaults here are "canned
    fake" (opted into via ``HERMES_WORKFLOW_FAKE``) or "refuse" — silently
    promoting an unauthorized caller to a live, billable LLM run would just
    trade one surprise for a more expensive one. Callers that want live
    execution pass an explicit worker, which is exactly what
    ``workflow.run()``/``resume()`` and the CLI do.
    """
    import os

    if os.environ.get("HERMES_WORKFLOW_FAKE"):
        return FakeWorker()
    raise RuntimeError(
        "Driver was constructed without a worker. The raw driver API does not "
        "pick one for you: a FakeWorker default would silently report canned "
        "successes for agent nodes that never ran, and a LiveWorker default "
        "would start a billable run below the workflow.enabled check. Pass an "
        "explicit worker=..., or call workflow.run()/workflow.resume() (which "
        "resolve one), or set HERMES_WORKFLOW_FAKE=1 for the no-LLM path."
    )


def _failed_key(nr: "NodeRun") -> str:
    """The id a node-run is recorded under in ``RunState.failed``.

    Review HIGH #5: fanout/map BRANCH node-runs share their parent's
    ``node_id``. The parent's own node-run is marked ``succeeded`` (it
    materialized its branches correctly) the moment it fans out, so a later
    branch failure appending the bare ``node_id`` put the SAME id in both
    ``succeeded`` and ``failed`` — two lists a caller is entitled to read as
    mutually exclusive per-node outcomes. Anything gating on
    ``"fan" not in env["failed"]`` got a wrong answer.

    Branches are therefore recorded branch-qualified (``fan#2``), which is
    both disjoint from the parent's entry and strictly more informative —
    you learn WHICH branch failed, not merely that one did.

    Note this list is a SUMMARY. Readiness/cascade decisions never consult
    it; they walk node-run statuses via ``node_runs_by_node`` (see
    ``_edge_state``), so changing the key here cannot alter control flow.
    """
    if nr.branch_index is not None:
        return f"{nr.parent_fanout or nr.node_id}#{nr.branch_index}"
    return nr.node_id


def _new_node_run_id(run_id: str, node_id: str) -> str:
    """Per-execution uuid4 (F1). Readable prefix + unique suffix."""
    return f"{run_id}__{node_id}__{uuid.uuid4().hex[:8]}"


def _validate_output_schema(spec_output: Dict[str, Any], output: Any) -> Optional[str]:
    """TASK 6: opt-in output schema validation for an agent/script node's
    result. ``spec_output`` is ``NodeSpec.output`` -- a JSON Schema dict (or
    ``{"$ref": ...}``). Returns None when the output matches (or there is
    nothing meaningful to check), else a short human-readable mismatch
    message.

    Uses the ``jsonschema`` package when importable for a real schema check;
    otherwise falls back to a minimal structural check (top-level "type", and
    "required" key presence for object schemas). Never hard-depends on a new
    package -- absence of `jsonschema` degrades the check, it doesn't disable
    the feature or raise ImportError.
    """
    if not isinstance(spec_output, dict) or not spec_output:
        return None
    if set(spec_output.keys()) == {"$ref"}:
        return None  # unresolved $ref-only schema: nothing to check here
    try:
        import jsonschema  # type: ignore
    except Exception:
        jsonschema = None  # type: ignore

    if jsonschema is not None:
        try:
            jsonschema.validate(instance=output, schema=spec_output)
            return None
        except jsonschema.exceptions.ValidationError as exc:  # type: ignore[attr-defined]
            return str(getattr(exc, "message", exc))
        except Exception as exc:
            return f"schema validation error: {exc}"

    expected_type = spec_output.get("type")
    if expected_type == "object":
        if not isinstance(output, dict):
            return f"expected object output, got {type(output).__name__}"
        for req in spec_output.get("required") or []:
            if req not in output:
                return f"missing required field '{req}'"
    elif expected_type == "array":
        if not isinstance(output, list):
            return f"expected array output, got {type(output).__name__}"
    elif expected_type == "string":
        if not isinstance(output, str):
            return f"expected string output, got {type(output).__name__}"
    elif expected_type == "boolean":
        if not isinstance(output, bool):
            return f"expected boolean output, got {type(output).__name__}"
    elif expected_type == "integer":
        if not isinstance(output, int) or isinstance(output, bool):
            return f"expected integer output, got {type(output).__name__}"
    elif expected_type == "number":
        if not isinstance(output, (int, float)) or isinstance(output, bool):
            return f"expected number output, got {type(output).__name__}"
    return None


@dataclass
class NodeRun:
    """A single execution of a node (one per fanout branch, too)."""

    node_run_id: str
    node_id: str
    kind: str
    status: str = "pending"
    attempt: int = 1
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    cost_usd: float = 0.0
    # TASK 5: per-node-run token rollup. Default 0 so a pre-Phase-3
    # checkpoint (no such keys on disk) still loads via `.get(..., 0)`.
    tokens_in: int = 0
    tokens_out: int = 0
    port: Optional[str] = None
    output: Any = None
    error: Optional[Dict[str, Any]] = None
    # for fanout branches: the synthetic leaf template, item, and parent id.
    # All three are checkpointed so a crash can resume a branch without
    # re-entering (and re-materializing) its parent fanout.
    branch_index: Optional[int] = None
    branch_node: Optional[Node] = None
    branch_item: Any = None
    parent_fanout: Optional[str] = None
    # which node-runs this join is waiting on
    waiting_on: Optional[List[str]] = None
    # Phase 2 TASK 7: the LiveWorker's actually-used model/provider (after
    # any inherit/override resolution). None for FakeWorker / non-agent
    # kinds — absence is a silent no-op, never required.
    effective_model: Optional[str] = None
    effective_provider: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_run_id": self.node_run_id,
            "node_id": self.node_id,
            "kind": self.kind,
            "status": self.status,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cost_usd": self.cost_usd,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "port": self.port,
            "output": self.output,
            "error": self.error,
            "branch_index": self.branch_index,
            "branch_node": self.branch_node.to_dict() if self.branch_node is not None else None,
            "branch_item": self.branch_item,
            "parent_fanout": self.parent_fanout,
            "waiting_on": self.waiting_on,
            "effective_model": self.effective_model,
            "effective_provider": self.effective_provider,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NodeRun":
        return cls(
            node_run_id=d["node_run_id"],
            node_id=d["node_id"],
            kind=d["kind"],
            status=d.get("status", "pending"),
            attempt=d.get("attempt", 1),
            started_at=d.get("started_at"),
            ended_at=d.get("ended_at"),
            cost_usd=d.get("cost_usd", 0.0),
            tokens_in=d.get("tokens_in", 0),
            tokens_out=d.get("tokens_out", 0),
            port=d.get("port"),
            output=d.get("output"),
            error=d.get("error"),
            branch_index=d.get("branch_index"),
            branch_node=Node.from_dict(d["branch_node"]) if d.get("branch_node") else None,
            branch_item=d.get("branch_item"),
            parent_fanout=d.get("parent_fanout"),
            waiting_on=d.get("waiting_on"),
            effective_model=d.get("effective_model"),
            effective_provider=d.get("effective_provider"),
        )


@dataclass
class RunState:
    """The mutable state of a run (persisted to checkpoint)."""

    run_id: str
    workflow_id: str
    attempt_id: int = 1
    status: str = "running"
    node_runs: Dict[str, NodeRun] = field(default_factory=dict)  # node_run_id -> NodeRun
    # node_id -> list of node_run_ids produced by it (1 for normal, N for fanout branches)
    node_runs_by_node: Dict[str, List[str]] = field(default_factory=dict)
    succeeded: List[str] = field(default_factory=list)  # node_ids
    failed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    awaiting_gate: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    cost_usd: float = 0.0
    # TASK 5: run-level token totals -- the sum of every LEAF node-run's
    # tokens (agent/script/branch), counted exactly once each. A fanout/map
    # node's own rolled-up tokens_in/out (its subtree total, for display) are
    # NOT added here a second time -- see Driver._finish_top_level.
    tokens_in: int = 0
    tokens_out: int = 0
    # Phase 2 TASK 5: why status == "paused" (currently only "BUDGET").
    # Defaults to None so pre-Phase-2 checkpoints still load.
    pause_reason: Optional[str] = None
    # Terminal-status fingerprint of the last notification actually sent,
    # so repeated resumes of an already-terminal run do not re-notify.
    notified_fingerprint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "attempt_id": self.attempt_id,
            "status": self.status,
            "node_runs": {nr: nr_obj.to_dict() for nr, nr_obj in self.node_runs.items()},
            "node_runs_by_node": self.node_runs_by_node,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "awaiting_gate": self.awaiting_gate,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cost_usd": self.cost_usd,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "pause_reason": self.pause_reason,
            "notified_fingerprint": self.notified_fingerprint,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunState":
        s = cls(
            run_id=d["run_id"],
            workflow_id=d["workflow_id"],
            attempt_id=d.get("attempt_id", 1),
            status=d.get("status", "running"),
            started_at=d.get("started_at"),
            ended_at=d.get("ended_at"),
            cost_usd=d.get("cost_usd", 0.0),
            tokens_in=d.get("tokens_in", 0),
            tokens_out=d.get("tokens_out", 0),
            awaiting_gate=d.get("awaiting_gate"),
            succeeded=list(d.get("succeeded") or []),
            failed=list(d.get("failed") or []),
            skipped=list(d.get("skipped") or []),
            node_runs_by_node={k: list(v) for k, v in (d.get("node_runs_by_node") or {}).items()},
            pause_reason=d.get("pause_reason"),
            notified_fingerprint=d.get("notified_fingerprint"),
        )
        for nr_id, nrd in (d.get("node_runs") or {}).items():
            s.node_runs[nr_id] = NodeRun.from_dict(nrd)
        return s


class Driver:
    """Deterministic ready-set walker."""

    def __init__(
        self,
        vir: VerifiedIR,
        *,
        worker: Optional[Worker] = None,
        run_id: Optional[str] = None,
        input: Optional[Dict[str, Any]] = None,
        max_parallel_nodes: int = 4,
        max_budget_usd: float = 10.0,
        notifier: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> None:
        self.vir = vir
        self.ir: WorkflowIR = vir.ir
        # Resolved lazily (see the `worker` property): a --dry-run plans the
        # ready set without ever executing a node, so it must not need — or
        # fail on the absence of — a worker.
        self._worker: Optional[Worker] = worker
        self.run_id = run_id or _new_run_id()
        self.input = input or {}
        # Phase 2 TASK 3: explicit override for tests; otherwise resolved
        # lazily (per-call, via `notify.default_notifier()`) so a test's
        # `set_notifier()` made after this Driver was constructed still
        # takes effect. See `_fire_notify`.
        self.notifier = notifier
        # TASK 4: real bounded concurrency. <= 1 takes the Phase 1/2
        # strictly-sequential code path verbatim (see `execute()`); > 1
        # dispatches pool-eligible node-runs (top-level agent/script, and
        # any fanout/map branch) through a
        # `concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel_nodes)`.
        # fanout/map/join/gate node-runs always run on the main thread (see
        # `_run_ready_batch`'s docstring for why that's a hard rule, not
        # just an optimization).
        self.max_parallel_nodes = max_parallel_nodes
        self.max_budget_usd = max_budget_usd
        self.nodes: Dict[str, Node] = {n.id: n for n in self.ir.nodes}
        self.edges = self.ir.edges
        # adjacency
        self.outgoing: Dict[str, List] = {}
        self.incoming: Dict[str, List] = {}
        for e in self.edges:
            self.outgoing.setdefault(e.from_, []).append(e)
            self.incoming.setdefault(e.to, []).append(e)
        self.state: RunState = RunState(run_id=self.run_id, workflow_id=self.ir.id, started_at=_ts())
        # transient branch items (not persisted): node_run_id -> branch item
        self._branch_items: Dict[str, Any] = {}
        # Resolved once per execute() from ir.workspace (see _ensure_workspace).
        self._workspace_ctx: Optional[Dict[str, Any]] = None
        # TASK 4: guards mutation of RunState aggregates (cost_usd,
        # tokens_in/out, succeeded/failed) from `_finish_top_level`/
        # `_finish_branch` -- both are only ever called from the main
        # thread in this Driver's own design, but the lock is cheap
        # insurance against a future call site forgetting that invariant.
        self._agg_lock = threading.Lock()
        # TASK 3 `on_fail: fail_run`: set True the instant a node-run's
        # effective policy is fail_run and it fails. Transient (not
        # checkpointed) -- the checkpointed signal is `state.status ==
        # "failed"`, set directly by `_abort_run`; this flag only tells
        # THIS execute() call's dispatch loop to stop handing out further
        # ready-set batches.
        self._aborted = False

    @property
    def worker(self) -> Worker:
        """The node executor, resolved on first use.

        Lazy so that ``execute(dry_run=True)`` — which returns the plan before
        the walk — never constructs (or fails to construct) a worker.
        """
        if self._worker is None:
            self._worker = _default_driver_worker()
        return self._worker

    @worker.setter
    def worker(self, value: Worker) -> None:
        self._worker = value

    # ---- public -----------------------------------------------------------

    def execute(self, *, dry_run: bool = False, retry_failed: bool = False, from_node: Optional[str] = None) -> Dict[str, Any]:
        """Run to completion (or until a gate parks). Returns a RunEnvelope dict."""
        fs.ensure_run_dirs(self.run_id)
        self._ensure_workspace(dry_run=dry_run)
        if not dry_run:
            ckpt.write_run_record(self.run_id, self._run_record_dict())
            from ..store import index
            index.upsert_run(self.run_id, self.ir.id, self.state.status, started=self.state.started_at, cost_usd=0.0)

        if dry_run:
            # compile + plan ready set without spawning
            ready = self._compute_ready()
            return {
                "run_id": self.run_id,
                "workflow_id": self.ir.id,
                "status": "dry_run",
                "ready": [r.node_id for r in ready],
                "node_runs": len(self.state.node_runs),
            }

        # initial ready set
        self._seed_initial_node_runs()
        if from_node:
            self._force_ready(from_node, retry_failed=retry_failed)

        # main loop
        while True:
            self._check_budget()
            if self.state.status == "paused":
                break
            ready = self._compute_ready()
            if not ready:
                break

            if self.max_parallel_nodes <= 1:
                # TASK 4: max_parallel_nodes <= 1 takes this strictly
                # sequential path VERBATIM (unchanged since Phase 1) --
                # never routed through the thread pool, so its behavior
                # (ordering, checkpoint count, timing) is byte-for-byte the
                # same as before this task existed.
                budget_tripped = False
                for nr in ready:
                    self._run_node_run(nr)
                    self._checkpoint()
                    # TASK 5: cost accrues mid-loop (each node-run adds to
                    # state.cost_usd), so re-check after every node-run too, not
                    # just at the top of the loop — otherwise a batch of ready
                    # nodes could all start before the trip is ever observed.
                    self._check_budget()
                    if self.state.status == "paused":
                        # Persist the pause immediately. Without this the last
                        # checkpoint still says "running" with cost already over
                        # budget, so a crash in the window before _finalize()
                        # leaves the on-disk record under-reporting the state.
                        self._checkpoint()
                        budget_tripped = True
                        break
                    if self._aborted:
                        # TASK 3 on_fail: fail_run — stop dispatching further
                        # ready node-runs THIS batch. Nothing after this point
                        # has been started yet, so "stop" is exact here (unlike
                        # the concurrent path, where a batch's pool items may
                        # already be in flight by the time the abort is seen).
                        break
                if budget_tripped or self._aborted:
                    break
            else:
                # TASK 4: bounded concurrency for pool-eligible node-runs.
                budget_tripped = self._run_ready_batch(ready)
                if budget_tripped or self._aborted:
                    break

            # a gate parks the run -> stop
            if self.state.status == GATE_PARKED:
                break

        self._finalize()
        return self._run_envelope()

    # ---- ready-set computation -------------------------------------------

    def _seed_initial_node_runs(self) -> None:
        """Create a NodeRun for every IR node that has no upstream requirement.

        Review (test pass) BUG #2: "no upstream" means no incoming EDGE *and*
        no `from:` list. The verifier accepts a `join` declared with `from:`
        and no matching `edges:` entry (its check is `if not n.from_ and not
        from_edges`), and this seeding used to look at `self.edges` alone — so
        such a join was misclassified as an entry node and pre-seeded
        `pending` at time zero. `_propagate_skips` then refused to touch it
        ("already has a node-run"), so when its upstreams failed or skipped it
        sat `pending` forever while the run itself went terminal: a node stuck
        in a non-terminal state, which is the one outcome the failed-upstream
        cascade exists to prevent.

        Nodes with `from:` are simply not seeded here; the normal ready-set
        walk creates their node-run once `_resolve_upstreams` says `ready`,
        and `_propagate_skips` can cascade them to `skipped` otherwise.
        """
        has_incoming = {e.to for e in self.edges}
        for n in self.ir.nodes:
            if n.id not in has_incoming and not n.from_:
                self._ensure_node_run(n.id)

    def _ensure_node_run(self, node_id: str, *, branch_index: Optional[int] = None, branch_node: Optional[Node] = None) -> NodeRun:
        """Create (or reuse) a NodeRun for a node. For fanout branches, always new."""
        if branch_index is not None or branch_node is not None:
            # always a fresh node_run for a branch (F1)
            nrid = _new_node_run_id(self.run_id, f"{node_id}#{branch_index}")
            nr = NodeRun(node_run_id=nrid, node_id=node_id, kind=(branch_node.kind if branch_node else self.nodes[node_id].kind), branch_index=branch_index, branch_node=branch_node)
            self.state.node_runs[nrid] = nr
            self.state.node_runs_by_node.setdefault(node_id, []).append(nrid)
            return nr
        existing = self.state.node_runs_by_node.get(node_id, [])
        if existing:
            return self.state.node_runs[existing[0]]
        n = self.nodes[node_id]
        nrid = _new_node_run_id(self.run_id, node_id)
        nr = NodeRun(node_run_id=nrid, node_id=node_id, kind=n.kind)
        self.state.node_runs[nrid] = nr
        self.state.node_runs_by_node.setdefault(node_id, []).append(nrid)
        return nr

    def _compute_ready(self) -> List[NodeRun]:
        """Return NodeRuns whose upstreams all succeeded and conditions hold.

        Conditionally-pruned nodes cascade to `skipped` first, to a fixed
        point (a whole downstream chain resolves in one call regardless of
        node declaration order — F3.2), before the ready set is collected.
        """
        self._propagate_skips()
        ready: List[NodeRun] = []
        for node_id, n in self.nodes.items():
            # gate nodes: seed a node-run when upstreams done, then run (parks).
            if n.kind == "gate":
                existing = self.state.node_runs_by_node.get(node_id, [])
                if not existing:
                    if self._node_ready(node_id):
                        nr = self._ensure_node_run(node_id)
                        if nr.status == "pending":
                            ready.append(nr)
                    continue
                nr = self.state.node_runs[existing[0]]
                if nr.status == "pending" and self._node_ready(node_id):
                    ready.append(nr)
                continue
            # normal node: ensure a node-run exists, check readiness
            existing = self.state.node_runs_by_node.get(node_id, [])
            if not existing:
                # fanout branches create their own node-runs at materialization;
                # all node kinds need a node-run seeded once upstreams are done
                if self._node_ready(node_id):
                    nr = self._ensure_node_run(node_id)
                    if nr.status == "pending":
                        ready.append(nr)
                continue
            for nrid in existing:
                nr = self.state.node_runs[nrid]
                if nr.status == "pending" and self._node_ready(node_id):
                    ready.append(nr)
        return ready

    def _node_ready(self, node_id: str) -> bool:
        return self._resolve_upstreams(node_id) == "ready"

    def _propagate_skips(self) -> None:
        """Mark nodes `skipped` whose upstream requirement can never be
        satisfied (a false edge condition, or an upstream that was itself
        skipped) — to a fixed point, so a multi-hop skip chain resolves in
        one pass regardless of node declaration order (F3.2)."""
        any_changed = False
        changed = True
        while changed:
            changed = False
            for node_id in self.nodes:
                if self.state.node_runs_by_node.get(node_id):
                    continue  # already has a node-run (ran, running, or skipped)
                if self._resolve_upstreams(node_id) == "skip":
                    self._mark_skipped(node_id)
                    changed = True
                    any_changed = True
        if any_changed:
            self._checkpoint()

    def _mark_skipped(self, node_id: str) -> None:
        n = self.nodes[node_id]
        nr = self._ensure_node_run(node_id)
        nr.status = "skipped"
        nr.started_at = _ts()
        nr.ended_at = nr.started_at
        # TASK 4: record *why* this node was auto-skipped -- a failed/skipped
        # upstream cascade vs. a false edge condition -- since "skipped"
        # alone doesn't distinguish "never ran because upstream failed" from
        # "deliberately pruned by a condition:".
        nr.output = self._skip_reason(node_id)
        events.start_event(self.run_id, nr.node_run_id, node_id, n.kind)
        events.end_event(self.run_id, nr.node_run_id, node_id, "skipped")
        if node_id not in self.state.skipped:
            self.state.skipped.append(node_id)

    def _skip_reason(self, node_id: str) -> Dict[str, Any]:
        """Best-effort explanation for an auto-skip: which upstream failed or
        was itself skipped, else a false edge condition (TASK 4)."""
        n = self.nodes[node_id]
        upstream_ids = n.from_ if n.from_ else [e.from_ for e in self.incoming.get(node_id, [])]
        for up in upstream_ids:
            for nrid in self.state.node_runs_by_node.get(up, []):
                st = self.state.node_runs[nrid].status
                if st == "failed":
                    return {"skipped_reason": "upstream_failed", "upstream": up}
                if st == "skipped":
                    return {"skipped_reason": "upstream_skipped", "upstream": up}
        return {"skipped_reason": "condition_false"}

    def _resolve_upstreams(self, node_id: str) -> str:
        """Resolve a node's upstream requirement to 'ready' (may run now),
        'waiting' (still pending on an in-flight upstream), or 'skip'
        (an upstream is terminal in a way that can never satisfy it).

        Two upstream mechanisms exist: `from:` (join fan-in list — a plain
        list of node ids, no per-edge conditions) and plain incoming
        `edges:` (each may carry `condition:`). The verifier does not forbid
        a node from declaring both; pre-existing driver behavior (unchanged
        by this fix) prefers `from:` when present and ignores incoming edges
        entirely for readiness in that case — so a `condition:` on an edge
        into a node that also has `from:` is silently inert. That is a
        pre-existing constraint of the two mechanisms, not new in F3.1/F3.2.
        """
        n = self.nodes[node_id]
        if n.from_:
            return self._resolve_from_upstreams(n.from_)
        incoming = self.incoming.get(node_id, [])
        if not incoming:
            return "ready"  # entry node — no upstream requirement
        return self._resolve_edge_upstreams(incoming)

    def _resolve_from_upstreams(self, upstream_ids: List[str]) -> str:
        waiting = False
        for up in upstream_ids:
            runs = self.state.node_runs_by_node.get(up, [])
            if not runs:
                waiting = True
                continue
            for nrid in runs:
                st = self.state.node_runs[nrid].status
                if st == "failed":
                    # Phase 3 §A on_fail: `continue` — a failed upstream
                    # with this policy never produces output, but must NOT
                    # block a `from:` join either. Treat it as resolved
                    # (neither blocking nor pending) and move on to the
                    # next upstream. Note `_run_join` does NOT then reduce it:
                    # envelope collection keeps only `succeeded` upstreams
                    # (review HIGH #4), so `continue` unblocks the join
                    # without injecting a null "result" into the reduction.
                    up_node = self.nodes.get(up)
                    if up_node is not None and up_node.fail_policy == "continue":
                        continue
                    # TASK 4 (Phase 2) failed-upstream cascade: every other
                    # policy (skip_downstream default, fail_run, retry-not-
                    # yet-exhausted is still `pending` not `failed` here) —
                    # a `failed` upstream in a `from:` join list never
                    # produces output either — same policy as `skipped`
                    # (see _edge_state for the `edges:` twin of this rule,
                    # and the module docstring on --retry-failed).
                    return "skip"
                if st == "skipped":
                    return "skip"
                if st != "succeeded":
                    waiting = True
        return "waiting" if waiting else "ready"

    def _resolve_edge_upstreams(self, edges: List) -> str:
        """AND across a node's incoming edges (matches prior _upstreams_done
        semantics for the unconditional case): one permanently-blocked edge
        dooms the whole node to 'skip', even if other edges are still pending."""
        waiting = False
        for e in edges:
            state = self._edge_state(e)
            if state == "blocked":
                return "skip"
            if state == "pending":
                waiting = True
        return "waiting" if waiting else "ready"

    def _edge_state(self, edge) -> str:
        """'satisfied' | 'blocked' | 'pending' for one incoming edge."""
        runs = self.state.node_runs_by_node.get(edge.from_, [])
        if not runs:
            return "pending"
        primary = self.state.node_runs[runs[0]]
        if primary.status == "skipped":
            return "blocked"
        if primary.status == "failed":
            # Phase 3 §A on_fail: `continue` — this upstream's failure must
            # NOT block downstream. There is no successful output to
            # template against (the node never produced one), so a
            # `condition:` on THIS edge can't be evaluated -- fail closed
            # (blocked) rather than silently treating an unevaluable
            # condition as true. An unconditional edge (no `condition:`) is
            # fully satisfied.
            upstream_node = self.nodes.get(edge.from_)
            if upstream_node is not None and upstream_node.fail_policy == "continue":
                return "blocked" if edge.condition is not None else "satisfied"
            # TASK 4 (Phase 2) failed-upstream cascade (Phase 1 debt): a
            # `failed` upstream never produces output, so a downstream node
            # waiting on it can never become ready either. Policy (every
            # OTHER on_fail value): treat it exactly like a skipped
            # upstream -> "blocked", which _resolve_edge_upstreams turns
            # into "skip", and _propagate_skips marks the downstream node
            # `skipped` (with a recorded reason) rather than `failed` -- it
            # never ran, so `failed` would misrepresent it. This cascades
            # transitively through _propagate_skips's fixed-point loop.
            #
            # `--retry-failed` on resume() resets a failed node-run back to
            # `pending`; resume() also walks forward from every reset node id
            # (Driver._cascade_reset_skipped_downstream) and resets any
            # node-run that a PRIOR attempt marked `skipped` because of this
            # failure back to `pending`, so it becomes ready again instead of
            # staying stuck skipped forever.
            return "blocked"
        if primary.status != "succeeded":
            return "pending"
        # a fanout upstream referenced directly by a plain edge (not `from:`):
        # every spawned branch must also be terminal before the edge counts.
        for nrid in runs[1:]:
            if self.state.node_runs[nrid].status != "succeeded":
                return "pending"
        if edge.condition is None:
            return "satisfied"
        try:
            ok = eval_condition(edge.condition, primary.output)
        except TemplateError:
            # unparseable at runtime (should already be caught at compile
            # time by verify.py's F3.3 check) — fail closed, same posture
            # as eval_condition's own docstring.
            return "blocked"
        return "satisfied" if ok else "blocked"

    # ---- node execution ---------------------------------------------------

    def _run_node_run(self, nr: NodeRun) -> None:
        """Sequential entry point: max_parallel_nodes <= 1's main loop, and
        (via `_run_branches`) the sequential fanout/map branch path. A
        branch node-run — including one resurfaced `pending` by an
        `on_fail: retry` reset (TASK 3), which `_compute_ready` will hand
        back here through the normal ready-set walk — always dispatches
        through its persisted branch leaf, never by re-entering the parent
        fanout.
        """
        if nr.branch_index is not None:
            self._run_one_branch(nr)
            return

        node = self.nodes.get(nr.node_id)
        if node is None and nr.branch_node is not None:
            node = nr.branch_node
        if node is None:
            nr.status = "failed"
            nr.error = {"code": "VERIFY", "message": f"unknown node {nr.node_id}", "retriable": False}
            if nr.node_id not in self.state.failed:
                self.state.failed.append(nr.node_id)
            return

        self._mark_top_level_running(nr, node)
        self._checkpoint()
        self._top_level_compute(nr, node)
        self._finish_top_level(nr, node)

    # ---- TASK 4: mark / compute / finish primitives ------------------------
    #
    # Every node-run's lifecycle is split into three phases so the same
    # pieces serve both the sequential path (`_run_node_run` above) and the
    # bounded-concurrency batch path (`_run_ready_batch` below) without
    # duplicating logic:
    #   mark    -- status="running" + started_at + start event. Cheap, must
    #              happen for the WHOLE batch before any future is submitted
    #              (checkpoint-before-dispatch).
    #   compute -- the actual work (worker call / script call / fanout
    #              materialize+reduce / join reduce / gate park). Mutates
    #              ONLY the node-run's own `nr` fields (plus, for
    #              fanout/map/join/gate, `self.state.node_runs`/
    #              `node_runs_by_node`/`status` directly -- those kinds
    #              never run inside a worker thread, see
    #              `_run_ready_batch`). Never raises: any exception is
    #              caught into nr.status/nr.error.
    #   finish  -- end event, RunState aggregate mutation (cost/tokens/
    #              succeeded/failed), output storage, on_fail dispatch.
    #              Main-thread only, always.

    def _mark_top_level_running(self, nr: NodeRun, node: Node) -> None:
        nr.status = "running"
        nr.started_at = _ts()
        events.start_event(self.run_id, nr.node_run_id, nr.node_id, node.kind)

    def _top_level_compute(self, nr: NodeRun, node: Node) -> None:
        try:
            if node.kind == "agent":
                self._run_agent(nr, node)
            elif node.kind == "script":
                self._run_script(nr, node)
            elif node.kind in ("fanout", "map"):
                self._run_fanout(nr, node)
            elif node.kind == "join":
                self._run_join(nr, node)
            elif node.kind == "gate":
                self._run_gate(nr, node)
            else:
                # triggers are lifecycle, not executed by the driver loop
                nr.status = "succeeded"
                nr.output = {"kind": node.kind}
        except Exception as exc:
            nr.status = "failed"
            nr.error = {"code": "AGENT", "message": str(exc), "retriable": True}

    def _finish_top_level(self, nr: NodeRun, node: Node) -> None:
        nr.ended_at = _ts()
        with self._agg_lock:
            if node.kind not in ("fanout", "map"):
                # TASK 5: a fanout/map node's own cost/tokens are a ROLLUP
                # over its branches, computed in `_run_fanout` for display
                # (so `{{ mynode.output }}`'s envelope reports true subtree
                # cost) -- NOT an additional contribution to the run-level
                # total. Each branch already added its own cost/tokens
                # exactly once via `_finish_branch`; adding the fanout/map
                # node's rolled-up total here too would double-count it.
                # Every other kind (agent/script/join/gate) is the SOLE
                # contributor of its own cost, so it's folded in exactly
                # once, right here.
                self.state.cost_usd += nr.cost_usd
                self.state.tokens_in += nr.tokens_in
                self.state.tokens_out += nr.tokens_out
            if nr.status == "succeeded" and node.id not in self.state.succeeded:
                self.state.succeeded.append(node.id)
            elif nr.status == "failed" and node.id not in self.state.failed:
                self.state.failed.append(node.id)
        if nr.cost_usd:
            events.cost_event(self.run_id, nr.node_run_id, nr.cost_usd)
        events.end_event(
            self.run_id,
            nr.node_run_id,
            nr.node_id,
            nr.status,
            effective_model=nr.effective_model,
            effective_provider=nr.effective_provider,
        )
        # store output (F1: keyed by node_run_id)
        if nr.output is not None:
            out_path = fs.store_node_output(self.run_id, nr.node_run_id, nr.output)
            events.emit(self.run_id, nr.node_run_id, {"event": "stored", "path": str(out_path.relative_to(fs.workflows_root()))})
        if nr.status == "failed":
            self._apply_on_fail(nr, node)

    def _ensure_workspace(self, *, dry_run: bool = False) -> None:
        """Materialize this run's corner of the workflow's named workspace.

        A ``--dry-run` plans without executing, so it must not create
        directories either -- it only resolves the ctx block (which is
        path/name data, no I/O beyond a listing of what already exists).
        Workspace creation is idempotent: re-entering an existing workspace on
        a resume is the normal case and never clears anything, which is what
        makes cross-run seeding work.
        """
        self._workspace_ctx = None
        name = getattr(self.ir, "workspace", None)
        if not name:
            return
        from ..store import workspace as ws

        if not dry_run:
            ws.ensure_workspace(name, self.run_id)
        self._workspace_ctx = ws.workspace_context(name, self.run_id)

    def _build_ctx(self, node: Node) -> Dict[str, Any]:
        """Build the template ctx from succeeded upstream outputs + run input."""
        ctx: Dict[str, Any] = {"input": self.input}
        # Named workspace (post-Phase-3): paths + names only, never file
        # bodies, so `{{ workspace.dir }}` renders a path an agent then reads
        # with its ordinary file tools at run time. Absent when the workflow
        # pins no workspace -- the verifier rejects `{{ workspace.* }}` in that
        # case, so a missing root here is never reached by a verified graph.
        if getattr(self, "_workspace_ctx", None):
            ctx["workspace"] = dict(self._workspace_ctx)
        # node outputs (envelope-shaped: {output: ...})
        #
        # Review (test pass) BUG #1: BRANCH node-runs are excluded. Every
        # fanout/map branch is created via `_ensure_node_run(node_id,
        # branch_index=...)`, so it shares its PARENT's node_id — and this
        # loop used to overwrite ctx[node_id] once per node-run, letting the
        # last branch processed win. A downstream node writing
        # `{{ mymap.output }}` therefore saw the last branch's raw output
        # instead of the map's reduced value, which is precisely the thing
        # map sugar exists to provide. (For a `fanout` it clobbered the
        # `{branches, count}` summary just as wrongly.)
        #
        # A branch's own data reaches its own template through `ctx["branch"]`
        # (set in `_run_one_branch`), never through the parent id, so nothing
        # legitimately depended on the shadowing.
        for nid, nrids in self.state.node_runs_by_node.items():
            for nrid in nrids:
                nr = self.state.node_runs[nrid]
                if nr.branch_index is not None:
                    continue
                if nr.status == "succeeded" and nr.output is not None:
                    ctx[nid] = {"output": nr.output, "node_run_id": nrid}
        # render node input mapping if present
        rendered_input: Dict[str, Any] = {}
        if node.spec and node.spec.input:
            from ..expr import render
            for k, v in node.spec.input.items():
                rendered_input[k] = render(v, ctx)
        ctx["input_for_node"] = rendered_input
        return ctx

    def _run_agent(self, nr: NodeRun, node: Node) -> None:
        """Compute-only (TASK 4): safe to call from a worker thread —
        mutates only `nr`'s own fields. RunState aggregate mutation
        (cost/tokens/succeeded/failed) happens later, on the main thread,
        in `_finish_top_level`."""
        ctx = self._build_ctx(node)
        # Pass rendered input as the worker ctx input
        ctx["input"] = ctx.get("input_for_node", {})
        result = self.worker.run_node(node, ctx)
        nr.output = result.get("output")
        nr.cost_usd = float(result.get("cost_usd", 0.0) or 0.0)
        # TASK 5: top-level cost_usd/tokens_in/tokens_out on the worker's
        # result envelope; a FakeWorker (or any worker that doesn't report
        # them) means `.get(..., 0)` -> 0, never raises.
        nr.tokens_in = int(result.get("tokens_in", 0) or 0)
        nr.tokens_out = int(result.get("tokens_out", 0) or 0)
        # TASK 7: LiveWorker reports the actually-resolved model/provider
        # (post inherit/override). FakeWorker doesn't return these keys, so
        # `.get()` -> None is a silent no-op — never required.
        nr.effective_model = result.get("effective_model")
        nr.effective_provider = result.get("effective_provider")
        if node.spec and node.spec.output:
            mismatch = _validate_output_schema(node.spec.output, nr.output)
            if mismatch:
                nr.status = "failed"
                nr.error = {"code": "SCHEMA", "message": mismatch, "retriable": False}
                return
        nr.status = "succeeded"

    def _run_script(self, nr: NodeRun, node: Node) -> None:
        """Compute-only (TASK 4): thread-safe like `_run_agent` — see that
        method's docstring."""
        ctx = self._build_ctx(node)
        if not node.run or not script_registry.is_registered(node.run):
            nr.status = "failed"
            nr.error = {"code": "SCRIPT", "message": f"run '{node.run}' not registered", "retriable": False}
            return
        fn = script_registry.get(node.run)
        # script input: rendered input mapping, or the upstream output
        script_input = ctx.get("input_for_node", {})
        if not script_input:
            # default: pass the first upstream's output
            ups = [e.from_ for e in self.incoming.get(node.id, [])] or (node.from_ or [])
            if ups and ups[0] in ctx:
                script_input = ctx[ups[0]].get("output", {})
        events.tool_event(self.run_id, nr.node_run_id, node.run)
        result = fn(script_input)
        nr.output = result
        if node.spec and node.spec.output:
            mismatch = _validate_output_schema(node.spec.output, nr.output)
            if mismatch:
                nr.status = "failed"
                nr.error = {"code": "SCHEMA", "message": mismatch, "retriable": False}
                return
        nr.status = "succeeded"

    def _run_fanout(self, nr: NodeRun, node: Node) -> None:
        """Materialize the branch list from `over`, spawn N branch node-runs
        (F1, F5), run them, and -- for `kind: map` only (TASK 1) -- reduce
        them into THIS node's own output.

        `map` is real sugar for "fanout + implicit join in one node": it
        materializes and runs branches exactly like `fanout`, but its own
        output becomes the REDUCED value over its branch envelopes (via
        `reduce:`, defaulting to `concat`) instead of the
        `{"branches": [...], "count": n}` shape. `fanout` keeps that
        original shape unchanged for back-compat -- existing tests/
        workflows depend on it. A `join` node downstream of either still
        only ever sees the branch envelopes (`_branch_envelopes` always
        skips the fanout/map node's own entry), so chaining an explicit
        `join` after a `map` keeps working unchanged.
        """
        ctx = self._build_ctx(node)
        from ..expr import render

        branches = render(node.over, ctx)
        if not isinstance(branches, list):
            # try to resolve a single dict -> wrap
            if branches is None:
                branches = []
            else:
                nr.status = "failed"
                nr.error = {"code": "CARDINALITY", "message": f"fanout over: did not resolve to a list (got {type(branches).__name__})", "retriable": False}
                return

        # F5: runtime hard cap — fail CARDINALITY without overspawning
        if node.max_branches is not None and len(branches) > node.max_branches:
            nr.status = "failed"
            nr.error = {
                "code": "CARDINALITY",
                "message": f"fanout over: list length {len(branches)} exceeds max_branches {node.max_branches}",
                "retriable": False,
            }
            return

        # build branch node template
        branch_node = self._make_branch_node(node)
        branch_nrids: List[str] = []
        for i, item in enumerate(branches):
            bnr = self._ensure_node_run(node.id, branch_index=i, branch_node=branch_node)
            bnr.status = "pending"
            # Persist the branch leaf inputs for crash-safe resume, while keeping
            # the transient cache for compatibility with pre-persistence callers.
            bnr.output = None
            bnr.waiting_on = None
            bnr.branch_item = item
            bnr.parent_fanout = node.id
            branch_nrids.append(bnr.node_run_id)
            self._branch_items[bnr.node_run_id] = item

        # the fanout/map node itself succeeds with the list of branch node_run_ids
        # (map overwrites this with its reduced output below).
        nr.output = {"branches": branch_nrids, "count": len(branch_nrids)}
        nr.status = "succeeded"

        # run the branches (TASK 4: sequential or pool-bounded per
        # max_parallel_nodes; TASK 2: short-circuit for map+first_k)
        self._run_branches(node.id, branch_node, node)

        # TASK 5: this node's own cost/tokens are the SUM over its branches
        # -- a subtree rollup for display only. See `_finish_top_level`'s
        # fanout/map guard for why this is not ALSO added to the run-level
        # total (each branch already contributed its own cost exactly once).
        cost_sum, in_sum, out_sum = self._rollup_branch_cost(node.id)
        nr.cost_usd = cost_sum
        nr.tokens_in = in_sum
        nr.tokens_out = out_sum

        if node.kind == "map":
            reducer = self._select_reducer(node)
            nr.output = reducer(self._branch_envelopes(node.id))

    def _rollup_branch_cost(self, fanout_node_id: str) -> Tuple[float, int, int]:
        """Sum cost_usd/tokens_in/tokens_out over a fanout/map node's own
        branch node-runs (TASK 5 subtree rollup)."""
        total_cost = 0.0
        total_in = 0
        total_out = 0
        for nrid in self.state.node_runs_by_node.get(fanout_node_id, []):
            b = self.state.node_runs[nrid]
            if b.branch_index is None:
                continue
            total_cost += b.cost_usd
            total_in += b.tokens_in
            total_out += b.tokens_out
        return total_cost, total_in, total_out

    def _branch_envelopes(self, fanout_node_id: str) -> List[Dict[str, Any]]:
        """This fanout/map node's own branch node-run envelopes, in
        branch-index order. Shared by `_run_join` (when its upstream is a
        fanout/map) and a `map` node's own implicit reduction (TASK 1), so
        both paths agree on exactly which node-runs count as "the
        branches" -- never the fanout/map node's OWN envelope, which holds
        the branch node_run_id list (or, for map, the already-reduced
        value), not branch data.

        Review HIGH #4: ONLY `succeeded` branches are reduced. A failed
        branch carries `output: None`, and a short-circuited or
        budget-exhausted branch carries a `{"skipped_reason": ...}` marker
        -- neither is a result. Feeding them to a reducer produced silently
        wrong answers, not just noisy ones: `first_k` returned a failed
        branch's `null` as one of its "first k" picks while real results
        sat further down the list, and `majority` counted `None` (or a
        skip marker) as a vote that could win an election.

        The count of excluded branches is not lost -- the branch node-runs
        remain in the checkpoint with their own statuses and reasons, and
        `majority`'s `total` reflects the votes actually cast, so a caller
        can still tell a 2-of-2 consensus from a 2-of-10 one.
        """
        entries: List[Tuple[int, Dict[str, Any]]] = []
        for nrid in self.state.node_runs_by_node.get(fanout_node_id, []):
            upnr = self.state.node_runs[nrid]
            if upnr.branch_index is None:
                continue
            if upnr.status != "succeeded":
                continue
            entries.append((upnr.branch_index, {"node_run_id": nrid, "output": upnr.output}))
        entries.sort(key=lambda t: t[0])
        return [e for _, e in entries]

    def _select_reducer(self, node: Node) -> Callable[[List[Dict[str, Any]]], Any]:
        """Single reducer-dispatch point shared by `_run_join` and a `map`
        node's own implicit reduction (TASK 1/2) -- one place decides which
        reducer runs and with which kwargs, so join and map can never drift
        apart on `reduce:` semantics. A registered `run:` callable (custom
        reducer) wins over the built-in `reduce:` block, matching the
        Phase 1 precedence `_run_join` already had."""
        if node.run and script_registry.is_registered(node.run):
            return script_registry.get(node.run)
        reduce_spec = node.reduce or {}
        reduce_type = reduce_spec.get("type", "concat")
        if reduce_type == "top_k":
            k = int(reduce_spec.get("k", 3))
            return lambda envs: script_registry.top_k(envs, k=k)
        if reduce_type == "first_k":
            k = int(reduce_spec.get("k", 3))
            return lambda envs: script_registry.first_k(envs, k=k)
        if reduce_type == "majority":
            key = reduce_spec.get("key")
            return lambda envs: script_registry.majority(envs, key=key)
        if reduce_type == "best":
            key = reduce_spec.get("key", "score")
            return lambda envs: script_registry.best(envs, key=key)
        return script_registry.concat

    def _run_branches(self, fanout_node_id: str, branch_node: Node, node: Optional[Node] = None) -> None:
        """Execute this fanout/map node's freshly-materialized branch
        node-runs.

        TASK 4: branch node-runs are pool-eligible. `max_parallel_nodes <=
        1` runs this exactly as Phase 1/2 did -- one branch at a time, in
        index order. `> 1` marks+checkpoints each about-to-run wave of up
        to `max_parallel_nodes` branches BEFORE submitting any of them
        (same checkpoint-before-dispatch invariant as the top-level batch
        loop), runs the wave through a bounded `ThreadPoolExecutor`, folds
        results back in index order, then moves to the next wave.

        TASK 2 short-circuit (`map` + `reduce: {type: first_k,
        short_circuit: true}`): once `k` branches have SUCCEEDED, every
        remaining NOT-YET-STARTED branch is marked `skipped`
        (`{"skipped_reason": "short_circuit"}`) instead of being dispatched.
        This is checked between waves (or before each branch, in the
        sequential path) -- it is COOPERATIVE cancellation only. A branch
        already running (already in a submitted wave, or already the
        subject of `_run_one_branch` in the sequential path) always runs to
        completion; nothing here kills in-flight work.
        """
        reduce_spec = (node.reduce or {}) if node is not None else {}
        short_circuit = (
            node is not None
            and node.kind == "map"
            and reduce_spec.get("type") == "first_k"
            and bool(reduce_spec.get("short_circuit"))
        )
        sc_k = int(reduce_spec.get("k", 3)) if short_circuit else None

        branch_nrs = [
            self.state.node_runs[nrid]
            for nrid in self.state.node_runs_by_node.get(fanout_node_id, [])
            if self.state.node_runs[nrid].branch_index is not None
        ]

        def succeeded_count() -> int:
            return sum(1 for b in branch_nrs if b.status == "succeeded")

        def skip_short_circuited(skip_nr: NodeRun) -> None:
            skip_nr.status = "skipped"
            skip_nr.started_at = _ts()
            skip_nr.ended_at = skip_nr.started_at
            skip_nr.output = {"skipped_reason": "short_circuit"}
            disp_id = f"{fanout_node_id}#{skip_nr.branch_index}"
            kind = skip_nr.branch_node.kind if skip_nr.branch_node else "agent"
            events.start_event(self.run_id, skip_nr.node_run_id, disp_id, kind)
            events.end_event(self.run_id, skip_nr.node_run_id, disp_id, "skipped")

        def budget_stop() -> bool:
            """Review BLOCKER #1: the budget circuit-breaker has to be armed
            INSIDE branch execution, not just around it.

            The top-level loop checked the budget before and after each
            node-run, but a fanout/map node is ONE node-run that internally
            runs N branches -- so every branch spent real money before the
            cap was ever re-examined. With a wide `over:` list that is an
            unbounded overshoot of a cap the operator set precisely to bound
            spend. Checked before each branch (sequential) and between waves
            (concurrent); a branch already dispatched still finishes, same
            cooperative bound as short_circuit.
            """
            self._check_budget()
            return self.state.status == "paused"

        def skip_over_budget(skip_nr: NodeRun) -> None:
            """A branch never started because the run hit its budget cap.
            Recorded as its own reason, distinct from short_circuit: one
            means 'we had enough answers', the other means 'we ran out of
            money', and an operator reading the checkpoint needs to tell
            them apart."""
            skip_nr.status = "skipped"
            skip_nr.started_at = _ts()
            skip_nr.ended_at = skip_nr.started_at
            skip_nr.output = {"skipped_reason": "budget_exhausted"}
            disp_id = f"{fanout_node_id}#{skip_nr.branch_index}"
            kind = skip_nr.branch_node.kind if skip_nr.branch_node else "agent"
            events.start_event(self.run_id, skip_nr.node_run_id, disp_id, kind)
            events.end_event(self.run_id, skip_nr.node_run_id, disp_id, "skipped")

        if self.max_parallel_nodes <= 1:
            stopped = False
            for one_nr in branch_nrs:
                if one_nr.status != "pending":
                    continue
                if stopped or budget_stop():
                    stopped = True
                    skip_over_budget(one_nr)
                    continue
                if short_circuit and succeeded_count() >= sc_k:
                    skip_short_circuited(one_nr)
                    continue
                self._run_one_branch(one_nr)
            if stopped:
                self._checkpoint()
            return

        # TASK 4 concurrent path: dispatch in bounded waves (rather than
        # submitting every pending branch at once) so short_circuit can
        # observe the succeeded-count BETWEEN waves and skip branches that
        # would otherwise never even need to start -- submitting everything
        # up-front would defeat the whole point of short_circuit.
        pending = [b for b in branch_nrs if b.status == "pending"]
        while pending:
            # Review BLOCKER #1: budget is re-checked between waves, so the
            # overshoot is bounded by one wave (<= max_parallel_nodes
            # branches) rather than by the whole `over:` list.
            if budget_stop():
                for one_nr in pending:
                    skip_over_budget(one_nr)
                self._checkpoint()
                break
            if short_circuit and succeeded_count() >= sc_k:
                for one_nr in pending:
                    skip_short_circuited(one_nr)
                break
            wave = pending[: self.max_parallel_nodes]
            pending = pending[self.max_parallel_nodes :]
            for one_nr in wave:
                self._mark_branch_running(one_nr)
            self._checkpoint()
            with ThreadPoolExecutor(max_workers=max(1, self.max_parallel_nodes)) as ex:
                futures = [ex.submit(self._branch_compute, one_nr) for one_nr in wave]
                for fut in futures:
                    fut.result()
            for one_nr in wave:
                self._finish_branch(one_nr)
            self._checkpoint()

    def _mark_branch_running(self, nr: NodeRun) -> None:
        fanout_node_id = nr.parent_fanout or nr.node_id
        kind = nr.branch_node.kind if nr.branch_node is not None else nr.kind
        nr.status = "running"
        nr.started_at = _ts()
        events.start_event(self.run_id, nr.node_run_id, f"{fanout_node_id}#{nr.branch_index}", kind)

    def _run_one_branch(self, nr: NodeRun) -> None:
        """Execute one persisted fanout/map branch leaf without re-running
        its parent (sequential path: max_parallel_nodes <= 1, or a single
        branch resurfaced `pending` by an `on_fail: retry` reset and picked
        up by the normal ready-set walk). Marks running + checkpoints
        BEFORE compute -- the same checkpoint-before-dispatch invariant the
        pool path (`_run_branches`) gives a whole wave."""
        self._mark_branch_running(nr)
        self._checkpoint()
        self._branch_compute(nr)
        self._finish_branch(nr)

    def _branch_compute(self, nr: NodeRun) -> None:
        """Execute one branch's actual work into `nr` only -- safe to call
        from a worker thread concurrently with OTHER branches'
        `_branch_compute` calls (each touches only its own NodeRun object,
        plus its own node_run_id-keyed events.jsonl/output.json files, so
        two branches never contend on the same file).

        The ENTIRE body (ctx build, template render, dispatch) is inside
        one try/except -- unlike Phase 1/2's `_run_one_branch`, which only
        guarded the kind-dispatch -- because this now runs inside a
        `concurrent.futures.Future`; an uncaught exception here would
        propagate out of `future.result()` in the fold loop and crash the
        whole batch instead of just failing this one branch.
        """
        node = nr.branch_node
        if node is None:
            nr.status = "failed"
            nr.error = {
                "code": "VERIFY",
                "message": "branch leaf template not recoverable from checkpoint",
                "retriable": False,
            }
            return
        try:
            item = nr.branch_item if nr.branch_item is not None else self._branch_items.get(nr.node_run_id)
            ctx = self._build_ctx(node)
            ctx["branch"] = item
            # Render against a copy: the persisted branch leaf remains a
            # reusable template for any subsequent retry/resume.
            from ..expr import render
            import copy as _copy

            bnode = _copy.deepcopy(node)
            rendered_input: Dict[str, Any] = {}
            if bnode.spec and bnode.spec.input:
                for k, v in bnode.spec.input.items():
                    rendered_input[k] = render(v, ctx)
            if bnode.spec and isinstance(bnode.spec.prompt, str):
                bnode.spec.prompt = render(bnode.spec.prompt, ctx)
            ctx["input"] = rendered_input or {"item": item}

            if bnode.kind == "agent":
                result = self.worker.run_node(bnode, ctx)
                nr.output = result.get("output")
                nr.cost_usd = float(result.get("cost_usd", 0.0) or 0.0)
                nr.tokens_in = int(result.get("tokens_in", 0) or 0)
                nr.tokens_out = int(result.get("tokens_out", 0) or 0)
                # TASK 7: same effective_model/provider capture as _run_agent.
                nr.effective_model = result.get("effective_model")
                nr.effective_provider = result.get("effective_provider")
            elif bnode.kind == "script":
                if not bnode.run or not script_registry.is_registered(bnode.run):
                    # review BLOCK #3: an unregistered branch `run:` must fail
                    # the branch, not silently return a fake result.
                    raise RuntimeError(
                        f"branch script run '{bnode.run}' is not registered (F4 allowlist)"
                    )
                events.tool_event(self.run_id, nr.node_run_id, bnode.run)
                nr.output = script_registry.get(bnode.run)(ctx.get("input", {}))
            else:
                nr.output = {"branch": nr.branch_index}
            nr.status = "succeeded"
        except Exception as exc:
            nr.status = "failed"
            nr.error = {"code": "AGENT", "message": str(exc), "retriable": True}

    def _finish_branch(self, nr: NodeRun) -> None:
        """Main-thread-only fold for one branch: end event, RunState
        aggregate mutation, output store, on_fail dispatch. Called once per
        branch, after `_branch_compute` completes (sequentially, or after
        its pool future resolves)."""
        fanout_node_id = nr.parent_fanout or nr.node_id
        node = nr.branch_node
        nr.ended_at = _ts()
        with self._agg_lock:
            self.state.cost_usd += nr.cost_usd
            self.state.tokens_in += nr.tokens_in
            self.state.tokens_out += nr.tokens_out
            if nr.status == "failed" and _failed_key(nr) not in self.state.failed:
                self.state.failed.append(_failed_key(nr))
        if nr.cost_usd:
            events.cost_event(self.run_id, nr.node_run_id, nr.cost_usd)
        events.end_event(
            self.run_id,
            nr.node_run_id,
            f"{fanout_node_id}#{nr.branch_index}",
            nr.status,
            effective_model=nr.effective_model,
            effective_provider=nr.effective_provider,
        )
        if nr.output is not None:
            fs.store_node_output(self.run_id, nr.node_run_id, nr.output)
        if nr.status == "failed" and node is not None:
            self._apply_on_fail(nr, node)

    def _make_branch_node(self, fanout: Node) -> Node:
        """Construct a Node from the fanout branch template."""
        if not fanout.branch:
            return Node(id=f"{fanout.id}_branch", kind="agent")
        bd = dict(fanout.branch)
        bd.setdefault("id", f"{fanout.id}_branch")
        bd.setdefault("kind", "agent")
        from ..ir import Node as _N, NodeSpec
        node = _N.from_dict(bd)
        if node.spec is None and "spec" in bd:
            node.spec = NodeSpec.from_dict(bd.get("spec"))
        return node

    def _run_join(self, nr: NodeRun, node: Node) -> None:
        """Reduce upstream branch outputs via the shared reducer-dispatch
        (`_select_reducer` -- TASK 1/2), the same dispatch a `map` node
        uses for its own implicit reduction."""
        upstream_ids = node.from_ or [e.from_ for e in self.incoming.get(node.id, [])]
        # collect upstream node-run envelopes. For a fanout/map upstream, collect
        # only the BRANCH node-runs (via `_branch_envelopes`) -- not the fanout/map
        # node's own envelope (which holds the branch node_run_id list / reduced
        # value, not branch data).
        envelopes: List[Dict[str, Any]] = []
        for up in upstream_ids:
            up_node = self.nodes.get(up)
            if up_node is not None and up_node.kind in ("fanout", "map"):
                envelopes.extend(self._branch_envelopes(up))
            else:
                for nrid in self.state.node_runs_by_node.get(up, []):
                    upnr = self.state.node_runs[nrid]
                    # Review HIGH #4: same rule as `_branch_envelopes` --
                    # only `succeeded` upstreams are reduced. This matters
                    # for an `on_fail: continue` upstream, which reaches a
                    # join `failed` with a null output: `continue` promises
                    # the failure does not BLOCK downstream, not that a
                    # non-result gets counted as a result by `majority` or
                    # occupies a slot in `first_k`.
                    if upnr.status != "succeeded":
                        continue
                    envelopes.append({"node_run_id": nrid, "output": upnr.output})
        reducer = self._select_reducer(node)
        nr.output = reducer(envelopes)
        nr.status = "succeeded"

    def _run_gate(self, nr: NodeRun, node: Node) -> None:
        """Park the run at a gate (F7: RunEnvelope.status == awaiting_gate)."""
        nr.status = "awaiting_gate"
        self.state.status = GATE_PARKED
        self.state.awaiting_gate = node.id
        # write a gate signal placeholder
        fs.atomic_write_json(fs.gate_signal_path(self.run_id, node.id), {"gate_id": node.id, "status": "pending"})
        # TASK 3: notify on park. The gate's own `notify:` flag gates this
        # independently of the run-level notify: block; _finalize()
        # deliberately skips the terminal notification for a gate-parked run
        # so this is the only notification fired for this occurrence.
        gate = self.ir.gates.get(node.id)
        notif = self._make_notification("awaiting_gate", gate_id=node.id)
        self._fire_notify(notif, workflow_notify=self.ir.notify, gate_notify=(gate.notify if gate else True))

    # ---- TASK 4 (Phase 3): bounded-concurrency ready-set batch dispatch ---

    def _classify_ready(self, nr: NodeRun) -> str:
        """'pool' (top-level agent/script node-runs, and ANY fanout/map
        branch node-run) vs 'sequential' (fanout/map/join/gate, or an
        unrecognized node id) -- see `_run_ready_batch`'s docstring for why
        the split is drawn exactly here."""
        if nr.branch_index is not None:
            return "pool"
        node = self.nodes.get(nr.node_id)
        if node is not None and node.kind in ("agent", "script"):
            return "pool"
        return "sequential"

    def _run_ready_batch(self, ready: List[NodeRun]) -> bool:
        """Execute one ready-set batch under bounded concurrency
        (`max_parallel_nodes > 1`; the <= 1 case never calls this — see
        `execute()`).

        Pool-eligible node-runs — top-level `agent`/`script` node-runs, and
        any fanout/map BRANCH node-run (including one resurfaced `pending`
        by an `on_fail: retry` reset) — run concurrently through a
        `ThreadPoolExecutor` bounded by `max_parallel_nodes`.
        `fanout`/`map`/`join`/`gate` node-runs mutate
        `self.state.node_runs`/`node_runs_by_node` (or `self.state.status`,
        for a gate) directly and MUST stay on the main thread — both
        because that mutation isn't lock-protected, and because a fanout
        materializing new branch node-runs while a pool worker thread
        concurrently iterates that very same dict in `_build_ctx` would be
        a "dictionary changed size during iteration" bug waiting to happen.

        So this batch runs in two strict phases:
          1. ALL pool-eligible items are submitted and FULLY AWAITED first
             — they only ever *read* `node_runs`/`node_runs_by_node`
             (building template ctx), so running them concurrently with
             each other is safe.
          2. Sequential (structural) items then run one at a time on the
             main thread, with nothing else touching shared state at the
             same time.

        CHECKPOINT-BEFORE-DISPATCH (non-negotiable): every node-run in the
        batch — pool or sequential — is marked `running` (+ start event)
        and the WHOLE batch is checkpointed in this main thread BEFORE any
        future is submitted and before any sequential item's body runs. A
        crash right after that checkpoint leaves every dispatched node-run
        durably `running` on disk, which F6's resume-reconciliation already
        treats as safely requeue-able.

        Results are folded into shared RunState aggregates (cost/tokens/
        succeeded/failed) one node-run at a time, in the ORIGINAL
        ready-set order (not completion order, and not "pool items then
        sequential items" order) — see the fold loop below — so two runs
        over identical inputs produce identically-ordered state regardless
        of real-world thread scheduling. Budget is re-checked after each
        fold, same as the sequential path, so a trip stops the fold loop
        before any further node-run's result is applied.

        An `on_fail: fail_run` hit during folding sets `self._aborted` +
        `self.state.status = "failed"` immediately (see `_abort_run`), but
        by the time we're folding, every item in THIS batch has already
        been computed (phase 1/2 above already ran to completion) — there
        is nothing left in this batch to "not dispatch". So, unlike the
        strictly-sequential path (which stops before even starting the
        next ready item), folding continues for the REST of this
        already-computed batch, so the final state accurately reflects
        every node-run that actually ran; only the OUTER `execute()` loop
        stops STARTING a new batch once `self._aborted` is set.

        Returns True if the budget circuit-breaker tripped during this
        batch.
        """
        kinds: Dict[int, str] = {id(nr): self._classify_ready(nr) for nr in ready}

        # Unknown top-level node id: fail immediately, exactly like the
        # sequential `_run_node_run` guard -- no running state, no event.
        for nr in ready:
            if nr.branch_index is None and self.nodes.get(nr.node_id) is None:
                nr.status = "failed"
                nr.error = {"code": "VERIFY", "message": f"unknown node {nr.node_id}", "retriable": False}
                kinds[id(nr)] = "unknown"

        budget_tripped = False
        for nr in ready:
            if kinds[id(nr)] == "unknown":
                if nr.node_id not in self.state.failed:
                    self.state.failed.append(nr.node_id)

        # ---- group the ready set into ordered dispatch waves ---------------
        # Review BLOCKER #1: the previous shape marked and submitted the
        # ENTIRE ready set before the fold loop's first `_check_budget()`, so
        # overshoot scaled with the width of the ready set rather than with
        # max_parallel_nodes -- a cap the operator set to bound spend could be
        # blown by an arbitrary multiple in one batch.
        #
        # Cost only lands in RunState at FOLD time (`_finish_*`), so a budget
        # check is only meaningful after the preceding work has been folded.
        # Hence: walk `ready` in order, greedily grouping consecutive pool
        # items into waves of <= max_parallel_nodes, with each structural
        # (sequential) item its own singleton wave. Each wave is
        # marked+checkpointed, run, folded, then budget-checked before the
        # next wave is dispatched.
        #
        # Because waves are consecutive slices of the ready-ordered list and
        # each is folded in order, the GLOBAL fold order is still exactly
        # ready-set order -- the determinism property the pool path exists to
        # preserve. And no pool future is ever in flight while a structural
        # node-run executes (each wave is fully awaited first), so a fanout
        # materializing branches still cannot race a pool worker's
        # `_build_ctx` read of `node_runs_by_node`.
        waves: List[List[NodeRun]] = []
        for nr in ready:
            kind = kinds[id(nr)]
            if kind == "unknown":
                continue
            if kind == "sequential":
                waves.append([nr])
                continue
            if waves and waves[-1] and kinds[id(waves[-1][0])] == "pool" and len(waves[-1]) < max(1, self.max_parallel_nodes):
                waves[-1].append(nr)
            else:
                waves.append([nr])

        for wave in waves:
            if self.state.status == "paused" or self._aborted:
                # Budget tripped (or an on_fail: fail_run abort landed) in an
                # earlier wave: leave the rest `pending`. They were never
                # marked running, so the outer loop simply re-evaluates them
                # on a resume rather than resurrecting phantom work.
                break

            # ---- mark the WHOLE wave running, THEN one checkpoint, BEFORE
            #      any future is submitted (checkpoint-before-dispatch).
            for nr in wave:
                if nr.branch_index is not None:
                    self._mark_branch_running(nr)
                else:
                    self._mark_top_level_running(nr, self.nodes[nr.node_id])
            self._checkpoint()

            if kinds[id(wave[0])] == "pool":
                with ThreadPoolExecutor(max_workers=max(1, self.max_parallel_nodes)) as ex:
                    futures = {}
                    for nr in wave:
                        if nr.branch_index is not None:
                            futures[nr.node_run_id] = ex.submit(self._branch_compute, nr)
                        else:
                            futures[nr.node_run_id] = ex.submit(
                                self._top_level_compute, nr, self.nodes[nr.node_id]
                            )
                    for nr in wave:
                        futures[nr.node_run_id].result()
            else:
                # structural (fanout/map/join/gate): main thread only
                for nr in wave:
                    self._top_level_compute(nr, self.nodes[nr.node_id])

            for nr in wave:
                if nr.branch_index is not None:
                    self._finish_branch(nr)
                else:
                    self._finish_top_level(nr, self.nodes[nr.node_id])
                self._checkpoint()
                self._check_budget()
                if self.state.status == "paused" and not budget_tripped:
                    # Persist the pause immediately -- same rationale as the
                    # sequential path (see execute()'s docstring comment there).
                    self._checkpoint()
                    budget_tripped = True
        return budget_tripped

    # ---- Phase 3 §A: on_fail policy dispatch -------------------------------

    def _fail_policy_for(self, nr: NodeRun) -> str:
        """Resolve the effective on_fail policy for a just-failed
        node-run. For a branch, the branch TEMPLATE node's own `on_fail`
        wins if set; otherwise it falls back to the parent fanout/map
        node's policy (which itself defaults to skip_downstream via
        `Node.fail_policy`). For a top-level node-run, it's simply that
        node's own policy."""
        if nr.branch_index is not None:
            if nr.branch_node is not None and nr.branch_node.on_fail:
                return nr.branch_node.on_fail
            parent = self.nodes.get(nr.parent_fanout or "")
            return parent.fail_policy if parent is not None else DEFAULT_ON_FAIL
        node = self.nodes.get(nr.node_id)
        return node.fail_policy if node is not None else DEFAULT_ON_FAIL

    def _apply_on_fail(self, nr: NodeRun, node: Node) -> None:
        """React to a just-failed node-run per its effective on_fail
        policy. Called once from `_finish_top_level`/`_finish_branch`,
        after end-of-node bookkeeping (event fired, output stored) so
        retry/fail_run see a fully-finalized failure. `node` is the failed
        node-run's OWN node/branch-template (used for F6's side_effects
        check and the retry attempts budget); the POLICY itself may come
        from a DIFFERENT node (a branch falls back to its parent fanout/map
        -- see `_fail_policy_for`)."""
        policy = self._fail_policy_for(nr)
        if policy in ("skip_downstream", "continue"):
            # skip_downstream: unchanged Phase 1/2 cascade, via
            # _edge_state/_resolve_from_upstreams treating a failed
            # upstream as blocked.
            # continue: handled structurally in those same two methods (the
            # failure doesn't block downstream) -- nothing to do here.
            return
        if policy == "fail_run":
            self._abort_run(nr, node)
            return
        if policy == "retry":
            self._maybe_retry(nr, node)
            return

    def _abort_run(self, nr: NodeRun, node: Node) -> None:
        """on_fail: fail_run -- a declared hard stop. Sets the run's
        terminal status directly to `failed` (never softened to `partial`
        by `_finalize`, even if other nodes already succeeded earlier in
        this same run -- see `_finalize`'s `self._aborted` check) and flips
        `self._aborted` so the main dispatch loop (sequential or pooled
        batch) stops STARTING further ready node-runs / batches for the
        rest of this `execute()` call."""
        self.state.status = "failed"
        self._aborted = True
        events.run_event(self.run_id, {"event": "aborted", "reason": "ON_FAIL", "node_id": nr.node_id})
        self._checkpoint()

    def _maybe_retry(self, nr: NodeRun, node: Node) -> None:
        """on_fail: retry -- bounded re-run.

        F6 hard requirement: a `side_effects: external` node (or branch
        template) must NEVER be auto-retried -- the entire point of F6 is
        not silently re-running side effects. It stays failed with its
        existing error, falling through to skip_downstream cascade
        semantics, exactly like an exhausted retry.

        The attempt bound is `node.attempts` (the failed node-run's OWN
        node/branch-template value) -- default 2 TOTAL attempts when
        unset. `NodeRun.attempt` is persisted on the checkpoint (see
        `NodeRun.to_dict`/`from_dict`), so a resume() cannot reset the
        counter and loop forever.
        """
        if node.side_effects == "external":
            return  # F6: never auto-retry a side-effecting node/branch.
        attempts_budget = node.attempts if node.attempts else 2
        if nr.attempt >= attempts_budget:
            return  # exhausted -> stays failed -> skip_downstream cascade
        nr.attempt += 1
        nr.status = "pending"
        nr.error = None
        nr.started_at = None
        nr.ended_at = None
        nr.output = None
        if _failed_key(nr) in self.state.failed:
            self.state.failed.remove(_failed_key(nr))
        events.run_event(
            self.run_id,
            {
                "event": "retry",
                "node_id": nr.node_id,
                "node_run_id": nr.node_run_id,
                "attempt": nr.attempt,
            },
        )
        self._checkpoint()

    # ---- resume / budget / finalize ---------------------------------------

    def _check_budget(self) -> None:
        """TASK 5: budget circuit-break. Called at the top of the main loop
        AND after every node-run (cost accrues mid-loop) so a trip is caught
        before any further node starts, not just at loop boundaries.
        Idempotent -- a run already paused/gate-parked is left alone. TASK 3
        on_fail: fail_run: also a no-op once `self._aborted` -- otherwise a
        budget trip observed right after `_abort_run` already set
        `state.status = "failed"` would silently overwrite it back to
        "paused", undoing the hard-stop `_abort_run` just committed."""
        if self.state.status in ("paused", GATE_PARKED) or self._aborted:
            return
        if self.max_budget_usd and self.state.cost_usd > self.max_budget_usd:
            self.state.status = "paused"
            self.state.pause_reason = "BUDGET"
            events.run_event(
                self.run_id,
                {
                    "event": "paused",
                    "reason": "BUDGET",
                    "cost_usd": self.state.cost_usd,
                    "max_budget_usd": self.max_budget_usd,
                },
            )

    def _force_ready(self, node_id: str, *, retry_failed: bool) -> None:
        n = self.nodes.get(node_id)
        if not n:
            return
        existing = self.state.node_runs_by_node.get(node_id, [])
        if existing:
            nr = self.state.node_runs[existing[0]]
            if retry_failed and nr.status == "failed":
                nr.status = "pending"
                nr.error = None
                if node_id in self.state.failed:
                    self.state.failed.remove(node_id)

    def _cascade_reset_skipped_downstream(self, seed_node_ids: List[str]) -> None:
        """TASK 4 / `--retry-failed` un-stick: resetting a failed node-run
        back to `pending` must also reset any node-run that a PRIOR attempt
        marked `skipped` as a consequence of that failure (see the
        `_edge_state`/`_resolve_from_upstreams` failed-upstream cascade),
        otherwise it stays stuck `skipped` forever even though its upstream
        is retriable now.

        Walks forward from each seed node id (via plain edges AND `from:`
        join lists) and resets any `skipped` node-run it finds back to
        `pending`, continuing the walk ONLY through nodes it actually reset.
        A node skipped for an unrelated reason (a false `condition:`, or a
        still-genuinely-skipped sibling upstream) is left alone -- it is
        either not reachable from the seed at all, or its status isn't
        `skipped` there was nothing to cascade past it for.
        """
        frontier = list(seed_node_ids)
        seen = set(seed_node_ids)
        while frontier:
            nid = frontier.pop()
            candidates = {e.to for e in self.outgoing.get(nid, [])}
            for node in self.ir.nodes:
                if node.from_ and nid in node.from_:
                    candidates.add(node.id)
            for cid in candidates:
                for nrid in self.state.node_runs_by_node.get(cid, []):
                    nr = self.state.node_runs[nrid]
                    if nr.status != "skipped":
                        continue
                    nr.status = "pending"
                    nr.started_at = None
                    nr.ended_at = None
                    nr.output = None
                    if cid in self.state.skipped:
                        self.state.skipped.remove(cid)
                    if cid not in seen:
                        frontier.append(cid)
                        seen.add(cid)

    def _finalize(self) -> None:
        if self.state.status == GATE_PARKED:
            pass  # leave awaiting_gate; its own park notification already fired
        elif self._aborted:
            # TASK 3 on_fail: fail_run -- a declared hard stop. `_abort_run`
            # already set state.status = "failed" directly; that must WIN
            # over the succeeded/failed framing below -- a fail_run node is
            # a hard stop, never softened to "partial" just because other
            # nodes had already succeeded earlier in this same run.
            pass
        elif self.state.status == "paused":
            # TASK 5: a budget pause must WIN over the failed/succeeded
            # framing below -- checked before `self.state.failed` so a node
            # that failed earlier in this same run doesn't silently flip the
            # terminal status back to "partial"/"failed" once paused.
            pass
        elif self.state.failed:
            # any succeeded + any failed -> partial; else failed
            if self.state.succeeded:
                self.state.status = "partial"
            else:
                self.state.status = "failed"
        else:
            self.state.status = "succeeded"
        self.state.ended_at = _ts()
        # persist final
        ckpt.write_run_record(self.run_id, self._run_record_dict())
        fs.atomic_write_json(fs.run_output_path(self.run_id), self._run_envelope())
        from ..store import index
        index.upsert_run(self.run_id, self.ir.id, self.state.status, started=self.state.started_at, ended=self.state.ended_at, cost_usd=self.state.cost_usd)

        # TASK 3: terminal notification. Deliberately excludes GATE_PARKED --
        # that path already fired its own notification in _run_gate, and
        # firing again here would double-notify on every park.
        #
        # De-duplicated across resumes. A resume of an already-terminal run
        # resets status to "running", finds nothing ready, and re-derives the
        # SAME terminal status here — so an operator retrying `--resume` on a
        # budget-paused or failed run used to get one external message per
        # attempt for zero new progress. Fingerprint the terminal state and
        # only notify when it actually changed; genuine progress (a new
        # succeeded/failed/skipped node) changes the fingerprint and notifies.
        if self.state.status in ("succeeded", "failed", "partial", "paused"):
            fingerprint = "|".join(
                [
                    self.state.status,
                    str(len(self.state.succeeded)),
                    str(len(self.state.failed)),
                    str(len(self.state.skipped)),
                ]
            )
            if fingerprint != self.state.notified_fingerprint:
                self.state.notified_fingerprint = fingerprint
                # Persist the marker so the de-dup survives a process restart,
                # not just repeated resumes inside one process.
                ckpt.write_run_record(self.run_id, self._run_record_dict())
                notif = self._make_notification(self.state.status)
                self._fire_notify(notif, workflow_notify=self.ir.notify, gate_notify=None)

    def _run_record_dict(self) -> Dict[str, Any]:
        return self.state.to_dict()

    def _make_notification(self, status: str, *, gate_id: Optional[str] = None) -> Any:
        from . import notify as notify_mod

        return notify_mod.Notification(
            run_id=self.run_id,
            workflow_id=self.ir.id,
            status=status,
            gate_id=gate_id,
            succeeded=list(self.state.succeeded),
            failed=list(self.state.failed),
            skipped=list(self.state.skipped),
            cost_usd=self.state.cost_usd,
            # Phase 3: a budget pause is the one terminal status where the
            # operator needs a number and a command, not just a verdict --
            # Notification.effective_detail() turns these two into "cost $X
            # exceeded the $Y cap; resume with --max-budget-usd <higher>".
            # Both are None for every other status, which is exactly when
            # effective_detail() falls back to the plain summary line.
            pause_reason=self.state.pause_reason,
            max_budget_usd=self.max_budget_usd,
        )

    def _fire_notify(self, notif: Any, *, workflow_notify: Any = None, gate_notify: Any = None) -> None:
        """TASK 3: best-effort notification dispatch. Resolves the notifier
        lazily (the Driver's own override, else the module-level default) so
        a test's `notify.set_notifier()` called after this Driver was
        constructed still takes effect. Belt-and-braces try/except: a
        notifier bug must never break a run, even though `notify()` itself
        already promises never to raise."""
        try:
            from . import notify as notify_mod

            fn = self.notifier or notify_mod.default_notifier()
            fn(notif, workflow_notify=workflow_notify, gate_notify=gate_notify)
        except Exception:
            pass

    def _run_envelope(self) -> Dict[str, Any]:
        resume_hint = f"re-run with --resume {self.run_id} to continue from checkpoint"
        if self.state.status == "paused" and self.state.pause_reason == "BUDGET":
            resume_hint = (
                f"run paused: cost ${self.state.cost_usd:.4f} exceeded --max-budget-usd "
                f"{self.max_budget_usd}; raise --max-budget-usd and resume with "
                f"`hermes workflow run --resume {self.run_id} --max-budget-usd <higher value>` to continue"
            )
        return {
            "run_id": self.run_id,
            "attempt_id": self.state.attempt_id,
            "workflow_id": self.ir.id,
            "status": self.state.status,
            "succeeded": list(self.state.succeeded),
            "failed": list(self.state.failed),
            "skipped": list(self.state.skipped),
            "awaiting_gate": self.state.awaiting_gate,
            "pause_reason": self.state.pause_reason,
            "started_at": self.state.started_at,
            "ended_at": self.state.ended_at,
            "cost_usd": self.state.cost_usd,
            # TASK 5: tokens travel with cost everywhere cost travels. A run
            # envelope that reports a dollar figure but not the token counts
            # backing it is the report an operator cannot audit -- and against
            # the real live path tokens are the number we can always recover,
            # while cost depends on the child exposing a price (see
            # live.extract_cost_and_tokens).
            "tokens_in": self.state.tokens_in,
            "tokens_out": self.state.tokens_out,
            "final_output_ref": f"runs/{self.run_id}/run_output.json",
            "resume_hint": resume_hint,
        }

    def _checkpoint(self) -> None:
        ckpt.commit_checkpoint(self.run_id, self.state.to_dict())
        # Keep run.json — the authoritative record `resume()` reads — current at
        # every step. Without this, a crash after a checkpoint but before
        # _finalize() left run.json stale and `resume()` raised FileNotFoundError
        # (review BLOCK #1: the advertised "resume after kill" window was broken).
        ckpt.write_run_record(self.run_id, self._run_record_dict())

    # _branch_items is a legacy transient cache; branch data is checkpointed on NodeRun.


# ---- module-level API (lib API §8) ----------------------------------------


def run(
    vir: VerifiedIR,
    *,
    input: Optional[Dict[str, Any]] = None,
    worker: Optional[Worker] = None,
    dry_run: bool = False,
    max_parallel_nodes: int = 4,
    max_budget_usd: float = 10.0,
    notifier: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Execute a verified IR with a worker (default FakeWorker).

    ``max_parallel_nodes <= 1`` runs nodes and fanout/map branches strictly
    sequentially (deterministic, checkpoint-safe) -- the Phase 1/2 behavior,
    unchanged. ``> 1`` (TASK 4, Phase 3) bounds real concurrency for
    pool-eligible node-runs (top-level ``agent``/``script`` node-runs, and
    fanout/map branch node-runs) via a
    ``concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel_nodes)``;
    ``fanout``/``map``/``join``/``gate`` node-runs always run on the main
    thread. See ``Driver._run_ready_batch`` for the checkpoint-before-dispatch
    and result-folding-order guarantees this provides.

    ``notifier`` (Phase 2 TASK 3) overrides the module-level default notifier
    (``workflow.runtime.notify.default_notifier()``) for this run only; tests
    usually prefer ``notify.set_notifier()`` instead so they don't have to
    thread an override through every call site.
    """
    d = Driver(
        vir,
        worker=worker,
        input=input,
        max_parallel_nodes=max_parallel_nodes,
        max_budget_usd=max_budget_usd,
        notifier=notifier,
    )
    return d.execute(dry_run=dry_run)


def _unpark_gate(d: "Driver") -> Optional[str]:
    """TASK 2 gate unpark. Called once per resume(), before the ready-set
    walk resumes. Resolves the parked gate's on-disk signal
    (``fs.gate_signal_path``, written by ``workflow.decide_gate()``) into the
    checkpoint state:

    - no signal file, or a signal still ``{"status": "pending"}`` (no
      ``decision`` key yet): leave state exactly as-is. ``d.state.status``
      stays ``awaiting_gate`` and the gate's node-run stays
      ``awaiting_gate`` -- the ready-set walk below finds nothing ready and
      ``_finalize()`` preserves ``awaiting_gate``. An open gate must NEVER
      become ``succeeded``.
    - ``decision == "approve"``: finalize the gate node-run ``succeeded``
      (``port="approve"``), clear ``awaiting_gate``, set status back to
      ``running`` so the normal ready-set walk continues into whatever is
      downstream of the gate.
    - ``decision == "shelve"``: finalize the gate node-run ``skipped``
      (``port="shelve"``), clear ``awaiting_gate``, set status back to
      ``running``. ``_propagate_skips`` / ``_edge_state`` already cascade a
      skipped upstream to everything downstream (the same machinery TASK 4
      reuses for failed-upstream cascades), so nothing downstream of the
      gate executes.
    - ``decision == "modify"``: not actionable in-place. There is no
      "edit a live checkpoint's IR and continue" primitive -- `modify`
      genuinely requires re-authoring the workflow definition (a new
      version) and re-running. The run stays ``awaiting_gate``; a note
      explaining this is returned so the caller can surface it honestly
      instead of silently doing nothing.

    Idempotent: once a gate is approved/shelved, ``d.state.awaiting_gate``
    is cleared, so a later resume() call is a no-op here (nothing to
    re-decide) even if the signal file still exists on disk.
    """
    gate_id = d.state.awaiting_gate
    if not gate_id:
        return None
    sig = fs.read_json(fs.gate_signal_path(d.run_id, gate_id))
    if not sig or "decision" not in sig:
        return None  # still pending -- stay parked

    decision = sig.get("decision")
    note = sig.get("note", "")
    gate_nrids = d.state.node_runs_by_node.get(gate_id, [])
    # defensive: an odd/hand-built checkpoint might not have a node-run for
    # the gate at all -- don't raise, just skip the node-run-level update.
    gate_nr = d.state.node_runs.get(gate_nrids[0]) if gate_nrids else None

    if decision == "approve":
        if gate_nr is not None and gate_nr.status != "succeeded":
            gate_nr.status = "succeeded"
            gate_nr.port = "approve"
            gate_nr.ended_at = _ts()
            gate_nr.output = {"gate": gate_id, "decision": "approve", "note": note, "approved_at": gate_nr.ended_at}
            events.end_event(d.run_id, gate_nr.node_run_id, gate_id, "succeeded", port="approve")
            fs.store_node_output(d.run_id, gate_nr.node_run_id, gate_nr.output)
        if gate_id not in d.state.succeeded:
            d.state.succeeded.append(gate_id)
        d.state.awaiting_gate = None
        d.state.status = "running"
        return None

    if decision == "shelve":
        if gate_nr is not None and gate_nr.status != "skipped":
            gate_nr.status = "skipped"
            gate_nr.port = "shelve"
            gate_nr.ended_at = _ts()
            gate_nr.output = {"gate": gate_id, "decision": "shelve", "note": note}
            events.end_event(d.run_id, gate_nr.node_run_id, gate_id, "skipped", port="shelve")
            fs.store_node_output(d.run_id, gate_nr.node_run_id, gate_nr.output)
        if gate_id not in d.state.skipped:
            d.state.skipped.append(gate_id)
        d.state.awaiting_gate = None
        d.state.status = "running"
        return None

    if decision == "modify":
        return (
            f"gate '{gate_id}' decision 'modify' requires re-authoring the workflow "
            "(a new definition/version) and re-running it; the driver has no "
            "in-place \"edit the live checkpoint and continue\" primitive, so the "
            "run stays awaiting_gate."
        )

    # unknown/unrecognized decision value: stay parked defensively.
    return f"gate '{gate_id}' has an unrecognized decision '{decision}'; run stays awaiting_gate"


def resume(
    run_id: str,
    *,
    worker: Optional[Worker] = None,
    retry_failed: bool = False,
    from_node: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
    notifier: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Resume a crashed/parked run from its checkpoint (design §4.4, F6).

    TASK 2 gate unpark: if the run is parked at a gate
    (``state.status == GATE_PARKED`` / ``state.awaiting_gate``), the gate's
    on-disk signal is resolved via ``_unpark_gate`` before the ready-set walk
    resumes -- see that function's docstring for the approve/shelve/modify
    policy. A second gate hit later in the same run parks again normally.

    TASK 5 budget resume policy: a run paused by the budget circuit-breaker
    (``pause_reason == "BUDGET"``) only makes forward progress if resumed
    with a HIGHER ``max_budget_usd`` than the value that tripped it (the CLI
    threads ``--max-budget-usd`` through to this parameter). Resuming with
    the same value (or omitting it, which reuses the Driver default) re-checks
    the budget at the top of the loop before anything executes and re-pauses
    immediately -- it does not silently ignore the still-tripped cap.

    TASK 4 ``--retry-failed``: resetting a failed node-run back to ``pending``
    also resets any node-run a PRIOR attempt marked ``skipped`` as a
    consequence of that failure, via
    ``Driver._cascade_reset_skipped_downstream``, so the retried branch
    becomes ready again instead of staying stuck skipped.
    """
    record = ckpt.load_run_record(run_id)
    if not record:
        raise FileNotFoundError(f"no run record for {run_id}")
    state = RunState.from_dict(record)
    # load the verified IR from the definition
    from ..store.fs import definition_path, read_json
    defp = definition_path(state.workflow_id)
    if not defp.exists():
        raise FileNotFoundError(f"no definition for workflow {state.workflow_id}")
    vir = VerifiedIR.from_dict(read_json(defp))

    # review BLOCK #2: re-verify the stored definition at load (design §4.4
    # "re-verified at load") so a tampered `definitions/<id>.json` is rejected
    # rather than trusted. Warn-mode (override-only fields) is lenient, matching
    # how a warn-accepted workflow would have compiled; structural/security
    # errors (cycle, unregistered run:, approve_auto+dual_control) still raise.
    from ..verify import verify_ir as _verify_ir
    from ..ir import WorkflowRejected as _WR

    try:
        _verify_ir(vir.ir, phase1_warn_overrides=True)
    except _WR as exc:
        raise ValueError(
            f"workflow definition '{state.workflow_id}' failed re-verification at "
            f"resume (definition tampered or corrupt): {exc}"
        ) from exc

    driver_kwargs: Dict[str, Any] = {"worker": worker, "run_id": run_id, "input": {}, "notifier": notifier}
    if max_budget_usd is not None:
        driver_kwargs["max_budget_usd"] = max_budget_usd
    d = Driver(vir, **driver_kwargs)
    d.state = state
    d.state.attempt_id += 1

    # TASK 2: resolve a parked gate's signal BEFORE the F6 running-node
    # reconciliation and the ready-set walk, so an approved/shelved gate's
    # downstream nodes are considered fresh this same resume() call rather
    # than requiring a second resume just to notice the gate cleared.
    gate_note: Optional[str] = None
    if d.state.status == GATE_PARKED or d.state.awaiting_gate:
        gate_note = _unpark_gate(d)
    else:
        # review HIGH #1 (unchanged): only clobber with "running" when the
        # run was not parked at a gate to begin with.
        d.state.status = "running"

    # F6 resume policy: side_effects:external running nodes -> failed (INTERRUPTED)
    for nrid, nr in state.node_runs.items():
        node = d.nodes.get(nr.node_id)
        if nr.status == "running":
            if node and node.side_effects == "external":
                nr.status = "failed"
                nr.error = {"code": "INTERRUPTED", "message": "side-effecting node was running at crash; not auto-requeued", "retriable": True}
                if _failed_key(nr) not in state.failed:
                    state.failed.append(_failed_key(nr))
            else:
                # safe memo-only requeue running->ready (design §4.4)
                nr.status = "pending"

    if from_node:
        d._force_ready(from_node, retry_failed=retry_failed)
    if retry_failed:
        reset_node_ids: List[str] = []
        for nrid, nr in state.node_runs.items():
            if nr.status == "failed":
                nr.status = "pending"
                nr.error = None
                if _failed_key(nr) in state.failed:
                    state.failed.remove(_failed_key(nr))
                reset_node_ids.append(nr.node_id)
        if reset_node_ids:
            d._cascade_reset_skipped_downstream(reset_node_ids)

    env = d.execute(retry_failed=retry_failed, from_node=from_node)
    if gate_note:
        env["note"] = gate_note
    return env