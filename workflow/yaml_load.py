"""YAML -> WorkflowIR (design §3.2). Uses PyYAML (already a Hermes dep)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from .ir import Edge, Gate, Node, NodeSpec, Trigger, WorkflowIR

__all__ = ["load_yaml", "load_yaml_file"]


def load_yaml_file(path: str) -> WorkflowIR:
    """Load a YAML workflow file into a WorkflowIR (unverified)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return load_yaml(text)


def load_yaml(text: str) -> WorkflowIR:
    """Parse YAML text into a WorkflowIR (unverified)."""
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError("workflow yaml root must be a mapping")
    return _from_yaml_mapping(raw)


def _from_yaml_mapping(raw: Dict[str, Any]) -> WorkflowIR:
    # The example minimal_workflow.yaml uses top-level key `workflow:` for the id.
    wf_id = raw.get("id") or raw.get("workflow")
    if not wf_id:
        raise ValueError("workflow yaml must define `id` or top-level `workflow:` name")

    defaults = dict(raw.get("defaults") or {})
    nodes = [_node_from_yaml(n) for n in (raw.get("nodes") or [])]
    edges = [Edge.from_dict(e) for e in (raw.get("edges") or [])]

    gates: Dict[str, Gate] = {}
    for gid, gd in (raw.get("gates") or {}).items():
        gd = dict(gd)
        gd.setdefault("id", gid)
        gates[gid] = Gate.from_dict(gd)

    triggers = [_trigger_from_yaml(t) for t in (raw.get("triggers") or [])]

    return WorkflowIR(
        id=str(wf_id),
        version=int(raw.get("version", 1)),
        name=raw.get("name"),
        defaults=defaults,
        nodes=nodes,
        edges=edges,
        triggers=triggers,
        gates=gates,
        max_budget_usd=raw.get("max_budget_usd"),
        notify=raw.get("notify"),
        # Top-level `workspace: <name>` pins the whole workflow to a named
        # persistent workspace (workflow.store.workspace). Note this is the
        # WORKFLOW-level key; the per-node `spec.workspace` mapping is a
        # different, still-unenforced field (ir.OVERRIDE_ONLY_FIELDS).
        workspace=raw.get("workspace"),
    )


def _node_from_yaml(d: Dict[str, Any]) -> Node:
    kind = d.get("kind", "agent")
    spec = None
    if "spec" in d and d["spec"] is not None:
        spec = NodeSpec.from_dict(d["spec"])
    # YAML shorthand: prompt_file: path -> spec.prompt = {"file": path}
    if "prompt_file" in d:
        spec = spec or NodeSpec()
        spec.prompt = {"file": d["prompt_file"]}
    # Hoist node-level fields that the examples (and minimal_workflow.yaml) place
    # at the node level into the spec, so the verifier/driver see them (api §2.4
    # puts these in NodeSpec, but YAML examples use the top-level shorthand).
    for key in ("prompt", "model", "provider", "tools", "deny_tools", "skills",
                "profile", "timeout_s", "max_turns", "budget_usd", "workspace",
                "input", "output"):
        if key in d and d[key] is not None:
            spec = spec or NodeSpec()
            cur = getattr(spec, key, None)
            if cur is None:
                setattr(spec, key, d[key])
    return Node(
        id=d["id"],
        kind=kind,
        spec=spec,
        run=d.get("run"),
        over=d.get("over"),
        max_branches=d.get("max_branches"),
        from_=list(d.get("from") or []) or None,
        reduce=d.get("reduce"),
        branch=d.get("branch"),
        ports=list(d.get("ports") or []),
        attempts=d.get("attempts"),
        idempotent=d.get("idempotent"),
        side_effects=d.get("side_effects"),
        on_fail=d.get("on_fail"),
    )


def _trigger_from_yaml(d: Dict[str, Any]) -> Trigger:
    # YAML forms: { cron: { schedule: ... } } | { webhook: { name: ... } } | { kind: manual }
    if "cron" in d:
        c = d["cron"] or {}
        return Trigger(kind="cron", schedule=c.get("schedule"), input=dict(c.get("input") or {}))
    if "webhook" in d:
        w = d["webhook"] or {}
        return Trigger(
            kind="webhook",
            name=w.get("name"),
            events=list(w.get("events") or []),
            input=dict(w.get("input") or {}),
        )
    return Trigger.from_dict(d)