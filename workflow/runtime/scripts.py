"""Allowlisted callable registry for ``run:`` (F4).

``run:`` callables resolve ONLY against this registry — NOT arbitrary dotted
import. ``os.system`` (or any non-registered dotted name) is rejected by the
verifier. Built-ins: reducers ``concat``, ``top_k``, ``first_k``,
``majority``, ``best``; demo callables ``workflow.examples.notify_telegram``
and ``workflow.examples.echo``.

A registered callable takes a single ``input`` dict argument and returns a
JSON-serializable result. Reducers take a list of upstream NodeRunEnvelope dicts
and return a reduced value.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "is_registered",
    "get",
    "registry",
    "concat",
    "top_k",
    "first_k",
    "majority",
    "judge_converge",
    "best",
    "frozen",
]

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


def first_k(envelopes: List[Dict[str, Any]], k: int = 3) -> Dict[str, Any]:
    """Return the first k envelopes in branch order (no scoring)."""
    k = int(k)
    selected = []
    for env in envelopes[:k]:
        out = env.get("output") if isinstance(env, dict) else env
        selected.append(
            {
                "node_run_id": env.get("node_run_id") if isinstance(env, dict) else None,
                "output": out,
            }
        )
    return {"kind": "first_k", "k": k, "selected": selected}


def majority(envelopes: List[Dict[str, Any]], key: Optional[str] = None) -> Dict[str, Any]:
    """Vote over branch outputs. Each branch's vote is canonicalized as
    ``json.dumps(out[key] if key else out, sort_keys=True, default=str)`` so
    structurally-equal dict/list outputs compare equal regardless of key
    order. The value with the highest count wins.

    Tie-break (documented, not incidental): when two or more values are tied
    for the highest count, the one that FIRST appeared (lowest branch order)
    wins, and the result carries ``"tie": true`` so a caller can tell a clean
    majority from a coin-flip. ``tally`` is sorted by count desc, then by
    first-appearance for ties.
    """
    tally: List[List[Any]] = []  # [canon, original_value, count], insertion order = first appearance
    canon_to_idx: Dict[str, int] = {}
    for env in envelopes:
        out = env.get("output") if isinstance(env, dict) else env
        val = out.get(key) if (key and isinstance(out, dict)) else out
        canon = json.dumps(val, sort_keys=True, default=str)
        if canon in canon_to_idx:
            tally[canon_to_idx[canon]][2] += 1
        else:
            canon_to_idx[canon] = len(tally)
            tally.append([canon, val, 1])

    total = len(envelopes)
    if not tally:
        return {"kind": "majority", "winner": None, "count": 0, "total": 0, "tie": False, "tally": []}

    # stable sort by count desc; ties keep first-appearance order (their
    # original insertion index is ascending already, so a stable sort on
    # -count alone preserves it).
    ranked = sorted(range(len(tally)), key=lambda i: -tally[i][2])
    winner_count = tally[ranked[0]][2]
    tie = sum(1 for t in tally if t[2] == winner_count) > 1
    return {
        "kind": "majority",
        "winner": tally[ranked[0]][1],
        "count": winner_count,
        "total": total,
        "tie": tie,
        "tally": [{"value": tally[i][1], "count": tally[i][2]} for i in ranked],
    }


def judge_converge(
    envelopes: List[Dict[str, Any]],
    judgment: Any = None,
    key: Optional[str] = None,
) -> Dict[str, Any]:
    """Fold a judge agent's ruling together with the arguments it ruled on
    (post-Phase-3 §3, the `judge_escalate` debate protocol).

    This is a REDUCER, not the judge: the judge is an ordinary agent child the
    driver runs through the same worker as any other node, and its output
    arrives here as ``judgment``. Keeping the fold pure is what makes the
    escalation auditable — the record shows every candidate that was on the
    table alongside the ruling that picked among them.

    ``majority`` already covers the plain `vote` protocol (canonicalized
    voting, documented tie-break), so nothing here re-implements counting; the
    tally is delegated to it verbatim and reported next to the ruling, letting
    a reader see whether the judge went with or against the room.
    """
    tally = majority(envelopes, key=key)
    candidates = []
    for env in envelopes:
        out = env.get("output") if isinstance(env, dict) else env
        candidates.append(
            {
                "node_run_id": env.get("node_run_id") if isinstance(env, dict) else None,
                "output": out,
            }
        )
    return {
        "kind": "judge_converge",
        "judgment": judgment,
        "judged": judgment is not None,
        "candidates": candidates,
        "tally": tally.get("tally", []),
        "majority_winner": tally.get("winner"),
        "tie": tally.get("tie", False),
    }


def best(envelopes: List[Dict[str, Any]], key: str = "score") -> Dict[str, Any]:
    """Select the single highest-scoring branch by ``out[key]``. A missing or
    non-numeric score counts as 0.0. Ties resolve to first appearance (a
    strict ``>`` comparison never displaces an earlier equal-scoring
    branch)."""
    best_idx: Optional[int] = None
    best_score: float = 0.0
    for i, env in enumerate(envelopes):
        out = env.get("output") if isinstance(env, dict) else env
        score = 0.0
        if isinstance(out, dict):
            try:
                score = float(out.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
        if best_idx is None or score > best_score:
            best_idx = i
            best_score = score
    if best_idx is None:
        return {"kind": "best", "selected": None, "score": 0.0}
    env = envelopes[best_idx]
    out = env.get("output") if isinstance(env, dict) else env
    return {
        "kind": "best",
        "selected": {
            "node_run_id": env.get("node_run_id") if isinstance(env, dict) else None,
            "output": out,
        },
        "score": best_score,
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
register("first_k", first_k)
register("majority", majority)
register("judge_converge", judge_converge)
register("best", best)
register("workflow.examples.notify_telegram", _notify_telegram)
register("workflow.examples.echo", _echo)
_FROZEN = True