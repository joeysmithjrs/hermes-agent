"""Phase 2 hardening — acceptance checklist: failed-upstream cascade,
retry-failed un-stick, budget pause + resume policy, output-schema
validation.

Maps to the acceptance checklist:
  1. Failed-upstream cascade: a -> b -> c, `a` fails. `b`/`c` end up
     `skipped` (not stuck `pending`); the run status is failed/partial per
     policy; no node-run is left `pending`.
  2. `--retry-failed`/`resume(retry_failed=True)` un-sticks the cascade: once
     `a` no longer fails, `a`/`b`/`c` all succeed.
  3. Budget pause: a per-node cost that trips `max_budget_usd` mid-run pauses
     the run (`pause_reason == "BUDGET"`); nodes after the trip do not run.
  4. Budget resume policy: resuming without a higher budget re-pauses (no
     progress); resuming with a higher `max_budget_usd` completes.
  5. Output schema: `spec.output` mismatch -> node `failed` with
     `error["code"] == "SCHEMA"`; a matching output succeeds; a node with no
     `output` block is unaffected (zero-impact opt-in).
"""

from __future__ import annotations

from workflow import compile_text, resume, run
from workflow.runtime.worker import FakeWorker
from workflow.store import checkpoint

CHAIN_YAML = """
workflow: cascade_chain
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "do a"}
  - id: b
    kind: agent
    spec: {prompt: "do b using {{ a.output }}"}
  - id: c
    kind: agent
    spec: {prompt: "do c using {{ b.output }}"}
edges:
  - { from: a, to: b }
  - { from: b, to: c }
triggers:
  - { kind: manual }
"""


# ---------------------------------------------------------------------------
# 1. Failed-upstream cascade
# ---------------------------------------------------------------------------


def test_failed_upstream_cascades_to_skipped_no_pending_left(wf_home):
    vir = compile_text(CHAIN_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(fail_nodes={"a": "boom"}))

    assert "a" in env["failed"], env
    assert set(env["skipped"]) == {"b", "c"}, env
    assert env["status"] == "failed", env  # nothing succeeded, one failed -> "failed"

    rec = checkpoint.load_run_record(env["run_id"])
    pending = [nrd for nrd in rec["node_runs"].values() if nrd["status"] == "pending"]
    assert pending == [], f"no node-run may be left pending: {pending}"


# ---------------------------------------------------------------------------
# 2. --retry-failed un-sticks the cascade
# ---------------------------------------------------------------------------


def test_retry_failed_unsticks_cascade_all_succeed(wf_home):
    vir = compile_text(CHAIN_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(fail_nodes={"a": "boom"}))
    assert env["status"] == "failed", env
    assert set(env["skipped"]) == {"b", "c"}, env

    env2 = resume(env["run_id"], worker=FakeWorker(), retry_failed=True)

    assert env2["status"] == "succeeded", env2
    assert set(env2["succeeded"]) == {"a", "b", "c"}, env2
    assert env2["failed"] == [], env2
    assert env2["skipped"] == [], env2


# ---------------------------------------------------------------------------
# 3. Budget pause
# ---------------------------------------------------------------------------


class _CostWorker:
    """Worker with configurable per-node cost_usd (stock FakeWorker always
    reports cost_usd=0.0, so a dedicated stub is needed to trip the budget
    circuit-breaker)."""

    def __init__(self, costs):
        self.costs = dict(costs)
        self.calls = []

    def run_node(self, node, ctx):
        self.calls.append(node.id)
        return {"output": {"ok": True, "node": node.id}, "cost_usd": self.costs.get(node.id, 0.0)}


def test_budget_pause_trips_mid_run_and_downstream_does_not_run(wf_home):
    vir = compile_text(CHAIN_YAML, phase1_warn_overrides=True)
    worker = _CostWorker({"a": 0.5, "b": 0.8, "c": 0.1})

    env = run(vir, input={}, worker=worker, max_budget_usd=1.0)

    assert env["status"] == "paused", env
    assert env["pause_reason"] == "BUDGET", env
    assert "a" in env["succeeded"], env
    assert "b" in env["succeeded"], env  # b ran and finished before the trip was observed
    assert "c" not in worker.calls, "c must not run after the budget trip"
    assert "c" not in env["succeeded"], env


# ---------------------------------------------------------------------------
# 4. Budget resume policy
# ---------------------------------------------------------------------------


def test_budget_resume_without_higher_cap_repauses(wf_home):
    vir = compile_text(CHAIN_YAML, phase1_warn_overrides=True)
    worker = _CostWorker({"a": 0.5, "b": 0.8, "c": 0.1})
    env = run(vir, input={}, worker=worker, max_budget_usd=1.0)
    assert env["status"] == "paused", env

    worker2 = _CostWorker({"a": 0.5, "b": 0.8, "c": 0.1})
    env2 = resume(env["run_id"], worker=worker2, max_budget_usd=1.0)

    assert env2["status"] == "paused", env2
    assert env2["pause_reason"] == "BUDGET", env2
    assert "c" not in worker2.calls, "no progress must be made without a higher cap"


def test_budget_resume_with_higher_cap_completes(wf_home):
    vir = compile_text(CHAIN_YAML, phase1_warn_overrides=True)
    worker = _CostWorker({"a": 0.5, "b": 0.8, "c": 0.1})
    env = run(vir, input={}, worker=worker, max_budget_usd=1.0)
    assert env["status"] == "paused", env

    worker2 = _CostWorker({"a": 0.5, "b": 0.8, "c": 0.1})
    env2 = resume(env["run_id"], worker=worker2, max_budget_usd=10.0)

    assert env2["status"] == "succeeded", env2
    assert set(env2["succeeded"]) == {"a", "b", "c"}, env2
    assert "c" in worker2.calls


# ---------------------------------------------------------------------------
# 5. Output schema validation
# ---------------------------------------------------------------------------

SCHEMA_YAML = """
workflow: schema_check
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: "produce structured output"
      output:
        type: object
        required: [foo]
edges: []
triggers:
  - { kind: manual }
"""

NO_SCHEMA_YAML = """
workflow: no_schema_check
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: "produce whatever"
edges: []
triggers:
  - { kind: manual }
"""


def test_output_schema_mismatch_fails_with_schema_code(wf_home):
    vir = compile_text(SCHEMA_YAML, phase1_warn_overrides=True)
    # output missing the required "foo" key
    worker = FakeWorker(outputs={"a": {"bar": 1}})
    env = run(vir, input={}, worker=worker)

    assert env["status"] == "failed", env
    assert "a" in env["failed"], env

    rec = checkpoint.load_run_record(env["run_id"])
    a_nr = next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "a")
    assert a_nr["status"] == "failed", a_nr
    assert a_nr["error"]["code"] == "SCHEMA", a_nr["error"]


def test_output_schema_match_succeeds(wf_home):
    vir = compile_text(SCHEMA_YAML, phase1_warn_overrides=True)
    worker = FakeWorker(outputs={"a": {"foo": "present"}})
    env = run(vir, input={}, worker=worker)

    assert env["status"] == "succeeded", env
    assert "a" in env["succeeded"], env


def test_no_output_block_is_unaffected_zero_impact_opt_in(wf_home):
    """A node with no spec.output must behave exactly as before -- any
    output shape succeeds, schema validation is a pure opt-in."""
    vir = compile_text(NO_SCHEMA_YAML, phase1_warn_overrides=True)
    # deliberately an output shape that WOULD fail the schema above
    worker = FakeWorker(outputs={"a": {"bar": 1}})
    env = run(vir, input={}, worker=worker)

    assert env["status"] == "succeeded", env
    assert "a" in env["succeeded"], env


# ---------------------------------------------------------------------------
# 6. Agent envelopes: the plan lives in `text`, however the model wrapped it
# ---------------------------------------------------------------------------
#
# An agent node's stored output is `{text, node, result, ...}` with the model's
# answer as a STRING in `text`. Schema validation must see through that wrapper,
# and through whatever markdown the model put around the JSON, or a node that
# emitted a perfectly good object fails SCHEMA and its downstream gate never
# opens. (PM Desk run wf_9e6e868d97f1 lost a morning exactly this way.)


def _envelope(text: str) -> dict:
    return {"text": text, "node": "a", "result": None}


def test_schema_accepts_json_wrapped_in_a_markdown_fence(wf_home):
    vir = compile_text(SCHEMA_YAML, phase1_warn_overrides=True)
    worker = FakeWorker(
        outputs={"a": _envelope('## Result\n\n```json\n{"foo": "present"}\n```\n')}
    )
    env = run(vir, input={}, worker=worker)
    assert env["status"] == "succeeded", env


def test_schema_accepts_json_after_an_unrelated_fence(wf_home):
    """A shell snippet first, the answer second. Only the first fence used to
    be considered, so the answer after it was thrown away."""
    vir = compile_text(SCHEMA_YAML, phase1_warn_overrides=True)
    text = 'Run:\n```bash\nhermes cron create 30m\n```\nThen:\n```json\n{"foo": "present"}\n```'
    env = run(vir, input={}, worker=FakeWorker(outputs={"a": _envelope(text)}))
    assert env["status"] == "succeeded", env


def test_schema_accepts_a_bare_object_surrounded_by_prose(wf_home):
    vir = compile_text(SCHEMA_YAML, phase1_warn_overrides=True)
    text = 'Here is the object: {"foo": "present"} — nothing else to report.'
    env = run(vir, input={}, worker=FakeWorker(outputs={"a": _envelope(text)}))
    assert env["status"] == "succeeded", env


def test_schema_still_fails_on_markdown_with_no_json_at_all(wf_home):
    """The recovery must not become "anything passes". Prose describing a file
    the model wrote elsewhere is still a failed node."""
    vir = compile_text(SCHEMA_YAML, phase1_warn_overrides=True)
    text = "## Plan\n\n- wrote execution_plan.json to the workspace\n- 2 monitors\n"
    env = run(vir, input={}, worker=FakeWorker(outputs={"a": _envelope(text)}))

    assert env["status"] == "failed", env
    rec = checkpoint.load_run_record(env["run_id"])
    a_nr = next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "a")
    assert a_nr["error"]["code"] == "SCHEMA", a_nr["error"]


def test_schema_still_fails_when_the_embedded_json_is_wrong(wf_home):
    vir = compile_text(SCHEMA_YAML, phase1_warn_overrides=True)
    text = '```json\n{"bar": 1}\n```'
    env = run(vir, input={}, worker=FakeWorker(outputs={"a": _envelope(text)}))

    assert env["status"] == "failed", env
    rec = checkpoint.load_run_record(env["run_id"])
    a_nr = next(nrd for nrd in rec["node_runs"].values() if nrd["node_id"] == "a")
    assert a_nr["error"]["code"] == "SCHEMA", a_nr["error"]
