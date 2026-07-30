"""Smoke test for the workflow package (Phase 1 acceptance subset).

Proves:
  (a) import works + a linear 3-node FakeWorker run -> RunEnvelope.status succeeded;
  (b) fanout 3 -> join -> 3 distinct node_run_id output.json paths (F1);
  (c) `hermes workflow validate` on minimal_workflow.yaml succeeds under
      phase1_warn_overrides (minimal_workflow.yaml sets tools: on agent nodes,
      which F2 strict-rejects; warn-mode accepts with warnings — documented in
      IMPLEMENTATION_LOG).

Hermetic: each test uses a fresh temp HERMES_HOME (tmp_path + monkeypatch).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MINIMAL_YAML = REPO_ROOT / "docs" / "proposals" / "workflow-dispatch" / "minimal_workflow.yaml"


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """Fresh HERMES_HOME per test (hermetic)."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "workflow:\n  enabled: true\n  phase1_warn_overrides: true\n  max_budget_usd: 5.0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Force FakeWorker for any CLI run path
    monkeypatch.setenv("HERMES_WORKFLOW_FAKE", "1")
    yield


LINEAR_YAML = """
workflow: linear_brief
version: 1
defaults:
  model: openrouter/x-ai/grok-4.5
nodes:
  - id: a
    kind: agent
    spec:
      prompt: "Do step A"
  - id: b
    kind: agent
    spec:
      prompt: "Do step B using {{ a.output }}"
  - id: c
    kind: script
    run: workflow.examples.echo
    input:
      text: "{{ b.output }}"
edges:
  - { from: a, to: b }
  - { from: b, to: c }
triggers:
  - { kind: manual }
"""

FANOUT_YAML = """
workflow: fanout_join
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input:
      items: [alpha, beta, gamma]
  - id: branches
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec:
        prompt: "Process branch {{ branch }}"
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


def _run_yaml(yaml_text, tmp_path):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(yaml_text, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    return vir, env


def test_imports():
    import workflow
    from workflow import compile_file, run, status, resume, cancel, decide_gate, Workflow, verify
    assert callable(compile_file)
    assert callable(run)
    assert callable(status)
    assert callable(verify)
    import workflow.cli
    assert workflow.cli is not None


def test_linear_three_node_success(tmp_path):
    vir, env = _run_yaml(LINEAR_YAML, tmp_path)
    assert env["status"] == "succeeded", env
    assert env["succeeded"] == ["a", "b", "c"], env["succeeded"]
    # run_output.json written
    from workflow.store import fs
    out_path = fs.run_output_path(env["run_id"])
    assert out_path.exists(), f"run_output.json missing at {out_path}"


def test_fanout_join_three_distinct_paths(tmp_path):
    vir, env = _run_yaml(FANOUT_YAML, tmp_path)
    assert env["status"] == "succeeded", env
    assert "branches" in env["succeeded"]
    assert "join" in env["succeeded"]
    from workflow.store import fs
    # collect branch node_run_id output paths (F1: keyed by node_run_id)
    nodes_dir = fs.run_dir(env["run_id"]) / "nodes"
    branch_outputs = []
    for nr_dir in nodes_dir.iterdir():
        if not nr_dir.is_dir():
            continue
        # branch node_run_ids contain "branches#" (the fanout node itself has no "#")
        if "branches#" in nr_dir.name:
            out_file = nr_dir / "output.json"
            if out_file.exists():
                branch_outputs.append(nr_dir.name)
    assert len(branch_outputs) == 3, f"expected 3 distinct branch node_run_id paths, got {branch_outputs}"
    # all distinct (F1: no collision)
    assert len(set(branch_outputs)) == 3
    # join output references 3 branches
    join_state = None
    from workflow.store import checkpoint
    rec = checkpoint.load_run_record(env["run_id"])
    for nrid, nrd in rec.get("node_runs", {}).items():
        if nrd.get("node_id") == "join":
            join_state = nrd
            break
    assert join_state is not None
    join_out = join_state.get("output", {})
    assert join_out.get("count") == 3 or len(join_out.get("branches", [])) == 3 or len(join_out.get("selected", [])) == 3, join_out


def test_validate_minimal_yaml_warn_mode(tmp_path):
    """validate minimal_workflow.yaml under phase1_warn_overrides -> exit 0.

    minimal_workflow.yaml sets tools: [web_search] on agent nodes; under strict
    F2 this would be rejected. Under warn-mode validate accepts with warnings.
    (Acceptance: validate reaches the verifier and returns cleanly for a
    compliant-enough yaml; the tools: tension is logged in IMPLEMENTATION_LOG.)
    """
    import subprocess
    import sys

    # Run `python -m workflow.cli`-style invocation via the cli module directly.
    from workflow.cli import _cmd_validate
    import argparse

    args = argparse.Namespace(path=str(MINIMAL_YAML), warn_overrides=True)
    rc = _cmd_validate(args)
    assert rc == 0, f"validate should succeed in warn-mode, got rc={rc}"


def test_dry_run_plans_ready_set(tmp_path):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(LINEAR_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(), dry_run=True)
    assert env["status"] == "dry_run"
    assert "a" in env["ready"], env


def test_validate_rejects_unregistered_run(tmp_path):
    """F4: a run: callable not in the allowlist is rejected (exit 2)."""
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    bad = """
workflow: bad_run
version: 1
nodes:
  - id: s
    kind: script
    run: os.system
edges: []
triggers:
  - { kind: manual }
"""
    with pytest.raises(WorkflowRejected):
        compile_text(bad, phase1_warn_overrides=True)


def test_fanout_cardinality_no_overspawn(tmp_path):
    """F5: over list > max_branches -> node failed CARDINALITY, no overspawn."""
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    yaml = """
workflow: cap
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input:
      items: [a, b, c, d, e]
  - id: br
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 3
    branch:
      kind: agent
      spec:
        prompt: "p {{ branch }}"
  - id: jn
    kind: join
    from: [br]
    reduce: { type: concat }
edges:
  - { from: seed, to: br }
  - { from: br, to: j }
edges2: []
"""
    # fix the yaml (remove stray edges2)
    yaml = yaml.replace("edges2: []", "")
    # also fix the join id mismatch (jn vs j)
    yaml = yaml.replace("- { from: br, to: j }", "- { from: br, to: jn }")
    vir = compile_text(yaml, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    assert "br" in env["failed"], env
    # no branch node-runs spawned (no overspawn)
    from workflow.store import fs, checkpoint
    rec = checkpoint.load_run_record(env["run_id"])
    branch_runs = [nr for nr in rec.get("node_runs", {}).values() if nr.get("branch_index") is not None]
    assert len(branch_runs) == 0, f"expected no spawned branches, got {len(branch_runs)}"