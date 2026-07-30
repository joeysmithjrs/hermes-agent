"""Shared hermetic fixtures for the workflow test suite.

Every test uses a fresh temp HERMES_HOME (no real config, no network). The
`wf_home` fixture is opt-in (NOT autouse) so it never interferes with
`test_smoke.py`'s own autouse fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def wf_home(tmp_path, monkeypatch):
    """Fresh HERMES_HOME with workflow.enabled=true + FakeWorker env.

    Returns the home Path. Tests that need a different config (e.g. disabled,
    or warn-overrides) build their own home instead of requesting this.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "workflow:\n"
        "  enabled: true\n"
        "  max_budget_usd: 10.0\n"
        "  max_parallel_nodes: 4\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Force the FakeWorker path for any CLI/lib run path (no live LLM).
    monkeypatch.setenv("HERMES_WORKFLOW_FAKE", "1")
    return home


def make_home(tmp_path, monkeypatch, *, enabled: bool, warn_overrides: bool = False, fake: bool = False) -> Path:
    """Helper to build an arbitrary hermetic HERMES_HOME for a test."""
    home = tmp_path / ("home_enabled" if enabled else "home_disabled")
    home.mkdir()
    lines = ["workflow:"]
    lines.append(f"  enabled: {'true' if enabled else 'false'}")
    if warn_overrides:
        lines.append("  phase1_warn_overrides: true")
    lines.append("  max_budget_usd: 10.0")
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    if fake:
        monkeypatch.setenv("HERMES_WORKFLOW_FAKE", "1")
    else:
        monkeypatch.delenv("HERMES_WORKFLOW_FAKE", raising=False)
    return home
