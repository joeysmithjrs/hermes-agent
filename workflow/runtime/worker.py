"""Worker protocol + FakeWorker + DelegateWorker (design §7).

The driver takes a ``worker`` (default FakeWorker in tests). The inherit path
(Phase 1) calls ``delegate_task(goal=<prompt>, context=<context>,
parent_agent=<...>)`` — honoring ``prompt``->``goal`` and ``context`` ONLY (F2).
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional, Protocol

from ..ir import Node

__all__ = ["Worker", "FakeWorker", "DelegateWorker"]


class Worker(Protocol):
    """A node executor. run_node returns a result dict with at least ``output``."""

    def run_node(self, node: Node, ctx: Dict[str, Any]) -> Dict[str, Any]:
        ...


def _default_fake_output(node: Node, ctx: Dict[str, Any]) -> Any:
    """Canned output by node kind/id for the FakeWorker."""
    if node.kind == "agent":
        return {"text": f"agent[{node.id}] done", "node": node.id}
    if node.kind == "script":
        from . import scripts
        if node.run and scripts.is_registered(node.run):
            return scripts.get(node.run)(ctx.get("input", {}))
        return {"script": node.id, "ok": True}
    return {"ok": True, "node": node.id}


class FakeWorker:
    """Test worker (no live LLM). Returns canned output by node id; can
    sleep/raise/record side-effect call counts (acceptance #3)."""

    def __init__(
        self,
        outputs: Optional[Dict[str, Any]] = None,
        sleep_s: float = 0.0,
        fail_nodes: Optional[Dict[str, str]] = None,
        side_effect_nodes: Optional[set] = None,
    ) -> None:
        self.outputs = outputs or {}
        self.sleep_s = sleep_s
        self.fail_nodes = fail_nodes or {}  # node_id -> error code
        self.side_effect_nodes = side_effect_nodes or set()
        self.side_effect_calls: Dict[str, int] = {}

    def run_node(self, node: Node, ctx: Dict[str, Any]) -> Dict[str, Any]:
        if node.id in self.side_effect_nodes:
            self.side_effect_calls[node.id] = self.side_effect_calls.get(node.id, 0) + 1
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if node.id in self.fail_nodes:
            raise RuntimeError(self.fail_nodes[node.id])
        if node.id in self.outputs:
            out = self.outputs[node.id]
            if callable(out):
                out = out(ctx)
            return {"output": out, "cost_usd": 0.0}
        return {"output": _default_fake_output(node, ctx), "cost_usd": 0.0}


class DelegateWorker:
    """Inherit-path worker: calls ``delegate_task`` (F2: prompt->goal, context only).

    In CLI/standalone runs there is no chat parent agent. For Phase 1 the live
    DelegateWorker requires a parent agent context passed in, OR a minimal shim.
    Documented in IMPLEMENTATION_LOG. The FakeWorker is the path exercised in
    tests and CI (no live LLM).
    """

    def __init__(self, parent_agent: Any = None, parent_shim: Optional[Callable] = None) -> None:
        self.parent_agent = parent_agent
        self.parent_shim = parent_shim

    def run_node(self, node: Node, ctx: Dict[str, Any]) -> Dict[str, Any]:
        if node.kind != "agent":
            # non-agent nodes are handled by the driver via script registry
            return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}
        if not self.parent_agent and not self.parent_shim:
            raise RuntimeError(
                "DelegateWorker requires a parent agent context (or shim) for live agent nodes; "
                "use FakeWorker for tests (HERMES_WORKFLOW_FAKE=1)."
            )
        from tools.delegate_tool import delegate_task

        goal = None
        context = None
        if node.spec:
            goal = node.spec.prompt if isinstance(node.spec.prompt, str) else json.dumps(node.spec.prompt)
            # context is the rendered input mapping (F2: context ONLY, not tools/model/profile)
            context = json.dumps(ctx.get("input", {}))
        parent = self.parent_agent or self.parent_shim
        result = delegate_task(goal=goal, context=context, role="leaf", parent_agent=parent)
        return {"output": {"delegate_result": result}, "cost_usd": 0.0}