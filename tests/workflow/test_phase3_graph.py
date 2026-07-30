"""Phase 3 graph / control-flow acceptance tests: map sugar, reducers,
first_k + short_circuit, on_fail policies, F6 retry-refusal, and the
failed-upstream-cascade-does-not-hang invariant.

Contract tests over brittle snapshots: assertions target documented
behavior (env["succeeded"/"failed"/"skipped"], checkpointed NodeRun fields,
reducer return shapes) rather than incidental formatting.

Two genuine implementation bugs were found while writing these tests. Both
have since been FIXED in `workflow/runtime/driver.py`; the tests below are
now their regression coverage, and the descriptions are kept because they
explain what the assertions are actually defending:

  1. `Driver._build_ctx` built its template context by iterating
     `state.node_runs_by_node[node_id]` and overwriting `ctx[node_id]` for
     EVERY node-run that shares that node_id -- which every fanout/map
     BRANCH node-run does (they're created via
     `_ensure_node_run(node_id, branch_index=..., ...)`, sharing the
     PARENT's node_id). The last-inserted node-run wins, which is always
     the last branch, not the fanout/map node's own top-level node-run. So
     a downstream template referencing `{{ mapnode.output }}` (or
     `{{ fanoutnode.output }}`) sees the LAST BRANCH's raw output instead
     of the node's own reduced/summary output.

  2. `Driver._seed_initial_node_runs` computed
     "has incoming" from `ir.edges` only: `{e.to for e in self.edges}`. A
     `join` node whose upstreams are declared via `from:` (a form the
     verifier explicitly accepts with NO `edges:` at all -- see
     `verify._check_node`'s join check) is therefore misclassified as a
     graph ENTRY POINT and given a premature `pending` node-run at time
     zero. When its upstream(s) later fail/skip, `_propagate_skips`'s
     `if self.state.node_runs_by_node.get(node_id): continue` guard treats
     "already has a node-run" as "nothing to do here", so the join can
     NEVER be marked `skipped` -- it sits `pending` forever even though
     the run reaches a terminal status. The everyday case (a `from:` join
     that ALSO has explicit `edges:` into it, which every fixture in the
     existing test suite uses) works correctly; only the from:-only,
     no-matching-edges authoring style triggers it.
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# 1. map sugar
# ---------------------------------------------------------------------------

MAP_CONCAT_YAML = """
workflow: map_sugar_concat
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [alpha, beta, gamma]}
  - id: mapped
    kind: map
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec: {prompt: "Process {{ branch }}"}
    reduce: {type: concat}
edges:
  - { from: seed, to: mapped }
triggers:
  - { kind: manual }
"""

FANOUT_SAME_SHAPE_YAML = """
workflow: fanout_own_shape
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
    reduce: {type: concat}
edges:
  - { from: seed, to: branches }
triggers:
  - { kind: manual }
"""

MAP_NO_JOIN_DOWNSTREAM_YAML = """
workflow: map_no_join_downstream
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [alpha, beta, gamma]}
  - id: mapped
    kind: map
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec: {prompt: "Process {{ branch }}"}
    reduce: {type: concat}
  - id: downstream
    kind: agent
    spec: {prompt: "Use {{ mapped.output }}"}
edges:
  - { from: seed, to: mapped }
  - { from: mapped, to: downstream }
triggers:
  - { kind: manual }
"""


class _DistinctBranchWorker:
    def run_node(self, node, ctx):
        if "branch" in ctx:
            item = ctx["branch"]
            return {"output": {"item": item, "marker": f"b-{item}"}, "cost_usd": 0.0}
        return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}


def test_map_own_output_is_the_reduced_value_not_branches_shape(wf_home):
    """A `map` node's OWN output IS the reduced value (default reduce:
    concat here) -- not the `{"branches": [...], "count": n}` fanout
    shape."""
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(MAP_CONCAT_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=_DistinctBranchWorker())
    assert env["status"] == "succeeded", env

    rec = checkpoint.load_run_record(env["run_id"])
    mapped_nr = next(
        nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "mapped" and nrd.get("branch_index") is None
    )
    out = mapped_nr["output"]
    assert out.get("kind") == "concat", out
    assert "branches" in out and len(out["branches"]) == 3, out
    # NOT the fanout {"branches": [ids...], "count": n} shape (a flat list of
    # node_run_ids, not the reduced branch envelopes)
    assert not (set(out.keys()) == {"branches", "count"} and isinstance(out["branches"][0], str))


def test_fanout_own_output_stays_branches_count_shape(wf_home):
    """Contrast: a plain `fanout` node's own output is UNCHANGED -- the
    `{"branches": [node_run_ids...], "count": n}` summary shape -- even
    when it carries a `reduce:` block (which has no effect on a fanout,
    only a `join`/`map` consumes it)."""
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(FANOUT_SAME_SHAPE_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=_DistinctBranchWorker())
    assert env["status"] == "succeeded", env

    rec = checkpoint.load_run_record(env["run_id"])
    fanout_nr = next(
        nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "branches" and nrd.get("branch_index") is None
    )
    out = fanout_nr["output"]
    assert set(out.keys()) == {"branches", "count"}, out
    assert out["count"] == 3, out
    assert all(isinstance(x, str) for x in out["branches"]), out  # node_run_id strings, not envelopes


def test_downstream_reads_reduced_map_output_via_template_no_join(wf_home):
    """Acceptance checklist item 1: a downstream node reads reduced data
    from `{{ mapnode.output }}` with no `join` node present."""
    from workflow import compile_text, run

    captured = {}

    class W:
        def run_node(self, node, ctx):
            if node.id == "downstream":
                captured["mapped_ctx"] = ctx.get("mapped")
            if "branch" in ctx:
                return {"output": {"item": ctx["branch"]}, "cost_usd": 0.0}
            return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}

    vir = compile_text(MAP_NO_JOIN_DOWNSTREAM_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=W())
    assert env["status"] == "succeeded", env

    mapped_ctx_output = captured["mapped_ctx"]["output"]
    # The downstream node must see the REDUCED value (kind: concat, with a
    # "branches" list of envelopes) -- not a single branch's raw {"item": ...}.
    assert mapped_ctx_output.get("kind") == "concat", mapped_ctx_output
    assert "item" not in mapped_ctx_output, mapped_ctx_output


# ---------------------------------------------------------------------------
# 2. Reducers -- pure functions + through a real driver run
# ---------------------------------------------------------------------------


def _env(node_run_id, output):
    return {"node_run_id": node_run_id, "output": output}


def test_first_k_pure_preserves_branch_order():
    from workflow.runtime.scripts import first_k

    envs = [_env("n0", {"v": "a"}), _env("n1", {"v": "b"}), _env("n2", {"v": "c"})]
    out = first_k(envs, k=2)
    assert out["kind"] == "first_k"
    assert out["k"] == 2
    assert [s["node_run_id"] for s in out["selected"]] == ["n0", "n1"]


def test_majority_pure_winner_count_total_tally():
    from workflow.runtime.scripts import majority

    envs = [_env("n0", {"v": "x"}), _env("n1", {"v": "y"}), _env("n2", {"v": "x"})]
    out = majority(envs, key="v")
    assert out["winner"] == "x"
    assert out["count"] == 2
    assert out["total"] == 3
    assert out["tie"] is False
    assert {t["value"]: t["count"] for t in out["tally"]} == {"x": 2, "y": 1}


def test_majority_pure_tie_break_first_appearance_wins():
    """Pinned tie-break: on an equal count the FIRST-appearing value wins,
    and the result carries tie=True."""
    from workflow.runtime.scripts import majority

    envs = [_env("n0", {"v": "x"}), _env("n1", {"v": "y"})]
    out = majority(envs, key="v")
    assert out["winner"] == "x"  # first-appearing
    assert out["count"] == 1
    assert out["tie"] is True

    # reversed insertion order -> reversed winner (proves it's genuinely
    # first-appearance, not e.g. alphabetical or reverse-insertion)
    envs2 = [_env("n0", {"v": "y"}), _env("n1", {"v": "x"})]
    out2 = majority(envs2, key="v")
    assert out2["winner"] == "y"
    assert out2["tie"] is True


def test_best_pure_highest_score():
    from workflow.runtime.scripts import best

    envs = [_env("n0", {"score": 1}), _env("n1", {"score": 5}), _env("n2", {"score": 3})]
    out = best(envs, key="score")
    assert out["selected"]["node_run_id"] == "n1"
    assert out["score"] == 5.0


def test_best_pure_tie_keeps_first_appearance():
    from workflow.runtime.scripts import best

    envs = [_env("n0", {"score": 5}), _env("n1", {"score": 5})]
    out = best(envs, key="score")
    assert out["selected"]["node_run_id"] == "n0"  # strict > never displaces an equal earlier score


def test_concat_and_top_k_still_behave():
    from workflow.runtime.scripts import concat, top_k

    envs = [_env("n0", {"v": 1}), _env("n1", {"v": 2})]
    c = concat(envs)
    assert c["kind"] == "concat"
    assert [b["node_run_id"] for b in c["branches"]] == ["n0", "n1"]

    scored = [_env("n0", {"score": 1}), _env("n1", {"score": 9}), _env("n2", {"score": 5})]
    t = top_k(scored, k=2)
    assert t["kind"] == "top_k"
    assert [s["node_run_id"] for s in t["selected"]] == ["n1", "n2"]


def _map_yaml(reduce_block: str, items=("a", "b", "c")) -> str:
    items_yaml = json.dumps(list(items))
    return f"""
workflow: map_reducer_test
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {{items: {items_yaml}}}
  - id: mapped
    kind: map
    over: "{{{{ seed.output.echo.items }}}}"
    max_branches: 10
    branch:
      kind: agent
      spec: {{prompt: "p {{{{ branch }}}}"}}
    reduce: {reduce_block}
edges:
  - {{ from: seed, to: mapped }}
triggers:
  - {{ kind: manual }}
"""


class _ScoredBranchWorker:
    """Branch output keyed by item, with a per-item score/vote."""

    def __init__(self, scores=None, votes=None):
        self.scores = scores or {}
        self.votes = votes or {}

    def run_node(self, node, ctx):
        if "branch" in ctx:
            item = ctx["branch"]
            out = {"item": item}
            if item in self.scores:
                out["score"] = self.scores[item]
            if item in self.votes:
                out["vote"] = self.votes[item]
            return {"output": out, "cost_usd": 0.0}
        return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}


def test_first_k_reducer_via_map_driver_run(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(_map_yaml("{type: first_k, k: 2}", items=["a", "b", "c"]), phase1_warn_overrides=True)
    env = run(vir, input={}, worker=_ScoredBranchWorker())
    assert env["status"] == "succeeded", env
    rec = checkpoint.load_run_record(env["run_id"])
    mapped_nr = next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "mapped" and nrd.get("branch_index") is None)
    out = mapped_nr["output"]
    assert out["kind"] == "first_k"
    assert [s["output"]["item"] for s in out["selected"]] == ["a", "b"]


def test_majority_reducer_via_map_driver_run(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(
        _map_yaml("{type: majority, key: vote}", items=["a", "b", "c"]), phase1_warn_overrides=True
    )
    env = run(vir, input={}, worker=_ScoredBranchWorker(votes={"a": "yes", "b": "yes", "c": "no"}))
    assert env["status"] == "succeeded", env
    rec = checkpoint.load_run_record(env["run_id"])
    mapped_nr = next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "mapped" and nrd.get("branch_index") is None)
    out = mapped_nr["output"]
    assert out["kind"] == "majority"
    assert out["winner"] == "yes"
    assert out["count"] == 2
    assert out["total"] == 3
    assert out["tie"] is False


def test_best_reducer_via_map_driver_run(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(_map_yaml("{type: best, key: score}", items=["a", "b", "c"]), phase1_warn_overrides=True)
    env = run(vir, input={}, worker=_ScoredBranchWorker(scores={"a": 1, "b": 9, "c": 5}))
    assert env["status"] == "succeeded", env
    rec = checkpoint.load_run_record(env["run_id"])
    mapped_nr = next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "mapped" and nrd.get("branch_index") is None)
    out = mapped_nr["output"]
    assert out["kind"] == "best"
    assert out["selected"]["output"]["item"] == "b"
    assert out["score"] == 9.0


# ---------------------------------------------------------------------------
# 3. first_k + short_circuit: cooperative cancellation
# ---------------------------------------------------------------------------


def test_map_first_k_short_circuit_skips_not_yet_started_branches(wf_home):
    """Once k branches SUCCEED, not-yet-started branches end `skipped`.
    Cancellation is COOPERATIVE: this asserts the documented outcome (which
    branches ran vs. were skipped, and why), not that in-flight work is
    forcibly killed -- there is no in-flight work to kill here since the
    sequential path (max_parallel_nodes<=1) checks the short-circuit
    condition BEFORE starting each not-yet-run branch."""
    from workflow import compile_text, run
    from workflow.store import checkpoint

    yaml_text = _map_yaml("{type: first_k, k: 2, short_circuit: true}", items=["a", "b", "c", "d"])
    vir = compile_text(yaml_text, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=_ScoredBranchWorker(), max_parallel_nodes=1)
    assert env["status"] == "succeeded", env

    rec = checkpoint.load_run_record(env["run_id"])
    branch_runs = sorted(
        (nrd for nrd in rec["node_runs"].values() if nrd.get("branch_index") is not None),
        key=lambda nrd: nrd["branch_index"],
    )
    assert len(branch_runs) == 4
    statuses = [b["status"] for b in branch_runs]
    assert statuses == ["succeeded", "succeeded", "skipped", "skipped"], statuses
    for skipped_branch in branch_runs[2:]:
        assert skipped_branch["output"] == {"skipped_reason": "short_circuit"}, skipped_branch

    mapped_nr = next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "mapped" and nrd.get("branch_index") is None)
    out = mapped_nr["output"]
    assert out["kind"] == "first_k"
    assert len(out["selected"]) == 2


# ---------------------------------------------------------------------------
# 4. on_fail policies
# ---------------------------------------------------------------------------

DEFAULT_CASCADE_YAML = """
workflow: default_cascade
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


def test_on_fail_unset_defaults_to_skip_downstream(wf_home):
    """No `on_fail:` set at all -> DEFAULT_ON_FAIL (skip_downstream): the
    downstream node cascades to `skipped`, matching Node.fail_policy's
    documented default."""
    from workflow import compile_text, run
    from workflow.ir import DEFAULT_ON_FAIL
    from workflow.runtime.worker import FakeWorker

    assert DEFAULT_ON_FAIL == "skip_downstream"
    vir = compile_text(DEFAULT_CASCADE_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(fail_nodes={"a": "boom"}))
    assert "a" in env["failed"], env
    assert env["skipped"] == ["b"], env
    assert "b" not in env["succeeded"], env


CONTINUE_YAML = """
workflow: continue_wf
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do a}
    on_fail: continue
  - id: b
    kind: agent
    spec: {prompt: do b}
edges:
  - { from: a, to: b }
triggers:
  - { kind: manual }
"""


def test_on_fail_continue_downstream_runs_despite_failure(wf_home):
    """on_fail: continue -- the failure does not block downstream; the
    edge counts as satisfied with a null output, so `b` still RUNS (and
    succeeds) even though `a` failed."""
    from workflow import compile_text, run

    class W:
        def run_node(self, node, ctx):
            if node.id == "a":
                raise RuntimeError("boom")
            return {"output": {"ok": True}, "cost_usd": 0.0}

    vir = compile_text(CONTINUE_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=W())
    assert "a" in env["failed"], env
    assert "b" in env["succeeded"], env
    assert env["skipped"] == [], env
    assert env["status"] == "partial", env


FAIL_RUN_YAML = """
workflow: fail_run_wf
version: 1
nodes:
  - id: x
    kind: agent
    spec: {prompt: do x}
  - id: y
    kind: agent
    spec: {prompt: do y}
    on_fail: fail_run
  - id: w
    kind: agent
    spec: {prompt: do w}
  - id: z
    kind: agent
    spec: {prompt: do z}
edges:
  - { from: x, to: w }
  - { from: y, to: z }
triggers:
  - { kind: manual }
"""


def test_on_fail_fail_run_aborts_immediately_terminal_failed_not_partial(wf_home):
    """on_fail: fail_run -- a declared hard stop. The run's terminal status
    is `failed`, NEVER softened to `partial` even though `x` already
    succeeded earlier in the same run. Nothing downstream-of-x (`w`) is
    even started once the abort fires -- no node-run exists for it at
    all (not even a skip bookkeeping entry)."""
    from workflow import compile_text, run
    from workflow.store import checkpoint

    class W:
        def run_node(self, node, ctx):
            if node.id == "y":
                raise RuntimeError("boom")
            return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}

    vir = compile_text(FAIL_RUN_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=W(), max_parallel_nodes=1)

    assert env["status"] == "failed", env  # never "partial", even though x succeeded
    assert "x" in env["succeeded"], env
    assert "y" in env["failed"], env

    rec = checkpoint.load_run_record(env["run_id"])
    node_ids_with_runs = {nrd["node_id"] for nrd in rec["node_runs"].values()}
    assert "w" not in node_ids_with_runs, "no further node may even be seeded once fail_run aborts"
    assert "z" not in node_ids_with_runs, "no further node may even be seeded once fail_run aborts"


RETRY_YAML = """
workflow: retry_wf
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do a}
    on_fail: retry
    attempts: 3
edges: []
triggers:
  - { kind: manual }
"""


def test_on_fail_retry_succeeds_on_later_attempt_and_increments_attempt(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    class FlakyWorker:
        def __init__(self):
            self.calls = 0

        def run_node(self, node, ctx):
            self.calls += 1
            if self.calls < 2:
                raise RuntimeError("boom")
            return {"output": {"ok": True}, "cost_usd": 0.0}

    vir = compile_text(RETRY_YAML, phase1_warn_overrides=True)
    worker = FlakyWorker()
    env = run(vir, input={}, worker=worker, max_parallel_nodes=1)
    assert env["status"] == "succeeded", env
    assert worker.calls == 2

    rec = checkpoint.load_run_record(env["run_id"])
    a_nr = next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "a")
    assert a_nr["attempt"] == 2, a_nr
    assert a_nr["status"] == "succeeded", a_nr


def test_on_fail_retry_exhausted_falls_back_to_skip_downstream(wf_home):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker
    from workflow.store import checkpoint

    vir = compile_text(RETRY_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(fail_nodes={"a": "always boom"}), max_parallel_nodes=1)
    assert env["status"] == "failed", env
    assert "a" in env["failed"], env

    rec = checkpoint.load_run_record(env["run_id"])
    a_nr = next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "a")
    assert a_nr["attempt"] == 3, a_nr  # attempts: 3 -> exhausted at attempt 3
    assert a_nr["status"] == "failed", a_nr


# ---------------------------------------------------------------------------
# 5. F6: on_fail: retry + side_effects: external is refused
# ---------------------------------------------------------------------------

RETRY_SIDE_EFFECT_YAML = """
workflow: retry_side_effect
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do a}
    on_fail: retry
    side_effects: external
    attempts: 3
    idempotent: true
edges: []
triggers:
  - { kind: manual }
"""


def test_on_fail_retry_on_side_effects_external_rejected_at_compile(wf_home):
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    with pytest.raises(WorkflowRejected) as exc:
        compile_text(RETRY_SIDE_EFFECT_YAML, phase1_warn_overrides=True)
    codes = [i.code for i in exc.value.issues if i.severity == "error"]
    assert "ON_FAIL" in codes, codes


def test_driver_refuses_auto_retry_on_side_effects_external_bypassing_verifier(wf_home):
    """Even if the verifier is bypassed entirely (a hand-built IR wrapped
    directly in VerifiedIR), the driver itself must never auto-retry a
    side_effects: external node -- F6's `_maybe_retry` early-returns on
    `node.side_effects == "external"`, so the node stays `failed` at
    attempt 1 (no retry happened) rather than being re-run."""
    from workflow.ir import WorkflowIR, Node, NodeSpec, Trigger, VerifiedIR
    from workflow.runtime.driver import Driver
    from workflow.runtime.worker import FakeWorker

    node = Node(
        id="a",
        kind="agent",
        spec=NodeSpec(prompt="do a"),
        on_fail="retry",
        side_effects="external",
        attempts=5,
    )
    ir = WorkflowIR(id="bypass_wf", nodes=[node], edges=[], triggers=[Trigger(kind="manual")])
    vir = VerifiedIR(ir=ir, issues=[])

    d = Driver(vir, worker=FakeWorker(fail_nodes={"a": "boom"}), max_parallel_nodes=1)
    env = d.execute()

    assert env["status"] == "failed", env
    assert "a" in env["failed"], env
    a_nr = next(nr for nr in d.state.node_runs.values() if nr.node_id == "a")
    assert a_nr.attempt == 1, "F6: side_effects:external must never be auto-retried, even bypassing the verifier"


# ---------------------------------------------------------------------------
# 6. Failed-upstream cascade never hangs (reaches a terminal status)
# ---------------------------------------------------------------------------

DIAMOND_WITH_EDGES_YAML = """
workflow: diamond_with_edges
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
  - id: d
    kind: join
    from: [b, c]
    reduce: {type: concat}
edges:
  - { from: a, to: b }
  - { from: a, to: c }
  - { from: b, to: d }
  - { from: c, to: d }
triggers:
  - { kind: manual }
"""


def test_diamond_join_reaches_skipped_not_stuck_pending(wf_home):
    """A downstream `join` two hops below a failed node reaches a terminal
    status (`skipped`) -- never left `pending` forever -- when it is wired
    with BOTH `from:` (readiness) and matching `edges:` (reachability),
    the canonical authoring style used throughout this codebase's existing
    fixtures."""
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker
    from workflow.store import checkpoint

    vir = compile_text(DIAMOND_WITH_EDGES_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(fail_nodes={"a": "boom"}))
    assert env["status"] == "failed", env
    assert set(env["skipped"]) == {"b", "c", "d"}, env

    rec = checkpoint.load_run_record(env["run_id"])
    pending = [nrd for nrd in rec["node_runs"].values() if nrd["status"] == "pending"]
    assert pending == [], f"no node-run may be left pending: {pending}"


DIAMOND_FROM_ONLY_YAML = """
workflow: diamond_from_only
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
  - id: d
    kind: join
    from: [b, c]
    reduce: {type: concat}
edges:
  - { from: a, to: b }
  - { from: a, to: c }
triggers:
  - { kind: manual }
"""


def test_from_only_join_still_reaches_skipped_not_stuck_pending(wf_home):
    """Same diamond, but `d` is wired with `from:` ONLY (no matching
    `edges:` into it) -- a legal, verifier-accepted graph. `d` must still
    reach a terminal status once its upstreams fail; it must not be left
    `pending` forever just because the fanout-free `from:`-only wiring
    style was used instead of also declaring `edges:`."""
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker
    from workflow.store import checkpoint

    vir = compile_text(DIAMOND_FROM_ONLY_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(fail_nodes={"a": "boom"}))

    rec = checkpoint.load_run_record(env["run_id"])
    pending = [nrd for nrd in rec["node_runs"].values() if nrd["status"] == "pending"]
    assert pending == [], f"no node-run may be left pending: {pending}"
    assert "d" in env["skipped"], env
