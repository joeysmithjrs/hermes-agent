"""Backward route / loop-back (post-Phase-3 §6).

The design claim under test: routing "backwards" is a NEW run linked by
recorded lineage, never a cycle in the graph. So the properties are

* the verifier still rejects an actual cycle;
* `restart` re-runs the source run's OWN workflow with no path argument,
  producing a fresh run_id while the source's artifacts stay intact;
* the new run records `from_run` (source, its status, and which slice of its
  envelope was fed forward) on the checkpoint, the envelope and `status()`;
* input selection is the shared `chain.py` path -- one implementation behind
  `run --from-run`, `chain` and `restart` alike.
"""

from __future__ import annotations

import argparse
import json

import pytest


SOURCE_YAML = """
workflow: loop_source
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "seed"}
edges: []
triggers:
  - { kind: manual }
"""


def _run_once(yaml_text=SOURCE_YAML, **input_kwargs):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(yaml_text)
    return run(vir, input=input_kwargs or {}, worker=FakeWorker())


def _restart_args(run_id, **over):
    args = dict(
        run_id=run_id,
        input_from_run=None,
        select=None,
        as_key="from_run",
        input=None,
        allow_incomplete=False,
        dry_run=False,
        max_budget_usd=None,
        fake=True,
    )
    args.update(over)
    return argparse.Namespace(**args)


# ---------------------------------------------------------------------------
# the graph stays acyclic
# ---------------------------------------------------------------------------


def test_verify_still_rejects_a_real_cycle(wf_home):
    """Loop-back sugar must not have bought a cycle by the back door."""
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    yaml_text = """
workflow: cyclic
nodes:
  - {id: a, kind: agent, spec: {prompt: "a"}}
  - {id: b, kind: agent, spec: {prompt: "b"}}
edges:
  - { from: a, to: b }
  - { from: b, to: a }
"""
    with pytest.raises(WorkflowRejected) as exc:
        compile_text(yaml_text)
    assert "CYCLE" in {i.code for i in exc.value.issues if i.severity == "error"}


# ---------------------------------------------------------------------------
# lineage
# ---------------------------------------------------------------------------


def test_build_lineage_records_source_and_selection():
    from workflow.chain import build_lineage

    lineage = build_lineage(
        {"run_id": "wf_a", "workflow_id": "w", "status": "succeeded", "cost_usd": 1.0},
        select="succeeded.0",
        as_key="prev",
    )
    assert lineage == {
        "run_id": "wf_a",
        "workflow_id": "w",
        "status": "succeeded",
        "select": "succeeded.0",
        "as": "prev",
    }


def test_resolve_chain_returns_input_and_lineage_from_one_read(wf_home):
    from workflow.chain import resolve_chain

    src = _run_once()
    resolved = resolve_chain(src["run_id"], select="status", as_key="prev")

    assert resolved["input"]["prev"] == "succeeded"
    assert resolved["input"]["source_run_id"] == src["run_id"]
    assert resolved["lineage"]["run_id"] == src["run_id"]
    assert resolved["lineage"]["status"] == "succeeded"
    assert resolved["lineage"]["select"] == "status"


def test_resolve_chain_input_still_returns_only_the_input(wf_home):
    """The pre-existing single-purpose entry point keeps its exact shape."""
    from workflow.chain import resolve_chain_input

    src = _run_once()
    derived = resolve_chain_input(src["run_id"])
    assert "from_run" in derived and "lineage" not in derived


def test_chained_run_records_lineage_on_checkpoint_and_envelope(wf_home, capsys):
    from workflow import status
    from workflow.cli import _cmd_run
    from workflow.store import checkpoint

    src = _run_once()
    args = argparse.Namespace(
        path_or_id=None,
        input=None,
        resume=None,
        from_node=None,
        retry_failed=False,
        dry_run=False,
        max_budget_usd=None,
        fake=True,
        from_run=src["run_id"],
        select="status",
        as_key="prev",
        allow_incomplete=False,
    )
    # `chain`/`run --from-run` need a target path; write the source yaml out
    path = wf_home / "loop.yaml"
    path.write_text(SOURCE_YAML, encoding="utf-8")
    args.path_or_id = str(path)

    assert _cmd_run(args) == 0
    env = json.loads(capsys.readouterr().out)

    assert env["run_id"] != src["run_id"]
    assert env["from_run"]["run_id"] == src["run_id"]
    assert env["from_run"]["select"] == "status"
    assert env["from_run"]["as"] == "prev"
    # durable, not just in the printed envelope
    assert checkpoint.load_run_record(env["run_id"])["from_run"]["run_id"] == src["run_id"]
    assert status(env["run_id"])["from_run"]["run_id"] == src["run_id"]


def test_unchained_run_has_no_lineage(wf_home):
    from workflow import status

    env = _run_once()
    assert env["from_run"] is None
    assert status(env["run_id"])["from_run"] is None


def test_pre_lineage_checkpoints_still_load(wf_home):
    """A checkpoint written before this field existed must not fail to load."""
    from workflow.runtime.driver import RunState

    state = RunState.from_dict({"run_id": "wf_old", "workflow_id": "w", "status": "succeeded"})
    assert state.from_run is None


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


def test_restart_reruns_the_same_workflow_without_a_path(wf_home, capsys):
    from workflow.cli import _cmd_restart

    src = _run_once()
    capsys.readouterr()

    assert _cmd_restart(_restart_args(src["run_id"])) == 0
    env = json.loads(capsys.readouterr().out)

    assert env["run_id"] != src["run_id"]
    assert env["workflow_id"] == "loop_source"
    assert env["status"] == "succeeded"
    assert env["from_run"]["run_id"] == src["run_id"]


def test_restart_leaves_the_source_run_intact(wf_home, capsys):
    """A backward route is a new run: the previous run's record is not touched,
    which is exactly what a real graph cycle would have cost us."""
    from workflow import status
    from workflow.cli import _cmd_restart
    from workflow.store import checkpoint

    src = _run_once()
    before = checkpoint.load_run_record(src["run_id"])
    capsys.readouterr()

    _cmd_restart(_restart_args(src["run_id"]))
    capsys.readouterr()

    assert checkpoint.load_run_record(src["run_id"]) == before
    assert status(src["run_id"])["status"] == "succeeded"


def test_restart_feeds_the_selected_slice_forward(wf_home, capsys):
    from workflow.cli import _cmd_restart
    from workflow.store import checkpoint

    src = _run_once()
    capsys.readouterr()

    _cmd_restart(_restart_args(src["run_id"], select="workflow_id", as_key="prev_wf"))
    env = json.loads(capsys.readouterr().out)

    rec = checkpoint.load_run_record(env["run_id"])
    assert rec["from_run"]["select"] == "workflow_id"
    assert rec["from_run"]["as"] == "prev_wf"


def test_restart_input_from_run_seeds_from_a_different_run(wf_home, capsys):
    from workflow.cli import _cmd_restart

    first = _run_once()
    second = _run_once()
    capsys.readouterr()

    # restart FIRST's workflow, but seeded from SECOND's output
    _cmd_restart(_restart_args(first["run_id"], input_from_run=second["run_id"]))
    env = json.loads(capsys.readouterr().out)

    assert env["from_run"]["run_id"] == second["run_id"]
    assert env["run_id"] not in (first["run_id"], second["run_id"])


def test_restart_chains_repeatedly_with_a_traceable_lineage(wf_home, capsys):
    """Three hops of loop-back: each run points at the one that seeded it, so
    the history a cycle would have blurred stays reconstructable."""
    from workflow.cli import _cmd_restart
    from workflow.store import checkpoint

    run_ids = [_run_once()["run_id"]]
    capsys.readouterr()
    for _ in range(2):
        _cmd_restart(_restart_args(run_ids[-1]))
        run_ids.append(json.loads(capsys.readouterr().out)["run_id"])

    assert len(set(run_ids)) == 3
    for child, parent in zip(run_ids[1:], run_ids[:-1]):
        assert checkpoint.load_run_record(child)["from_run"]["run_id"] == parent


def test_restart_refuses_an_unknown_run(wf_home, capsys):
    from workflow.cli import _cmd_restart

    assert _cmd_restart(_restart_args("wf_nope")) == 1
    assert "no run" in capsys.readouterr().err


def test_restart_refuses_a_non_terminal_source(wf_home, capsys):
    from workflow.cli import _cmd_restart
    from workflow.runtime.driver import RunState
    from workflow.store import checkpoint

    src = _run_once()
    state = RunState.from_dict(checkpoint.load_run_record(src["run_id"]))
    state.status = "running"
    checkpoint.write_run_record(src["run_id"], state.to_dict())

    assert _cmd_restart(_restart_args(src["run_id"])) == 1
    assert "not one of" in capsys.readouterr().err

    # ...unless the operator opts in explicitly
    assert _cmd_restart(_restart_args(src["run_id"], allow_incomplete=True)) == 0


def test_restart_rejects_a_tampered_definition(wf_home, capsys):
    """The stored definition is re-verified at load, not trusted because some
    earlier run used it."""
    from workflow.cli import _cmd_restart
    from workflow.store import fs

    src = _run_once()
    defp = fs.definition_path("loop_source")
    doc = fs.read_json(defp)
    doc["ir"]["nodes"].append({"id": "evil", "kind": "script", "run": "os.system"})
    fs.atomic_write_json(defp, doc)
    capsys.readouterr()

    assert _cmd_restart(_restart_args(src["run_id"])) == 2
    assert "re-verification" in capsys.readouterr().err


def test_restart_dry_run_plans_without_executing(wf_home, capsys):
    from workflow.cli import _cmd_restart
    from workflow.store import index

    src = _run_once()
    capsys.readouterr()

    assert _cmd_restart(_restart_args(src["run_id"], dry_run=True)) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["status"] == "dry_run"
    assert env["ready"] == ["a"]


# ---------------------------------------------------------------------------
# resume --from-node (the pre-existing surface, spelled out)
# ---------------------------------------------------------------------------


def test_run_accepts_from_node_as_an_alias_of_from(wf_home):
    from workflow.cli import register_subparser
    import argparse as _argparse

    parser = _argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register_subparser(sub)

    long_form = parser.parse_args(["workflow", "run", "wf.yaml", "--from-node", "b"])
    short_form = parser.parse_args(["workflow", "run", "wf.yaml", "--from", "b"])
    assert long_form.from_node == short_form.from_node == "b"


def test_resume_from_node_reruns_only_that_node(wf_home):
    """`--from-node` on a resume is the in-run backward step: an already-
    succeeded node is re-run without restarting the whole workflow."""
    import workflow
    from workflow.runtime.worker import FakeWorker

    yaml_text = """
workflow: resume_from_node
version: 1
nodes:
  - {id: a, kind: agent, spec: {prompt: "a"}}
  - {id: b, kind: agent, spec: {prompt: "b {{ a.output.text }}"}}
edges:
  - { from: a, to: b }
triggers:
  - { kind: manual }
"""
    vir = workflow.compile_text(yaml_text)
    first = workflow.run(vir, worker=FakeWorker())
    assert first["status"] == "succeeded"

    class _Counting(FakeWorker):
        def __init__(self):
            super().__init__()
            self.seen = []

        def run_node(self, node, ctx):
            self.seen.append(node.id)
            return super().run_node(node, ctx)

    worker = _Counting()
    env = workflow.resume(first["run_id"], worker=worker, from_node="b", retry_failed=True)
    assert env["run_id"] == first["run_id"]  # a resume, not a new run
    assert worker.seen == []  # b already succeeded; --from-node only re-readies failures
    assert env["status"] == "succeeded"
