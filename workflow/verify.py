"""Verifier / linter (design §5). All F-list rules live here.

Runs at compile and at run-load. The driver NEVER walks an unverified graph.
Raises WorkflowRejected with a list of Issue(severity, code, node, message).
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Set

from .expr import TemplateError, validate_condition_syntax
from .ir import (
    Issue,
    Node,
    ON_FAIL_POLICIES,
    OVERRIDE_ONLY_FIELDS,
    REDUCE_TYPES,
    VerifiedIR,
    WorkflowIR,
    WorkflowRejected,
)
from .runtime import scripts as script_registry

__all__ = ["verify", "verify_ir"]

# A node whose tools include any of these is "live/side-effecting" and must be
# gated (F3) and declare side_effects: external (F6). Phase 1 conservative set.
_LIVE_TOOL_TAGS = ("trade_live", "trade_paper", "exec", "send_email", "notify_telegram")

# ---------------------------------------------------------------------------
# TOOLS_UNKNOWN universe (Phase 3 §B) -- lazily resolved and cached at module
# scope because walking every toolset's tool list is not cheap and verify_ir
# can run once per compile in a hot CLI loop. `_TOOL_UNIVERSE_COMPUTED`
# distinguishes "computed, registry unavailable -> None" from "not yet
# computed" so a genuinely-empty/unavailable registry isn't retried on every
# call.
# ---------------------------------------------------------------------------
_TOOL_UNIVERSE_LOCK = threading.Lock()
_TOOL_UNIVERSE_CACHE: Optional[Set[str]] = None
_TOOL_UNIVERSE_COMPUTED = False


def _tool_universe() -> Optional[Set[str]]:
    """Union of individual tool names + toolset names + the F3 live-tool
    pseudo-names -- the valid universe for ``spec.tools`` entries.

    Returns None when the ``toolsets`` registry can't be imported or yields
    no toolsets at all. Callers MUST then skip the TOOLS_UNKNOWN check
    entirely -- hermetic unit tests must not depend on the tool registry
    being loadable, and we never want a broken/missing registry to turn into
    spurious rejections of otherwise-valid workflows.
    """
    global _TOOL_UNIVERSE_CACHE, _TOOL_UNIVERSE_COMPUTED
    if _TOOL_UNIVERSE_COMPUTED:
        return _TOOL_UNIVERSE_CACHE
    with _TOOL_UNIVERSE_LOCK:
        if _TOOL_UNIVERSE_COMPUTED:
            return _TOOL_UNIVERSE_CACHE
        try:
            import toolsets as _toolsets

            names = _toolsets.get_toolset_names()
            if not names:
                _TOOL_UNIVERSE_CACHE = None
                return None
            universe: Set[str] = set(_LIVE_TOOL_TAGS)
            for name in names:
                universe.add(name)
                try:
                    universe.update(_toolsets.resolve_toolset(name))
                except Exception:  # noqa: BLE001 - one bad toolset must not sink the rest
                    continue
            _TOOL_UNIVERSE_CACHE = universe
        except Exception:  # noqa: BLE001 - soft-fail: registry unavailable in this process
            _TOOL_UNIVERSE_CACHE = None
        finally:
            _TOOL_UNIVERSE_COMPUTED = True
        return _TOOL_UNIVERSE_CACHE


def verify_ir(ir: WorkflowIR, *, phase1_warn_overrides: bool = False) -> VerifiedIR:
    """Verify a WorkflowIR. Returns VerifiedIR or raises WorkflowRejected.

    ``phase1_warn_overrides`` (config workflow.phase1_warn_overrides): when True,
    F2 override-only agent fields downgrade from hard reject to warning (so a
    security boundary is never silently ignored — it is loud).
    """
    issues: List[Issue] = []
    errors: List[Issue] = []

    def err(code: str, node: Optional[str], msg: str) -> None:
        i = Issue("error", code, node, msg)
        issues.append(i)
        errors.append(i)

    def warn(code: str, node: Optional[str], msg: str) -> None:
        issues.append(Issue("warning", code, node, msg))

    nodes: Dict[str, Node] = {n.id: n for n in ir.nodes}
    if not nodes:
        err("STRUCTURE", None, "workflow has no nodes")
        _raise_or_return(ir, issues, errors)
        return VerifiedIR(ir, [i for i in issues if i.severity == "warning"])

    # ---- structure: known kinds --------------------------------------------
    for n in ir.nodes:
        if n.kind not in ("agent", "script", "fanout", "join", "gate", "map", "cron_trigger", "webhook_trigger"):
            err("STRUCTURE", n.id, f"unknown node kind '{n.kind}'")
        if not n.id or not n.id.replace("_", "a").isalnum():
            err("STRUCTURE", n.id, "node id must be [a-z][a-z0-9_]*")

    # ---- structure: edges form ---------------------------------------------
    edge_pairs = set()
    for e in ir.edges:
        if e.from_ not in nodes:
            err("REF", e.from_, f"edge from unknown node '{e.from_}'")
        if e.to not in nodes:
            err("REF", e.to, f"edge to unknown node '{e.to}'")
        edge_pairs.add((e.from_, e.to))
        # port targets must exist on upstream
        if e.port and e.from_ in nodes:
            up = nodes[e.from_]
            if up.ports and e.port not in up.ports:
                err("REF", e.from_, f"edge references unknown port '{e.port}'")
        # condition syntax must parse at compile time, not blow up mid-run (F3.3)
        if e.condition:
            try:
                validate_condition_syntax(e.condition)
            except TemplateError as exc:
                err("CONDITION", e.to, f"malformed edge condition: {exc}")

    # ---- structure: acyclic (F-acyclic v1) ----------------------------------
    _check_acyclic(nodes, ir.edges, err)

    # ---- structure: single entry, >=1 terminal, reachable ------------------
    has_incoming = {e.to for e in ir.edges}
    entries = [n.id for n in nodes.values() if n.id not in has_incoming]
    if len(entries) == 0:
        err("STRUCTURE", None, "no entry node (every node has an incoming edge -> cycle?)")
    # terminals
    has_outgoing = {e.from_ for e in ir.edges}
    terminals = [n for n in nodes.values() if n.id not in has_outgoing]
    if not terminals:
        err("STRUCTURE", None, "no terminal node (every node has outgoing edge)")
    # reachability from entries
    reachable = _reachable_from(entries, ir.edges)
    for n in nodes.values():
        if n.id not in reachable:
            err("STRUCTURE", n.id, "unreachable node")

    # ---- per-node checks ----------------------------------------------------
    for n in ir.nodes:
        _check_node(n, nodes, ir, phase1_warn_overrides, err, warn)

    # ---- gates (F8, F3) -----------------------------------------------------
    for gid, g in ir.gates.items():
        if not g.channel:
            err("GATE", gid, "gate missing channel")
        if not g.approvers:
            err("GATE", gid, "gate missing approvers")
        if g.on_timeout not in ("shelve", "block", "approve_auto"):
            err("GATE", gid, f"invalid on_timeout '{g.on_timeout}'")
        # F8: approve_auto hard-rejected when dual_control: true
        if g.on_timeout == "approve_auto" and g.dual_control:
            err("GATE_TIMEOUT", gid, "on_timeout: approve_auto is hard-rejected when dual_control: true")
        if g.on_timeout == "approve_auto" and not g.dual_control:
            warn("GATE_SECURITY", gid, "approve_auto is insecure (auto-approves on timeout)")

    # ---- live-tool gating (F3): side-effecting node reachable without gate --
    _check_live_tool_gating(nodes, ir, err)

    if errors:
        raise WorkflowRejected(issues)
    return VerifiedIR(ir, [i for i in issues if i.severity == "warning"])


# Public alias matching the public export name
verify = verify_ir


def _raise_or_return(ir, issues, errors) -> None:
    if errors:
        raise WorkflowRejected(issues)


def _check_acyclic(nodes: Dict[str, Node], edges, err) -> None:
    adj: Dict[str, List[str]] = {nid: [] for nid in nodes}
    for e in edges:
        if e.from_ in nodes and e.to in nodes:
            adj[e.from_].append(e.to)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}

    def dfs(u: str, path: List[str]) -> bool:
        color[u] = GRAY
        for v in adj[u]:
            if color[v] == GRAY:
                err("CYCLE", v, f"cycle detected: {' -> '.join(path + [u, v])}")
                return True
            if color[v] == WHITE and dfs(v, path + [u]):
                return True
        color[u] = BLACK
        return False

    for nid in nodes:
        if color[nid] == WHITE:
            dfs(nid, [])


def _reachable_from(entries, edges) -> Set[str]:
    adj: Dict[str, List[str]] = {}
    for e in edges:
        adj.setdefault(e.from_, []).append(e.to)
    seen: Set[str] = set()
    stack = list(entries)
    while stack:
        u = stack.pop()
        if u in seen:
            continue
        seen.add(u)
        stack.extend(adj.get(u, []))
    return seen


def _check_node(
    n: Node,
    nodes: Dict[str, Node],
    ir: WorkflowIR,
    phase1_warn_overrides: bool,
    err,
    warn,
) -> None:
    # on_fail (Phase 3 §A) applies to every node kind, not just agents.
    if n.on_fail is not None and n.on_fail not in ON_FAIL_POLICIES:
        err(
            "ON_FAIL",
            n.id,
            f"unknown on_fail policy '{n.on_fail}' (expected one of: {', '.join(ON_FAIL_POLICIES)})",
        )
    elif n.on_fail == "retry" and n.side_effects == "external":
        # F6: auto-retrying a declared side-effecting node risks duplicate
        # real-world effects (double trade, double email...); the driver
        # refuses to do this, so reject it at compile time rather than let
        # it silently degrade to skip_downstream at run time.
        err(
            "ON_FAIL",
            n.id,
            "on_fail: retry is rejected on a side_effects: external node (F6) -- "
            "auto-retrying a declared side-effecting action risks duplicate effects; "
            "use on_fail: fail_run or skip_downstream, or drop side_effects if the "
            "node is not actually side-effecting",
        )

    # agent must have a prompt (design §5 prompts)
    if n.kind == "agent":
        if not n.spec or n.spec.prompt is None or n.spec.prompt == "":
            err("PROMPT", n.id, "agent node missing prompt")
        else:
            _check_template_refs(n, ir, err)
        # F2: override-only fields no execution path honors yet. Phase 3
        # shrinks this to profile/workspace -- model/provider (Phase 2) and
        # tools/max_turns (Phase 3) are honored via the LiveWorker
        # child-construction path; see ir.PHASE2_OVERRIDE_FIELDS /
        # ir.PHASE3_OVERRIDE_FIELDS.
        if n.spec:
            for f in OVERRIDE_ONLY_FIELDS:
                val = getattr(n.spec, f, None)
                if val:
                    if phase1_warn_overrides:
                        warn(
                            "PHASE1_OVERRIDE",
                            n.id,
                            f"agent node sets override-only field '{f}', which no execution "
                            "path enforces yet (spec.model, spec.provider, spec.tools, and "
                            "spec.max_turns ARE honored; spec.profile and spec.workspace "
                            "are not)",
                        )
                    else:
                        err(
                            "PHASE1_OVERRIDE",
                            n.id,
                            f"agent node sets override-only field '{f}'. spec.model, "
                            "spec.provider, spec.tools, and spec.max_turns are honored by "
                            f"the live worker; '{f}' is still not enforced by any execution "
                            "path — remove it or set workflow.phase1_warn_overrides",
                        )
        # Phase 3 §B: spec.tools must be a known tool or toolset name.
        _check_tools_unknown(n, err, warn)
        # F6: side-effecting agent node must declare side_effects: external
        if _has_live_tool(n):
            if n.side_effects != "external":
                err(
                    "SIDE_EFFECTS",
                    n.id,
                    "agent node uses live/side-effecting tools but does not declare "
                    "side_effects: external",
                )

    # script must have a registered run callable (F4)
    if n.kind == "script":
        if not n.run:
            err("SCRIPT", n.id, "script node missing run:")
        elif not script_registry.is_registered(n.run):
            err("SCRIPT", n.id, f"run callable '{n.run}' is not registered (F4 allowlist)")
        if n.side_effects == "external" and not n.idempotent and n.attempts is None:
            err("SCRIPT", n.id, "side-effecting script must declare idempotent or attempts")

    # fanout/map: max_branches required (F5)
    if n.kind in ("fanout", "map"):
        if n.max_branches is None:
            err("CARDINALITY", n.id, f"{n.kind} node missing required max_branches (F5)")
        elif n.max_branches <= 0:
            err("CARDINALITY", n.id, "max_branches must be > 0")
        if not n.over:
            err("REF", n.id, f"{n.kind} node missing over:")
        if not n.branch:
            err("REF", n.id, f"{n.kind} node missing branch template")
        else:
            # review BLOCK #2: the branch template is a nested node and MUST be
            # verified with the same rules as a top-level node — otherwise a
            # strict workflow could put override-only fields (tools/model/profile/
            # max_turns/workspace) or unregistered `run:` under fanout.branch.spec
            # and bypass F2/F4.
            try:
                bd = dict(n.branch)
                bd.setdefault("id", f"{n.id}_branch")
                bd.setdefault("kind", "agent")
                from .ir import Node as _BranchNode

                branch_node = _BranchNode.from_dict(bd)
                _check_node(branch_node, nodes, ir, phase1_warn_overrides, err, warn)
            except Exception as exc:  # malformed branch template
                err("STRUCTURE", n.id, f"invalid branch template: {exc}")
        # Phase 3 §A: a `map` node compiles to fanout + implicit reduce (its
        # own output IS the reduced branch result), so an optional reduce:
        # block on `map` gets the same validation as `join`. A reduce: on a
        # plain `fanout` has no consumer (fanout has no implicit reduce
        # step) so it is meaningless -- warn rather than silently ignoring.
        if n.kind == "map" and n.reduce:
            _validate_reduce_block(n, n.reduce, err, warn)
        elif n.kind == "fanout" and n.reduce:
            warn(
                "REDUCE",
                n.id,
                "reduce: on a plain fanout node has no effect (fanout has no implicit "
                "reduce step) -- use a join node downstream, or use kind: map instead",
            )

    # join: must have upstreams (edges or from:)
    if n.kind == "join":
        from_edges = [e for e in ir.edges if e.to == n.id]
        if not n.from_ and not from_edges:
            err("STRUCTURE", n.id, "join node has no upstreams")
        _validate_reduce_block(n, n.reduce, err, warn)
        if n.run and not script_registry.is_registered(n.run):
            err("SCRIPT", n.id, f"join run callable '{n.run}' is not registered (F4 allowlist)")

    # gate node must have a matching gates entry
    if n.kind == "gate":
        if n.id not in ir.gates:
            err("GATE", n.id, "gate node has no gates: entry")


def _has_live_tool(n: Node) -> bool:
    if not n.spec or not n.spec.tools:
        return False
    return any(t in _LIVE_TOOL_TAGS for t in n.spec.tools)


def _validate_reduce_block(n: Node, reduce_dict: Optional[Dict[str, Any]], err, warn) -> None:
    """Shared reduce: validation for `join` and `map` nodes (Phase 3 §A,
    ir.REDUCE_TYPES). Applies the same rules regardless of which node kind
    carries the block, so a `map`'s optional reduce: is exactly as strict as
    a `join`'s.
    """
    if not reduce_dict:
        return
    rtype = reduce_dict.get("type")
    if rtype is not None and rtype not in REDUCE_TYPES:
        err(
            "REDUCE",
            n.id,
            f"unknown reduce type '{rtype}' (expected one of: {', '.join(REDUCE_TYPES)})",
        )
    if rtype in ("top_k", "first_k") and "k" in reduce_dict:
        k = reduce_dict.get("k")
        if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
            err("REDUCE", n.id, f"reduce.k must be an int > 0 for reduce type '{rtype}'")
    if "short_circuit" in reduce_dict:
        sc = reduce_dict.get("short_circuit")
        if not isinstance(sc, bool):
            err("REDUCE", n.id, "reduce.short_circuit must be a bool")
        if not (n.kind == "map" and rtype == "first_k"):
            # Not an error -- short_circuit elsewhere is simply inert, but a
            # silently-ignored knob is exactly the kind of footgun the
            # verifier should surface.
            warn(
                "REDUCE",
                n.id,
                "reduce.short_circuit is only meaningful with reduce: {type: first_k} "
                "on a map node; it has no effect here",
            )


def _check_tools_unknown(n: Node, err, warn) -> None:
    """Phase 3 §B: spec.tools entries must be a known tool or toolset name
    (code TOOLS_UNKNOWN). Soft-fails (skips entirely) when the toolsets
    registry can't be resolved -- see _tool_universe().

    Also: spec.deny_tools is accepted by the schema but enforced by no
    execution path -- warn (DENY_TOOLS_UNENFORCED) rather than silently
    implying a restriction that isn't applied.
    """
    if n.spec and n.spec.tools:
        universe = _tool_universe()
        if universe is not None:
            unknown = sorted(t for t in n.spec.tools if t not in universe)
            if unknown:
                err(
                    "TOOLS_UNKNOWN",
                    n.id,
                    f"spec.tools references unknown tool/toolset name(s): {', '.join(unknown)} "
                    "-- use a registered tool or toolset name (see `hermes tools list` for "
                    "the available names)",
                )
    if n.spec and n.spec.deny_tools:
        warn(
            "DENY_TOOLS_UNENFORCED",
            n.id,
            "spec.deny_tools is set but no execution path enforces it -- it does not "
            "restrict the child's tools; remove it or express the restriction positively "
            "via spec.tools",
        )


def _check_template_refs(n: Node, ir: WorkflowIR, err) -> None:
    """Check that template variables in an agent prompt resolve to real nodes."""
    import re

    if not n.spec or not isinstance(n.spec.prompt, str):
        return
    node_ids = {nd.id for nd in ir.nodes}
    for m in re.finditer(r"\{\{\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*\}\}", n.spec.prompt):
        path = m.group(1)
        head = path.split(".")[0]
        if head in ("input", "branch", "run"):
            continue
        if head not in node_ids:
            err("TEMPLATE", n.id, f"prompt references unknown node '{head}' in '{{{{ {path} }}}}'")


def _check_live_tool_gating(nodes: Dict[str, Node], ir: WorkflowIR, err) -> None:
    """F3: a node with live/side-effecting tools must have a gate on every
    inbound path. Reject if reachable from an entry without crossing a gate."""
    # Find gate node ids
    gates = {n.id for n in ir.nodes if n.kind == "gate"}
    adj: Dict[str, List[str]] = {nid: [] for nid in nodes}
    radj: Dict[str, List[str]] = {nid: [] for nid in nodes}
    for e in ir.edges:
        if e.from_ in nodes and e.to in nodes:
            adj[e.from_].append(e.to)
            radj[e.to].append(e.from_)

    has_incoming = {e.to for e in ir.edges}
    entries = [nid for nid in nodes if nid not in has_incoming]

    def path_without_gate(start: str, target: str) -> bool:
        """True if there's a path start->target that crosses no gate (excl target)."""
        # BFS over reverse adjacency from target back to start, blocking at gates
        stack = [target]
        seen = {target}
        while stack:
            u = stack.pop()
            for p in radj.get(u, []):
                if p == start:
                    return True
                if p in gates:
                    continue  # this path crosses a gate — blocked
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return False

    for n in ir.nodes:
        if n.kind != "agent":
            continue
        if not _has_live_tool(n):
            continue
        # check each entry -> n path; if any entry reaches n without crossing a gate, reject
        for entry in entries:
            if entry == n.id:
                continue
            if path_without_gate(entry, n.id):
                err(
                    "LIVE_UNGATED",
                    n.id,
                    "side-effecting agent node is reachable from an entry without "
                    "crossing a gate (F3)",
                )
                break