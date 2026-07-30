"""Supervisor primitive (post-Phase-3 §4): advisory policies + hard budget cap.

The shape under test is "a cheap supervisor owns the answer and may buy a
second opinion, at most N times":

* the node's output is always the SUPERVISOR's final turn, never the advisor's;
* `budget` is a hard stop on advisor calls, enforced once per node, and it
  stops the loop rather than silently skipping a call;
* a failed advisor does not fail the supervision -- the supervisor keeps the
  answer it already had, and the failed turn stays failed in the checkpoint;
* compile time rejects an unnamed policy, a missing cap, a missing advisor, and
  a supervisor-of-supervisors.
"""

from __future__ import annotations

import re

import pytest


class _SupervisorWorker:
    """Scripted supervisor/advisor pair.

    `uncertain_turns` is the set of supervisor turn numbers that come back
    asking for advice (`request_advisory: true`) -- the one documented signal
    the driver acts on.
    """

    def __init__(self, uncertain_turns=(), cost=0.0, fail_roles=(), advice="buy"):
        self.uncertain_turns = set(uncertain_turns)
        self.cost = cost
        self.fail_roles = set(fail_roles)
        self.advice = advice
        self.turns = []  # (role, stage_no, node_id, model)
        self.seen = []

    def run_node(self, node, ctx):
        item = ctx.get("branch") or {}
        role = item.get("role")
        self.turns.append(
            (role, item.get("turn") or item.get("round"), node.id, getattr(node.spec, "model", None))
        )
        self.seen.append({"node": node, "item": item, "workspace": ctx.get("workspace")})
        if role in self.fail_roles:
            raise RuntimeError(f"{role} blew up")
        if role == "advisor":
            return {"output": {"advice": self.advice}, "cost_usd": self.cost, "tokens_in": 5, "tokens_out": 1}
        out = {"decision": "hold", "turn": item.get("turn"), "advice_seen": len(item.get("advice") or [])}
        if item.get("turn") in self.uncertain_turns:
            out["request_advisory"] = True
        return {"output": out, "cost_usd": self.cost, "tokens_in": 3, "tokens_out": 2}


def _sup_yaml(
    *,
    policy="ask_on_uncertain",
    budget=2,
    max_advisory_rounds=None,
    advisor=True,
    extra="",
):
    advisor_block = (
        "\n    advisor:\n"
        "      id: quant\n"
        "      kind: agent\n"
        '      spec: {prompt: "advise on {{ branch.question }}"}'
        if advisor
        else ""
    )
    rounds = f"\n    max_advisory_rounds: {max_advisory_rounds}" if max_advisory_rounds else ""
    budget_line = f"\n    budget: {budget}" if budget is not None else ""
    return f"""
workflow: supervisor_demo
version: 1
nodes:
  - id: sup
    kind: supervisor
    advisory_policy: {policy}{budget_line}{rounds}{extra}
    spec:
      prompt: "supervise (advice so far: {{{{ branch.advice }}}})"{advisor_block}
edges: []
triggers:
  - {{ kind: manual }}
"""


def _roles(worker, role):
    return [t for t in worker.turns if t[0] == role]


# ---------------------------------------------------------------------------
# policies
# ---------------------------------------------------------------------------


def test_never_ask_runs_one_supervisor_turn_and_no_advisor(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_sup_yaml(policy="never_ask", budget=None, advisor=False))
    worker = _SupervisorWorker(uncertain_turns=(1,))  # asks, but the policy says no
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    assert len(_roles(worker, "supervisor")) == 1
    assert _roles(worker, "advisor") == []
    out = _sup_output(env["run_id"])
    assert out["advisor_calls"] == 0
    assert out["stopped_reason"] == "never_ask"
    assert out["result"]["decision"] == "hold"


def test_ask_on_uncertain_spends_nothing_when_the_supervisor_is_sure(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_sup_yaml(policy="ask_on_uncertain", budget=3))
    worker = _SupervisorWorker(uncertain_turns=())
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    assert _roles(worker, "advisor") == []  # the cheap path stays cheap
    out = _sup_output(env["run_id"])
    assert out["advisor_calls"] == 0
    assert out["stopped_reason"] == "no_request"


def test_ask_on_uncertain_consults_then_stops_when_the_doubt_clears(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_sup_yaml(policy="ask_on_uncertain", budget=3))
    worker = _SupervisorWorker(uncertain_turns=(1,))  # turn 2 (post-advice) is confident
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    assert len(_roles(worker, "advisor")) == 1
    assert len(_roles(worker, "supervisor")) == 2
    out = _sup_output(env["run_id"])
    assert out["advisor_calls"] == 1
    assert out["stopped_reason"] == "no_request"
    # the FINAL supervisor turn is the node's answer, and it saw the advice
    assert out["result"]["turn"] == 2
    assert out["result"]["advice_seen"] == 1
    assert out["advisory"][0]["advice"] == {"advice": "buy"}


def test_always_ask_consults_every_round_up_to_the_cap(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_sup_yaml(policy="always_ask", budget=2))
    worker = _SupervisorWorker(uncertain_turns=())  # never asks; policy asks anyway
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    assert len(_roles(worker, "advisor")) == 2
    assert len(_roles(worker, "supervisor")) == 3  # first + one after each advisory
    out = _sup_output(env["run_id"])
    assert out["advisor_calls"] == 2
    assert out["stopped_reason"] == "budget"
    assert out["result"]["advice_seen"] == 2


def test_budget_policy_spends_the_budget_then_answers(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_sup_yaml(policy="budget", budget=3))
    env = run(vir, worker=_SupervisorWorker())

    out = _sup_output(env["run_id"])
    assert out["advisor_calls"] == 3
    assert out["result"]["advice_seen"] == 3


# ---------------------------------------------------------------------------
# caps
# ---------------------------------------------------------------------------


def test_budget_is_a_hard_stop_not_a_suggestion(wf_home):
    """An always-uncertain supervisor would consult forever; `budget` is what
    makes that terminate, and it stops the loop rather than skipping calls."""
    from workflow import compile_text, run

    vir = compile_text(_sup_yaml(policy="ask_on_uncertain", budget=2))
    worker = _SupervisorWorker(uncertain_turns=(1, 2, 3, 4, 5, 6, 7))
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    assert len(_roles(worker, "advisor")) == 2  # exactly the budget, no overshoot
    out = _sup_output(env["run_id"])
    assert out["advisor_calls"] == 2
    assert out["stopped_reason"] == "budget"


def test_max_advisory_rounds_can_bind_tighter_than_budget(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_sup_yaml(policy="always_ask", budget=5, max_advisory_rounds=1))
    worker = _SupervisorWorker()
    env = run(vir, worker=worker)

    assert len(_roles(worker, "advisor")) == 1
    out = _sup_output(env["run_id"])
    assert out["stopped_reason"] == "max_advisory_rounds"
    assert out["max_advisory_rounds"] == 1


def test_run_budget_breaker_stops_the_advisory_loop(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_sup_yaml(policy="always_ask", budget=5))
    worker = _SupervisorWorker(cost=0.05)
    env = run(vir, worker=worker, max_budget_usd=0.06)

    assert env["status"] == "paused", env
    assert env["pause_reason"] == "BUDGET"
    # An advisory round is atomic: turn 1 (0.05) -> advisor (0.10, now over cap)
    # -> the supervisor still reads the advice it just paid for. The breaker
    # then stops the NEXT round, so the overshoot is one round, not the
    # remaining four.
    assert [t[0] for t in worker.turns] == ["supervisor", "advisor", "supervisor"]
    assert _sup_output(env["run_id"])["stopped_reason"] == "budget_exhausted"


# ---------------------------------------------------------------------------
# models / context / audit
# ---------------------------------------------------------------------------


def test_supervisor_and_advisor_models_route_separately(wf_home):
    from workflow import compile_text, run

    vir = compile_text(
        _sup_yaml(
            policy="always_ask",
            budget=1,
            extra='\n    supervisor_model: "claude-haiku-4-5-20251001"\n    advisor_model: "claude-opus-5"',
        )
    )
    worker = _SupervisorWorker()
    run(vir, worker=worker)

    models = {role: model for role, _, _, model in worker.turns}
    assert models["supervisor"] == "claude-haiku-4-5-20251001"
    assert models["advisor"] == "claude-opus-5"


def test_advisory_context_is_visible_to_both_sides_and_audited(wf_home):
    """`advisory_context` must be a declared, auditable field -- not a hidden
    injection into someone's system prompt."""
    from workflow import compile_text, run

    vir = compile_text(
        _sup_yaml(policy="always_ask", budget=1, extra='\n    advisory_context: "desk rules v3"')
    )
    worker = _SupervisorWorker()
    env = run(vir, worker=worker)

    assert all(s["item"]["advisory_context"] == "desk rules v3" for s in worker.seen)
    assert _sup_output(env["run_id"])["advisory_context"] == "desk rules v3"


def test_node_run_ids_separate_supervisor_turns_from_advisory_calls(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(_sup_yaml(policy="always_ask", budget=1))
    env = run(vir, worker=_SupervisorWorker())

    rec = checkpoint.load_run_record(env["run_id"])
    ids = [k for k, v in rec["node_runs"].items() if v.get("branch_index") is not None]
    assert len(ids) == 3
    stages = sorted(
        re.search(r"__supervisor_sup__(\w+?)__agent_(\w+)__[0-9a-f]{8}$", i).groups() for i in ids
    )
    assert stages == [("adv_1", "advisor"), ("turn_1", "supervisor"), ("turn_2", "supervisor")]


def test_cost_rolls_up_over_every_turn_without_double_counting(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(_sup_yaml(policy="always_ask", budget=1))
    env = run(vir, worker=_SupervisorWorker(cost=0.02))

    parent = _parent_run(checkpoint.load_run_record(env["run_id"]))
    assert env["cost_usd"] == pytest.approx(0.06)  # 3 turns
    assert parent["cost_usd"] == pytest.approx(0.06)  # display rollup, not a 2nd charge


def test_supervisor_turns_see_the_workflow_workspace(wf_home):
    from workflow import compile_text, run
    from workflow.store import workspace as ws

    yaml_text = _sup_yaml(policy="always_ask", budget=1).replace(
        "version: 1", "version: 1\nworkspace: sup-desk"
    )
    worker = _SupervisorWorker()
    env = run(compile_text(yaml_text), worker=worker)

    assert ws.workspace_run_dir("sup-desk", env["run_id"]).is_dir()
    assert all(s["workspace"]["name"] == "sup-desk" for s in worker.seen)


# ---------------------------------------------------------------------------
# failure handling
# ---------------------------------------------------------------------------


def test_failed_advisor_leaves_the_supervisor_answer_standing(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(_sup_yaml(policy="always_ask", budget=2))
    env = run(vir, worker=_SupervisorWorker(fail_roles=("advisor",)))

    out = _sup_output(env["run_id"])
    assert out["stopped_reason"] == "advisor_failed"
    assert out["result"]["decision"] == "hold"  # the supervisor's own turn 1
    assert out["advisory"][0]["failed"] is True
    # the failed turn is not swallowed: it stays failed on the checkpoint
    rec = checkpoint.load_run_record(env["run_id"])
    failed_turns = [
        v for v in rec["node_runs"].values()
        if v.get("branch_index") is not None and v["status"] == "failed"
    ]
    assert len(failed_turns) == 1


def test_failed_first_supervisor_turn_fails_the_node(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(_sup_yaml(policy="always_ask", budget=1))
    env = run(vir, worker=_SupervisorWorker(fail_roles=("supervisor",)))

    assert env["status"] == "failed", env
    parent = _parent_run(checkpoint.load_run_record(env["run_id"]))
    assert parent["error"]["code"] == "SUPERVISOR"
    assert parent["output"]["result"] is None


def test_supervisor_never_silently_falls_back_to_a_fake_worker(wf_home, monkeypatch):
    from workflow import compile_text
    from workflow.runtime.driver import Driver

    monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)
    env = Driver(compile_text(_sup_yaml(policy="never_ask", budget=None, advisor=False))).execute()

    assert env["status"] == "failed", env


# ---------------------------------------------------------------------------
# verify: fail-closed
# ---------------------------------------------------------------------------


def _codes(yaml_text):
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    with pytest.raises(WorkflowRejected) as exc:
        compile_text(yaml_text)
    return {i.code for i in exc.value.issues if i.severity == "error"}


def test_verify_requires_a_named_advisory_policy(wf_home):
    yaml_text = _sup_yaml().replace("    advisory_policy: ask_on_uncertain\n", "")
    assert "SUPERVISOR" in _codes(yaml_text)


def test_verify_rejects_an_unknown_advisory_policy(wf_home):
    assert "SUPERVISOR" in _codes(_sup_yaml(policy="wing_it"))


def test_verify_requires_a_budget_for_any_asking_policy(wf_home):
    assert "SUPERVISOR" in _codes(_sup_yaml(policy="always_ask", budget=None))


def test_verify_requires_an_advisor_template(wf_home):
    assert "SUPERVISOR" in _codes(_sup_yaml(policy="always_ask", advisor=False))


def test_verify_requires_a_supervisor_prompt(wf_home):
    yaml_text = _sup_yaml().replace(
        '      prompt: "supervise (advice so far: {{ branch.advice }})"', "      prompt: \"\""
    )
    assert "PROMPT" in _codes(yaml_text)


def test_verify_rejects_a_supervisor_of_supervisors(wf_home):
    yaml_text = """
workflow: nested_supervisor
nodes:
  - id: sup
    kind: supervisor
    advisory_policy: always_ask
    budget: 1
    spec: {prompt: "supervise"}
    advisor:
      id: inner
      kind: supervisor
      advisory_policy: always_ask
      budget: 1
      spec: {prompt: "supervise deeper"}
      advisor: {id: deepest, kind: agent, spec: {prompt: "advise"}}
edges: []
"""
    assert "STRUCTURE" in _codes(yaml_text)


def test_verify_recurses_into_the_advisor_template(wf_home):
    yaml_text = _sup_yaml().replace('      spec: {prompt: "advise on {{ branch.question }}"}', "      spec: {}")
    assert "PROMPT" in _codes(yaml_text)


def test_verify_rejects_a_negative_budget(wf_home):
    assert "SUPERVISOR" in _codes(_sup_yaml(policy="always_ask", budget=0))


def test_verify_requires_side_effects_on_a_live_tooled_advisor(wf_home):
    """F6 follows the node that dispatches the child, same as for debate."""
    yaml_text = """
workflow: live_supervisor
nodes:
  - id: seed
    kind: agent
    spec: {prompt: "go"}
  - id: sup
    kind: supervisor
    advisory_policy: always_ask
    budget: 1
    spec: {prompt: "supervise"}
    advisor:
      id: trader
      kind: agent
      side_effects: external
      spec: {prompt: "advise", tools: [trade_live]}
edges:
  - { from: seed, to: sup }
"""
    codes = _codes(yaml_text)
    assert "SIDE_EFFECTS" in codes  # the supervisor node itself must declare it
    assert "LIVE_UNGATED" in codes  # ...and sit behind a gate


def test_never_ask_warns_about_an_unreachable_advisor(wf_home):
    from workflow import compile_text

    vir = compile_text(_sup_yaml(policy="never_ask", budget=None, advisor=True))
    assert any(i.code == "SUPERVISOR" and i.severity == "warning" for i in vir.issues)


def test_supervisor_survives_a_definition_round_trip(wf_home):
    from workflow import compile_text
    from workflow.ir import WorkflowIR

    vir = compile_text(
        _sup_yaml(policy="budget", budget=2, max_advisory_rounds=1, extra='\n    advisor_model: "claude-opus-5"')
    )
    node = {n.id: n for n in WorkflowIR.from_dict(vir.ir.to_dict()).nodes}["sup"]
    assert node.kind == "supervisor"
    assert node.advisory_policy == "budget"
    assert node.budget == 2
    assert node.max_advisory_rounds == 1
    assert node.advisor_model == "claude-opus-5"
    assert node.advisor["id"] == "quant"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parent_run(rec):
    return [
        v for v in rec["node_runs"].values()
        if v.get("branch_index") is None and v["node_id"] == "sup"
    ][0]


def _sup_output(run_id):
    from workflow.store import checkpoint

    return _parent_run(checkpoint.load_run_record(run_id))["output"]
