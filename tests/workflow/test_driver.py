"""Driver integration tests with FakeWorker — test-plan §3 + AUDIT F1, F5, F6, F7.

F1: fanout 3 -> join -> 3 distinct node_run_id branch outputs (+ reducers).
F5: oversize fanout -> node failed CARDINALITY, no overspawn.
F6: resume after kill — side_effects:external running node -> failed INTERRUPTED
    (not auto-rerun); a plain running node is re-run; succeeded nodes are not
    double-finalized.
F7: gate parks the run -> RunEnvelope.status == awaiting_gate; the run-status
    enum includes awaiting_gate + paused.
"""

from __future__ import annotations

import json

import pytest

LINEAR_YAML = """
workflow: linear_drv
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "Do A"}
  - id: b
    kind: agent
    spec: {prompt: "Do B using {{ a.output }}"}
  - id: c
    kind: script
    run: workflow.examples.echo
    input: {text: "{{ b.output }}"}
edges:
  - { from: a, to: b }
  - { from: b, to: c }
triggers:
  - { kind: manual }
"""

FANOUT_CONCAT_YAML = """
workflow: fanout_concat
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [alpha, beta, gamma]}
  - id: branches
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec: {prompt: "Process {{ branch }}"}
  - id: join
    kind: join
    from: [branches]
    reduce: { type: concat }
edges:
  - { from: seed, to: branches }
  - { from: branches, to: join }
triggers:
  - { kind: manual }
"""

FANOUT_TOPK_YAML = """
workflow: fanout_topk
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [alpha, beta, gamma, delta]}
  - id: branches
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec: {prompt: "Process {{ branch }}"}
  - id: join
    kind: join
    from: [branches]
    reduce: { type: top_k, k: 2 }
edges:
  - { from: seed, to: branches }
  - { from: branches, to: join }
triggers:
  - { kind: manual }
"""

CARDINALITY_YAML = """
workflow: card
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b, c, d, e]}
  - id: br
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 3
    branch:
      kind: agent
      spec: {prompt: "p {{ branch }}"}
  - id: jn
    kind: join
    from: [br]
    reduce: { type: concat }
edges:
  - { from: seed, to: br }
  - { from: br, to: jn }
triggers:
  - { kind: manual }
"""

GATE_YAML = """
workflow: gate_drv
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
  - id: g1
    kind: gate
gates:
  g1:
    channel: "#ops"
    approvers: [joe]
edges:
  - { from: a, to: g1 }
triggers:
  - { kind: manual }
"""


class _CallCountWorker:
    """Worker that records every run_node call per node id (for F6 / dry-run)."""

    def __init__(self):
        self.calls = {}  # node_id -> count

    def run_node(self, node, ctx):
        self.calls[node.id] = self.calls.get(node.id, 0) + 1
        return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}


class _DistinctBranchWorker:
    """Branch output distinct per branch item (for F1 content-distinctness)."""

    def run_node(self, node, ctx):
        if "branch" in ctx and node.id == "branches_branch":
            item = ctx["branch"]
            return {"output": {"item": item, "marker": f"b-{item}"}, "cost_usd": 0.0}
        return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}


# ---------------------------------------------------------------------------
# Linear
# ---------------------------------------------------------------------------


def test_linear_three_node_success(wf_home):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(LINEAR_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    assert env["status"] == "succeeded", env
    assert env["succeeded"] == ["a", "b", "c"], env["succeeded"]


# ---------------------------------------------------------------------------
# F1 — fanout -> join -> 3 distinct branch outputs + reducers
# ---------------------------------------------------------------------------


def test_f1_fanout_concat_three_branch_outputs(wf_home):
    from workflow import compile_text, run
    from workflow.store import fs, checkpoint

    vir = compile_text(FANOUT_CONCAT_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=_DistinctBranchWorker())
    assert env["status"] == "succeeded", env
    assert "branches" in env["succeeded"] and "join" in env["succeeded"]

    nodes_dir = fs.run_dir(env["run_id"]) / "nodes"
    branch_outs = []
    for nr_dir in nodes_dir.iterdir():
        if nr_dir.is_dir() and "branches#" in nr_dir.name:
            out_file = nr_dir / "output.json"
            assert out_file.exists()
            branch_outs.append((nr_dir.name, json.loads(out_file.read_text(encoding="utf-8"))))
    assert len(branch_outs) == 3
    assert len(set(n for n, _ in branch_outs)) == 3  # distinct node_run_ids (F1)
    assert len(set(json.dumps(c, sort_keys=True) for _, c in branch_outs)) == 3  # distinct content

    # concat reducer preserves the 3 branch node_run_ids
    rec = checkpoint.load_run_record(env["run_id"])
    join_nr = next(nrd for nrid, nrd in rec["node_runs"].items() if nrd["node_id"] == "join")
    join_out = join_nr["output"]
    assert join_out.get("kind") == "concat"
    assert len(join_out["branches"]) == 3
    branch_nrids_in_join = {b["node_run_id"] for b in join_out["branches"]}
    actual_branch_nrids = {nrid for nrid in rec["node_runs"] if "branches#" in nrid}
    assert branch_nrids_in_join == actual_branch_nrids


def test_fanout_top_k_reducer(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(FANOUT_TOPK_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=_DistinctBranchWorker())
    assert env["status"] == "succeeded", env

    rec = checkpoint.load_run_record(env["run_id"])
    join_nr = next(nrd for nrid, nrd in rec["node_runs"].items() if nrd["node_id"] == "join")
    join_out = join_nr["output"]
    assert join_out.get("kind") == "top_k"
    assert join_out["k"] == 2
    # 4 branches -> top_k(k=2) selects exactly 2
    assert len(join_out["selected"]) == 2
    selected_nrids = {s["node_run_id"] for s in join_out["selected"]}
    actual_branch_nrids = {nrid for nrid in rec["node_runs"] if "branches#" in nrid}
    assert selected_nrids.issubset(actual_branch_nrids)


# ---------------------------------------------------------------------------
# F5 — oversize fanout: CARDINALITY, no overspawn
# ---------------------------------------------------------------------------


def test_f5_oversize_fanout_cardinality_no_overspawn(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    from workflow.runtime.worker import FakeWorker

    vir = compile_text(CARDINALITY_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    # the fanout node must be failed
    assert "br" in env["failed"], env

    rec = checkpoint.load_run_record(env["run_id"])
    br_nr = next(nrd for nrid, nrd in rec["node_runs"].items()
                 if nrd["node_id"] == "br" and nrd.get("branch_index") is None)
    assert br_nr["status"] == "failed", br_nr
    assert br_nr["error"]["code"] == "CARDINALITY", br_nr["error"]

    # NO branch node-runs were spawned (no overspawn)
    branch_runs = [nrd for nrid, nrd in rec["node_runs"].items() if nrd.get("branch_index") is not None]
    assert len(branch_runs) == 0, f"expected 0 spawned branches, got {len(branch_runs)}"


# ---------------------------------------------------------------------------
# F7 — gate parks the run; run-status enum includes awaiting_gate/paused
# ---------------------------------------------------------------------------


def test_f7_gate_parks_run_awaiting_gate(wf_home):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(GATE_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    assert env["status"] == "awaiting_gate", env
    assert env["awaiting_gate"] == "g1", env


def test_f7_run_status_enum_includes_awaiting_gate_and_paused():
    """F7: the run-status enum has awaiting_gate + paused (types exist even if
    the gate runtime unpark is Phase 2)."""
    from workflow.ir import RUN_STATUSES

    assert "awaiting_gate" in RUN_STATUSES, RUN_STATUSES
    assert "paused" in RUN_STATUSES, RUN_STATUSES


# ---------------------------------------------------------------------------
# F6 — resume after kill (the trickiest invariant)
# ---------------------------------------------------------------------------

RESUME_YAML = """
workflow: resumewf
version: 1
nodes:
  - id: safe
    kind: agent
    spec: {prompt: safe}
  - id: plain
    kind: agent
    spec: {prompt: plain}
  - id: eff
    kind: agent
    spec: {prompt: eff}
    side_effects: external
edges:
  - { from: safe, to: plain }
  - { from: safe, to: eff }
triggers:
  - { kind: manual }
"""


def test_f6_resume_after_kill(wf_home):
    """F6: after a simulated crash mid-run:

      - a SUCCEEDED node (`safe`) is NOT re-finalized / double-run;
      - a `side_effects: external` node (`eff`) that was RUNNING at crash resumes
        to `failed` (INTERRUPTED) and is NOT auto-rerun (side-effect count stays
        at the single call that happened before the crash);
      - a plain (non-side-effecting) node (`plain`) that was RUNNING resumes to
        `ready` and IS re-run (its call count increases).

    The crash is simulated by writing a checkpoint with node statuses directly
    (the synchronous driver can't be SIGKILLed mid-node); the worker object's
    recorded call counts represent the calls that already happened.
    """
    from workflow import compile_text, resume
    from workflow.runtime.driver import NodeRun, RunState
    from workflow.store import checkpoint

    vir = compile_text(RESUME_YAML, phase1_warn_overrides=True)
    run_id = "wf_resumetestabcd"

    safe_nr = NodeRun(node_run_id=f"{run_id}__safe__aaaa", node_id="safe",
                      kind="agent", status="succeeded", output={"text": "done"})
    plain_nr = NodeRun(node_run_id=f"{run_id}__plain__bbbb", node_id="plain",
                       kind="agent", status="running")
    eff_nr = NodeRun(node_run_id=f"{run_id}__eff__cccc", node_id="eff",
                     kind="agent", status="running")

    state = RunState(run_id=run_id, workflow_id="resumewf", status="running",
                     succeeded=["safe"])
    for nr in (safe_nr, plain_nr, eff_nr):
        state.node_runs[nr.node_run_id] = nr
    state.node_runs_by_node = {
        "safe": [safe_nr.node_run_id],
        "plain": [plain_nr.node_run_id],
        "eff": [eff_nr.node_run_id],
    }
    checkpoint.write_run_record(run_id, state.to_dict())

    # Worker whose recorded counts represent the calls that already happened
    # before the crash (safe ran once, plain started once, eff started once).
    worker = _CallCountWorker()
    worker.calls = {"safe": 1, "plain": 1, "eff": 1}

    env = resume(run_id, worker=worker)
    assert env["status"] == "partial", env  # safe+plain succeeded, eff failed

    rec = checkpoint.load_run_record(run_id)
    by_node = {nrd["node_id"]: nrd for nrid, nrd in rec["node_runs"].items()}

    # F6: side-effecting `eff` -> failed INTERRUPTED, NOT re-run (count stays 1)
    assert by_node["eff"]["status"] == "failed", by_node["eff"]
    assert by_node["eff"]["error"]["code"] == "INTERRUPTED", by_node["eff"]["error"]
    assert worker.calls["eff"] == 1, worker.calls

    # F6: succeeded `safe` is NOT re-finalized / double-run (count stays 1)
    assert by_node["safe"]["status"] == "succeeded", by_node["safe"]
    assert worker.calls["safe"] == 1, worker.calls

    # F6: plain running node resumes to ready and IS re-run (count 1 -> 2)
    assert by_node["plain"]["status"] == "succeeded", by_node["plain"]
    assert worker.calls["plain"] == 2, worker.calls


def test_resume_retries_failed_with_retry_flag(wf_home):
    """resume(retry_failed=True) re-runs a failed node. Companion to F6 to prove
    the INTERRUPTED node is retriable when explicitly requested."""
    from workflow import compile_text, resume
    from workflow.runtime.driver import NodeRun, RunState
    from workflow.store import checkpoint

    vir = compile_text(RESUME_YAML, phase1_warn_overrides=True)
    run_id = "wf_retrytestabcd"

    safe_nr = NodeRun(node_run_id=f"{run_id}__safe__aaaa", node_id="safe",
                      kind="agent", status="succeeded", output={"text": "done"})
    eff_nr = NodeRun(node_run_id=f"{run_id}__eff__bbbb", node_id="eff",
                     kind="agent", status="failed",
                     error={"code": "INTERRUPTED", "message": "x", "retriable": True})
    state = RunState(run_id=run_id, workflow_id="resumewf", status="running",
                     succeeded=["safe"], failed=["eff"])
    state.node_runs[safe_nr.node_run_id] = safe_nr
    state.node_runs[eff_nr.node_run_id] = eff_nr
    state.node_runs_by_node = {"safe": [safe_nr.node_run_id], "eff": [eff_nr.node_run_id]}
    checkpoint.write_run_record(run_id, state.to_dict())

    worker = _CallCountWorker()  # counts start at 0
    env = resume(run_id, worker=worker, retry_failed=True)
    assert env["status"] == "succeeded", env  # eff re-run successfully
    assert worker.calls.get("eff", 0) == 1, worker.calls
    assert worker.calls.get("safe", 0) == 0, worker.calls  # safe not re-run


# ---------------------------------------------------------------------------
# dry_run + cancel
# ---------------------------------------------------------------------------


def test_dry_run_plans_ready_without_spawning(wf_home):
    from workflow import compile_text, run
    from workflow.store import fs

    vir = compile_text(LINEAR_YAML, phase1_warn_overrides=True)
    worker = _CallCountWorker()
    env = run(vir, input={}, worker=worker, dry_run=True)
    assert env["status"] == "dry_run", env
    # the entry node is in the planned ready set
    assert "a" in env["ready"], env
    # no node was spawned (worker never called; no node output dirs written)
    assert worker.calls == {}, worker.calls
    nodes_dir = fs.run_dir(env["run_id"]) / "nodes"
    if nodes_dir.exists():
        assert not any(nodes_dir.iterdir()), "dry_run must not write node outputs"


def test_cancel_marks_run_cancelled(wf_home):
    """cancel() is a record-level operation (the driver has no mid-run
    interrupt in Phase 1). It writes run.json status=cancelled + updates the
    index."""
    from workflow import compile_text, run, cancel, status
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(LINEAR_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    assert env["status"] == "succeeded", env

    res = cancel(env["run_id"])
    assert res["status"] == "cancelled", res
    st = status(env["run_id"])
    assert st["status"] == "cancelled", st


# ---------------------------------------------------------------------------
# Remediation pass 3 — conditional edges: parsed but never evaluated (bug repro)
# ---------------------------------------------------------------------------

CONDITIONAL_YAML = """
workflow: test_conditional
version: 1
nodes:
  - id: check
    kind: agent
    spec: {prompt: "Score it"}
  - id: stop_path
    kind: script
    run: workflow.examples.echo
    input: {branch: "stop"}
  - id: continue_path
    kind: script
    run: workflow.examples.echo
    input: {branch: "continue"}
edges:
  - { from: check, to: stop_path, condition: "$.score < 5" }
  - { from: check, to: continue_path, condition: "$.score >= 5" }
triggers:
  - { kind: manual }
"""

CHAIN_YAML = """
workflow: cond_chain
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "Do A"}
  - id: b
    kind: agent
    spec: {prompt: "Do B"}
  - id: c
    kind: agent
    spec: {prompt: "Do C"}
edges:
  - { from: a, to: b, condition: "$.go == true" }
  - { from: b, to: c }
triggers:
  - { kind: manual }
"""

MALFORMED_CONDITION_YAML = """
workflow: bad_cond
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "Do A"}
  - id: b
    kind: agent
    spec: {prompt: "Do B"}
edges:
  - { from: a, to: b, condition: "score should be low" }
triggers:
  - { kind: manual }
"""


def test_conditional_edge_only_satisfied_branch_runs(wf_home):
    """The pass-3 bug repro: `condition:` on an edge was parsed (Edge.condition
    round-trips fine) but `eval_condition` was never called anywhere in the
    driver, so BOTH mutually-exclusive branches ran. Only the edge whose
    condition is true against the upstream output should execute; the other
    must land in `skipped` — not `succeeded`, not `failed`."""
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(CONDITIONAL_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(outputs={"check": {"score": 3}}))
    assert env["status"] == "succeeded", env
    assert "stop_path" in env["succeeded"], env
    assert "continue_path" not in env["succeeded"], env
    assert "continue_path" not in env["failed"], env
    assert "continue_path" in env["skipped"], env


def test_conditional_edge_opposite_score_flips_branch(wf_home):
    """Same graph, opposite literal — confirms the driver evaluates the
    condition (not just always taking the first/last edge)."""
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(CONDITIONAL_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(outputs={"check": {"score": 9}}))
    assert env["status"] == "succeeded", env
    assert "continue_path" in env["succeeded"], env
    assert "stop_path" in env["skipped"], env


def test_skip_propagates_transitively_through_chain(wf_home):
    """F3.2: A -cond false-> B -unconditional-> C. B is skipped because its
    inbound condition is false; C must also be skipped (it is downstream of
    only a skipped node), and the run must finalize rather than deadlock in
    `running`/`partial` forever."""
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(CHAIN_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(outputs={"a": {"go": False}}))
    assert env["status"] == "succeeded", env
    assert env["succeeded"] == ["a"], env["succeeded"]
    assert set(env["skipped"]) == {"b", "c"}, env["skipped"]
    assert env["failed"] == [], env["failed"]


def test_unconditional_edges_unaffected_by_condition_wiring(wf_home):
    """Regression guard: a plain edge (condition: None) behaves exactly as
    before this fix — upstream succeeded is sufficient, no skip involved."""
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(LINEAR_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    assert env["status"] == "succeeded", env
    assert env["succeeded"] == ["a", "b", "c"], env["succeeded"]
    assert env["skipped"] == [], env["skipped"]


def test_malformed_condition_rejected_at_compile_time(wf_home):
    """F3.3: `hermes workflow validate` (compile_text/verify_ir) must reject a
    malformed `condition:` string before any run starts, not blow up mid-run."""
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    with pytest.raises(WorkflowRejected) as exc:
        compile_text(MALFORMED_CONDITION_YAML, phase1_warn_overrides=True)
    codes = [i.code for i in exc.value.issues if i.severity == "error"]
    assert "CONDITION" in codes, codes
