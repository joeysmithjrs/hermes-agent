"""Regression tests for the Phase 2 adversarial-review findings.

Each test here pins a defect the review pass found AFTER the feature work
landed. They are the reason the fixes can't silently regress:

  BLOCKER 1  raw `Driver`/`driver.run()` defaulted to FakeWorker, so a caller
             below the `workflow.enabled` boundary got canned "successes"
             reported as `status: succeeded`.
  BLOCKER 2  `--dry-run` combined with `--resume` silently dropped the flag
             and performed a real, billable resume — including on the
             agent-facing `workflow_run` tool, whose schema promises
             "costs nothing".
  BLOCKER 3  `_PARENT_CACHE` was keyed on call arguments that are always
             `(None, None)` in the real path, so a long-lived process pinned
             the first-resolved model/credentials forever.
  HIGH 4     repeated `resume()` of an already-terminal run re-fired the
             terminal notification each time (external message spam).
  HIGH 5     reported `effective_provider` was the raw spec value, not the
             canonicalized provider the child actually ran on.
"""

from __future__ import annotations

import argparse
import json

import pytest

from tests.workflow.conftest import make_home

ONE_AGENT_YAML = """
workflow: p2_review_fixes
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "do the thing"}
edges: []
triggers:
  - { kind: manual }
"""


# ---------------------------------------------------------------------------
# BLOCKER 1 — the raw driver API must never default to a canned FakeWorker
# ---------------------------------------------------------------------------


def test_driver_without_worker_refuses_instead_of_faking(wf_home, monkeypatch):
    """A Driver built with no worker must NOT silently produce canned success.

    The refusal surfaces the way every other node-level error does — the node
    fails with the reason attached, rather than propagating out of execute().
    What matters for the invariant is that it is NOT reported as succeeded
    with FakeWorker's canned `agent[a] done` output.
    """
    import workflow
    from workflow.runtime.driver import Driver

    monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)
    vir = workflow.compile_text(ONE_AGENT_YAML)

    d = Driver(vir)  # construction itself is fine — the worker is lazy
    env = d.execute()

    assert env["status"] != "succeeded", env
    assert "a" in env["failed"], env
    nr = next(iter(d.state.node_runs.values()))
    assert "without a worker" in nr.error["message"], nr.error
    assert nr.output is None, "must not carry canned FakeWorker output"


def test_module_level_driver_run_without_worker_refuses(wf_home, monkeypatch):
    """`workflow.runtime.driver.run()` is public API and was the reported repro:
    it used to return `status: succeeded` carrying FakeWorker's canned text."""
    import workflow
    from workflow.runtime.driver import run as driver_run

    monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)
    vir = workflow.compile_text(ONE_AGENT_YAML)

    env = driver_run(vir)
    assert env["status"] != "succeeded", env
    assert "a" in env["failed"], env


def test_driver_without_worker_still_allows_fake_when_env_opted_in(wf_home):
    """The no-LLM path stays available — it just requires the explicit opt-in.

    `wf_home` sets HERMES_WORKFLOW_FAKE=1.
    """
    import workflow
    from workflow.runtime.driver import run as driver_run

    vir = workflow.compile_text(ONE_AGENT_YAML)
    env = driver_run(vir)
    assert env["status"] == "succeeded", env


def test_dry_run_needs_no_worker_at_all(tmp_path, monkeypatch):
    """A plan-only run must not construct (or fail on) a worker — the lazy
    `Driver.worker` property exists for exactly this."""
    import workflow
    from workflow.runtime.driver import Driver

    make_home(tmp_path, monkeypatch, enabled=True, fake=False)
    monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)
    vir = workflow.compile_text(ONE_AGENT_YAML)

    env = Driver(vir).execute(dry_run=True)
    assert env["status"] == "dry_run", env
    assert env["ready"] == ["a"], env


# ---------------------------------------------------------------------------
# BLOCKER 2 — --dry-run + --resume must never silently execute live
# ---------------------------------------------------------------------------


def test_cli_refuses_dry_run_with_resume(wf_home, capsys):
    """Silently dropping --dry-run here would turn a requested free preview
    into a real billable resume."""
    from workflow.cli import _cmd_run, EXIT_USAGE

    rc = _cmd_run(
        argparse.Namespace(
            path_or_id="ignored",
            input=None,
            resume="wf_doesnotexist",
            from_node=None,
            retry_failed=False,
            dry_run=True,
            max_budget_usd=None,
            fake=False,
        )
    )
    captured = capsys.readouterr()
    assert rc == EXIT_USAGE, captured.err
    assert "--dry-run cannot be combined with --resume" in captured.err
    # and nothing was executed: no banner about picking a worker
    assert "LiveWorker" not in captured.err


def test_workflow_run_tool_refuses_dry_run_with_resume(wf_home):
    """The agent-facing surface matters most: its schema promises dry_run
    'costs nothing', so it must not quietly run a real resume."""
    import importlib

    import tools.workflow_tools as wt

    importlib.reload(wt)
    # enable both gates for this temp home
    (wf_home / "config.yaml").write_text(
        "workflow:\n  enabled: true\n  tool_enabled: true\n", encoding="utf-8"
    )
    out = json.loads(wt.workflow_run(resume="wf_whatever", dry_run=True))
    assert "error" in out
    assert "dry_run is not supported with resume" in out["error"]


# ---------------------------------------------------------------------------
# BLOCKER 3 — parent cache must key on the RESOLVED identity
# ---------------------------------------------------------------------------


class _StubAgent:
    def __init__(self, **kwargs):
        self.model = kwargs.get("model")
        self.provider = kwargs.get("provider")
        self.kwargs = kwargs

    def close(self):
        pass


@pytest.fixture
def clear_parent_cache():
    from workflow.runtime import live as live_mod

    live_mod._PARENT_CACHE.clear()
    yield
    live_mod._PARENT_CACHE.clear()


def test_parent_cache_does_not_pin_a_stale_model(monkeypatch, clear_parent_cache):
    """Editing the configured model must produce a NEW parent, not silently
    reuse the first one resolved in this process."""
    from workflow.runtime import live as live_mod

    cfg = {"model": {"default": "model-A"}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {
            "provider": "openrouter",
            "base_url": "https://example.invalid",
            "api_key": "key-1",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr("run_agent.AIAgent", _StubAgent)

    p1 = live_mod.build_runtime_parent()
    assert p1.model == "model-A"

    # operator edits config.yaml between runs
    cfg["model"]["default"] = "model-B"
    p2 = live_mod.build_runtime_parent()
    assert p2.model == "model-B", "stale parent reused after a config change"
    assert p1 is not p2


def test_parent_cache_does_not_pin_a_rotated_credential(monkeypatch, clear_parent_cache):
    """A rotated API key must miss the cache rather than keep using the old one."""
    from workflow.runtime import live as live_mod

    key = {"v": "key-1"}
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda *a, **k: {"model": {"default": "m"}}
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {
            "provider": "openrouter",
            "base_url": "https://example.invalid",
            "api_key": key["v"],
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr("run_agent.AIAgent", _StubAgent)

    p1 = live_mod.build_runtime_parent()
    key["v"] = "key-2"
    p2 = live_mod.build_runtime_parent()
    assert p1 is not p2
    assert p2.kwargs["api_key"] == "key-2"


def test_parent_cache_still_reuses_on_identical_identity(monkeypatch, clear_parent_cache):
    """The cache must still do its job when nothing changed (one AIAgent per
    identity, not one per node)."""
    from workflow.runtime import live as live_mod

    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda *a, **k: {"model": {"default": "m"}}
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {
            "provider": "openrouter",
            "base_url": "https://example.invalid",
            "api_key": "same",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr("run_agent.AIAgent", _StubAgent)

    assert live_mod.build_runtime_parent() is live_mod.build_runtime_parent()


def test_close_runtime_parents_drops_the_cache(monkeypatch, clear_parent_cache):
    from workflow.runtime import live as live_mod

    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda *a, **k: {"model": {"default": "m"}}
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {"provider": "p", "base_url": "u", "api_key": "k", "api_mode": "cc"},
    )
    monkeypatch.setattr("run_agent.AIAgent", _StubAgent)

    live_mod.build_runtime_parent()
    assert live_mod.close_runtime_parents() == 1
    assert live_mod._PARENT_CACHE == {}


# ---------------------------------------------------------------------------
# HIGH 5 — effective_provider must be the canonicalized provider
# ---------------------------------------------------------------------------


def test_effective_provider_reports_canonical_not_raw_spec_value(monkeypatch):
    """`spec.provider: ollama` actually runs on provider 'custom'; the
    checkpoint/events/notify must say 'custom', not 'ollama'."""
    from workflow.ir import Node, NodeSpec
    from workflow.runtime.live import LiveWorker

    class _Parent:
        model = "parent-model"
        provider = "parent-provider"

    recorded = {}

    def _builder(**kwargs):
        recorded.update(kwargs)
        return object()

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {
            "provider": "custom",  # canonicalized from the "ollama" alias
            "base_url": "http://localhost:11434",
            "api_key": "x",
            "api_mode": "chat_completions",
        },
    )

    node = Node(id="a", kind="agent", spec=NodeSpec(prompt="hi", provider="ollama"))
    worker = LiveWorker(
        parent_agent=_Parent(),
        child_builder=_builder,
        child_runner=lambda *a, **k: {"summary": "ok", "cost_usd": 0.0},
    )
    result = worker.run_node(node, {"input": {}})

    assert result["effective_provider"] == "custom", result
    assert result["output"]["effective_provider"] == "custom", result
    # and it matches what the child was actually built with
    assert recorded["override_provider"] == "custom"


# ---------------------------------------------------------------------------
# HIGH 4 — no duplicate terminal notifications across no-op resumes
# ---------------------------------------------------------------------------


TWO_NODE_YAML = """
workflow: p2_notify_dedup
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "one"}
  - id: b
    kind: agent
    spec: {prompt: "two"}
edges:
  - { from: a, to: b }
triggers:
  - { kind: manual }
"""


def test_resume_of_terminal_run_does_not_renotify(wf_home):
    """Re-running `--resume` on an already-finished run must not spam the
    notification channel once per attempt."""
    import workflow
    from workflow.runtime import notify as notify_mod
    from workflow.runtime.worker import FakeWorker

    fired = []

    def _recording(n, **kwargs):
        fired.append(n.status)
        return {"delivered": True}

    notify_mod.set_notifier(_recording)
    try:
        vir = workflow.compile_text(TWO_NODE_YAML)
        env = workflow.run(vir, worker=FakeWorker())
        assert env["status"] == "succeeded", env
        assert fired == ["succeeded"], fired

        workflow.resume(env["run_id"], worker=FakeWorker())
        workflow.resume(env["run_id"], worker=FakeWorker())
        assert fired == ["succeeded"], f"duplicate notifications fired: {fired}"
    finally:
        notify_mod.set_notifier(notify_mod.notify)


def test_notify_fires_again_when_a_resume_makes_real_progress(wf_home):
    """The de-dup must not swallow a genuine second transition: a failed run
    that is retried into success has to notify again."""
    import workflow
    from workflow.runtime import notify as notify_mod
    from workflow.runtime.worker import FakeWorker

    fired = []

    def _recording(n, **kwargs):
        fired.append(n.status)
        return {"delivered": True}

    notify_mod.set_notifier(_recording)
    try:
        vir = workflow.compile_text(TWO_NODE_YAML)
        env = workflow.run(vir, worker=FakeWorker(fail_nodes={"a": "boom"}))
        assert env["status"] == "failed", env
        assert fired == ["failed"], fired

        env2 = workflow.resume(env["run_id"], worker=FakeWorker(), retry_failed=True)
        assert env2["status"] == "succeeded", env2
        assert fired == ["failed", "succeeded"], fired
    finally:
        notify_mod.set_notifier(notify_mod.notify)
