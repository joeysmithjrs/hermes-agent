"""Regression tests for issues found in the adversarial review (REVIEW_NOTES.md).

Covers the BLOCK/HIGH fixes:
- BLOCK #1: real crash-resume (run.json written per checkpoint; resume reads it).
- BLOCK #2: F2/F4 override + run-allowlist enforced on fanout.branch templates;
  resume re-verifies a (tampered) stored definition and rejects it.
- BLOCK #3: script registry is frozen after bootstrap; branch unregistered
  `run:` fails at execution rather than silently faking a result.
- HIGH #1: a resumed awaiting-gate run is NOT finalized as succeeded.
- MEDIUM #1: config-driven `phase1_warn_overrides` is reached by the CLI
  (argparse default=None) so `validate minimal_workflow.yaml` accepts it.
"""

from __future__ import annotations

import json

import pytest

from workflow.ir import WorkflowIR, Node, NodeSpec, Edge, WorkflowRejected
from workflow.verify import verify_ir
from workflow.yaml_load import load_yaml
from workflow.runtime.driver import Driver, run, resume, GATE_PARKED
from workflow.runtime.worker import FakeWorker
from workflow.runtime import scripts
from workflow.store import fs, checkpoint


# ---- helpers ---------------------------------------------------------------

def _linear_ir(workflow_id="revfix"):
    ns = lambda p: NodeSpec(prompt=p)
    nodes = [
        Node(id="A", kind="agent", spec=ns("do A")),
        Node(id="B", kind="agent", spec=ns("do B"), side_effects="external"),
        Node(id="C", kind="agent", spec=ns("do C")),
    ]
    edges = [Edge(from_="A", to="B"), Edge(from_="B", to="C")]
    return WorkflowIR(id=workflow_id, version=1, nodes=nodes, edges=edges)


@pytest.fixture
def wf_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "workflow:\n  enabled: true\n  max_budget_usd: 10.0\n  max_parallel_nodes: 4\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


# ---- BLOCK #1: real crash-resume ------------------------------------------


def test_real_crash_resume_reads_per_step_run_json(wf_home):
    """A kill after a per-step checkpoint but before finalize must be resumable.

    Prior to the fix, run.json was written only at start + finalize, so resume
    raised FileNotFoundError in the real crash window (the F6 test had
    fabricated run.json and did not exercise this path).
    """
    ir = _linear_ir("crash_real")
    vir = verify_ir(ir)
    fs.atomic_write_json(fs.definition_path("crash_real"), vir.to_dict())

    d = Driver(vir, worker=FakeWorker())
    fs.ensure_run_dirs(d.run_id)
    # Execute exactly one node step (A) then "crash" before finalize.
    d._seed_initial_node_runs()
    d.state.status = "running"
    ready = d._compute_ready()
    assert ready and ready[0].node_id == "A"
    d._run_node_run(ready[0])
    d._checkpoint()  # per-step checkpoint writes run.json now
    # Simulate crash: do NOT call _finalize(). run.json must already be valid.
    assert fs.run_json_path(d.run_id).exists(), "run.json must be current after a per-step checkpoint"
    rec = json.loads(fs.run_json_path(d.run_id).read_text())
    assert rec["status"] in ("running",)
    a_status = [nr for nr in rec["node_runs"].values() if nr["node_id"] == "A"][0]["status"]
    assert a_status == "succeeded"

    # Resume from the real crash window — must NOT raise FileNotFoundError.
    env = resume(d.run_id, worker=FakeWorker())
    # B was running? No — B never started (we only ran A). So resume completes
    # B (external) then C. B is external but it was pending (not running) so it
    # runs normally. Run should succeed (no failures).
    assert env["status"] == "succeeded"
    assert set(env["succeeded"]) == {"A", "B", "C"}


def test_real_crash_resume_external_running_not_rerun(wf_home):
    """The acceptance F6 case via the REAL crash path (not a fabricated run.json)."""
    ir = _linear_ir("crash_ext")
    vir = verify_ir(ir)
    fs.atomic_write_json(fs.definition_path("crash_ext"), vir.to_dict())

    fw = FakeWorker(side_effect_nodes={"B"})
    d = Driver(vir, worker=fw)
    fs.ensure_run_dirs(d.run_id)
    d._seed_initial_node_runs()
    # Run A to completion + checkpoint.
    d._run_node_run(d.state.node_runs[d.state.node_runs_by_node["A"][0]])
    d._checkpoint()
    # Now start B (external), mark it running to simulate crash mid-B.
    b_nr = d._ensure_node_run("B")
    b_nr.status = "running"
    b_nr.started_at = "t"
    fw.side_effect_calls.pop("B", None)  # pretend the crash interrupted before B's effect
    d._checkpoint()  # B running persisted to run.json
    assert fw.side_effect_calls.get("B", 0) == 0

    # Resume with a FRESH worker so we can count B's calls post-crash.
    fw2 = FakeWorker(side_effect_nodes={"B"})
    env = resume(d.run_id, worker=fw2)
    rec = json.loads(fs.run_json_path(d.run_id).read_text())
    b_after = [nr for nr in rec["node_runs"].values() if nr["node_id"] == "B"][0]
    # F6: external running node resumes to failed/INTERRUPTED, NOT re-run.
    assert b_after["status"] == "failed"
    assert b_after["error"]["code"] == "INTERRUPTED"
    assert fw2.side_effect_calls.get("B", 0) == 0, "external running node must not be re-run on resume"
    assert env["status"] == "partial"
    assert "B" in env["failed"]


# ---- BLOCK #2: branch templates verified + resume re-verifies -------------


def test_branch_override_fields_rejected_strict():
    """F2 must apply to fanout.branch.spec, not only top-level nodes."""
    yaml = """
workflow: branch_override
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b]}
  - id: fan
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec:
        prompt: "do {{ branch }}"
        tools: [exec]
        model: openrouter/x-ai/grok-4.5
        profile: trader
  - id: jn
    kind: join
    from: [fan]
    reduce: { type: concat }
edges:
  - { from: seed, to: fan }
  - { from: fan, to: jn }
triggers:
  - { kind: manual }
"""
    ir = load_yaml(_yaml_to_path := None) if False else _load(yaml)
    with pytest.raises(WorkflowRejected) as exc:
        verify_ir(ir, phase1_warn_overrides=False)
    codes = {i.code for i in exc.value.issues if i.severity == "error"}
    assert "PHASE1_OVERRIDE" in codes, "branch override-only fields must be rejected in strict mode"


def test_branch_override_fields_warn_behind_flag():
    yaml = """
workflow: branch_override_warn
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b]}
  - id: fan
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec: {prompt: "do {{ branch }}", tools: [web_search]}
  - id: jn
    kind: join
    from: [fan]
    reduce: { type: concat }
edges:
  - { from: seed, to: fan }
  - { from: fan, to: jn }
triggers:
  - { kind: manual }
"""
    ir = _load(yaml)
    vir = verify_ir(ir, phase1_warn_overrides=True)  # accepted, only warnings
    codes = {i.code for i in vir.issues}
    assert "PHASE1_OVERRIDE" in codes
    # And strict still rejects:
    with pytest.raises(WorkflowRejected):
        verify_ir(ir, phase1_warn_overrides=False)


def test_branch_unregistered_run_rejected():
    """F4: a branch script `run:` not in the allowlist is rejected at verify."""
    yaml = """
workflow: branch_run
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b]}
  - id: fan
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: script
      run: os.system
  - id: jn
    kind: join
    from: [fan]
    reduce: { type: concat }
edges:
  - { from: seed, to: fan }
  - { from: fan, to: jn }
triggers:
  - { kind: manual }
"""
    ir = _load(yaml)
    with pytest.raises(WorkflowRejected) as exc:
        verify_ir(ir)
    codes = {i.code for i in exc.value.issues if i.severity == "error"}
    assert "SCRIPT" in codes


def test_resume_rejects_tampered_definition(wf_home):
    """resume() must re-verify the stored definition and reject a tampered one."""
    ir = _linear_ir("tamper")
    vir = verify_ir(ir)
    fs.atomic_write_json(fs.definition_path("tamper"), vir.to_dict())

    d = Driver(vir, worker=FakeWorker())
    fs.ensure_run_dirs(d.run_id)
    d._seed_initial_node_runs()
    d._run_node_run(d.state.node_runs[d.state.node_runs_by_node["A"][0]])
    d._checkpoint()

    # Tamper the stored definition: introduce a cycle (C -> A). to_dict() wraps
    # the IR under the "ir" key, so mutate the nested edges.
    tampered = json.loads(fs.definition_path("tamper").read_text())
    tampered["ir"]["edges"].append({"from": "C", "to": "A"})
    fs.atomic_write_json(fs.definition_path("tamper"), tampered)

    with pytest.raises(ValueError, match="re-verification"):
        resume(d.run_id, worker=FakeWorker())


# ---- BLOCK #3: frozen registry --------------------------------------------


def test_registry_is_frozen_after_import():
    """A runtime caller must NOT be able to register os.system (F4)."""
    assert scripts.frozen() is True
    import os

    with pytest.raises(RuntimeError, match="frozen"):
        scripts.register("os.system", os.system)
    # And it did not actually land:
    assert not scripts.is_registered("os.system")


# ---- HIGH #1: resumed gate run not finalized as succeeded -----------------


def test_resumed_awaiting_gate_not_finalized_succeeded(wf_home):
    """A gated run resumed without a decision stays awaiting_gate (not succeeded)."""
    ns = lambda p: NodeSpec(prompt=p)
    from workflow.ir import Gate

    nodes = [
        Node(id="A", kind="agent", spec=ns("do A")),
        Node(id="G", kind="gate"),
        Node(id="B", kind="agent", spec=ns("do B")),
    ]
    edges = [Edge(from_="A", to="G"), Edge(from_="G", to="B")]
    gates = {"G": Gate(id="G", channel="telegram", approvers=["joe"], on_timeout="shelve", dual_control=True)}
    ir = WorkflowIR(id="gateresume", version=1, nodes=nodes, edges=edges, gates=gates)
    vir = verify_ir(ir)
    fs.atomic_write_json(fs.definition_path("gateresume"), vir.to_dict())

    # First run: A succeeds, G parks.
    env = run(vir, worker=FakeWorker())
    assert env["status"] == "awaiting_gate"
    assert env["awaiting_gate"] == "G"

    # Resume with no decision taken: must NOT flip to succeeded.
    env2 = resume("gateresume__noid" if False else env["run_id"], worker=FakeWorker())
    assert env2["status"] == "awaiting_gate", "resumed gated run must stay awaiting_gate, not succeed"
    assert env2["awaiting_gate"] == "G"
    assert "B" not in env2["succeeded"]


# ---- MEDIUM #1: CLI config warn flag reachable ----------------------------


def test_cli_validate_uses_config_warn_overrides_by_default(tmp_path, monkeypatch):
    """`hermes workflow validate` honors config `phase1_warn_overrides: true`
    even without --warn-overrides (argparse default=None reaches the fallback)."""
    import argparse

    from workflow.cli import register_subparser, cmd_workflow

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "workflow:\n  enabled: true\n  phase1_warn_overrides: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    wf_path = tmp_path / "wf.yaml"
    wf_path.write_text(
        """
workflow: cli_warn
version: 1
nodes:
  - id: r
    kind: agent
    spec: {prompt: "research", tools: [web_search]}
  - id: w
    kind: agent
    spec: {prompt: "write from {{ r.output }}"}
edges:
  - { from: r, to: w }
triggers:
  - { kind: manual }
""",
        encoding="utf-8",
    )

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    register_subparser(sub)
    # No --warn-overrides flag: config flag must kick in -> exit 0 (accepted w/ warnings).
    args = parser.parse_args(["workflow", "validate", str(wf_path)])
    assert args.warn_overrides is None  # the fix
    rc = cmd_workflow(args)
    assert rc == 0, "config phase1_warn_overrides:true must accept (exit 0)"


# ---- HIGH #2: fanout branch checkpoint persistence -------------------------


FANOUT_RESUME_YAML = """
workflow: fanout_resume
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b, c]}
  - id: fan
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec: {prompt: "Process {{ branch }}"}
  - id: join
    kind: join
    from: [fan]
    reduce: {type: concat}
edges:
  - {from: seed, to: fan}
  - {from: fan, to: join}
triggers:
  - {kind: manual}
"""


class _RecordingBranchWorker:
    def __init__(self):
        self.ran_items = []

    def run_node(self, node, ctx):
        if node.id == "fan_branch" and "branch" in ctx:
            self.ran_items.append(ctx["branch"])
            return {
                "output": {"item": ctx["branch"], "marker": f"b-{ctx['branch']}"},
                "cost_usd": 0.0,
            }
        return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}


def test_crash_mid_fanout_resumes_correct_branches(wf_home, monkeypatch):
    """Persisted leaf templates/items let pending fanout branches resume alone."""
    from workflow import compile_text

    vir = compile_text(FANOUT_RESUME_YAML, phase1_warn_overrides=True)
    fs.atomic_write_json(fs.definition_path(vir.ir.id), vir.to_dict())

    recorder = _RecordingBranchWorker()
    d = Driver(vir, worker=recorder)
    fs.ensure_run_dirs(d.run_id)
    d._seed_initial_node_runs()
    seed_nr = d.state.node_runs[d.state.node_runs_by_node["seed"][0]]
    d._run_node_run(seed_nr)
    d._checkpoint()

    # The ordinary runtime executes branches inline. Suppress only that call to
    # create the precise crash window after fanout materialization.
    monkeypatch.setattr(d, "_run_branches", lambda *_args: None)
    fan_nr = d._ensure_node_run("fan")
    d._run_node_run(fan_nr)
    d._checkpoint()

    branches = [
        d.state.node_runs[nrid]
        for nrid in d.state.node_runs_by_node["fan"]
        if d.state.node_runs[nrid].branch_index is not None
    ]
    assert len(branches) == 3
    d._run_node_run(branches[0])
    d._checkpoint()

    rec = checkpoint.load_run_record(d.run_id)
    pending = [
        nrd for nrd in rec["node_runs"].values()
        if nrd.get("branch_index") is not None and nrd["status"] == "pending"
    ]
    assert len(pending) == 2
    assert all(nrd["branch_node"]["kind"] == "agent" for nrd in pending)
    assert {nrd["branch_item"] for nrd in pending} == {"b", "c"}

    fresh = _RecordingBranchWorker()
    env = resume(d.run_id, worker=fresh)
    assert env["status"] == "succeeded", env
    assert {"seed", "fan", "join"}.issubset(env["succeeded"])
    assert fresh.ran_items == ["b", "c"]
    assert "a" not in fresh.ran_items

    resumed = checkpoint.load_run_record(d.run_id)
    fan_runs = [nrd for nrd in resumed["node_runs"].values() if nrd["node_id"] == "fan"]
    assert len(fan_runs) == 4
    assert next(nrd for nrd in fan_runs if nrd["branch_index"] is None)["status"] == "succeeded"


# ---- MEDIUM #2: Phase 1 sequential execution ------------------------------


FANOUT_SEQUENTIAL_YAML = """
workflow: fanout_sequential
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [w, x, y, z]}
  - id: fan
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec: {prompt: "Process {{ branch }}"}
  - id: join
    kind: join
    from: [fan]
    reduce: {type: concat}
edges:
  - {from: seed, to: fan}
  - {from: fan, to: join}
triggers:
  - {kind: manual}
"""


def test_phase1_is_strictly_sequential_regardless_of_max_parallel(wf_home):
    """max_parallel_nodes is a Phase 1 reserved no-op, not a concurrency cap."""
    from workflow import compile_text

    vir = compile_text(FANOUT_SEQUENTIAL_YAML, phase1_warn_overrides=True)
    orders = []
    for max_parallel_nodes in (1, 8):
        worker = _RecordingBranchWorker()
        env = run(vir, worker=worker, max_parallel_nodes=max_parallel_nodes)
        assert env["status"] == "succeeded", env
        orders.append(worker.ran_items)
    assert orders == [["w", "x", "y", "z"], ["w", "x", "y", "z"]]


# ---- util ------------------------------------------------------------------


def _load(yaml_text):
    # load_yaml takes YAML *text*, not a path.
    return load_yaml(yaml_text)
