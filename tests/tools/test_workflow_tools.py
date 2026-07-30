"""tools/workflow_tools.py — the double-gated `workflow` toolset surface.

Maps to the Phase 2 acceptance checklist ("tools/workflow_tools.py —
workflow_run/workflow_status, gated by check_workflow_tool_requirements()"):
  1. `check_workflow_tool_requirements()` is False by default, False with
     only `enabled`, False with only `tool_enabled`, True with both.
  2. `workflow_run`/`workflow_status` return the disabled error JSON when
     the gate is off.
  3. With both enabled (temp HERMES_HOME + HERMES_WORKFLOW_FAKE),
     `workflow_run(path=..., dry_run=True)` returns a plan without spawning;
     `workflow_status(action="get", run_id=...)` returns the envelope.
  4. The registry has both tools under toolset "workflow", and neither
     appears in `get_tool_definitions()`-equivalent filtering
     (`registry.get_definitions()`) when the gate is off.
  5. There is NO gate-decision tool exposed (`workflow_gate`/`workflow_decide`
     are not registered) -- an agent must not be able to approve its own
     gates.

Hermetic: every test builds its own temp HERMES_HOME; no network, no real
config.yaml, no real LLM.
"""

from __future__ import annotations

import json
from pathlib import Path


# Importing this module registers workflow_run/workflow_status as a side
# effect (tools/registry.py auto-discovery), same as the real tool loader.
import tools.workflow_tools as workflow_tools

ONE_AGENT_YAML = """
workflow: tool_surface_check
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "do the thing"}
edges: []
triggers:
  - { kind: manual }
"""


def _make_home(tmp_path, monkeypatch, *, enabled: bool, tool_enabled: bool, fake: bool = False) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "workflow:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        f"  tool_enabled: {'true' if tool_enabled else 'false'}\n"
        "  max_budget_usd: 10.0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    if fake:
        monkeypatch.setenv("HERMES_WORKFLOW_FAKE", "1")
    else:
        monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)
    return home


# ---------------------------------------------------------------------------
# 1. check_workflow_tool_requirements() gate combinations
# ---------------------------------------------------------------------------


def test_gate_false_by_default_no_config(tmp_path, monkeypatch):
    home = tmp_path / "home_empty"
    home.mkdir()
    (home / "config.yaml").write_text("workflow:\n  max_budget_usd: 10.0\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)

    assert workflow_tools.check_workflow_tool_requirements() is False


def test_gate_false_with_only_enabled(tmp_path, monkeypatch):
    _make_home(tmp_path, monkeypatch, enabled=True, tool_enabled=False)
    assert workflow_tools.check_workflow_tool_requirements() is False


def test_gate_false_with_only_tool_enabled(tmp_path, monkeypatch):
    _make_home(tmp_path, monkeypatch, enabled=False, tool_enabled=True)
    assert workflow_tools.check_workflow_tool_requirements() is False


def test_gate_true_with_both(tmp_path, monkeypatch):
    _make_home(tmp_path, monkeypatch, enabled=True, tool_enabled=True)
    assert workflow_tools.check_workflow_tool_requirements() is True


# ---------------------------------------------------------------------------
# 2. tool calls return the disabled error JSON when the gate is off
# ---------------------------------------------------------------------------


def test_workflow_run_returns_disabled_error_when_gate_off(tmp_path, monkeypatch):
    _make_home(tmp_path, monkeypatch, enabled=True, tool_enabled=False)

    raw = workflow_tools.workflow_run(path="whatever.yaml")
    result = json.loads(raw)
    assert "error" in result, result
    assert "disabled" in result["error"].lower(), result


def test_workflow_status_returns_disabled_error_when_gate_off(tmp_path, monkeypatch):
    _make_home(tmp_path, monkeypatch, enabled=False, tool_enabled=True)

    raw = workflow_tools.workflow_status(action="get", run_id="wf_doesnotmatter")
    result = json.loads(raw)
    assert "error" in result, result
    assert "disabled" in result["error"].lower(), result


# ---------------------------------------------------------------------------
# 3. both enabled: dry_run plans without spawning; status reads the envelope
# ---------------------------------------------------------------------------


def test_workflow_run_dry_run_and_status_roundtrip(tmp_path, monkeypatch):
    _make_home(tmp_path, monkeypatch, enabled=True, tool_enabled=True, fake=True)
    wf_path = tmp_path / "wf.yaml"
    wf_path.write_text(ONE_AGENT_YAML, encoding="utf-8")

    dry_raw = workflow_tools.workflow_run(path=str(wf_path), dry_run=True)
    dry_env = json.loads(dry_raw)
    assert dry_env.get("status") == "dry_run", dry_env
    assert "a" in dry_env.get("ready", []), dry_env

    # a dry run must not have spawned anything -- no node output on disk.
    from workflow.store import fs

    nodes_dir = fs.run_dir(dry_env["run_id"]) / "nodes"
    if nodes_dir.exists():
        assert not any(nodes_dir.iterdir()), "dry_run must not spawn/write node output"

    # a real (FakeWorker, hermetic) run, then read it back via workflow_status
    run_raw = workflow_tools.workflow_run(path=str(wf_path))
    run_env = json.loads(run_raw)
    assert run_env.get("status") == "succeeded", run_env
    run_id = run_env["run_id"]

    status_raw = workflow_tools.workflow_status(action="get", run_id=run_id)
    status_env = json.loads(status_raw)
    assert status_env["run_id"] == run_id
    assert status_env["status"] == "succeeded", status_env


def test_workflow_status_list_action(tmp_path, monkeypatch):
    _make_home(tmp_path, monkeypatch, enabled=True, tool_enabled=True, fake=True)
    wf_path = tmp_path / "wf.yaml"
    wf_path.write_text(ONE_AGENT_YAML, encoding="utf-8")
    workflow_tools.workflow_run(path=str(wf_path))

    raw = workflow_tools.workflow_status(action="list")
    result = json.loads(raw)
    assert "runs" in result, result
    assert len(result["runs"]) >= 1, result


# ---------------------------------------------------------------------------
# 4. registry wiring: both tools under toolset "workflow"; hidden when gate off
# ---------------------------------------------------------------------------


def test_registry_entries_are_toolset_workflow():
    from tools.registry import registry

    run_entry = registry.get_entry("workflow_run")
    status_entry = registry.get_entry("workflow_status")
    assert run_entry is not None, "workflow_run must be registered"
    assert status_entry is not None, "workflow_status must be registered"
    assert run_entry.toolset == "workflow", run_entry.toolset
    assert status_entry.toolset == "workflow", status_entry.toolset


def test_tools_absent_from_definitions_when_gate_off(tmp_path, monkeypatch):
    from tools.registry import registry, invalidate_check_fn_cache

    _make_home(tmp_path, monkeypatch, enabled=False, tool_enabled=False)
    invalidate_check_fn_cache()

    defs = registry.get_definitions({"workflow_run", "workflow_status"})
    assert defs == [], defs


def test_tools_present_in_definitions_when_gate_on(tmp_path, monkeypatch):
    from tools.registry import registry, invalidate_check_fn_cache

    _make_home(tmp_path, monkeypatch, enabled=True, tool_enabled=True)
    invalidate_check_fn_cache()

    defs = registry.get_definitions({"workflow_run", "workflow_status"})
    names = {d["function"]["name"] for d in defs}
    assert names == {"workflow_run", "workflow_status"}, names


# ---------------------------------------------------------------------------
# 5. no gate-decision tool is exposed
# ---------------------------------------------------------------------------


def test_no_gate_decision_tool_registered():
    from tools.registry import registry

    for name in ("workflow_gate", "workflow_decide", "workflow_gate_decide", "workflow_approve"):
        assert registry.get_entry(name) is None, f"{name} must NOT be a registered tool"
