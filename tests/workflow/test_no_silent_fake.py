"""Acceptance: "no silent FakeWorker" — Phase 2 acceptance checklist.

The Phase 1 bug this guards against: `_default_worker()` (or a `run()` call
with no explicit worker) silently falling back to `FakeWorker` and reporting
canned success for what was supposed to be a real live-agent run, with no
signal to the caller that no LLM was ever invoked.

Maps to the acceptance checklist:
  1. `workflow._default_worker()` picks FakeWorker only when
     HERMES_WORKFLOW_FAKE is set, LiveWorker otherwise.
  2. `workflow.run(...)` with no worker and no HERMES_WORKFLOW_FAKE does NOT
     silently produce a fake success when the live parent can't be built --
     the agent node FAILS, loudly, with the RuntimeParentError surfacing.
  3. CLI `_cmd_run`: `--fake` uses FakeWorker; without it (and without the
     env var) it constructs a LiveWorker -- and the stderr banner text
     distinguishes the two paths so a human watching the terminal can tell
     which path is live.
"""

from __future__ import annotations

import argparse
import json


from tests.workflow.conftest import make_home

ONE_AGENT_YAML = """
workflow: no_silent_fake
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "do the real thing"}
edges: []
triggers:
  - { kind: manual }
"""


# ---------------------------------------------------------------------------
# 1. _default_worker() env-var gated
# ---------------------------------------------------------------------------


def test_default_worker_is_fakeworker_when_env_set(monkeypatch):
    import workflow
    from workflow.runtime.worker import FakeWorker

    monkeypatch.setenv("HERMES_WORKFLOW_FAKE", "1")
    worker = workflow._default_worker()
    assert isinstance(worker, FakeWorker)


def test_default_worker_is_liveworker_when_env_unset(monkeypatch):
    import workflow
    from workflow.runtime.live import LiveWorker

    monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)
    worker = workflow._default_worker()
    assert isinstance(worker, LiveWorker)


# ---------------------------------------------------------------------------
# 2. workflow.run() with no worker + no FAKE env does not fake a success
# ---------------------------------------------------------------------------


def test_run_with_no_worker_and_no_fake_env_fails_loudly_not_silently(tmp_path, monkeypatch):
    """The regression test for the exact bug Phase 2 fixes: a run that can't
    build a live parent must land the agent node `failed` (with the real
    error surfacing), never `succeeded`."""
    import workflow
    import workflow.runtime.live as live_mod
    from workflow.runtime.live import RuntimeParentError

    make_home(tmp_path, monkeypatch, enabled=True, fake=False)
    monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)

    def _raise(*args, **kwargs):
        raise RuntimeParentError("no live model configured (test double)")

    monkeypatch.setattr(live_mod, "build_runtime_parent", _raise)

    vir = workflow.compile_text(ONE_AGENT_YAML, phase1_warn_overrides=True)
    env = workflow.run(vir, input={}, worker=None)

    # MUST NOT silently succeed with canned output.
    assert env["status"] != "succeeded", env
    assert "a" in env["failed"], env
    assert "a" not in env["succeeded"], env


def test_run_failure_error_message_surfaces_runtime_parent_error(tmp_path, monkeypatch):
    """The failure must carry the actual RuntimeParentError text, not a
    generic/opaque message -- an operator debugging this needs to see WHY."""
    import workflow
    import workflow.runtime.live as live_mod
    from workflow.runtime.live import RuntimeParentError
    from workflow.store import checkpoint

    make_home(tmp_path, monkeypatch, enabled=True, fake=False)
    monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)

    def _raise(*args, **kwargs):
        raise RuntimeParentError("no live model configured (test double, unique-marker-xyz)")

    monkeypatch.setattr(live_mod, "build_runtime_parent", _raise)

    vir = workflow.compile_text(ONE_AGENT_YAML, phase1_warn_overrides=True)
    env = workflow.run(vir, input={}, worker=None)

    rec = checkpoint.load_run_record(env["run_id"])
    a_nr = next(nr for nr in rec["node_runs"].values() if nr["node_id"] == "a")
    assert a_nr["status"] == "failed", a_nr
    assert "unique-marker-xyz" in a_nr["error"]["message"], a_nr["error"]


# ---------------------------------------------------------------------------
# 3. CLI --fake vs. no --fake banner + worker type
# ---------------------------------------------------------------------------


def _run_args(path, *, fake):
    return argparse.Namespace(
        path_or_id=path,
        input=None,
        resume=None,
        from_node=None,
        retry_failed=False,
        dry_run=False,
        max_budget_usd=None,
        fake=fake,
    )


def test_cli_run_with_fake_flag_uses_fakeworker_and_banner_says_so(tmp_path, monkeypatch, capsys):
    from workflow.cli import _cmd_run

    make_home(tmp_path, monkeypatch, enabled=True, fake=False)
    path = tmp_path / "wf.yaml"
    path.write_text(ONE_AGENT_YAML, encoding="utf-8")

    rc = _cmd_run(_run_args(str(path), fake=True))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "FakeWorker" in captured.err, captured.err
    env = json.loads(captured.out)
    assert env["status"] == "succeeded", env  # FakeWorker always "succeeds" canned nodes


def test_cli_run_without_fake_uses_liveworker_and_banner_says_so(tmp_path, monkeypatch, capsys):
    """Without --fake and without HERMES_WORKFLOW_FAKE, the CLI must construct
    a LiveWorker (never silently substitute FakeWorker). The live parent
    build is stubbed to fail fast so this test stays hermetic (no network)."""
    from workflow.cli import _cmd_run
    import workflow.runtime.live as live_mod
    from workflow.runtime.live import RuntimeParentError

    make_home(tmp_path, monkeypatch, enabled=True, fake=False)
    monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)

    def _raise(*args, **kwargs):
        raise RuntimeParentError("no live model configured (test double)")

    monkeypatch.setattr(live_mod, "build_runtime_parent", _raise)

    path = tmp_path / "wf.yaml"
    path.write_text(ONE_AGENT_YAML, encoding="utf-8")

    rc = _cmd_run(_run_args(str(path), fake=False))
    captured = capsys.readouterr()
    assert "LiveWorker" in captured.err, captured.err
    assert "FakeWorker" not in captured.err, captured.err
    # the run still completes the CLI call (rc==0 unless awaiting_gate) but
    # the node itself must have failed, not silently succeeded.
    env = json.loads(captured.out)
    assert env["status"] != "succeeded", env
