"""Config loading for the workflow package (api §10).

Reads the ``workflow:`` section from Hermes config via the canonical
``hermes_cli.config.load_config()`` path (do NOT yaml.safe_load config.yaml
directly — guard test: tests/hermes_cli/test_config_read_guard.py).

Defaults:
  enabled: false, max_budget_usd: 10.0, max_parallel_nodes: 4,
  default_node_timeout_s: 1800, phase1_warn_overrides: false.
Does NOT add new required HERMES_* env vars for behavior. MAY use HERMES_HOME
(existing) and optional HERMES_WORKFLOW_FAKE=1 (documented).
"""

from __future__ import annotations

from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "tool_enabled": False,
    "max_budget_usd": 10.0,
    "max_parallel_nodes": 4,  # Reserved no-op in Phase 1; concurrency is Phase 2.
    "default_node_timeout_s": 1800,
    "store_dir": None,
    "allow_network_scripts": False,
    "phase1_warn_overrides": False,
}

__all__ = ["load_workflow_config", "DEFAULTS"]


def load_workflow_config() -> Dict[str, Any]:
    """Load the workflow config section from Hermes config (merged w/ defaults)."""
    cfg = dict(DEFAULTS)
    hermes_cfg: Dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config

        hermes_cfg = load_config() or {}
    except Exception:
        # If config subsystem is unavailable (unit hermetic edge cases), defaults only.
        hermes_cfg = {}
    wf = hermes_cfg.get("workflow") or {}
    if isinstance(wf, dict):
        for k, v in wf.items():
            cfg[k] = v
    return cfg
