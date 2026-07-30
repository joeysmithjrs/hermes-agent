"""Phase 3 surface acceptance tests: workflow/chain.py units, CLI
watch/list --cost/schedule, LiveWorker tools-subset kwargs,
live.extract_cost_and_tokens, notify presets, budget-pause notification
content, and the kanban projection stub.

Hermetic: no network, no live LLM, no real config, no real message send, no
real kanban write. Every polling loop is bounded (interval/timeout) so a
broken assumption fails fast instead of hanging CI.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# 12. workflow/chain.py units
# ---------------------------------------------------------------------------


def test_select_path_dict_list_int_index_missing_never_raises():
    from workflow.chain import select_path

    assert select_path({"a": {"b": [10, 20, 30]}}, "a.b.1") == 20
    assert select_path({"a": 1}, None) == {"a": 1}  # empty path -> whole obj
    assert select_path({"a": 1}, "") == {"a": 1}
    assert select_path({"a": 1}, "missing") is None  # missing dict key
    assert select_path({"a": 1}, "a.b") is None  # indexing through a scalar
    assert select_path([1, 2, 3], "5") is None  # out-of-range list index
    assert select_path([1, 2, 3], "-1") is None  # negative index -> None (never raises)
    assert select_path([1, 2, 3], "x") is None  # non-integer index into a list
    assert select_path(None, "a.b") is None  # None object -> None, not a crash


def test_build_chain_input_provenance_keys_and_extra_input_wins():
    from workflow.chain import build_chain_input

    source_envelope = {
        "run_id": "wf_source1",
        "workflow_id": "wf_id_1",
        "status": "succeeded",
        "output": {"x": 42},
    }
    derived = build_chain_input(
        source_envelope,
        select="output.x",
        as_key="seed",
        extra_input={"source_run_id": "OVERRIDDEN", "extra_field": True},
    )
    assert derived["seed"] == 42
    # provenance keys present...
    assert derived["source_workflow_id"] == "wf_id_1"
    assert derived["source_status"] == "succeeded"
    # ...but extra_input wins on key collision (documented, not accidental)
    assert derived["source_run_id"] == "OVERRIDDEN"
    assert derived["extra_field"] is True


def test_build_chain_input_default_as_key_and_no_extra_input():
    from workflow.chain import build_chain_input

    derived = build_chain_input({"run_id": "r1", "workflow_id": "w1", "status": "failed"})
    assert "from_run" in derived  # default as_key
    assert derived["from_run"] == {"run_id": "r1", "workflow_id": "w1", "status": "failed"}


def test_resolve_chain_input_raises_when_source_not_terminal(monkeypatch):
    import workflow as wf
    from workflow.chain import resolve_chain_input, ChainSourceNotTerminal

    monkeypatch.setattr(wf, "status", lambda rid: {"run_id": rid, "workflow_id": "w1", "status": "running"})

    with pytest.raises(ChainSourceNotTerminal):
        resolve_chain_input("wf_notdone")

    # allow_incomplete=True bypasses the check
    result = resolve_chain_input("wf_notdone", allow_incomplete=True)
    assert result["source_status"] == "running"
    assert result["source_run_id"] == "wf_notdone"


@pytest.mark.parametrize("done_status", ["succeeded", "failed", "partial", "cancelled"])
def test_resolve_chain_input_accepts_every_done_status(monkeypatch, done_status):
    import workflow as wf
    from workflow.chain import resolve_chain_input, DONE_STATUSES

    assert done_status in DONE_STATUSES
    monkeypatch.setattr(wf, "status", lambda rid: {"run_id": rid, "workflow_id": "w1", "status": done_status})
    result = resolve_chain_input("wf_done")
    assert result["source_status"] == done_status


def test_resolve_chain_input_integration_real_run(wf_home):
    """End-to-end (no monkeypatch): a real FakeWorker run reaches
    `succeeded`, and resolve_chain_input reads its real on-disk envelope."""
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker
    from workflow.chain import resolve_chain_input

    yaml_text = """
workflow: chain_source
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
edges: []
triggers:
  - { kind: manual }
"""
    vir = compile_text(yaml_text, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    assert env["status"] == "succeeded", env

    derived = resolve_chain_input(env["run_id"], select="status", as_key="src")
    assert derived["src"] == "succeeded"
    assert derived["source_run_id"] == env["run_id"]
    assert derived["source_workflow_id"] == "chain_source"


# ---------------------------------------------------------------------------
# 13. `hermes workflow watch` exit codes
# ---------------------------------------------------------------------------


def _seed_run_record(run_id, *, status, **extra):
    from workflow.runtime.driver import RunState
    from workflow.store import checkpoint as ckpt

    state = RunState(run_id=run_id, workflow_id="watchwf", status=status, **extra)
    ckpt.write_run_record(run_id, state.to_dict())


@pytest.mark.parametrize(
    "run_status,expected_rc",
    [
        ("succeeded", 0),
        ("awaiting_gate", 4),
        ("failed", 1),
        ("partial", 1),
        ("cancelled", 1),
    ],
)
def test_watch_exits_with_status_specific_code(wf_home, run_status, expected_rc):
    from workflow.cli import _cmd_watch

    run_id = f"wf_watchcode_{run_status}"
    extra = {"awaiting_gate": "g1"} if run_status == "awaiting_gate" else {}
    _seed_run_record(run_id, status=run_status, **extra)

    args = argparse.Namespace(run_id=run_id, interval=0.01, timeout=2.0, as_json=False)
    rc = _cmd_watch(args)
    assert rc == expected_rc, (run_status, rc)


def test_watch_timeout_exits_1_and_never_hangs(wf_home):
    from workflow.cli import _cmd_watch

    run_id = "wf_watchtimeout01"
    _seed_run_record(run_id, status="running")

    args = argparse.Namespace(run_id=run_id, interval=0.02, timeout=0.2, as_json=False)
    t0 = time.monotonic()
    rc = _cmd_watch(args)
    elapsed = time.monotonic() - t0
    assert rc == 1, rc
    assert elapsed < 5.0, "watch must respect --timeout and never hang"


# ---------------------------------------------------------------------------
# 14. `list --cost` breakdown; default output unchanged
# ---------------------------------------------------------------------------

LIST_YAML = """
workflow: list_cost_test
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
edges: []
triggers:
  - { kind: manual }
"""


def _run_one(tmp_path, capsys):
    from workflow.cli import _cmd_run

    path = _write(tmp_path, "wf.yaml", LIST_YAML)
    args = argparse.Namespace(
        path_or_id=path, input=None, resume=None, from_node=None,
        retry_failed=False, dry_run=False, max_budget_usd=None,
    )
    rc = _cmd_run(args)
    assert rc == 0, rc
    out = capsys.readouterr().out
    return json.loads(out)["run_id"]


def test_list_default_output_is_five_tab_fields_no_token_breakdown(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_list

    rid = _run_one(tmp_path, capsys)

    rc = _cmd_list(argparse.Namespace(status_filter=None, workflow_id=None, limit=50, cost=False))
    assert rc == 0, rc
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1
    fields = lines[0].split("\t")
    assert len(fields) == 5, fields  # run_id, status, workflow_id, started, $cost -- unchanged shape
    assert fields[0] == rid
    assert fields[1] == "succeeded"
    assert fields[2] == "list_cost_test"
    assert fields[4].startswith("$")
    assert "tokens_in" not in out
    assert "tokens_out" not in out
    assert "TOTAL" not in out


def test_list_cost_shows_token_breakdown_and_total_line(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_list

    rid = _run_one(tmp_path, capsys)

    rc = _cmd_list(argparse.Namespace(status_filter=None, workflow_id=None, limit=50, cost=True))
    assert rc == 0, rc
    out = capsys.readouterr().out
    assert rid in out
    assert "tokens_in=" in out
    assert "tokens_out=" in out
    lines = [ln for ln in out.splitlines() if ln]
    assert any(ln.startswith("TOTAL") for ln in lines)


# ---------------------------------------------------------------------------
# `hermes workflow schedule` -- cron/trigger sugar
# ---------------------------------------------------------------------------


def test_schedule_print_only_uses_workflow_own_cron_trigger(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_schedule

    yaml_text = """
workflow: sched_own_trigger
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
edges: []
triggers:
  - cron: {schedule: "0 9 * * *"}
"""
    path = _write(tmp_path, "sched.yaml", yaml_text)
    args = argparse.Namespace(path=path, cron=None, input=None, name=None, register=False, fake=False)
    rc = _cmd_schedule(args)
    assert rc == 0, rc
    out = capsys.readouterr().out
    assert "hermes cron create" in out
    assert "0 9 * * *" in out
    assert "--no-agent" in out
    # zero side effects in print-only mode
    from workflow.store import fs
    assert not (fs.workflows_root().parent / "scripts").exists() or not list((fs.workflows_root().parent / "scripts").glob("*"))


def test_schedule_no_cron_anywhere_is_usage_error(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_schedule, EXIT_USAGE

    yaml_text = """
workflow: sched_no_cron
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
edges: []
triggers:
  - { kind: manual }
"""
    path = _write(tmp_path, "sched.yaml", yaml_text)
    args = argparse.Namespace(path=path, cron=None, input=None, name=None, register=False, fake=False)
    rc = _cmd_schedule(args)
    assert rc == EXIT_USAGE, rc
    err = capsys.readouterr().err
    assert "no --cron" in err


def test_schedule_native_schedule_spec_has_no_fake_crontab_line(wf_home, tmp_path, capsys):
    """A hermes-native schedule spec ('30m') is not raw cron syntax; the
    command must not print a fabricated crontab-equivalent line for it."""
    from workflow.cli import _cmd_schedule

    yaml_text = """
workflow: sched_native
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
edges: []
triggers:
  - { kind: manual }
"""
    path = _write(tmp_path, "sched.yaml", yaml_text)
    args = argparse.Namespace(path=path, cron="30m", input=None, name=None, register=False, fake=False)
    rc = _cmd_schedule(args)
    assert rc == 0, rc
    out = capsys.readouterr().out
    assert "hermes-native schedule spec" in out
    assert "equivalent raw crontab line" not in out


# ---------------------------------------------------------------------------
# 15. spec.tools subset -> LiveWorker child construction kwargs
# ---------------------------------------------------------------------------


class _RecordingBuilder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return object()

    @property
    def last(self):
        return self.calls[-1]


def _stub_runner():
    def _run(task_index, goal, child=None, parent_agent=None):
        return {"cost_usd": 0.0}

    return _run


class _DummyParent:
    def __init__(self, model="p-model", provider="p-provider"):
        self.model = model
        self.provider = provider


def test_spec_tools_subset_resolves_to_covering_toolsets(wf_home):
    from workflow.ir import Node, NodeSpec
    from workflow.runtime.live import LiveWorker

    builder = _RecordingBuilder()
    worker = LiveWorker(parent_agent=_DummyParent(), child_builder=builder, child_runner=_stub_runner())

    node = Node(id="a", kind="agent", spec=NodeSpec(prompt="do", tools=["read_file"]))
    worker.run_node(node, {"input": {}})

    toolsets = builder.last["toolsets"]
    assert toolsets is not None
    assert isinstance(toolsets, list)
    assert len(toolsets) >= 1  # resolved to a real covering toolset (not passed through raw)
    assert "read_file" not in toolsets  # a TOOLSET name, not the bare tool name


def test_spec_tools_unset_means_inherit_all_toolsets_none(wf_home):
    from workflow.ir import Node, NodeSpec
    from workflow.runtime.live import LiveWorker

    builder = _RecordingBuilder()
    worker = LiveWorker(parent_agent=_DummyParent(), child_builder=builder, child_runner=_stub_runner())

    node = Node(id="a", kind="agent", spec=NodeSpec(prompt="do"))  # no tools
    worker.run_node(node, {"input": {}})

    assert builder.last["toolsets"] is None


def test_spec_max_turns_lands_in_max_iterations(wf_home):
    from workflow.ir import Node, NodeSpec
    from workflow.runtime.live import LiveWorker

    builder = _RecordingBuilder()
    worker = LiveWorker(parent_agent=_DummyParent(), child_builder=builder, child_runner=_stub_runner())

    node = Node(id="a", kind="agent", spec=NodeSpec(prompt="do", max_turns=7))
    worker.run_node(node, {"input": {}})

    assert builder.last["max_iterations"] == 7


# ---------------------------------------------------------------------------
# 16. live.extract_cost_and_tokens
# ---------------------------------------------------------------------------


def test_extract_cost_and_tokens_flat_dict():
    from workflow.runtime.live import extract_cost_and_tokens

    cost, tin, tout = extract_cost_and_tokens({"cost_usd": 1.5, "tokens_in": 100, "tokens_out": 40})
    assert (cost, tin, tout) == (1.5, 100, 40)


def test_extract_cost_and_tokens_nested_under_result():
    from workflow.runtime.live import extract_cost_and_tokens

    cost, tin, tout = extract_cost_and_tokens({"result": {"cost_usd": 2.0, "input_tokens": 50, "output_tokens": 20}})
    assert (cost, tin, tout) == (2.0, 50, 20)


def test_extract_cost_and_tokens_nested_under_usage():
    from workflow.runtime.live import extract_cost_and_tokens

    cost, tin, tout = extract_cost_and_tokens({"usage": {"cost": 0.75, "prompt_tokens": 12, "completion_tokens": 6}})
    assert (cost, tin, tout) == (0.75, 12, 6)


def test_extract_cost_and_tokens_json_string():
    from workflow.runtime.live import extract_cost_and_tokens

    payload = json.dumps({"total_cost": 3.25, "tokens_in": 5, "tokens_out": 2})
    cost, tin, tout = extract_cost_and_tokens(payload)
    assert (cost, tin, tout) == (3.25, 5, 2)


@pytest.mark.parametrize("garbage", [None, 42, [1, 2, 3], "not json {{{", object()])
def test_extract_cost_and_tokens_garbage_never_raises(garbage):
    from workflow.runtime.live import extract_cost_and_tokens

    assert extract_cost_and_tokens(garbage) == (0.0, 0, 0)


def test_extract_cost_and_tokens_real_delegate_tool_tokens_shape():
    """The actual shape `tools/delegate_tool.py` produces:
    {"tokens": {"input": N, "output": M}} -- bare "input"/"output" keys
    under a "tokens" container, not "*_tokens"."""
    from workflow.runtime.live import extract_cost_and_tokens

    cost, tin, tout = extract_cost_and_tokens({"tokens": {"input": 77, "output": 33}})
    assert tin == 77
    assert tout == 33
    assert cost == 0.0  # no cost key present anywhere in this shape


# ---------------------------------------------------------------------------
# 17. Notify presets
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_send(monkeypatch):
    sent = []

    def fake_send_message_tool(args):
        sent.append(args)
        return json.dumps({"ok": True})

    import tools.send_message_tool as smt

    monkeypatch.setattr(smt, "send_message_tool", fake_send_message_tool)
    return sent


def test_preset_gates_and_failures_resolves_expected_statuses(_stub_send):
    from workflow.runtime import notify as notify_mod
    from workflow.runtime.notify import Notification

    for st, should_deliver in [("awaiting_gate", True), ("failed", True), ("partial", True), ("succeeded", False)]:
        n = Notification(run_id="r1", workflow_id="w1", status=st)
        result = notify_mod.notify(n, workflow_notify={"preset": "gates_and_failures"}, config={"notify_target": "t"})
        assert result["delivered"] is should_deliver, (st, result)
        if should_deliver:
            assert result.get("preset") == "gates_and_failures"


def test_explicit_on_beats_preset(_stub_send):
    from workflow.runtime import notify as notify_mod
    from workflow.runtime.notify import Notification

    n = Notification(run_id="r2", workflow_id="w1", status="succeeded")
    result = notify_mod.notify(
        n, workflow_notify={"on": ["succeeded"], "preset": "failures"}, config={"notify_target": "t"}
    )
    assert result["delivered"] is True  # explicit `on` wins even though "failures" preset excludes succeeded
    assert "preset" not in result


def test_unknown_preset_falls_back_to_config_notify_on_not_everything_or_nothing(_stub_send):
    from workflow.runtime import notify as notify_mod
    from workflow.runtime.notify import Notification

    n = Notification(run_id="r3", workflow_id="w1", status="succeeded")
    result = notify_mod.notify(
        n, workflow_notify={"preset": "does_not_exist"}, config={"notify_target": "t", "notify_on": ["succeeded"]}
    )
    assert result["delivered"] is True
    assert result.get("preset_unknown") == "does_not_exist"


def test_unknown_preset_falls_back_to_builtin_default_when_no_config_notify_on(_stub_send):
    from workflow.runtime import notify as notify_mod
    from workflow.runtime.notify import Notification

    # built-in default is ("failed", "partial", "awaiting_gate") -- "succeeded" is excluded
    n = Notification(run_id="r4", workflow_id="w1", status="succeeded")
    result = notify_mod.notify(n, workflow_notify={"preset": "does_not_exist"}, config={"notify_target": "t"})
    assert result["delivered"] is False
    assert result["reason"] == "filtered"
    assert result.get("preset_unknown") == "does_not_exist"

    n2 = Notification(run_id="r4b", workflow_id="w1", status="failed")
    result2 = notify_mod.notify(n2, workflow_notify={"preset": "does_not_exist"}, config={"notify_target": "t"})
    assert result2["delivered"] is True


def test_notify_preset_names_are_exactly_the_documented_set():
    from workflow.runtime.notify import NOTIFY_PRESETS

    assert set(NOTIFY_PRESETS.keys()) == {"all", "failures", "gates", "budget", "gates_and_failures", "success"}


def test_template_renders_and_unknown_placeholder_does_not_raise(_stub_send):
    from workflow.runtime import notify as notify_mod
    from workflow.runtime.notify import Notification

    n = Notification(run_id="r5", workflow_id="w1", status="failed", failed=["a"])
    result = notify_mod.notify(
        n,
        workflow_notify={"template": "run {run_id} status {status} x={this_field_does_not_exist}"},
        config={"notify_target": "t"},
    )
    assert result["delivered"] is True
    sent_message = _stub_send[-1]["message"]
    assert "r5" in sent_message
    assert "failed" in sent_message
    assert "x=" in sent_message  # unknown placeholder renders empty, never raises/crashes


# ---------------------------------------------------------------------------
# 18. Budget-pause notification content
# ---------------------------------------------------------------------------


def test_budget_pause_notification_carries_cost_cap_and_resume_command():
    from workflow.runtime.notify import Notification

    n = Notification(
        run_id="wf_budgetnotify1",
        workflow_id="w1",
        status="paused",
        pause_reason="BUDGET",
        cost_usd=3.1415,
        max_budget_usd=2.0,
    )
    text = n.summary_text()
    assert "3.1415" in text
    assert "2.0000" in text
    assert "hermes workflow run --resume wf_budgetnotify1 --max-budget-usd" in text


def test_budget_pause_notification_fires_through_a_real_run(wf_home):
    from workflow.runtime import notify as notify_mod
    from workflow import compile_text, run

    calls = []

    def recorder(n, **kwargs):
        calls.append(n)
        return {"delivered": False, "reason": "test-recorder"}

    notify_mod.set_notifier(recorder)
    try:
        yaml_text = """
workflow: budget_notify_wf
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do a}
edges: []
triggers:
  - { kind: manual }
"""
        vir = compile_text(yaml_text, phase1_warn_overrides=True)

        class CostWorker:
            def run_node(self, node, ctx):
                return {"output": {"ok": True}, "cost_usd": 5.0}

        env = run(vir, input={}, worker=CostWorker(), max_budget_usd=1.0)
        assert env["status"] == "paused", env

        paused_calls = [n for n in calls if n.status == "paused"]
        assert len(paused_calls) == 1, calls
        notif = paused_calls[0]
        assert notif.pause_reason == "BUDGET"
        assert notif.max_budget_usd == 1.0
        assert notif.cost_usd == pytest.approx(5.0)
        assert "--max-budget-usd" in notif.summary_text()
    finally:
        notify_mod.set_notifier(None)


# ---------------------------------------------------------------------------
# 19. Kanban projection stub
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_kanban_projector():
    from workflow.runtime import kanban as kanban_mod

    yield
    kanban_mod.set_projector(None)


def test_projection_enabled_false_by_default():
    from workflow.runtime.kanban import projection_enabled

    assert projection_enabled(config={}) is False
    assert projection_enabled(config={"kanban_projection": False}) is False


def test_project_run_disabled_returns_projected_false_and_touches_nothing():
    from workflow.runtime import kanban as kanban_mod

    spy_calls = []
    kanban_mod.set_projector(lambda payload: spy_calls.append(payload) or {"projected": True})

    result = kanban_mod.project_run("r1", "w1", "succeeded", config={})
    assert result == {"projected": False, "reason": "disabled"}
    assert spy_calls == [], "the injected projector must never be called while disabled"


def test_project_run_with_injected_projector_payload_matches_expected():
    from workflow.runtime import kanban as kanban_mod

    seen = []

    def fake_projector(payload):
        seen.append(payload)
        return {"projected": True, "card_id": "c-123"}

    kanban_mod.set_projector(fake_projector)
    result = kanban_mod.project_run(
        "r1",
        "w1",
        "succeeded",
        succeeded=["a", "b"],
        failed=[],
        skipped=["c"],
        cost_usd=1.23,
        config={"kanban_projection": True, "kanban_board": "board1"},
    )
    assert result == {"projected": True, "card_id": "c-123"}
    assert len(seen) == 1
    payload = seen[0]
    assert payload["run_id"] == "r1"
    assert payload["workflow_id"] == "w1"
    assert payload["status"] == "succeeded"
    assert payload["succeeded"] == ["a", "b"]
    assert payload["failed"] == []
    assert payload["skipped"] == ["c"]
    assert payload["cost_usd"] == 1.23
    assert payload["board"] == "board1"


def test_project_run_projector_exception_never_raises():
    from workflow.runtime import kanban as kanban_mod

    def raiser(payload):
        raise RuntimeError("board unreachable")

    kanban_mod.set_projector(raiser)
    result = kanban_mod.project_run("r1", "w1", "failed", config={"kanban_projection": True})
    assert result["projected"] is False
    assert "projector_error" in result["reason"]
