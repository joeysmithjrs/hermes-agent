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


def _new_node_run_id(run_id: str, node_id: str) -> str:
    """Per-execution uuid4 (F1). Readable prefix + unique suffix."""
    return f"{run_id}__{node_id}__{uuid.uuid4().hex[:8]}"


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
    ) -> None:
        self.vir = vir
        self.ir: WorkflowIR = vir.ir
        self.worker: Worker = worker or FakeWorker()
        self.run_id = run_id or _new_run_id()
        self.input = input or {}
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
            ready = self._compute_ready()
            if not ready:
                break
            for nr in ready:
                self._run_node_run(nr)
                self._checkpoint()
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
        """Return NodeRuns whose upstreams all succeeded and conditions hold."""
        ready: List[NodeRun] = []
        for node_id, n in self.nodes.items():
            # gate nodes: seed a node-run when upstreams done, then run (parks).
            if n.kind == "gate":
                existing = self.state.node_runs_by_node.get(node_id, [])
                if not existing:
                    if self._upstreams_done(node_id):
                        nr = self._ensure_node_run(node_id)
                        if nr.status == "pending":
                            ready.append(nr)
                    continue
                nr = self.state.node_runs[existing[0]]
                if nr.status == "pending" and self._upstreams_done(node_id):
                    ready.append(nr)
                continue
            # normal node: ensure a node-run exists, check readiness
            existing = self.state.node_runs_by_node.get(node_id, [])
            if not existing:
                # fanout branches create their own node-runs at materialization;
                # all node kinds need a node-run seeded once upstreams are done
                if self._upstreams_done(node_id):
                    nr = self._ensure_node_run(node_id)
                    if nr.status == "pending":
                        ready.append(nr)
                continue
            for nrid in existing:
                nr = self.state.node_runs[nrid]
                if nr.status == "pending" and self._upstreams_done(node_id):
                    ready.append(nr)
        return ready

    def _upstreams_done(self, node_id: str) -> bool:
        """All upstream node-runs (per incoming edges or from_) have succeeded."""
        n = self.nodes[node_id]
        upstream_ids: List[str] = []
        if n.from_:
            upstream_ids = list(n.from_)
        else:
            upstream_ids = [e.from_ for e in self.incoming.get(node_id, [])]
        for up in upstream_ids:
            runs = self.state.node_runs_by_node.get(up, [])
            if not runs:
                return False
            for nrid in runs:
                if self.state.node_runs[nrid].status != "succeeded":
                    return False
        return True

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
        events.end_event(self.run_id, nr.node_run_id, nr.node_id, nr.status)
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
        events.end_event(self.run_id, nr.node_run_id, f"{fanout_node_id}#{nr.branch_index}", nr.status)
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

    # ---- resume / budget / finalize ---------------------------------------

    def _check_budget(self) -> None:
        if self.max_budget_usd and self.state.cost_usd > self.max_budget_usd:
            # budget circuit-break -> pause (F7: paused status exists)
            self.state.status = "paused"

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

    def _finalize(self) -> None:
        if self.state.status == GATE_PARKED:
            pass  # leave awaiting_gate
        elif self.state.failed:
            # any succeeded + any failed -> partial; else failed
            if self.state.succeeded:
                self.state.status = "partial"
            else:
                self.state.status = "failed"
        elif self.state.status == "paused":
            pass
        else:
            self.state.status = "succeeded"
        self.state.ended_at = _ts()
        # persist final
        ckpt.write_run_record(self.run_id, self._run_record_dict())
        fs.atomic_write_json(fs.run_output_path(self.run_id), self._run_envelope())
        from ..store import index
        index.upsert_run(self.run_id, self.ir.id, self.state.status, started=self.state.started_at, ended=self.state.ended_at, cost_usd=self.state.cost_usd)

    def _run_record_dict(self) -> Dict[str, Any]:
        return self.state.to_dict()

    def _run_envelope(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "attempt_id": self.state.attempt_id,
            "workflow_id": self.ir.id,
            "status": self.state.status,
            "succeeded": list(self.state.succeeded),
            "failed": list(self.state.failed),
            "skipped": list(self.state.skipped),
            "awaiting_gate": self.state.awaiting_gate,
            "started_at": self.state.started_at,
            "ended_at": self.state.ended_at,
            "cost_usd": self.state.cost_usd,
            "final_output_ref": f"runs/{self.run_id}/run_output.json",
            "resume_hint": f"re-run with --resume {self.run_id} to continue from checkpoint",
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
) -> Dict[str, Any]:
    """Execute a verified IR with a worker (default FakeWorker).

    Phase 1 executes nodes and fanout branches strictly sequentially
    (deterministic, checkpoint-safe). ``max_parallel_nodes`` is accepted for
    config/forward-compat but is a reserved no-op; bounded concurrency is a
    Phase 2 deliverable.
    """
    d = Driver(vir, worker=worker, input=input, max_parallel_nodes=max_parallel_nodes, max_budget_usd=max_budget_usd)
    return d.execute(dry_run=dry_run)


def resume(
    run_id: str,
    *,
    worker: Optional[Worker] = None,
    retry_failed: bool = False,
    from_node: Optional[str] = None,
) -> Dict[str, Any]:
    """Resume a crashed/parked run from its checkpoint (design §4.4, F6)."""
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

    d = Driver(vir, worker=worker, run_id=run_id, input={})
    d.state = state
    d.state.attempt_id += 1
    # review HIGH #1: do NOT clobber an awaiting_gate run with "running" on
    # resume. A gated run that resumes with no decision must STAY awaiting_gate
    # (full unpark is Phase 2), never finalize as "succeeded" with a pending gate.
    if state.status != GATE_PARKED and not state.awaiting_gate:
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
        for nrid, nr in state.node_runs.items():
            if nr.status == "failed":
                nr.status = "pending"
                nr.error = None
                if nr.node_id in state.failed:
                    state.failed.remove(nr.node_id)

    return d.execute(retry_failed=retry_failed, from_node=from_node)