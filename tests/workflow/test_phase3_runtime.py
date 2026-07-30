"""Phase 3 runtime acceptance tests: bounded concurrency, checkpoint-before-
dispatch durability, determinism across max_parallel_nodes, cost/token
rollup, and budget pause under parallelism.

Contract tests over brittle snapshots: no assertions on wall-clock timing or
thread-scheduling order; every concurrency claim is checked via durable
on-disk state (the checkpoint) or via succeeded/failed/skipped SETS, never
incidental list ordering.
"""

from __future__ import annotations

import threading

import pytest

# ---------------------------------------------------------------------------
# 7. max_parallel_nodes > 1: N branches complete correctly
# ---------------------------------------------------------------------------

FANOUT_N_YAML = """
workflow: fanout_parallel_n
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b, c, d, e, f]}
  - id: branches
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 10
    branch:
      kind: agent
      spec: {prompt: "p {{ branch }}"}
  - id: join
    kind: join
    from: [branches]
    reduce: {type: concat}
edges:
  - { from: seed, to: branches }
  - { from: branches, to: join }
triggers:
  - { kind: manual }
"""


class _CostedBranchWorker:
    def __init__(self, cost_per_branch=0.05, tin_per_branch=10, tout_per_branch=4):
        self.cost_per_branch = cost_per_branch
        self.tin_per_branch = tin_per_branch
        self.tout_per_branch = tout_per_branch
        self._lock = threading.Lock()
        self.seen_items = []

    def run_node(self, node, ctx):
        if "branch" in ctx:
            with self._lock:
                self.seen_items.append(ctx["branch"])
            return {
                "output": {"item": ctx["branch"], "marker": f"b-{ctx['branch']}"},
                "cost_usd": self.cost_per_branch,
                "tokens_in": self.tin_per_branch,
                "tokens_out": self.tout_per_branch,
            }
        return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}


def test_max_parallel_nodes_fanout_all_branches_unique_and_complete(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(FANOUT_N_YAML, phase1_warn_overrides=True)
    worker = _CostedBranchWorker(cost_per_branch=0.05)
    env = run(vir, input={}, worker=worker, max_parallel_nodes=3)
    assert env["status"] == "succeeded", env

    rec = checkpoint.load_run_record(env["run_id"])
    branch_runs = [nrd for nrd in rec["node_runs"].values() if nrd.get("branch_index") is not None]
    assert len(branch_runs) == 6, branch_runs

    node_run_ids = [b["node_run_id"] for b in branch_runs]
    assert len(set(node_run_ids)) == 6, "every branch must have a unique node_run_id"

    branch_indices = sorted(b["branch_index"] for b in branch_runs)
    assert branch_indices == [0, 1, 2, 3, 4, 5], "no branch lost or duplicated"

    for b in branch_runs:
        assert b["status"] == "succeeded", b
        assert b["output"] is not None, b

    items_seen = sorted(worker.seen_items)
    assert items_seen == ["a", "b", "c", "d", "e", "f"]

    # run-level aggregates: 6 branches * 0.05 each
    assert env["cost_usd"] == pytest.approx(0.3)
    assert env["tokens_in"] == 60
    assert env["tokens_out"] == 24


# ---------------------------------------------------------------------------
# 8. Checkpoint-before-dispatch under concurrency (the important one)
# ---------------------------------------------------------------------------


class _DiskCheckWorker:
    """Reads the ON-DISK checkpoint the moment run_node is entered and
    records what it saw there -- proving the whole dispatched batch was
    already durably marked `running` before any of them started (not just
    "before I, personally, started")."""

    def __init__(self, run_id, *, branch_mode: bool):
        self.run_id = run_id
        self.branch_mode = branch_mode
        self.observed_running_counts = []
        self._lock = threading.Lock()

    def run_node(self, node, ctx):
        from workflow.store import checkpoint as ckpt

        rec = ckpt.load_checkpoint(self.run_id)
        node_runs = rec.get("node_runs", {}) or {}
        if self.branch_mode:
            running = [nr for nr in node_runs.values() if nr.get("branch_index") is not None and nr.get("status") == "running"]
        else:
            running = [nr for nr in node_runs.values() if nr.get("branch_index") is None and nr.get("status") == "running"]
        with self._lock:
            self.observed_running_counts.append(len(running))
        return {"output": {"ok": True}, "cost_usd": 0.0}


BRANCH_WAVE_YAML = """
workflow: ckpt_branch_wave
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b, c, d]}
  - id: branches
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 10
    branch:
      kind: agent
      spec: {prompt: "p {{ branch }}"}
edges:
  - { from: seed, to: branches }
triggers:
  - { kind: manual }
"""


def test_checkpoint_before_dispatch_branch_wave(wf_home):
    """A whole fanout wave (4 branches, max_parallel_nodes=4 -> exactly one
    wave) must be durably marked `running` on disk BEFORE any future is
    submitted. Every branch's run_node, when it executes, must see ALL 4
    branches already `running` in the on-disk checkpoint -- not just
    itself."""
    from workflow import compile_text
    from workflow.runtime.driver import Driver

    vir = compile_text(BRANCH_WAVE_YAML, phase1_warn_overrides=True)
    run_id = "wf_ckptbranchwave01"
    worker = _DiskCheckWorker(run_id, branch_mode=True)
    d = Driver(vir, worker=worker, run_id=run_id, max_parallel_nodes=4)
    env = d.execute()

    assert env["status"] == "succeeded", env
    assert len(worker.observed_running_counts) == 4
    assert all(c == 4 for c in worker.observed_running_counts), worker.observed_running_counts


TOP_LEVEL_BATCH_YAML = """
workflow: ckpt_top_level_batch
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do a}
  - id: b
    kind: agent
    spec: {prompt: do b}
  - id: c
    kind: agent
    spec: {prompt: do c}
edges: []
triggers:
  - { kind: manual }
"""


def test_checkpoint_before_dispatch_top_level_batch(wf_home):
    """Same invariant for a top-level (non-fanout) ready-set batch: 3
    independent entry agent nodes, max_parallel_nodes=3 -> one batch. Each
    node's run_node must see all 3 already `running` on disk."""
    from workflow import compile_text
    from workflow.runtime.driver import Driver

    vir = compile_text(TOP_LEVEL_BATCH_YAML, phase1_warn_overrides=True)
    run_id = "wf_ckpttoplevelbatch01"
    worker = _DiskCheckWorker(run_id, branch_mode=False)
    d = Driver(vir, worker=worker, run_id=run_id, max_parallel_nodes=3)
    env = d.execute()

    assert env["status"] == "succeeded", env
    assert len(worker.observed_running_counts) == 3
    assert all(c == 3 for c in worker.observed_running_counts), worker.observed_running_counts


# ---------------------------------------------------------------------------
# 9. Determinism: max_parallel_nodes=1 vs. >1 produce the same result
# ---------------------------------------------------------------------------

REORDER_YAML = """
workflow: reorder_determinism
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [0, 1, 2, 3, 4]}
  - id: branches
    kind: map
    over: "{{ seed.output.echo.items }}"
    max_branches: 10
    branch:
      kind: agent
      spec: {prompt: "p {{ branch }}"}
    reduce: {type: concat}
edges:
  - { from: seed, to: branches }
triggers:
  - { kind: manual }
"""


class _ReversedSleepWorker:
    """Branch 0 sleeps longest, branch 4 sleeps shortest -- under real
    concurrency this reorders WALL-CLOCK completion (branch 4 finishes
    first), while the sequential path (max_parallel_nodes<=1) completes
    them strictly in index order regardless of sleep."""

    def __init__(self):
        self._lock = threading.Lock()
        self.completion_order = []

    def run_node(self, node, ctx):
        if "branch" in ctx:
            item = int(ctx["branch"])
            import time

            time.sleep((4 - item) * 0.02)
            with self._lock:
                self.completion_order.append(item)
            return {"output": {"item": item}, "cost_usd": 0.0}
        return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}


def test_determinism_across_max_parallel_nodes_out_of_order_completion(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(REORDER_YAML, phase1_warn_overrides=True)

    seq_worker = _ReversedSleepWorker()
    env_seq = run(vir, input={}, worker=seq_worker, max_parallel_nodes=1)
    # sequential: no reordering possible, completes in index order
    assert seq_worker.completion_order == [0, 1, 2, 3, 4]

    par_worker = _ReversedSleepWorker()
    env_par = run(vir, input={}, worker=par_worker, max_parallel_nodes=5)
    # concurrent: real wall-clock reordering actually happened (branch 4,
    # the shortest sleep, finishes before branch 0 -- proving this is a
    # genuine concurrency exercise, not an accidental no-op)
    assert par_worker.completion_order[0] == 4, par_worker.completion_order
    assert par_worker.completion_order != [0, 1, 2, 3, 4]

    # ...yet the FINAL result is identical regardless of completion order
    assert env_seq["status"] == env_par["status"] == "succeeded"
    assert set(env_seq["succeeded"]) == set(env_par["succeeded"])
    assert set(env_seq["failed"]) == set(env_par["failed"]) == set()
    assert set(env_seq["skipped"]) == set(env_par["skipped"]) == set()

    rec_seq = checkpoint.load_run_record(env_seq["run_id"])
    rec_par = checkpoint.load_run_record(env_par["run_id"])
    mapped_seq = next(nrd for nrd in rec_seq["node_runs"].values() if nrd["node_id"] == "branches" and nrd.get("branch_index") is None)
    mapped_par = next(nrd for nrd in rec_par["node_runs"].values() if nrd["node_id"] == "branches" and nrd.get("branch_index") is None)
    # same reduced content: branch envelopes sorted by branch index in BOTH
    seq_items = [b["output"]["item"] for b in mapped_seq["output"]["branches"]]
    par_items = [b["output"]["item"] for b in mapped_par["output"]["branches"]]
    assert seq_items == par_items == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# 10. Cost/token rollup
# ---------------------------------------------------------------------------

LINEAR_COST_YAML = """
workflow: linear_cost
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do a}
  - id: b
    kind: agent
    spec: {prompt: do b}
edges:
  - { from: a, to: b }
triggers:
  - { kind: manual }
"""


class _FixedCostWorker:
    def __init__(self, per_node):
        self.per_node = per_node

    def run_node(self, node, ctx):
        c = self.per_node.get(node.id, {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0})
        return {"output": {"ok": True}, **c}


def test_run_level_cost_and_tokens_increase_from_stub_worker(wf_home):
    from workflow import compile_text, run, status

    vir = compile_text(LINEAR_COST_YAML, phase1_warn_overrides=True)
    worker = _FixedCostWorker(
        {
            "a": {"cost_usd": 0.2, "tokens_in": 20, "tokens_out": 8},
            "b": {"cost_usd": 0.3, "tokens_in": 30, "tokens_out": 12},
        }
    )
    env = run(vir, input={}, worker=worker)
    assert env["status"] == "succeeded", env
    assert env["cost_usd"] == pytest.approx(0.5)
    assert env["tokens_in"] == 50
    assert env["tokens_out"] == 20

    st = status(env["run_id"])
    assert st["cost_usd"] == pytest.approx(0.5)
    assert st["tokens_in"] == 50
    assert st["tokens_out"] == 20


MAP_COST_YAML = """
workflow: map_cost_rollup
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b, c]}
  - id: mapped
    kind: map
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec: {prompt: "p {{ branch }}"}
    reduce: {type: concat}
edges:
  - { from: seed, to: mapped }
triggers:
  - { kind: manual }
"""


def test_map_node_own_envelope_reports_branch_sum_not_double_counted(wf_home):
    from workflow import compile_text, run, status
    from workflow.store import checkpoint

    class W:
        def run_node(self, node, ctx):
            if "branch" in ctx:
                return {"output": {"ok": True}, "cost_usd": 0.1, "tokens_in": 10, "tokens_out": 5}
            return {"output": {"ok": True}, "cost_usd": 0.0}

    vir = compile_text(MAP_COST_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=W(), max_parallel_nodes=1)
    assert env["status"] == "succeeded", env

    # 3 branches * 0.1 = 0.3 total -- NOT 0.6 (would be double-counted if
    # the map node's own rolled-up cost were ALSO added to the run total)
    assert env["cost_usd"] == pytest.approx(0.3)
    assert env["tokens_in"] == 30
    assert env["tokens_out"] == 15

    rec = checkpoint.load_run_record(env["run_id"])
    mapped_nr = next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "mapped" and nrd.get("branch_index") is None)
    # the map node's OWN envelope reports the branch SUM, for display
    assert mapped_nr["cost_usd"] == pytest.approx(0.3)
    assert mapped_nr["tokens_in"] == 30
    assert mapped_nr["tokens_out"] == 15

    st = status(env["run_id"])
    assert st["cost_usd"] == pytest.approx(0.3)
    assert st["tokens_in"] == 30
    assert st["tokens_out"] == 15


# ---------------------------------------------------------------------------
# 11. Budget pause under parallelism
# ---------------------------------------------------------------------------

BUDGET_FANOUT_YAML = """
workflow: budget_fanout
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b, c, d]}
  - id: branches
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 10
    branch:
      kind: agent
      spec: {prompt: "p {{ branch }}"}
  - id: after
    kind: script
    run: workflow.examples.echo
    input: {done: true}
edges:
  - { from: seed, to: branches }
  - { from: branches, to: after }
triggers:
  - { kind: manual }
"""


class _PerBranchCostWorker:
    def __init__(self, cost=1.0):
        self.cost = cost
        self.calls = []

    def run_node(self, node, ctx):
        self.calls.append(node.id if "branch" not in ctx else f"branch:{ctx['branch']}")
        if "branch" in ctx:
            return {"output": {"ok": True}, "cost_usd": self.cost}
        return {"output": {"ok": True}, "cost_usd": 0.0}


def test_budget_pause_under_parallelism_persists_and_repauses_on_resume(wf_home):
    from workflow import compile_text, run, resume, status

    vir = compile_text(BUDGET_FANOUT_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=_PerBranchCostWorker(cost=1.0), max_parallel_nodes=4, max_budget_usd=2.5)

    assert env["status"] == "paused", env
    assert env["pause_reason"] == "BUDGET", env

    # persisted to disk -- a fresh status() read (independent of the `env`
    # returned by run()) must agree
    st = status(env["run_id"])
    assert st["status"] == "paused", st
    assert st["pause_reason"] == "BUDGET", st

    # resume WITHOUT raising the cap -> re-pauses, no forward progress
    worker2 = _PerBranchCostWorker(cost=1.0)
    env2 = resume(env["run_id"], worker=worker2, max_budget_usd=2.5)
    assert env2["status"] == "paused", env2
    assert env2["pause_reason"] == "BUDGET", env2
    # "after" is a script node (never dispatched through worker.run_node);
    # check the run envelope itself for forward progress instead.
    assert "after" not in env2["succeeded"], "no progress must be made without a higher cap"

    # resume WITH a higher cap -> completes
    worker3 = _PerBranchCostWorker(cost=1.0)
    env3 = resume(env["run_id"], worker=worker3, max_budget_usd=100.0)
    assert env3["status"] == "succeeded", env3
    assert "after" in env3["succeeded"], env3
