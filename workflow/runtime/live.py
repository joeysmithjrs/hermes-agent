"""Live agent worker (Phase 2) — real ``AIAgent`` children for agent nodes.

Bridges the workflow driver's ``Worker`` protocol (``workflow/runtime/worker.py``)
to the delegation subsystem's public, stable entry points
(``tools.delegate_tool.build_child_agent`` / ``run_child_agent``), which wrap
the canonical inherit-or-override construction path
(``tools.delegate_tool._build_child_agent``: ``effective_model = model or
parent_agent.model``).

Model/provider semantics (api §2.4, ir.PHASE2_OVERRIDE_FIELDS):
  - ``node.spec.model`` / ``node.spec.provider`` unset -> the child inherits
    the runtime parent's model/provider (pass ``model=None`` to the child
    builder so it applies ``model or parent_agent.model`` itself).
  - ``node.spec.model`` set, ``node.spec.provider`` unset -> the child uses
    the override model on the SAME provider/credentials as the parent (pass
    ``model=<override>``, leave override credentials ``None``).
  - ``node.spec.provider`` set (with or without ``node.spec.model``) -> fresh
    credentials are resolved via ``hermes_cli.runtime_provider.resolve_runtime_provider``
    and passed through as ``override_*`` kwargs.

Tools/max_turns semantics (ir.PHASE3_OVERRIDE_FIELDS):
  - ``node.spec.tools`` unset -> ``toolsets=None`` passed to the child builder
    (inherit all of the parent's toolsets, unchanged Phase 1/2 behavior).
  - ``node.spec.tools`` set -> resolved to the minimal covering set of
    TOOLSET names (see ``_resolve_child_toolsets``) and passed as
    ``toolsets=[...]``. ``tools.delegate_tool.build_child_agent`` then
    INTERSECTS that list with the parent's own toolsets (child ⊆ parent is
    enforced there, not here). Because the grant is by toolset rather than by
    individual tool, the actual guarantee is: (a) child toolsets ⊆ parent
    toolsets, and (b) every requested tool's toolset is present -- the child
    MAY also receive sibling tools that share a toolset with a requested
    tool. There is no per-tool gating at this construction boundary. The
    verifier (workflow/verify.py, code TOOLS_UNKNOWN) is the compile-time
    check that every name is real; the resolution here is defense in depth,
    not the primary gate.
  - ``node.spec.max_turns`` set -> overrides the ``max_iterations`` passed to
    the child builder (precedence: spec.max_turns > LiveWorker(max_iterations=)
    > delegation.max_iterations config default).

Heavy imports (``run_agent``, ``hermes_cli.runtime_provider``,
``tools.delegate_tool``, ``toolsets``, ``model_tools``) are function-local so
``import workflow`` stays cheap and hermetic (no LLM/network/config touched
at import time).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..ir import Node

__all__ = [
    "RuntimeParentError",
    "build_runtime_parent",
    "LiveWorker",
    "resolve_effective_model",
    "extract_cost_and_tokens",
]

logger = logging.getLogger(__name__)

# Per-process cache of constructed runtime parents. The key is the FULLY
# RESOLVED identity of the parent (model, provider, base_url, and a digest of
# the API key) — never the raw call arguments.
#
# Keying on the arguments was a real bug: LiveWorker._ensure_parent() calls
# build_runtime_parent() with no arguments, so every real invocation collapsed
# to the single key (None, None). A long-lived process (a gateway, or an
# interactive session where the agent calls workflow_run more than once)
# resolved the runtime parent once and then served every later inherit-default
# run from that first resolution — ignoring subsequent edits to
# workflow.live_model / model.default and, worse, continuing to use a rotated
# -away API key. Resolution is now redone on every call (it is cheap, no
# network); only the expensive AIAgent construction is cached, and a config
# edit or key rotation naturally produces a different key.
_PARENT_CACHE: Dict[Tuple[Any, ...], Any] = {}
_PARENT_CACHE_LOCK = threading.Lock()


def _credential_fingerprint(api_key: Optional[str]) -> str:
    """Short digest of an API key for cache identity — never the key itself."""
    if not api_key:
        return "-"
    import hashlib

    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:16]


def close_runtime_parents() -> int:
    """Close and drop every cached runtime parent. Returns how many were closed.

    Long-lived hosts should call this on shutdown (or after a credential
    rotation) so cached AIAgent instances release their session/DB handles.
    """
    with _PARENT_CACHE_LOCK:
        parents = list(_PARENT_CACHE.values())
        _PARENT_CACHE.clear()
    closed = 0
    for p in parents:
        try:
            close = getattr(p, "close", None)
            if callable(close):
                close()
            closed += 1
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
    return closed


class RuntimeParentError(RuntimeError):
    """Raised when a live runtime parent agent cannot be constructed.

    Never returned as None — callers get an actionable error naming what was
    missing (model, provider credentials, ...) and how to fix it, or how to
    fall back to a no-LLM dry run.
    """


def _fake_run_hint() -> str:
    return "run with --fake or set HERMES_WORKFLOW_FAKE=1 for a dry no-LLM run"


def build_runtime_parent(
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    toolsets: Optional[list] = None,
):
    """Build (or reuse a cached) runtime ``AIAgent`` to serve as the parent
    for live workflow agent-node children.

    Resolution order: explicit ``model``/``provider`` args -> workflow config
    (``workflow.live_model`` / ``workflow.live_provider``) -> Hermes global
    config (``model:`` block) -> ``RuntimeParentError`` if no model resolves.

    Raises ``RuntimeParentError`` (never returns None) on any failure —
    missing config module, unresolvable model, or credential resolution
    failure.
    """
    try:
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_cli.tools_config import _get_platform_tools
    except Exception as exc:  # pragma: no cover - defensive import guard
        raise RuntimeParentError(
            "could not import the Hermes runtime/config modules needed to "
            f"build a live agent worker parent ({exc}); {_fake_run_hint()}."
        ) from exc

    try:
        cfg = load_config()
    except Exception as exc:
        raise RuntimeParentError(
            f"failed to load Hermes config via hermes_cli.config.load_config(): {exc}; "
            "set model.default in config.yaml or workflow.live_model, and configure "
            f"provider credentials; or {_fake_run_hint()}."
        ) from exc

    model_cfg = cfg.get("model") if isinstance(cfg, dict) else None
    if isinstance(model_cfg, str):
        cfg_model = model_cfg.strip()
    elif isinstance(model_cfg, dict):
        cfg_model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    else:
        cfg_model = ""

    try:
        from ..config import load_workflow_config

        wf_cfg = load_workflow_config()
    except Exception:
        wf_cfg = {}
    live_model = str(wf_cfg.get("live_model") or "").strip()
    live_provider = str(wf_cfg.get("live_provider") or "").strip()

    effective_model = (model or "").strip() or live_model or cfg_model or None
    effective_provider = (provider or "").strip() or live_provider or None

    if not effective_model:
        raise RuntimeParentError(
            "no model resolvable for the live agent worker: set model.default in "
            "config.yaml, or workflow.live_model in the workflow: config block, and "
            f"configure provider credentials; or {_fake_run_hint()}."
        )

    try:
        runtime = resolve_runtime_provider(
            requested=effective_provider, target_model=effective_model
        )
    except Exception as exc:
        raise RuntimeParentError(
            f"failed to resolve runtime provider credentials for model={effective_model!r} "
            f"provider={effective_provider!r}: {exc}; configure provider credentials in "
            f"config.yaml (providers.<name>) or the environment; or {_fake_run_hint()}."
        ) from exc

    # Cache identity is the RESOLVED parent, so a config edit or a rotated key
    # misses the cache instead of silently reusing a stale agent.
    cache_key = (
        effective_model,
        runtime.get("provider"),
        runtime.get("base_url"),
        _credential_fingerprint(runtime.get("api_key")),
        tuple(toolsets) if toolsets is not None else None,
    )
    with _PARENT_CACHE_LOCK:
        cached = _PARENT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        toolsets_list = list(toolsets) if toolsets is not None else sorted(_get_platform_tools(cfg, "cli"))
    except Exception:
        toolsets_list = list(toolsets) if toolsets else None

    try:
        from run_agent import AIAgent

        parent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            requested_provider=runtime.get("requested_provider"),
            api_mode=runtime.get("api_mode"),
            model=effective_model,
            enabled_toolsets=toolsets_list,
            quiet_mode=True,
            platform="cli",
            skip_context_files=True,
            skip_memory=True,
            credential_pool=runtime.get("credential_pool"),
        )
    except Exception as exc:
        raise RuntimeParentError(
            f"failed to construct the live agent worker parent (model={effective_model!r}, "
            f"provider={runtime.get('provider')!r}): {exc}; check provider credentials "
            f"(API key/base_url) in config.yaml or the environment; or {_fake_run_hint()}."
        ) from exc

    with _PARENT_CACHE_LOCK:
        _PARENT_CACHE[cache_key] = parent
    return parent


def resolve_effective_model(node: Node, parent_agent: Any) -> Tuple[Optional[str], Optional[str]]:
    """Pure helper (no I/O): the (model, provider) a node will run with.

    ``node.spec.model``/``node.spec.provider`` win when set; otherwise the
    node inherits the parent agent's ``model``/``provider`` attributes.
    ``parent_agent`` may be any object exposing (or lacking) those
    attributes — this is directly unit-testable with a dummy/stub parent.
    """
    spec = getattr(node, "spec", None)
    spec_model = getattr(spec, "model", None) if spec is not None else None
    spec_provider = getattr(spec, "provider", None) if spec is not None else None
    effective_model = spec_model or getattr(parent_agent, "model", None)
    effective_provider = spec_provider or getattr(parent_agent, "provider", None)
    return effective_model, effective_provider


def _resolve_child_toolsets(tool_names: List[str], node_id: str) -> Optional[List[str]]:
    """Resolve ``node.spec.tools`` names to the minimal covering set of
    TOOLSET names that ``build_child_agent``'s ``toolsets=`` kwarg expects.

    A name that is already a toolset name (``toolsets.validate_toolset``)
    passes through unchanged; a bare tool name maps to its owning toolset via
    ``model_tools.get_toolset_for_tool``. The verifier (code TOOLS_UNKNOWN)
    already rejects unknown names at compile time, so an unresolvable name
    here is dropped with a warning rather than raised -- this is defense in
    depth, not the primary gate. Returns None (inherit-all, matching the
    unset-spec.tools default) if nothing resolves, since ``toolsets=[]`` and
    ``toolsets=None`` are already equivalent in ``build_child_agent`` (both
    fall through to its inherit-from-parent branch) -- returning None here
    just makes that equivalence explicit rather than accidental.
    """
    try:
        import model_tools as _model_tools
        import toolsets as _toolsets
    except Exception as exc:  # pragma: no cover - defensive import guard
        logger.warning(
            "could not import toolsets/model_tools to resolve spec.tools for node "
            "%r (%s); falling back to toolsets=None (inherit all)",
            node_id,
            exc,
        )
        return None

    resolved: set = set()
    for name in tool_names:
        try:
            is_toolset = _toolsets.validate_toolset(name)
        except Exception:
            is_toolset = False
        if is_toolset:
            resolved.add(name)
            continue
        try:
            owning_toolset = _model_tools.get_toolset_for_tool(name)
        except Exception:
            owning_toolset = None
        if owning_toolset:
            resolved.add(owning_toolset)
        else:
            logger.warning(
                "node %r spec.tools entry %r did not resolve to a known tool or "
                "toolset name; dropping it (the verifier should have already "
                "rejected this at compile time)",
                node_id,
                name,
            )
    return sorted(resolved) if resolved else None


# ---------------------------------------------------------------------------
# Cost/token extraction (Phase 3 §C)
# ---------------------------------------------------------------------------

# Cost keys observed (or plausible) in a child result envelope.
_COST_KEYS = ("cost_usd", "total_cost_usd", "total_cost", "cost")
# Token-count key aliases. tools/delegate_tool.py's real child-result entry
# (``_run_single_child``'s ``entry``) nests counts as
# ``{"tokens": {"input": N, "output": N}}`` -- bare "input"/"output", not
# "*_tokens" -- so the "tokens" container gets its own bare-name aliases in
# addition to the generic ones used by other shapes/tests.
_TOKENS_IN_KEYS = ("tokens_in", "input_tokens", "prompt_tokens")
_TOKENS_OUT_KEYS = ("tokens_out", "output_tokens", "completion_tokens")
# One nested level under these container keys is also searched. "tokens" is
# delegate_tool's real container name; the rest are common alternate shapes.
_NESTED_CONTAINERS = ("result", "usage", "token_usage", "metrics", "cost", "tokens")


def _first_scalar(d: Dict[str, Any], keys: Tuple[str, ...]) -> Tuple[Any, bool]:
    """First present, non-container value among ``keys`` in ``d``.

    Skips dict/list values -- a key like "cost" can legitimately be either a
    scalar cost value OR a nested container of cost-shaped fields (both
    "cost" the key and "cost" the container are checked), and a container
    value must not be mistaken for a (uncoercible) scalar that then blocks
    the nested-container search from ever running.
    """
    for k in keys:
        if k in d:
            v = d[k]
            if v is None or isinstance(v, (dict, list)):
                continue
            return v, True
    return None, False


def _coerce_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _coerce_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def extract_cost_and_tokens(result: Any) -> Tuple[float, int, int]:
    """Best-effort ``(cost_usd, tokens_in, tokens_out)`` from a child result.

    Accepts a dict, a JSON string that parses to a dict, or anything else
    (returns zeros for the latter two -- never raises). Searches the top
    level AND one nested level under common containers (result, usage,
    token_usage, metrics, cost, tokens); first match wins per field, and a
    present-but-non-numeric value coerces to 0 rather than falling through to
    a later container.
    """
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError):
            return 0.0, 0, 0
    if not isinstance(parsed, dict):
        return 0.0, 0, 0

    cost_usd = 0.0
    tokens_in = 0
    tokens_out = 0
    found_cost = found_tin = found_tout = False

    v, ok = _first_scalar(parsed, _COST_KEYS)
    if ok:
        cost_usd, found_cost = _coerce_float(v), True
    v, ok = _first_scalar(parsed, _TOKENS_IN_KEYS)
    if ok:
        tokens_in, found_tin = _coerce_int(v), True
    v, ok = _first_scalar(parsed, _TOKENS_OUT_KEYS)
    if ok:
        tokens_out, found_tout = _coerce_int(v), True

    for container_name in _NESTED_CONTAINERS:
        if found_cost and found_tin and found_tout:
            break
        container = parsed.get(container_name)
        if not isinstance(container, dict):
            continue
        if not found_cost:
            v, ok = _first_scalar(container, _COST_KEYS)
            if ok:
                cost_usd, found_cost = _coerce_float(v), True
        if not found_tin:
            in_keys = _TOKENS_IN_KEYS + (("input",) if container_name == "tokens" else ())
            v, ok = _first_scalar(container, in_keys)
            if ok:
                tokens_in, found_tin = _coerce_int(v), True
        if not found_tout:
            out_keys = _TOKENS_OUT_KEYS + (("output",) if container_name == "tokens" else ())
            v, ok = _first_scalar(container, out_keys)
            if ok:
                tokens_out, found_tout = _coerce_int(v), True

    return cost_usd, tokens_in, tokens_out


def _extract_result_text(result: Any) -> str:
    """Best-effort text extraction from a (possibly JSON-string) child result."""
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError):
            return result
    if isinstance(parsed, dict):
        for key in ("summary", "response", "final_response", "result"):
            val = parsed.get(key)
            if isinstance(val, str) and val:
                return val
        return ""
    if parsed is None:
        return ""
    return str(parsed)


class LiveWorker:
    """Worker (design §7 protocol) that runs agent nodes as real AIAgent children.

    Non-agent node kinds are handled by the driver directly (script/join/etc);
    ``run_node`` returns a trivial ok envelope for them, matching FakeWorker's
    contract, so a LiveWorker can be dropped in wherever a Worker is expected.
    """

    def __init__(
        self,
        parent_agent: Any = None,
        *,
        build_parent: Optional[Callable[..., Any]] = None,
        child_builder: Optional[Callable[..., Any]] = None,
        child_runner: Optional[Callable[..., Any]] = None,
        max_iterations: Optional[int] = None,
    ) -> None:
        self._parent_agent = parent_agent
        self._build_parent = build_parent or build_runtime_parent
        # Injectable so tests can substitute stubs that just record kwargs
        # instead of spawning a real child. Defaults are resolved lazily
        # inside run_node (heavy import: tools.delegate_tool).
        self._child_builder = child_builder
        self._child_runner = child_runner
        self.max_iterations = max_iterations
        self._parent_lock = threading.Lock()

    def _ensure_parent(self) -> Any:
        if self._parent_agent is None:
            with self._parent_lock:
                if self._parent_agent is None:
                    self._parent_agent = self._build_parent()
        return self._parent_agent

    def run_node(self, node: Node, ctx: Dict[str, Any]) -> Dict[str, Any]:
        if node.kind != "agent":
            # non-agent kinds (script/join/fanout/map/gate/...) are handled
            # by the driver itself; a Worker is never asked to execute them.
            return {"output": {"ok": True, "node": node.id}, "cost_usd": 0.0}

        child_builder = self._child_builder
        child_runner = self._child_runner
        if child_builder is None or child_runner is None:
            from tools.delegate_tool import build_child_agent, run_child_agent

            child_builder = child_builder or build_child_agent
            child_runner = child_runner or run_child_agent

        parent = self._ensure_parent()

        spec = node.spec
        prompt = spec.prompt if spec is not None else None
        goal = prompt if isinstance(prompt, str) else json.dumps(prompt)
        context = json.dumps(ctx.get("input", {}))

        effective_model, effective_provider = resolve_effective_model(node, parent)

        spec_model = getattr(spec, "model", None) if spec is not None else None
        spec_provider = getattr(spec, "provider", None) if spec is not None else None

        override_provider = None
        override_base_url = None
        override_api_key = None
        override_api_mode = None
        # spec.provider explicitly given -> resolve fresh credentials for it
        # (covers both "provider differs from the parent" and "provider set
        # to the same value" -- an explicit ask for that provider's own
        # credential resolution). spec.model alone (no spec.provider) stays
        # on the parent's provider/credentials -- only the model overrides.
        if spec_provider:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            runtime = resolve_runtime_provider(
                requested=effective_provider, target_model=effective_model
            )
            override_provider = runtime.get("provider")
            override_base_url = runtime.get("base_url")
            override_api_key = runtime.get("api_key")
            override_api_mode = runtime.get("api_mode")
            # Report the CANONICAL provider, matching _build_child_agent's own
            # `effective_provider = override_provider or parent.provider`.
            # resolve_runtime_provider canonicalizes aliases (ollama/vllm/
            # llamacpp all resolve to "custom"), so reporting the raw spec
            # value here would make the checkpoint, the node events, and the
            # notification all misstate which provider actually ran the node.
            effective_provider = override_provider or effective_provider

        # Inherit case: pass model=None so _build_child_agent applies
        # `model or parent_agent.model` itself -- do not pre-resolve to a
        # string here, that would defeat the inherit semantic. We still
        # report the computed effective_model in the returned metadata.
        child_model = spec_model if spec_model else None

        max_iterations = self.max_iterations
        if max_iterations is None:
            # Same budget a chat-initiated delegation gets: the operator owns it
            # via delegation.max_iterations in config.yaml. Don't invent a
            # workflow-specific ceiling the operator can't see or tune.
            from tools.delegate_tool import DEFAULT_MAX_ITERATIONS, _load_config

            try:
                max_iterations = int(_load_config().get("max_iterations", DEFAULT_MAX_ITERATIONS))
            except Exception:
                max_iterations = DEFAULT_MAX_ITERATIONS

        # spec.max_turns wins over both the config default AND
        # LiveWorker(max_iterations=...) -- it is the node author's explicit,
        # per-node ask. Coerce defensively: a non-numeric or non-positive
        # value is ignored (with a warning) rather than breaking the run.
        spec_max_turns = getattr(spec, "max_turns", None) if spec is not None else None
        if spec_max_turns is not None:
            try:
                coerced_max_turns = int(spec_max_turns)
            except (TypeError, ValueError):
                logger.warning(
                    "node %r spec.max_turns=%r is not an int; ignoring", node.id, spec_max_turns
                )
                coerced_max_turns = None
            if coerced_max_turns is not None:
                if coerced_max_turns > 0:
                    max_iterations = coerced_max_turns
                else:
                    logger.warning(
                        "node %r spec.max_turns=%r is not positive; ignoring",
                        node.id,
                        spec_max_turns,
                    )

        # spec.tools unset -> inherit all of the parent's toolsets (toolsets=
        # None); set -> resolve to the minimal covering toolset names (see
        # module docstring for the exact ⊆-guarantee this provides).
        spec_tools = list(getattr(spec, "tools", None) or []) if spec is not None else []
        child_toolsets = _resolve_child_toolsets(spec_tools, node.id) if spec_tools else None

        child = child_builder(
            task_index=0,
            goal=goal,
            context=context,
            toolsets=child_toolsets,
            model=child_model,
            max_iterations=max_iterations,
            task_count=1,
            parent_agent=parent,
            role="leaf",
            override_provider=override_provider,
            override_base_url=override_base_url,
            override_api_key=override_api_key,
            override_api_mode=override_api_mode,
        )
        result = child_runner(0, goal, child=child, parent_agent=parent)

        cost_usd, tokens_in, tokens_out = extract_cost_and_tokens(result)
        # The returned entry is NOT a complete cost record, and that is the
        # real cause of the Phase 2 under-report. `tools.delegate_tool` builds
        # the child entry with `tokens: {input, output}` but stashes the dollar
        # figure under `_child_cost_usd`, which `_finalize_child_results` then
        # POPS off the entry and folds into the parent's session total — so by
        # the time `run_child_agent` returns, the caller can never see what the
        # child cost. Parsing the result harder cannot fix that; the number is
        # genuinely gone.
        #
        # The child agent object itself still carries the counters, and we hold
        # a reference to it (we built it), so read them straight off the child.
        # This is exact and per-child rather than a diff of the parent's running
        # total, which is what makes it correct under `max_parallel_nodes > 1`:
        # several children fold into the same parent concurrently, so a
        # before/after delta on the parent would mis-attribute cost between
        # nodes. `child.close()` (called in the runner's finally block) releases
        # sessions/handles but does not reset these attributes.
        #
        # Only used as a FALLBACK, so an explicit cost/token in the result --
        # which is what the test stubs return -- always wins.
        if not cost_usd:
            cost_usd = _coerce_float(getattr(child, "session_estimated_cost_usd", 0.0))
        if not tokens_in:
            tokens_in = _coerce_int(getattr(child, "session_prompt_tokens", 0))
        if not tokens_out:
            tokens_out = _coerce_int(getattr(child, "session_completion_tokens", 0))

        return {
            "output": {
                "text": _extract_result_text(result),
                "node": node.id,
                "result": result,
                "effective_model": effective_model,
                "effective_provider": effective_provider,
            },
            "cost_usd": cost_usd,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "effective_model": effective_model,
            "effective_provider": effective_provider,
        }
