"""Workflow intermediate representation — dataclasses + JSON ser/des.

The IR is the single contract between authoring (YAML/DSL), the verifier, and
the deterministic driver. Everything serializes to JSON so YAML/DSL/checkpoints
share one schema (design §2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "NodeSpec",
    "Node",
    "Edge",
    "Gate",
    "Trigger",
    "WorkflowIR",
    "Issue",
    "VerifiedIR",
    "WorkflowRejected",
    "NODE_KINDS",
    "RUN_STATUSES",
    "NODE_STATUSES",
    "OVERRIDE_ONLY_FIELDS",
    "PHASE2_OVERRIDE_FIELDS",
]

# design §2.2 — only justified kinds
NODE_KINDS = (
    "agent",
    "script",
    "fanout",
    "join",
    "gate",
    "map",
    "cron_trigger",
    "webhook_trigger",
)

# design §2.5 — run-level status enum (F7: includes awaiting_gate + paused)
RUN_STATUSES = (
    "pending",
    "running",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
    "awaiting_gate",
    "paused",
)

# design §2.5 — node-level status enum
NODE_STATUSES = (
    "pending",
    "ready",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
    "awaiting_gate",
)

# F2 — override-only fields the Phase 1 inherit path cannot honor
OVERRIDE_ONLY_FIELDS = ("tools", "profile", "max_turns", "workspace")

# Phase 2: model/provider ARE now honored via the live worker's child-
# construction path (workflow.runtime.live.LiveWorker -> resolve_effective_model
# + tools.delegate_tool.build_child_agent override_* kwargs) — see verify.py's
# _check_node, which no longer rejects/warns on these two fields. They are
# deliberately NOT part of OVERRIDE_ONLY_FIELDS above. The remaining
# OVERRIDE_ONLY_FIELDS (tools/profile/max_turns/workspace) are still not
# enforced by any execution path, so they are still rejected — we do not
# silently claim an isolation boundary we don't actually enforce.
PHASE2_OVERRIDE_FIELDS = ("model", "provider")


@dataclass
class NodeSpec:
    """Per-agent granularity block (design §6 / api §2.4)."""

    model: Optional[str] = None
    provider: Optional[str] = None
    prompt: Optional[Any] = None  # str | {"file": path}
    skills: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    deny_tools: List[str] = field(default_factory=list)
    profile: Optional[str] = None
    timeout_s: Optional[int] = None
    max_turns: Optional[int] = None
    budget_usd: Optional[float] = None
    workspace: Optional[Dict[str, Any]] = None
    input: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None  # JSON schema or {"$ref": ...}

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["NodeSpec"]:
        if d is None:
            return None
        return cls(
            model=d.get("model"),
            provider=d.get("provider"),
            prompt=d.get("prompt"),
            skills=list(d.get("skills") or []),
            tools=list(d.get("tools") or []),
            deny_tools=list(d.get("deny_tools") or []),
            profile=d.get("profile"),
            timeout_s=d.get("timeout_s"),
            max_turns=d.get("max_turns"),
            budget_usd=d.get("budget_usd"),
            workspace=d.get("workspace"),
            input=d.get("input"),
            output=d.get("output"),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # drop None-valued keys for compact storage, keep empties as-is
        return {k: v for k, v in d.items() if v not in (None, [], {}) or k in ("prompt",)}


@dataclass
class Node:
    """A workflow node (design §2.2 / api §2.2)."""

    id: str
    kind: str
    spec: Optional[NodeSpec] = None
    run: Optional[str] = None  # registered callable name for script|join reducer
    over: Optional[str] = None  # fanout/map source template
    max_branches: Optional[int] = None  # REQUIRED on fanout/map (F5)
    from_: Optional[List[str]] = None  # join upstreams (alternative to edges)
    reduce: Optional[Dict[str, Any]] = None
    branch: Optional[Dict[str, Any]] = None  # nested node template for fanout/map
    ports: List[str] = field(default_factory=list)
    attempts: Optional[int] = None
    idempotent: Optional[bool] = None
    side_effects: Optional[str] = None  # none|external (F6)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Node":
        return cls(
            id=d["id"],
            kind=d["kind"],
            spec=NodeSpec.from_dict(d.get("spec")),
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
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "kind": self.kind,
            "spec": self.spec.to_dict() if self.spec else None,
            "run": self.run,
            "over": self.over,
            "max_branches": self.max_branches,
            "from": self.from_,
            "reduce": self.reduce,
            "branch": self.branch,
            "ports": self.ports,
            "attempts": self.attempts,
            "idempotent": self.idempotent,
            "side_effects": self.side_effects,
        }
        return {k: v for k, v in d.items() if v not in (None, [], {}) or k in ("id", "kind")}


@dataclass
class Edge:
    from_: str
    to: str
    port: Optional[str] = None
    condition: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Edge":
        return cls(
            from_=d["from"],
            to=d["to"],
            port=d.get("port"),
            condition=d.get("condition"),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {"from": self.from_, "to": self.to}
        if self.port:
            d["port"] = self.port
        if self.condition:
            d["condition"] = self.condition
        return d


@dataclass
class Gate:
    id: str
    channel: str = ""
    approvers: List[str] = field(default_factory=list)
    timeout_s: Optional[int] = None
    on_timeout: str = "shelve"  # shelve|block|approve_auto (F8)
    dual_control: bool = True
    notify: bool = True

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Gate":
        return cls(
            id=d["id"],
            channel=d.get("channel", ""),
            approvers=list(d.get("approvers") or []),
            timeout_s=d.get("timeout_s"),
            on_timeout=d.get("on_timeout", "shelve"),
            dual_control=d.get("dual_control", True),
            notify=d.get("notify", True),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "approvers": self.approvers,
            "timeout_s": self.timeout_s,
            "on_timeout": self.on_timeout,
            "dual_control": self.dual_control,
            "notify": self.notify,
        }


@dataclass
class Trigger:
    kind: str  # cron|webhook|manual
    schedule: Optional[str] = None
    name: Optional[str] = None
    events: List[str] = field(default_factory=list)
    input: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Trigger":
        return cls(
            kind=d.get("kind", ""),
            schedule=d.get("schedule"),
            name=d.get("name"),
            events=list(d.get("events") or []),
            input=dict(d.get("input") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {"kind": self.kind}
        if self.schedule:
            d["schedule"] = self.schedule
        if self.name:
            d["name"] = self.name
        if self.events:
            d["events"] = self.events
        if self.input:
            d["input"] = self.input
        return d


@dataclass
class WorkflowIR:
    id: str
    version: int = 1
    name: Optional[str] = None
    hash: Optional[str] = None
    defaults: Dict[str, Any] = field(default_factory=dict)
    nodes: List[Node] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)
    triggers: List[Trigger] = field(default_factory=list)
    gates: Dict[str, Gate] = field(default_factory=dict)
    max_budget_usd: Optional[float] = None
    notify: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WorkflowIR":
        gates = {}
        for gid, gd in (d.get("gates") or {}).items():
            gd = dict(gd)
            gd.setdefault("id", gid)
            gates[gid] = Gate.from_dict(gd)
        return cls(
            id=d["id"],
            version=d.get("version", 1),
            name=d.get("name"),
            hash=d.get("hash"),
            defaults=dict(d.get("defaults") or {}),
            nodes=[Node.from_dict(n) for n in (d.get("nodes") or [])],
            edges=[Edge.from_dict(e) for e in (d.get("edges") or [])],
            triggers=[Trigger.from_dict(t) for t in (d.get("triggers") or [])],
            gates=gates,
            max_budget_usd=d.get("max_budget_usd"),
            notify=d.get("notify"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "hash": self.hash,
            "defaults": self.defaults,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "triggers": [t.to_dict() for t in self.triggers],
            "gates": {gid: g.to_dict() for gid, g in self.gates.items()},
            "max_budget_usd": self.max_budget_usd,
            "notify": self.notify,
        }

    def content_hash(self) -> str:
        """Content-addressed hash of the IR (minus the hash field itself)."""
        d = self.to_dict()
        d.pop("hash", None)
        raw = json.dumps(d, sort_keys=True, default=str).encode()
        return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass
class Issue:
    severity: str  # error|warning
    code: str
    node: Optional[str]
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "node": self.node,
            "message": self.message,
        }


class WorkflowRejected(Exception):
    """Raised when the verifier produces >=1 error (design §3.3)."""

    def __init__(self, issues: List[Issue]):
        self.issues = issues
        errs = [i for i in issues if i.severity == "error"]
        msgs = "; ".join(f"[{i.code}] {i.node or '?'}: {i.message}" for i in errs)
        super().__init__(f"workflow rejected: {msgs}")

    @property
    def issues_dict(self) -> List[Dict[str, Any]]:
        return [i.to_dict() for i in self.issues]


@dataclass
class VerifiedIR:
    """A WorkflowIR the verifier has accepted. Carries warnings only."""

    ir: WorkflowIR
    issues: List[Issue] = field(default_factory=list)  # warnings

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ir": self.ir.to_dict(),
            "warnings": [i.to_dict() for i in self.issues],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VerifiedIR":
        return cls(
            ir=WorkflowIR.from_dict(d["ir"]),
            issues=[Issue(**w) for w in d.get("warnings", [])],
        )

    @classmethod
    def from_json(cls, s: str) -> "VerifiedIR":
        return cls.from_dict(json.loads(s))