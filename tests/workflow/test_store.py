"""Store / resume invariants — test-plan §2 (store) + AUDIT F1, F11, F17.

F1  (P0): fanout N branches -> N DISTINCT nodes/<node_run_id>/output.json, no
           overwrite (the collision that made the Phase 1 fanout acceptance
           criterion impossible).
F11      : sqlite index REUSES hermes_state hardening helpers (not reinvented).
F17      : run_id is uuid4-based (wf_<hex>), collision-free across runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FANOUT_YAML = """
workflow: fanout_store
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

LINEAR_YAML = """
workflow: linear_store
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "Do A"}
  - id: b
    kind: agent
    spec: {prompt: "Do B"}
edges:
  - { from: a, to: b }
triggers:
  - { kind: manual }
"""


class _BranchWorker:
    """A FakeWorker-style worker whose branch output is distinct per branch item
    (so F1 content-distinctness is observable; the default FakeWorker yields
    identical output for every branch since they share one branch node id)."""

    def __init__(self):
        self.calls = 0

    def run_node(self, node, ctx):
        self.calls += 1
        # branch runs carry ctx["branch"]; the materialized branch node shares
        # the id "branches_branch" for all branches, so distinguish by branch item.
        if "branch" in ctx and node.kind == "agent" and node.id == "branches_branch":
            item = ctx["branch"]
            return {"output": {"item": item, "marker": f"b-{item}"}, "cost_usd": 0.0}
        return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}


# ---------------------------------------------------------------------------
# F1 — the P0 store-path collision
# ---------------------------------------------------------------------------


def test_f1_fanout_branches_distinct_node_run_id_paths(wf_home):
    """F1 (P0): 3 fanout branches produce 3 DISTINCT nodes/<node_run_id>/output.json
    paths (no overwrite), each with distinct content."""
    from workflow import compile_text, run
    from workflow.store import fs

    vir = compile_text(FANOUT_YAML, phase1_warn_overrides=True)
    worker = _BranchWorker()
    env = run(vir, input={}, worker=worker)
    assert env["status"] == "succeeded", env

    nodes_dir = fs.run_dir(env["run_id"]) / "nodes"
    branch_paths = []
    branch_contents = []
    for nr_dir in sorted(nodes_dir.iterdir()):
        if not nr_dir.is_dir():
            continue
        # fanout branch node_run_ids contain "branches#"
        if "branches#" in nr_dir.name:
            out_file = nr_dir / "output.json"
            assert out_file.exists(), f"missing output.json at {out_file}"
            branch_paths.append(str(out_file))
            branch_contents.append(json.loads(out_file.read_text(encoding="utf-8")))

    assert len(branch_paths) == 3, branch_paths
    # F1: the 3 path STRINGS are mutually distinct (keyed by node_run_id)
    assert len(set(branch_paths)) == 3, branch_paths
    # F1: each file carries distinct content (no overwrite clobbered a sibling)
    assert len(set(json.dumps(c, sort_keys=True) for c in branch_contents)) == 3, branch_contents
    # the fanout node's own envelope (non-branch) lists the 3 branch node_run_ids
    fanout_dirs = [d.name for d in nodes_dir.iterdir() if d.is_dir() and "branches#" not in d.name and "branches" in d.name]
    assert len(fanout_dirs) == 1, fanout_dirs


# ---------------------------------------------------------------------------
# Atomic checkpoint + valid JSON
# ---------------------------------------------------------------------------


def test_checkpoint_and_run_json_are_valid_json(wf_home):
    """After a completed run, checkpoint.json and run.json both parse as JSON.

    (fs.atomic_write uses temp-file + os.replace; a completed run leaves valid
    files — the atomic-replace survival property.)
    """
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker
    from workflow.store import fs

    vir = compile_text(LINEAR_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    assert env["status"] == "succeeded", env

    ckpt = fs.checkpoint_path(env["run_id"])
    rj = fs.run_json_path(env["run_id"])
    assert ckpt.exists() and rj.exists()
    c = json.loads(ckpt.read_text(encoding="utf-8"))
    r = json.loads(rj.read_text(encoding="utf-8"))
    assert c["run_id"] == env["run_id"]
    assert r["run_id"] == env["run_id"]
    # checkpoint.json is the last per-step commit (status may still be
    # "running"); run.json is the authoritative final record written by
    # finalize() — so run.json carries the terminal status.
    assert r["status"] == "succeeded"


# ---------------------------------------------------------------------------
# F17 — run_id is uuid4-based, collision-free
# ---------------------------------------------------------------------------


def test_f17_run_id_format_and_uniqueness(wf_home):
    """F17: run_id is uuid4-based (prefix wf_ + hex), and two runs do not collide."""
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    vir = compile_text(LINEAR_YAML, phase1_warn_overrides=True)
    env1 = run(vir, input={}, worker=FakeWorker())
    env2 = run(vir, input={}, worker=FakeWorker())

    for rid in (env1["run_id"], env2["run_id"]):
        assert rid.startswith("wf_"), rid
        suffix = rid[len("wf_"):]
        # uuid4 hex[:12] -> 12 hex chars (not a trivially short/colliding id)
        assert len(suffix) == 12 and all(ch in "0123456789abcdef" for ch in suffix), rid

    assert env1["run_id"] != env2["run_id"]


# ---------------------------------------------------------------------------
# F11 — sqlite index reuses hermes_state hardening; FS source-of-truth fallback
# ---------------------------------------------------------------------------


def test_f11_sqlite_index_reuses_hermes_state_helpers():
    """F11: the index module imports hermes_state WAL/journal hardening helpers
    rather than reinventing them."""
    from workflow.store import index

    # The module imports these names from hermes_state (proven at import time).
    assert hasattr(index, "apply_wal_with_fallback")
    # the hardening helpers must be the real hermes_state callables (same object)
    import hermes_state

    assert index.apply_wal_with_fallback is hermes_state.apply_wal_with_fallback


def test_index_created_and_list_status_query_it(wf_home):
    """After a run: index.sqlite exists; list/status query it; and FS is the
    source of truth (deleting the index still lets `status` read run.json)."""
    from workflow import compile_text, run, status
    from workflow.runtime.worker import FakeWorker
    from workflow.store import fs, index

    vir = compile_text(LINEAR_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    rid = env["run_id"]

    # index.sqlite was created
    assert fs.index_path().exists(), "index.sqlite not created"
    rows = index.list_runs()
    assert any(r["run_id"] == rid for r in rows), rows
    # status via the index fast-path is consistent
    st = status(rid)
    assert st["run_id"] == rid
    assert st["workflow_id"] == "linear_store"
    assert st["status"] == "succeeded"

    # FS source-of-truth fallback: delete the index, status still works (run.json)
    idx_p = fs.index_path()
    if idx_p.exists():
        idx_p.unlink()
        # also remove wal/shm sidecars if present
        for side in ("-wal", "-shm"):
            side_p = idx_p.with_name(idx_p.name + side)
            if side_p.exists():
                side_p.unlink()
    st2 = status(rid)
    assert st2["run_id"] == rid
    assert st2["status"] == "succeeded"

    # rebuild_index restores the index from FS
    n = index.rebuild_index()
    assert n >= 1, n
    rows2 = index.list_runs()
    assert any(r["run_id"] == rid for r in rows2), rows2
