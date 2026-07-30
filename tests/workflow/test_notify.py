"""Notification hooks — Phase 2 acceptance checklist §"notify hooks with
mocks".

Maps to the acceptance checklist:
  1. `set_notifier()` with a recording fake: fires on terminal `succeeded`,
     on `failed`/`partial`, and on entering `awaiting_gate` (with `gate_id`
     set) -- and does NOT ALSO fire a terminal notification for the same run
     that parked at a gate (single notification per park, not a double).
  2. `notify()` status filtering: a status not in `notify_on` is filtered,
     no delivery attempted.
  3. `notify()` with no target configured -> delivered False, no raise.
  4. A notifier that raises must not break the run -- the run still
     completes with its correct status.
  5. Delivery path: `tools.send_message_tool.send_message_tool` is called
     with the configured target and a message containing the run id and
     status.
"""

from __future__ import annotations

import json

import pytest

from workflow.runtime import notify as notify_mod
from workflow.runtime.notify import Notification


@pytest.fixture(autouse=True)
def _reset_notifier():
    """set_notifier() is a module-global override; always restore the real
    notifier after each test so a failure/early-return can't leak a stub
    notifier into a sibling test file."""
    yield
    notify_mod.set_notifier(None)


class _RecordingNotifier:
    def __init__(self):
        self.calls = []

    def __call__(self, n, *, workflow_notify=None, gate_notify=None, config=None):
        self.calls.append(n)
        return {"delivered": False, "reason": "test-recorder"}


# ---------------------------------------------------------------------------
# 1. set_notifier fires on the right statuses, exactly once per occurrence
# ---------------------------------------------------------------------------

ONE_AGENT_YAML = """
workflow: notify_one
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
edges: []
triggers:
  - { kind: manual }
"""

TWO_ENTRY_YAML = """
workflow: notify_partial
version: 1
nodes:
  - id: p1
    kind: agent
    spec: {prompt: do p1}
  - id: p2
    kind: agent
    spec: {prompt: do p2}
edges: []
triggers:
  - { kind: manual }
"""

GATE_YAML = """
workflow: notify_gate
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
  - id: g
    kind: gate
  - id: b
    kind: agent
    spec: {prompt: do b}
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


def test_fires_on_succeeded(wf_home):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    recorder = _RecordingNotifier()
    notify_mod.set_notifier(recorder)

    vir = compile_text(ONE_AGENT_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    assert env["status"] == "succeeded", env

    statuses = [n.status for n in recorder.calls]
    assert "succeeded" in statuses, statuses
    assert all(n.run_id == env["run_id"] for n in recorder.calls)


def test_fires_on_failed(wf_home):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    recorder = _RecordingNotifier()
    notify_mod.set_notifier(recorder)

    vir = compile_text(ONE_AGENT_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(fail_nodes={"a": "boom"}))
    assert env["status"] == "failed", env

    statuses = [n.status for n in recorder.calls]
    assert "failed" in statuses, statuses


def test_fires_on_partial(wf_home):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    recorder = _RecordingNotifier()
    notify_mod.set_notifier(recorder)

    vir = compile_text(TWO_ENTRY_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker(fail_nodes={"p1": "boom"}))
    assert env["status"] == "partial", env

    statuses = [n.status for n in recorder.calls]
    assert "partial" in statuses, statuses


def test_fires_on_awaiting_gate_with_gate_id_and_not_also_terminal(wf_home):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    recorder = _RecordingNotifier()
    notify_mod.set_notifier(recorder)

    vir = compile_text(GATE_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    assert env["status"] == "awaiting_gate", env

    gate_calls = [n for n in recorder.calls if n.status == "awaiting_gate"]
    assert len(gate_calls) == 1, recorder.calls
    assert gate_calls[0].gate_id == "g", gate_calls[0]

    # a gate-parked run must NOT also fire a terminal notification (would be
    # a spurious "succeeded"/"failed" double-notify for the same occurrence)
    terminal_calls = [n for n in recorder.calls if n.status in ("succeeded", "failed", "partial", "paused")]
    assert terminal_calls == [], terminal_calls
    assert len(recorder.calls) == 1, recorder.calls


# ---------------------------------------------------------------------------
# 2. status filtering
# ---------------------------------------------------------------------------


def test_notify_filters_status_not_in_notify_on():
    n = Notification(run_id="wf_filter", workflow_id="wf1", status="succeeded")
    # explicit config with the default notify_on (no "succeeded" in it)
    result = notify_mod.notify(n, config={"notify_on": ["failed", "partial", "awaiting_gate"]})
    assert result == {"delivered": False, "reason": "filtered"}


def test_notify_default_status_filter_excludes_succeeded():
    """No config at all -> the built-in default filter (failed/partial/
    awaiting_gate) still excludes a bare terminal succeeded."""
    n = Notification(run_id="wf_filter2", workflow_id="wf1", status="succeeded")
    result = notify_mod.notify(n, config={})
    assert result == {"delivered": False, "reason": "filtered"}


# ---------------------------------------------------------------------------
# 3. no target configured
# ---------------------------------------------------------------------------


def test_notify_no_target_configured_does_not_raise():
    n = Notification(run_id="wf_notarget", workflow_id="wf1", status="failed", failed=["a"])
    result = notify_mod.notify(n, config={})  # in filter, but no notify_target
    assert result == {"delivered": False, "reason": "no_target"}


# ---------------------------------------------------------------------------
# 4. a raising notifier must not break the run
# ---------------------------------------------------------------------------


def test_raising_notifier_does_not_break_run(wf_home):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    def _raiser(n, **kwargs):
        raise RuntimeError("notifier exploded")

    notify_mod.set_notifier(_raiser)

    vir = compile_text(ONE_AGENT_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())

    assert env["status"] == "succeeded", env
    assert env["succeeded"] == ["a"], env


def test_raising_notifier_does_not_break_gate_park(wf_home):
    from workflow import compile_text, run
    from workflow.runtime.worker import FakeWorker

    def _raiser(n, **kwargs):
        raise RuntimeError("notifier exploded on gate park")

    notify_mod.set_notifier(_raiser)

    vir = compile_text(GATE_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())

    assert env["status"] == "awaiting_gate", env
    assert env["awaiting_gate"] == "g", env


# ---------------------------------------------------------------------------
# 5. delivery path via tools.send_message_tool.send_message_tool
# ---------------------------------------------------------------------------


def test_notify_delivery_calls_send_message_tool(monkeypatch):
    calls = []

    def fake_send_message_tool(args):
        calls.append(args)
        return json.dumps({"ok": True})

    import tools.send_message_tool as smt

    monkeypatch.setattr(smt, "send_message_tool", fake_send_message_tool)

    n = Notification(
        run_id="wf_deliver123",
        workflow_id="wf1",
        status="failed",
        failed=["a"],
        cost_usd=1.5,
    )
    result = notify_mod.notify(n, config={"notify_target": "slack:#ops"})

    assert result["delivered"] is True, result
    assert result["target"] == "slack:#ops"
    assert len(calls) == 1, calls
    sent = calls[0]
    assert sent["target"] == "slack:#ops"
    assert sent["action"] == "send"
    assert "wf_deliver123" in sent["message"]
    assert "failed" in sent["message"]


def test_notify_delivery_error_from_send_message_tool_is_not_delivered(monkeypatch):
    def fake_send_message_tool(args):
        return json.dumps({"error": "platform unreachable"})

    import tools.send_message_tool as smt

    monkeypatch.setattr(smt, "send_message_tool", fake_send_message_tool)

    n = Notification(run_id="wf_deliverfail", workflow_id="wf1", status="failed", failed=["a"])
    result = notify_mod.notify(n, config={"notify_target": "slack:#ops"})

    assert result["delivered"] is False, result
    assert "platform unreachable" in result["reason"]


def test_notify_delivery_exception_from_send_message_tool_does_not_raise(monkeypatch):
    def fake_send_message_tool(args):
        raise RuntimeError("network exploded")

    import tools.send_message_tool as smt

    monkeypatch.setattr(smt, "send_message_tool", fake_send_message_tool)

    n = Notification(run_id="wf_deliverraise", workflow_id="wf1", status="failed", failed=["a"])
    result = notify_mod.notify(n, config={"notify_target": "slack:#ops"})  # must not raise

    assert result["delivered"] is False, result
