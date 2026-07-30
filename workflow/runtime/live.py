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

Heavy imports (``run_agent``, ``hermes_cli.runtime_provider``,
``tools.delegate_tool``) are function-local so ``import workflow`` stays cheap
and hermetic (no LLM/network/config touched at import time).
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from ..ir import Node

__all__ = [
    "RuntimeParentError",
    "build_runtime_parent",
    "LiveWorker",
    "resolve_effective_model",
]

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

        child = child_builder(
            task_index=0,
            goal=goal,
            context=context,
            toolsets=None,
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

        cost_usd = 0.0
        if isinstance(result, dict):
            try:
                cost_usd = float(result.get("cost_usd") or 0.0)
            except (TypeError, ValueError):
                cost_usd = 0.0

        return {
            "output": {
                "text": _extract_result_text(result),
                "node": node.id,
                "result": result,
                "effective_model": effective_model,
                "effective_provider": effective_provider,
            },
            "cost_usd": cost_usd,
            "effective_model": effective_model,
            "effective_provider": effective_provider,
        }
