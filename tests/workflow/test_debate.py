"""Debate primitive (post-Phase-3 §3): protocols, bounds, and fail-closed verify.

Contract tests, not snapshots. The properties that matter for a primitive whose
whole job is to spend money in a bounded way:

* it terminates -- `participants x max_rounds` turns is the hard ceiling, and
  early convergence is the only thing that makes it fewer;
* the budget breaker is armed INSIDE the round loop, not merely around the node;
* a failed participant is tolerated by the debate but never silently tolerated
  by the run (the turn stays failed, downstream still resolves);
* compile time rejects the shapes that would make any of the above untrue
  (unbounded rounds, a nested debate, an ungated live tool hiding in a child).
"""

from __future__ import annotations

import re

import pytest


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------


class _VoteWorker:
    """Agents that vote a scripted answer. `votes` maps a participant node id
    to a value, or to a callable(round) -> value for a position that moves."""

    def __init__(self, votes, cost=0.0, fail_nodes=(), judge_answer="judged"):
        self.votes = votes
        self.cost = cost
        self.fail_nodes = set(fail_nodes)
        self.judge_answer = judge_answer
        self.turns = []  # (node_id, round, role)
        self.seen_ctx = []

    def run_node(self, node, ctx):
        item = ctx.get("branch") or {}
        role = item.get("role")
        rnd = item.get("round")
        self.turns.append((node.id, rnd, role))
        self.seen_ctx.append({"node": node, "item": item, "workspace": ctx.get("workspace")})
        if node.id in self.fail_nodes:
            raise RuntimeError(f"participant {node.id} blew up")
        if role == "judge":
            answer = self.judge_answer
        else:
            answer = self.votes.get(node.id, "abstain")
            if callable(answer):
                answer = answer(rnd)
        return {
            "output": {"answer": answer, "by": node.id, "round": rnd},
            "cost_usd": self.cost,
            "tokens_in": 3,
            "tokens_out": 2,
        }


def _debate_yaml(
    *,
    protocol="vote",
    max_rounds=2,
    participants=("hawk", "dove"),
    extra_directive="",
    judge=None,
    downstream=False,
):
    parts = "\n".join(
        f"      - id: {p}\n"
        f"        kind: agent\n"
        f'        spec: {{prompt: "round {{{{ branch.round }}}}: {{{{ branch.topic }}}}"}}'
        for p in participants
    )
    judge_block = ""
    if judge:
        judge_block = (
            f"\n      judge:\n"
            f"        id: {judge}\n"
            f"        kind: agent\n"
            f'        spec: {{prompt: "rule on {{{{ branch.topic }}}}"}}'
        )
    tail = ""
    if downstream:
        tail = (
            "\n  - id: after\n"
            "    kind: agent\n"
            '    spec: {prompt: "verdict was {{ d.output.result.winner }}"}\n'
        )
    edges = "edges:\n  - { from: d, to: after }\n" if downstream else "edges: []\n"
    return f"""
workflow: debate_demo
version: 1
nodes:
  - id: d
    kind: debate
    protocol: {protocol}
    max_rounds: {max_rounds}
    directive:
      topic: "ship or hold?"
      vote_key: answer{extra_directive}{judge_block}
    participants:
{parts}{tail}
{edges}triggers:
  - {{ kind: manual }}
"""


def _turn_count(worker, role="participant"):
    return sum(1 for t in worker.turns if t[2] == role)


# ---------------------------------------------------------------------------
# protocol: vote
# ---------------------------------------------------------------------------


def test_vote_converges_early_and_stops_spending(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_debate_yaml(protocol="vote", max_rounds=3, participants=("hawk", "dove", "owl")))
    worker = _VoteWorker({"hawk": "ship", "dove": "ship", "owl": "hold"})
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    # 2 of 3 is a strict majority of the votes cast -> round 1 ends it, so the
    # remaining 2 rounds are never paid for.
    assert _turn_count(worker) == 3
    out = _debate_output(env["run_id"])
    assert out["rounds_run"] == 1
    assert out["converged"] is True
    assert out["stopped_reason"] == "converged"
    assert out["result"]["kind"] == "majority"
    assert out["result"]["winner"] == "ship"
    assert out["result"]["count"] == 2


def test_vote_tie_fails_diverged_after_max_rounds(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_debate_yaml(protocol="vote", max_rounds=2))
    worker = _VoteWorker({"hawk": "ship", "dove": "hold"})
    env = run(vir, worker=worker)

    # a vote protocol exists to produce a decision; "no decision" is a failure,
    # not a success with an empty verdict.
    assert env["status"] == "failed", env
    assert _turn_count(worker) == 4  # 2 participants x 2 rounds, no more
    out = _debate_output(env["run_id"])
    assert out["rounds_run"] == 2
    assert out["converged"] is False
    assert out["stopped_reason"] == "max_rounds"

    from workflow.store import checkpoint

    rec = checkpoint.load_run_record(env["run_id"])
    parent = _parent_run(rec)
    assert parent["error"]["code"] == "DEBATE_DIVERGED"


def test_vote_threshold_raises_the_bar(wf_home):
    from workflow import compile_text, run

    vir = compile_text(
        _debate_yaml(
            protocol="vote",
            max_rounds=2,
            participants=("hawk", "dove", "owl"),
            extra_directive="\n      threshold: 3",
        )
    )
    # 2/3 would converge by default, but threshold: 3 demands unanimity
    worker = _VoteWorker({"hawk": "ship", "dove": "ship", "owl": "hold"})
    env = run(vir, worker=worker)

    assert env["status"] == "failed", env
    assert _debate_output(env["run_id"])["rounds_run"] == 2
    assert _turn_count(worker) == 6


def test_vote_converges_in_a_later_round_when_a_position_moves(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_debate_yaml(protocol="vote", max_rounds=4))
    worker = _VoteWorker({"hawk": "ship", "dove": lambda r: "hold" if r == 1 else "ship"})
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    out = _debate_output(env["run_id"])
    assert out["rounds_run"] == 2
    assert out["result"]["winner"] == "ship"
    assert _turn_count(worker) == 4


# ---------------------------------------------------------------------------
# protocol: continue / max_rounds bound
# ---------------------------------------------------------------------------


def test_continue_runs_every_round_and_concatenates(wf_home):
    from workflow import compile_text, run

    vir = compile_text(_debate_yaml(protocol="continue", max_rounds=3))
    worker = _VoteWorker({"hawk": "ship", "dove": "hold"})
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    assert _turn_count(worker) == 6  # the hard ceiling, exactly
    out = _debate_output(env["run_id"])
    assert out["rounds_run"] == 3
    assert out["stopped_reason"] == "max_rounds"
    assert out["result"]["kind"] == "concat"
    assert len(out["result"]["branches"]) == 6
    assert len(out["transcript"]) == 6


def test_participants_argue_from_previous_rounds_only(wf_home):
    """Every participant in a round sees the same transcript -- so a round is
    order-independent, and round N's transcript is exactly rounds 1..N-1."""
    from workflow import compile_text, run

    vir = compile_text(_debate_yaml(protocol="continue", max_rounds=3))
    worker = _VoteWorker({"hawk": "ship", "dove": "hold"})
    run(vir, worker=worker)

    by_round = {}
    for seen in worker.seen_ctx:
        by_round.setdefault(seen["item"]["round"], []).append(seen["item"]["transcript"])
    assert [len(t) for t in by_round[1]] == [0, 0]
    assert [len(t) for t in by_round[2]] == [2, 2]
    assert [len(t) for t in by_round[3]] == [4, 4]
    assert {e["participant"] for e in by_round[3][0]} == {"hawk", "dove"}


# ---------------------------------------------------------------------------
# protocol: judge_escalate
# ---------------------------------------------------------------------------


def test_judge_escalate_runs_the_judge_only_when_diverged(wf_home):
    from workflow import compile_text, run

    vir = compile_text(
        _debate_yaml(protocol="judge_escalate", max_rounds=2, judge="arbiter")
    )
    worker = _VoteWorker({"hawk": "ship", "dove": "hold"}, judge_answer="hold")
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    assert _turn_count(worker, role="judge") == 1
    out = _debate_output(env["run_id"])
    assert out["result"]["kind"] == "judge_converge"
    assert out["result"]["judged"] is True
    assert out["result"]["judgment"]["answer"] == "hold"
    # the tally the judge ruled against is preserved next to the ruling
    assert out["result"]["tie"] is True
    assert len(out["result"]["candidates"]) == 2
    assert "agent_judge" in out["judge_node_run_id"]


def test_judge_escalate_skips_the_judge_when_the_room_converges(wf_home):
    from workflow import compile_text, run

    vir = compile_text(
        _debate_yaml(
            protocol="judge_escalate",
            max_rounds=2,
            participants=("hawk", "dove", "owl"),
            judge="arbiter",
        )
    )
    worker = _VoteWorker({"hawk": "ship", "dove": "ship", "owl": "hold"})
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    assert _turn_count(worker, role="judge") == 0  # no escalation, no judge spend
    out = _debate_output(env["run_id"])
    assert out["converged"] is True
    assert out["result"]["judged"] is False
    assert out["result"]["majority_winner"] == "ship"
    assert out["judge_node_run_id"] is None


def test_judge_model_override_reaches_the_judge_child(wf_home):
    from workflow import compile_text, run

    vir = compile_text(
        _debate_yaml(
            protocol="judge_escalate",
            max_rounds=1,
            judge="arbiter",
            extra_directive='\n      judge_model: "claude-opus-5"',
        )
    )
    worker = _VoteWorker({"hawk": "ship", "dove": "hold"})
    run(vir, worker=worker)

    judge_nodes = [s["node"] for s in worker.seen_ctx if s["item"].get("role") == "judge"]
    assert len(judge_nodes) == 1
    assert judge_nodes[0].spec.model == "claude-opus-5"
    # participants keep whatever they declared (nothing)
    assert all(
        s["node"].spec.model is None
        for s in worker.seen_ctx
        if s["item"].get("role") == "participant"
    )


# ---------------------------------------------------------------------------
# audit trail / accounting
# ---------------------------------------------------------------------------


def test_node_run_id_naming_separates_rounds_and_agents(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(_debate_yaml(protocol="continue", max_rounds=2))
    run(vir, worker=_VoteWorker({"hawk": "ship", "dove": "hold"}))

    rec = checkpoint.load_run_record(_last_run_id())
    turn_ids = [k for k, v in rec["node_runs"].items() if v.get("branch_index") is not None]
    assert len(turn_ids) == 4
    pattern = re.compile(r"__debate_d__round_(\d)__agent_(hawk|dove)__[0-9a-f]{8}$")
    matched = [pattern.search(t) for t in turn_ids]
    assert all(matched), turn_ids
    assert {(m.group(1), m.group(2)) for m in matched} == {
        ("1", "hawk"), ("1", "dove"), ("2", "hawk"), ("2", "dove")
    }


def test_cost_rolls_up_without_double_counting(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(_debate_yaml(protocol="continue", max_rounds=2))
    env = run(vir, worker=_VoteWorker({"hawk": "ship", "dove": "hold"}, cost=0.01))

    rec = checkpoint.load_run_record(env["run_id"])
    parent = _parent_run(rec)
    assert env["cost_usd"] == pytest.approx(0.04)  # 4 turns, counted once each
    assert parent["cost_usd"] == pytest.approx(0.04)  # display rollup, not a 2nd charge
    assert parent["tokens_in"] == 12 and parent["tokens_out"] == 8
    assert env["tokens_in"] == 12 and env["tokens_out"] == 8


def test_budget_breaker_stops_the_debate_mid_round(wf_home):
    """The cap has to be armed inside the round loop: a debate is one node-run
    that internally runs participants x rounds billable turns."""
    from workflow import compile_text, run

    vir = compile_text(_debate_yaml(protocol="continue", max_rounds=5))
    worker = _VoteWorker({"hawk": "ship", "dove": "hold"}, cost=0.05)
    env = run(vir, worker=worker, max_budget_usd=0.06)

    assert env["status"] == "paused", env
    assert env["pause_reason"] == "BUDGET"
    # 2 turns spent 0.10 > 0.06; the remaining 8 turns of the ceiling never ran
    assert _turn_count(worker) == 2
    out = _debate_output(env["run_id"])
    assert out["stopped_reason"] == "budget_exhausted"


# ---------------------------------------------------------------------------
# failure handling / downstream
# ---------------------------------------------------------------------------


def test_failed_participant_is_recorded_but_does_not_wedge_downstream(wf_home):
    from workflow import compile_text, run

    vir = compile_text(
        _debate_yaml(
            protocol="vote", max_rounds=2, participants=("hawk", "dove", "owl"), downstream=True
        )
    )
    worker = _VoteWorker({"hawk": "ship", "owl": "ship"}, fail_nodes=("dove",))
    env = run(vir, worker=worker)

    # the failed turn is reported honestly (branch-qualified), the debate still
    # reaches a verdict on the arguments that survived, and `after` runs.
    assert "d#1" in env["failed"], env
    assert "after" in env["succeeded"], env
    out = _debate_output(env["run_id"])
    assert out["converged"] is True
    assert out["result"]["winner"] == "ship"
    assert out["result"]["total"] == 2


def test_debate_fails_when_no_participant_produces_an_argument(wf_home):
    from workflow import compile_text, run
    from workflow.store import checkpoint

    vir = compile_text(_debate_yaml(protocol="continue", max_rounds=1))
    env = run(vir, worker=_VoteWorker({}, fail_nodes=("hawk", "dove")))

    assert env["status"] == "failed", env
    parent = _parent_run(checkpoint.load_run_record(env["run_id"]))
    assert parent["error"]["code"] == "DEBATE"


def test_on_fail_retry_happens_inside_the_round(wf_home):
    """A retried turn re-runs in its own round rather than being left pending
    for the ready-set walk to pick up after the debate concluded.

    Note the attempts budget is read off the TURN's own template (the same rule
    fanout branches follow), while the POLICY falls back to the debate node."""
    from workflow import compile_text, run

    yaml_text = """
workflow: debate_retry
nodes:
  - id: d
    kind: debate
    protocol: continue
    max_rounds: 1
    on_fail: retry
    directive: {topic: "t"}
    participants:
      - {id: hawk, kind: agent, attempts: 3, spec: {prompt: "a"}}
      - {id: dove, kind: agent, spec: {prompt: "b"}}
edges: []
"""
    calls = {"n": 0}

    class _FlakyWorker(_VoteWorker):
        def run_node(self, node, ctx):
            if node.id == "hawk":
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("flaky")
            return super().run_node(node, ctx)

    vir = compile_text(yaml_text)
    env = run(vir, worker=_FlakyWorker({"hawk": "ship", "dove": "hold"}))

    assert calls["n"] == 3
    assert env["status"] == "succeeded", env
    out = _debate_output(env["run_id"])
    assert len(out["transcript"]) == 2  # both participants argued in the end


def test_debate_never_silently_falls_back_to_a_fake_worker(wf_home, monkeypatch):
    """A workerless driver must not hand a debate canned arguments: the turns
    fail loudly and the debate fails with them (HERMES_WORKFLOW_FAKE unset)."""
    from workflow import compile_text
    from workflow.runtime.driver import Driver
    from workflow.store import checkpoint

    monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)
    vir = compile_text(_debate_yaml(protocol="continue", max_rounds=1))
    env = Driver(vir).execute()

    assert env["status"] == "failed", env
    rec = checkpoint.load_run_record(env["run_id"])
    turns = [v for v in rec["node_runs"].values() if v.get("branch_index") is not None]
    assert turns and all(t["status"] == "failed" for t in turns)
    assert "without a worker" in turns[0]["error"]["message"]
    assert _parent_run(rec)["error"]["code"] == "DEBATE"


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------


def test_debate_turns_see_the_workflow_workspace(wf_home):
    from workflow import compile_text, run
    from workflow.store import workspace as ws

    yaml_text = _debate_yaml(protocol="continue", max_rounds=1).replace(
        "version: 1", "version: 1\nworkspace: debate-desk"
    )
    vir = compile_text(yaml_text)
    worker = _VoteWorker({"hawk": "ship", "dove": "hold"})
    env = run(vir, worker=worker)

    assert env["status"] == "succeeded", env
    assert ws.workspace_run_dir("debate-desk", env["run_id"]).is_dir()
    for seen in worker.seen_ctx:
        assert seen["workspace"]["name"] == "debate-desk"
        assert seen["workspace"]["run_id"] == env["run_id"]


# ---------------------------------------------------------------------------
# verify: fail-closed
# ---------------------------------------------------------------------------


def _codes(yaml_text):
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    with pytest.raises(WorkflowRejected) as exc:
        compile_text(yaml_text)
    return {i.code for i in exc.value.issues if i.severity == "error"}


def test_verify_requires_a_directive_topic(wf_home):
    yaml_text = _debate_yaml().replace('      topic: "ship or hold?"\n', "")
    assert "DEBATE" in _codes(yaml_text)


def test_verify_requires_bounded_rounds(wf_home):
    assert "DEBATE" in _codes(_debate_yaml().replace("    max_rounds: 2\n", ""))
    assert "DEBATE" in _codes(_debate_yaml(max_rounds=0))


def test_verify_rejects_unknown_protocol(wf_home):
    assert "DEBATE" in _codes(_debate_yaml(protocol="freeforall"))


def test_verify_requires_at_least_two_participants(wf_home):
    assert "DEBATE" in _codes(_debate_yaml(participants=("hawk",)))


def test_verify_requires_a_judge_for_judge_escalate(wf_home):
    assert "DEBATE" in _codes(_debate_yaml(protocol="judge_escalate"))


def test_verify_rejects_judge_profile(wf_home):
    yaml_text = _debate_yaml(
        protocol="judge_escalate", judge="arbiter", extra_directive='\n      judge_profile: "trader"'
    )
    assert "DEBATE" in _codes(yaml_text)


def test_verify_rejects_a_nested_debate_participant(wf_home):
    """The one shape that turns a bounded primitive into recursive spend."""
    yaml_text = """
workflow: nested_debate
nodes:
  - id: d
    kind: debate
    protocol: continue
    max_rounds: 2
    directive: {topic: "t"}
    participants:
      - id: inner
        kind: debate
        protocol: continue
        max_rounds: 2
        directive: {topic: "t"}
        participants:
          - {id: a, kind: agent, spec: {prompt: "a"}}
          - {id: b, kind: agent, spec: {prompt: "b"}}
      - {id: c, kind: agent, spec: {prompt: "c"}}
edges: []
"""
    assert "STRUCTURE" in _codes(yaml_text)


def test_verify_recurses_into_participant_templates(wf_home):
    """A participant is a node: a missing prompt is caught at compile time,
    not discovered as an empty goal at run time."""
    yaml_text = _debate_yaml().replace(
        '        spec: {prompt: "round {{ branch.round }}: {{ branch.topic }}"}\n'
        "      - id: dove",
        "        spec: {}\n      - id: dove",
    )
    assert "PROMPT" in _codes(yaml_text)


def test_verify_requires_side_effects_on_a_live_tooled_participant(wf_home):
    """A live tool inside a participant is exactly as side-effecting as one on
    an agent node -- nesting must not launder it past F6."""
    yaml_text = """
workflow: live_debate
nodes:
  - id: seed
    kind: agent
    spec: {prompt: "go"}
  - id: d
    kind: debate
    protocol: continue
    max_rounds: 1
    directive: {topic: "t"}
    participants:
      - {id: a, kind: agent, spec: {prompt: "a", tools: [trade_live]}}
      - {id: b, kind: agent, spec: {prompt: "b"}}
edges:
  - { from: seed, to: d }
"""
    assert "SIDE_EFFECTS" in _codes(yaml_text)


def test_verify_requires_a_gate_before_a_live_tooled_debate(wf_home):
    """...and F3 gating follows the node that dispatches the children."""
    yaml_text = """
workflow: live_debate_gate
nodes:
  - id: seed
    kind: agent
    spec: {prompt: "go"}
  - id: d
    kind: debate
    protocol: continue
    max_rounds: 1
    side_effects: external
    directive: {topic: "t"}
    participants:
      - {id: a, kind: agent, spec: {prompt: "a", tools: [trade_live]}}
      - {id: b, kind: agent, spec: {prompt: "b"}}
edges:
  - { from: seed, to: d }
"""
    assert "LIVE_UNGATED" in _codes(yaml_text)


def test_gated_live_debate_compiles(wf_home):
    """Both declarations are required and mean different things: the PARTICIPANT
    is the side-effecting agent (F6), and the DEBATE node is what the gate sits
    in front of and what resume treats as INTERRUPTED rather than requeueing."""
    from workflow import compile_text

    yaml_text = """
workflow: live_debate_ok
nodes:
  - id: seed
    kind: agent
    spec: {prompt: "go"}
  - id: approve
    kind: gate
  - id: d
    kind: debate
    protocol: continue
    max_rounds: 1
    side_effects: external
    directive: {topic: "t"}
    participants:
      - {id: a, kind: agent, side_effects: external, spec: {prompt: "a", tools: [trade_live]}}
      - {id: b, kind: agent, spec: {prompt: "b"}}
gates:
  approve:
    channel: telegram
    approvers: [joe]
edges:
  - { from: seed, to: approve }
  - { from: approve, to: d }
"""
    vir = compile_text(yaml_text)
    assert [n.kind for n in vir.ir.nodes if n.id == "d"] == ["debate"]


def test_debate_survives_a_definition_round_trip(wf_home):
    """The IR is the contract shared by authoring, verify, driver and resume --
    a debate's fields must survive to_dict/from_dict intact."""
    from workflow import compile_text
    from workflow.ir import WorkflowIR

    vir = compile_text(_debate_yaml(protocol="judge_escalate", max_rounds=3, judge="arbiter"))
    reloaded = WorkflowIR.from_dict(vir.ir.to_dict())
    node = {n.id: n for n in reloaded.nodes}["d"]
    assert node.kind == "debate"
    assert node.protocol == "judge_escalate"
    assert node.max_rounds == 3
    assert node.directive["topic"] == "ship or hold?"
    assert [p["id"] for p in node.participants] == ["hawk", "dove"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parent_run(rec):
    parents = [v for v in rec["node_runs"].values() if v.get("branch_index") is None]
    assert len(parents) >= 1
    return [p for p in parents if p["node_id"] == "d"][0]


def _debate_output(run_id):
    from workflow.store import checkpoint

    return _parent_run(checkpoint.load_run_record(run_id))["output"]


def _last_run_id():
    from workflow.store import fs

    runs = sorted((fs.workflows_root() / "runs").iterdir(), key=lambda p: p.stat().st_mtime)
    return runs[-1].name
