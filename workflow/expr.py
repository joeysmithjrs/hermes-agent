"""Tiny template renderer + edge condition language (api §2.9).

Templates: ``{{ node_id.output.field }}`` with the bare shorthand
``{{ node_id.field }}`` == ``{{ node_id.output.field }}`` (F10 canonical form).
Special roots ``input`` and ``branch`` are NOT given the .output shorthand.

Conditions: ``$.field op literal`` referencing the immediate upstream output,
``op`` in ``==, !=, >, >=, <, <=, in, exists``. Also bare ``true``/``false``.
No arbitrary Python (design §2.3).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

__all__ = ["render", "resolve_path", "eval_condition", "TemplateError"]

_TEMPLATE_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
# roots that are NOT subject to the bare node.field -> node.output.field shorthand
_BARE_ROOTS = ("input", "branch", "run")


class TemplateError(Exception):
    """Raised when a template path or condition cannot be resolved."""


def resolve_path(path: str, ctx: Dict[str, Any]) -> Any:
    """Resolve a dotted path like ``seed.output.branches`` against ctx.

    ctx maps: node_id -> NodeRunEnvelope dict (with ``output`` key) OR raw output
    dict, plus special roots ``input``, ``branch``, ``run``.
    Bare ``node.field`` is shorthand for ``node.output.field`` (F10).
    """
    parts = path.split(".")
    head = parts[0]
    if head not in ctx:
        raise TemplateError(f"unknown template root '{head}' in '{path}'")
    val = ctx[head]
    rest = parts[1:]
    # Apply bare shorthand: node.field -> node.output.field, unless root is special.
    if (
        rest
        and head not in _BARE_ROOTS
        and rest[0] != "output"
        and isinstance(val, dict)
        and "output" in val
        and rest[0] in (val.get("output") or {})
    ):
        rest = ["output", *rest]
    for p in rest:
        if val is None:
            raise TemplateError(f"path '{path}' dereferences None at '{p}'")
        if isinstance(val, dict):
            if p not in val:
                raise TemplateError(f"key '{p}' not found in '{path}'")
            val = val[p]
        elif isinstance(val, list):
            try:
                val = val[int(p)]
            except (ValueError, IndexError):
                raise TemplateError(f"index '{p}' invalid in '{path}'")
        else:
            raise TemplateError(f"cannot dereference '{p}' on {type(val).__name__} in '{path}'")
    return val


def render(template: Any, ctx: Dict[str, Any]) -> Any:
    """Render a string template. Non-strings returned unchanged.

    If the whole string is a single ``{{ ... }}`` expression, the resolved
    Python value is returned (preserves lists/dicts — needed for ``over:``).
    """
    if not isinstance(template, str):
        return template
    # Single-expression whole-string -> return raw value (e.g. over: lists)
    m_full = re.fullmatch(r"\s*\{\{\s*(.*?)\s*\}\}\s*", template)
    if m_full:
        return resolve_path(m_full.group(1), ctx)
    if "{{" not in template:
        return template

    def sub(m: re.Match) -> str:
        val = resolve_path(m.group(1), ctx)
        if isinstance(val, (dict, list)):
            return json.dumps(val)
        return str(val)

    return _TEMPLATE_RE.sub(sub, template)


def eval_condition(condition: str, upstream_output: Any) -> bool:
    """Evaluate ``$.field op literal`` or ``true``/``false`` against upstream output."""
    if condition is None:
        return True
    cond = condition.strip()
    if cond.lower() == "true":
        return True
    if cond.lower() == "false":
        return False
    m = re.fullmatch(
        r"\$\.(?P<field>[A-Za-z_][A-Za-z0-9_\.]*)\s*(?P<op>==|!=|>=|<=|>|<|in|exists)\s*(?P<lit>.+)",
        cond,
    )
    if not m:
        # Unknown condition form — fail closed (design §2.3: no arbitrary Python)
        raise TemplateError(f"unparseable condition '{condition}'")
    field, op, lit = m.group("field"), m.group("op"), m.group("lit").strip()
    # Resolve $.field against upstream_output (a dict/output envelope)
    val = upstream_output
    if isinstance(val, dict) and "output" in val and field in (val.get("output") or {}):
        val = val["output"]
    if isinstance(val, dict):
        if field not in val:
            if op == "exists":
                return False
            raise TemplateError(f"condition field '{field}' not in upstream output")
        val = val[field]
    else:
        if op == "exists":
            return val is not None
        raise TemplateError(f"cannot index '{field}' on non-dict upstream output")
    if op == "exists":
        return True
    # Parse literal
    lit_parsed = _parse_literal(lit)
    try:
        if op == "==":
            return val == lit_parsed
        if op == "!=":
            return val != lit_parsed
        if op == ">":
            return val > lit_parsed
        if op == ">=":
            return val >= lit_parsed
        if op == "<":
            return val < lit_parsed
        if op == "<=":
            return val <= lit_parsed
        if op == "in":
            return val in lit_parsed
    except TypeError:
        return False
    raise TemplateError(f"unsupported op '{op}'")


def _parse_literal(lit: str) -> Any:
    lit = lit.strip()
    if lit.lower() == "true":
        return True
    if lit.lower() == "false":
        return False
    if lit.lower() == "null":
        return None
    if (lit.startswith('"') and lit.endswith('"')) or (lit.startswith("'") and lit.endswith("'")):
        return lit[1:-1]
    if lit.startswith("[") and lit.endswith("]"):
        try:
            return json.loads(lit)
        except json.JSONDecodeError:
            return [s.strip() for s in lit[1:-1].split(",")]
    try:
        if "." in lit:
            return float(lit)
        return int(lit)
    except ValueError:
        return lit  # bare word — treat as string