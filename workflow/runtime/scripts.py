"""Allowlisted callable registry for ``run:`` (F4).

``run:`` callables resolve ONLY against this registry — NOT arbitrary dotted
import. ``os.system`` (or any non-registered dotted name) is rejected by the
verifier. Built-ins: reducers ``concat``, ``top_k``; demo callables
``workflow.examples.notify_telegram`` and ``workflow.examples.echo``.

A registered callable takes a single ``input`` dict argument and returns a
JSON-serializable result. Reducers take a list of upstream NodeRunEnvelope dicts
and return a reduced value.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

__all__ = ["is_registered", "get", "registry", "concat", "top_k", "frozen"]

_REGISTRY: Dict[str, Callable[..., Any]] = {}
# Once the built-in allowlist is bootstrapped at import, the registry is FROZEN.
# An in-process caller must NOT be able to pollute it (review BLOCK #3: a public
# mutable register() let `scripts.register("os.system", os.system)` turn
# `run: os.system` into accepted arbitrary execution). Only the module's own
# bootstrap (below) registers; runtime mutation raises.
_FROZEN = False


def register(name: str, fn: Callable[..., Any]) -> None:
    if _FROZEN:
        raise RuntimeError(
            f"workflow script registry is frozen; cannot register '{name}'. "
            "Phase 1 `run:` resolution is an allowlist fixed at import (F4)."
        )
    _REGISTRY[name] = fn


def frozen() -> bool:
    """True once the bootstrap allowlist is sealed."""
    return _FROZEN


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def get(name: str) -> Callable[..., Any]:
    if name not in _REGISTRY:
        raise KeyError(f"run callable '{name}' is not registered")
    return _REGISTRY[name]


def registry() -> Dict[str, Callable[..., Any]]:
    return dict(_REGISTRY)


# --- built-in reducers (design §4.1 / api §2.2) -----------------------------


def concat(envelopes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Concatenate upstream branch outputs preserving branch ids (test-plan §2)."""
    items = []
    for env in envelopes:
        out = env.get("output") if isinstance(env, dict) else env
        items.append(
            {
                "node_run_id": env.get("node_run_id") if isinstance(env, dict) else None,
                "output": out,
            }
        )
    return {"kind": "concat", "branches": items}


def top_k(envelopes: List[Dict[str, Any]], k: int = 3) -> Dict[str, Any]:
    """Return the top-k branch outputs by a ``score`` field if present, else first k."""
    scored = []
    for env in envelopes:
        out = env.get("output") if isinstance(env, dict) else env
        score = 0.0
        if isinstance(out, dict):
            score = float(out.get("score", out.get("priority", 0.0)) or 0.0)
        scored.append((score, env.get("node_run_id") if isinstance(env, dict) else None, out))
    # stable sort descending by score, preserving insertion order for ties
    scored.sort(key=lambda t: t[0], reverse=True)
    return {
        "kind": "top_k",
        "k": k,
        "selected": [
            {"node_run_id": nid, "output": out} for (_, nid, out) in scored[:k]
        ],
    }


# --- demo callables ---------------------------------------------------------


def _notify_telegram(input: Dict[str, Any]) -> Dict[str, Any]:
    """Demo side-effecting callable. Records the call, returns a receipt."""
    return {"notified": True, "text": str(input.get("text", "")), "channel": "telegram"}


def _echo(input: Dict[str, Any]) -> Dict[str, Any]:
    """Demo passthrough callable."""
    return {"echo": input}


# registration (module import time) — F4 allowlist, then FROZEN.
register("concat", concat)
register("top_k", top_k)
register("workflow.examples.notify_telegram", _notify_telegram)
register("workflow.examples.echo", _echo)
_FROZEN = True