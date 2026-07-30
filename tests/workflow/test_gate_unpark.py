"""Gate unpark — Phase 2 acceptance checklist §"gate unpark".

`workflow.decide_gate()` writes a decision signal to disk; `resume()`
(`workflow.runtime.driver._unpark_gate`) resolves that signal BEFORE the
ready-set walk continues. Maps to the acceptance checklist:
  1. park: a(agent) -> g(gate) -> b(agent) parks at `g`; `b` does not run.
  2. approve: gate -> succeeded, port="approve"; `b` runs and succeeds.
  3. shelve: gate -> skipped, port="shelve"; `b` is NEVER invoked (proven via
     a FakeWorker side-effect call count, not just its status).
  4. no decision: resume with only the pending signal stays awaiting_gate --
     never flips to succeeded.
  5. modify: stays parked; the envelope carries an honest note explaining why.
  6. idempotency: resuming twice after approve doesn't duplicate the gate's
     entry in `succeeded` and doesn't re-run `b`.

test_review_fixes.py already covers the "resumed with no decision at all"
case end-to-end (test_resumed_awaiting_gate_not_finalized_succeeded) via a
hand-built IR; this file exercises the full decide_gate()->resume() signal
path via YAML + the public API, and additionally proves `b` never executes
(side-effect call counts), which that test does not check.
"""

from __future__ import annotations

from workflow import compile_text, decide_gate, resume, run
from workflow.runtime.worker import FakeWorker
from workflow.store import checkpoint

GATE_UNPARK_YAML = """
workflow: gate_unpark
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "do a"}
  - id: g
    kind: gate
  - id: b
    kind: agent
    spec: {prompt: "do b"}
gates:
  g:
    channel: "#ops"
    approvers: [joe]
edges:
  - { from: a, to: g }
  - { from: g, to: b }
triggers:
  - { kind: manual }
"""


def _compile(wf_home):
    return compile_text(GATE_UNPARK_YAML, phase1_warn_overrides=True)


def _gate_node_run(run_id, gate_id="g"):
    rec = checkpoint.load_run_record(run_id)
    return next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == gate_id and nrd.get("branch_index") is None)


# ---------------------------------------------------------------------------
# 1. park
# ---------------------------------------------------------------------------


def test_park_stops_before_downstream_node(wf_home):
    _compile(wf_home)
    worker = FakeWorker(side_effect_nodes={"b"})
    env = run(compile_text(GATE_UNPARK_YAML, phase1_warn_overrides=True), input={}, worker=worker)

    assert env["status"] == "awaiting_gate", env
    assert env["awaiting_gate"] == "g", env
    assert worker.side_effect_calls.get("b", 0) == 0, "b must not run while parked"
    assert "b" not in env["succeeded"], env


# ---------------------------------------------------------------------------
# 2. approve
# ---------------------------------------------------------------------------


def test_approve_unparks_and_runs_downstream(wf_home):
    vir = compile_text(GATE_UNPARK_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(side_effect_nodes={"b"}))
    run_id = env["run_id"]

    decide_gate(run_id, "g", "approve", note="looks good")
    b_worker = FakeWorker(side_effect_nodes={"b"})
    env2 = resume(run_id, worker=b_worker)

    assert env2["status"] == "succeeded", env2
    assert "b" in env2["succeeded"], env2
    assert b_worker.side_effect_calls.get("b", 0) == 1, b_worker.side_effect_calls

    gate_nr = _gate_node_run(run_id)
    assert gate_nr["status"] == "succeeded", gate_nr
    assert gate_nr["port"] == "approve", gate_nr


# ---------------------------------------------------------------------------
# 3. shelve
# ---------------------------------------------------------------------------


def test_shelve_skips_gate_and_downstream_never_invoked(wf_home):
    vir = compile_text(GATE_UNPARK_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(side_effect_nodes={"b"}))
    run_id = env["run_id"]

    decide_gate(run_id, "g", "shelve", note="not now")
    b_worker = FakeWorker(side_effect_nodes={"b"})
    env2 = resume(run_id, worker=b_worker)

    gate_nr = _gate_node_run(run_id)
    assert gate_nr["status"] == "skipped", gate_nr
    assert gate_nr["port"] == "shelve", gate_nr

    # the crucial acceptance proof: b was never CALLED, not merely "skipped".
    assert b_worker.side_effect_calls.get("b", 0) == 0, b_worker.side_effect_calls
    assert "b" in env2["skipped"], env2
    assert "b" not in env2["succeeded"], env2
    # the run finalizes (no dangling running/pending)
    assert env2["status"] in ("succeeded", "partial", "failed"), env2


# ---------------------------------------------------------------------------
# 4. no decision
# ---------------------------------------------------------------------------


def test_resume_with_pending_signal_stays_parked(wf_home):
    vir = compile_text(GATE_UNPARK_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(side_effect_nodes={"b"}))
    run_id = env["run_id"]

    # No decide_gate() call at all -- resume with nothing but the original
    # pending signal file written by _run_gate().
    b_worker = FakeWorker(side_effect_nodes={"b"})
    env2 = resume(run_id, worker=b_worker)

    assert env2["status"] == "awaiting_gate", env2
    assert env2["status"] != "succeeded", env2  # the critical assertion
    assert env2["awaiting_gate"] == "g", env2
    assert b_worker.side_effect_calls.get("b", 0) == 0, b_worker.side_effect_calls


# ---------------------------------------------------------------------------
# 5. modify
# ---------------------------------------------------------------------------


def test_modify_stays_parked_with_honest_note(wf_home):
    vir = compile_text(GATE_UNPARK_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(side_effect_nodes={"b"}))
    run_id = env["run_id"]

    decide_gate(run_id, "g", "modify", note="please tweak the prompt")
    env2 = resume(run_id, worker=FakeWorker(side_effect_nodes={"b"}))

    assert env2["status"] == "awaiting_gate", env2
    note = env2.get("note")
    assert note, "modify must surface an honest explanatory note, not silently no-op"
    assert "modify" in note.lower()
    assert "g" in note


# ---------------------------------------------------------------------------
# 6. idempotency
# ---------------------------------------------------------------------------


def test_double_resume_after_approve_is_idempotent(wf_home):
    vir = compile_text(GATE_UNPARK_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(side_effect_nodes={"b"}))
    run_id = env["run_id"]

    decide_gate(run_id, "g", "approve")
    first_worker = FakeWorker(side_effect_nodes={"b"})
    env2 = resume(run_id, worker=first_worker)
    assert env2["status"] == "succeeded", env2
    assert first_worker.side_effect_calls.get("b", 0) == 1

    # Resume again: the signal file still says "approve" on disk, but the
    # gate node-run is already succeeded -- must be a no-op, not a re-decide.
    second_worker = FakeWorker(side_effect_nodes={"b"})
    env3 = resume(run_id, worker=second_worker)

    assert env3["status"] == "succeeded", env3
    assert env3["succeeded"].count("g") == 1, env3["succeeded"]
    assert env3["succeeded"].count("b") == 1, env3["succeeded"]
    # b was NOT re-run on the second resume
    assert second_worker.side_effect_calls.get("b", 0) == 0, second_worker.side_effect_calls
