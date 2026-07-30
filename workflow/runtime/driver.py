"""Deterministic driver — ready-set walk over a VerifiedIR (design §4).

Control flow is code, not an LLM loop. The driver:
1. Loads verified IR + run record (or creates one).
2. Computes ready node-runs (upstreams succeeded, conditions satisfied).
3. Starts each ready node per kind (agent via worker, script via registry,
   fanout materializes branches, join reduces, gate parks).
4. Writes envelope + event log, checkpoints, repeats until terminal.

Node bodies keyed by ``node_run_id`` (F1). Fanout branches each get a unique
node_run_id. side_effects: external nodes resume to failed (INTERRUPTED), not
requeued (F6). Fanout over max_branches -> failed CARDINALITY, no overspawn (F5).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from ..expr import TemplateError, eval_condition
from ..ir import Node, VerifiedIR, WorkflowIR
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
        # Phase 1 executes nodes and fanout branches strictly sequentially
        # (deterministic, checkpoint-safe). max_parallel_nodes is accepted for
        # config/forward-compat but is a reserved no-op; bounded concurrency is
        # a Phase 2 deliverable.
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
            if budget_tripped:
                break
            # a gate parks the run -> stop
            if self.state.status == GATE_PARKED:
                break

        self._finalize()
        return self._run_envelope()

    # ---- ready-set computation -------------------------------------------

    def _seed_initial_node_runs(self) -> None:
        """Create a NodeRun for every IR node that has no incoming edges."""
        has_incoming = {e.to for e in self.edges}
        for n in self.ir.nodes:
            if n.id not in has_incoming:
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
                # TASK 4 failed-upstream cascade: a `failed` upstream in a
                # `from:` join list never produces output either — same
                # policy as `skipped` (see _edge_state for the `edges:` twin
                # of this rule, and the module docstring on --retry-failed).
                if st in ("skipped", "failed"):
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
            # TASK 4 failed-upstream cascade (Phase 1 debt): a `failed`
            # upstream never produces output, so a downstream node waiting on
            # it can never become ready either. Policy: treat it exactly like
            # a skipped upstream -> "blocked", which _resolve_edge_upstreams
            # turns into "skip", and _propagate_skips marks the downstream
            # node `skipped` (with a recorded reason) rather than `failed` --
            # it never ran, so `failed` would misrepresent it. This cascades
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
        # Branch node-runs retain the parent fanout node_id. Always dispatch them
        # through their persisted branch leaf rather than re-entering the fanout.
        if nr.branch_index is not None:
            self._run_one_branch(nr)
            return

        node = self.nodes.get(nr.node_id)
        if node is None and nr.branch_node is not None:
            node = nr.branch_node
        if node is None:
            nr.status = "failed"
            nr.error = {"code": "VERIFY", "message": f"unknown node {nr.node_id}", "retriable": False}
            self.state.failed.append(nr.node_id)
            return

        nr.status = "running"
        nr.started_at = _ts()
        events.start_event(self.run_id, nr.node_run_id, nr.node_id, node.kind)
        self._checkpoint()

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
            if node.id not in self.state.failed:
                self.state.failed.append(node.id)
            events.end_event(self.run_id, nr.node_run_id, nr.node_id, "failed")
            return

        nr.ended_at = _ts()
        events.end_event(
            self.run_id,
            nr.node_run_id,
            nr.node_id,
            nr.status,
            effective_model=nr.effective_model,
            effective_provider=nr.effective_provider,
        )
        if nr.status == "succeeded" and node.id not in self.state.succeeded:
            self.state.succeeded.append(node.id)
        # store output (F1: keyed by node_run_id)
        if nr.output is not None:
            out_path = fs.store_node_output(self.run_id, nr.node_run_id, nr.output)
            events.emit(self.run_id, nr.node_run_id, {"event": "stored", "path": str(out_path.relative_to(fs.workflows_root()))})

    def _build_ctx(self, node: Node) -> Dict[str, Any]:
        """Build the template ctx from succeeded upstream outputs + run input."""
        ctx: Dict[str, Any] = {"input": self.input}
        # node outputs (envelope-shaped: {output: ...})
        for nid, nrids in self.state.node_runs_by_node.items():
            for nrid in nrids:
                nr = self.state.node_runs[nrid]
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
        ctx = self._build_ctx(node)
        # Pass rendered input as the worker ctx input
        ctx["input"] = ctx.get("input_for_node", {})
        result = self.worker.run_node(node, ctx)
        nr.output = result.get("output")
        nr.cost_usd = float(result.get("cost_usd", 0.0) or 0.0)
        self.state.cost_usd += nr.cost_usd
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
                if node.id not in self.state.failed:
                    self.state.failed.append(node.id)
                return
        nr.status = "succeeded"

    def _run_script(self, nr: NodeRun, node: Node) -> None:
        ctx = self._build_ctx(node)
        if not node.run or not script_registry.is_registered(node.run):
            nr.status = "failed"
            nr.error = {"code": "SCRIPT", "message": f"run '{node.run}' not registered", "retriable": False}
            if node.id not in self.state.failed:
                self.state.failed.append(node.id)
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
                if node.id not in self.state.failed:
                    self.state.failed.append(node.id)
                return
        nr.status = "succeeded"

    def _run_fanout(self, nr: NodeRun, node: Node) -> None:
        """Materialize the branch list from `over`, spawn N branch node-runs (F1, F5)."""
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
                if node.id not in self.state.failed:
                    self.state.failed.append(node.id)
                return

        # F5: runtime hard cap — fail CARDINALITY without overspawning
        if node.max_branches is not None and len(branches) > node.max_branches:
            nr.status = "failed"
            nr.error = {
                "code": "CARDINALITY",
                "message": f"fanout over: list length {len(branches)} exceeds max_branches {node.max_branches}",
                "retriable": False,
            }
            if node.id not in self.state.failed:
                self.state.failed.append(node.id)
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

        # the fanout node itself succeeds with the list of branch node_run_ids
        nr.output = {"branches": branch_nrids, "count": len(branch_nrids)}
        nr.status = "succeeded"
        if node.id not in self.state.succeeded:
            self.state.succeeded.append(node.id)

        # immediately run the branches (deterministic order, bounded by max_parallel)
        self._run_branches(node.id, branch_node)

    def _run_branches(self, fanout_node_id: str, branch_node: Node) -> None:
        """Execute spawned branch node-runs in deterministic index order."""
        for nrid in list(self.state.node_runs_by_node.get(fanout_node_id, [])):
            nr = self.state.node_runs[nrid]
            if nr.branch_index is not None and nr.status == "pending":
                self._run_one_branch(nr)

    def _run_one_branch(self, nr: NodeRun) -> None:
        """Execute one persisted fanout branch leaf without re-running its parent."""
        node = nr.branch_node
        fanout_node_id = nr.parent_fanout or nr.node_id
        if node is None:
            nr.status = "failed"
            nr.error = {
                "code": "VERIFY",
                "message": "branch leaf template not recoverable from checkpoint",
                "retriable": False,
            }
            if nr.node_id not in self.state.failed:
                self.state.failed.append(nr.node_id)
            return

        item = nr.branch_item if nr.branch_item is not None else self._branch_items.get(nr.node_run_id)
        ctx = self._build_ctx(node)
        ctx["branch"] = item
        # Render against a copy: the persisted branch leaf remains a reusable
        # template for any subsequent retry/resume.
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
        nr.status = "running"
        nr.started_at = _ts()
        events.start_event(self.run_id, nr.node_run_id, f"{fanout_node_id}#{nr.branch_index}", bnode.kind)
        self._checkpoint()
        try:
            if bnode.kind == "agent":
                result = self.worker.run_node(bnode, ctx)
                nr.output = result.get("output")
                nr.cost_usd = float(result.get("cost_usd", 0.0) or 0.0)
                self.state.cost_usd += nr.cost_usd
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
            if nr.node_id not in self.state.failed:
                self.state.failed.append(nr.node_id)
        nr.ended_at = _ts()
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
        """Reduce upstream branch outputs (concat/top_k)."""
        upstream_ids = node.from_ or [e.from_ for e in self.incoming.get(node.id, [])]
        # collect upstream node-run envelopes. For a fanout/map upstream, collect
        # only the BRANCH node-runs (branch_index is not None), not the fanout
        # node's own envelope (which holds the branch node_run_id list, not data).
        envelopes: List[Dict[str, Any]] = []
        for up in upstream_ids:
            up_node = self.nodes.get(up)
            for nrid in self.state.node_runs_by_node.get(up, []):
                upnr = self.state.node_runs[nrid]
                if up_node and up_node.kind in ("fanout", "map"):
                    if upnr.branch_index is None:
                        continue  # skip the fanout node's own envelope
                envelopes.append({"node_run_id": nrid, "output": upnr.output})
        # select reducer
        reduce_type = (node.reduce or {}).get("type", "concat") if node.reduce else "concat"
        if node.run and script_registry.is_registered(node.run):
            nr.output = script_registry.get(node.run)(envelopes)
        elif reduce_type == "top_k":
            from .scripts import top_k
            k = int((node.reduce or {}).get("k", 3))
            nr.output = top_k(envelopes, k=k)
        else:
            from .scripts import concat
            nr.output = concat(envelopes)
        nr.status = "succeeded"
        if node.id not in self.state.succeeded:
            self.state.succeeded.append(node.id)

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

    # ---- resume / budget / finalize ---------------------------------------

    def _check_budget(self) -> None:
        """TASK 5: budget circuit-break. Called at the top of the main loop
        AND after every node-run (cost accrues mid-loop) so a trip is caught
        before any further node starts, not just at loop boundaries.
        Idempotent -- a run already paused/gate-parked is left alone."""
        if self.state.status in ("paused", GATE_PARKED):
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

    Phase 1 executes nodes and fanout branches strictly sequentially
    (deterministic, checkpoint-safe). ``max_parallel_nodes`` is accepted for
    config/forward-compat but is a reserved no-op; bounded concurrency is a
    Phase 2 deliverable.

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
                if nr.node_id not in state.failed:
                    state.failed.append(nr.node_id)
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
                if nr.node_id in state.failed:
                    state.failed.remove(nr.node_id)
                reset_node_ids.append(nr.node_id)
        if reset_node_ids:
            d._cascade_reset_skipped_downstream(reset_node_ids)

    env = d.execute(retry_failed=retry_failed, from_node=from_node)
    if gate_note:
        env["note"] = gate_note
    return env