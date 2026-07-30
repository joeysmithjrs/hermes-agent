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

__all__ = [
    "render",
    "render_params",
    "resolve_path",
    "eval_condition",
    "validate_condition_syntax",
    "TemplateError",
]

_TEMPLATE_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
# roots that are NOT subject to the bare node.field -> node.output.field shorthand
# (`workspace` is the post-Phase-3 named-workspace block: paths/names, never a
# node envelope, so the `.output` shorthand must not apply to it either)
_BARE_ROOTS = ("input", "branch", "run", "workspace")

# Shared by eval_condition (runtime) and validate_condition_syntax (compile-time,
# workflow/verify.py) — one regex, not two, so the two can't drift apart.
_CONDITION_RE = re.compile(
    r"\$\.(?P<field>[A-Za-z_][A-Za-z0-9_\.]*)\s*(?P<op>==|!=|>=|<=|>|<|in|exists)\s*(?P<lit>.+)"
)


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


_PARAMS_RE = re.compile(r"\{\{\s*params\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_params(text: str, params: Optional[Dict[str, Any]]) -> str:
    """Substitute ``{{ params.<key> }}`` placeholders and NOTHING else.

    This is the load-time half of the two-stage rendering both the catalog
    (``store/catalog.py``) and the prompt library (``prompts/library.py``) use:
    a recipe/prompt body is parameterized at LOAD time by its author-supplied
    params, then the ordinary ``render()`` resolves ``{{ node.output.field }}``
    against live run data later. Touching only the ``params`` root is what
    keeps the two stages from stepping on each other.

    A placeholder with no matching param raises ``TemplateError`` rather than
    rendering an empty string — a prompt silently missing its directive is
    worse than one that refuses to load.
    """
    params = params or {}

    def sub(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key not in params:
            raise TemplateError(
                f"template references {{{{ params.{key} }}}} but no '{key}' param was "
                f"supplied (given: {sorted(params)})"
            )
        val = params[key]
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, (dict, list)):
            return json.dumps(val, default=str)
        return str(val)

    return _PARAMS_RE.sub(sub, text)


def _dotted_root_present(field: str, output: Any) -> bool:
    """True if the first component of a dotted ``field`` path exists in
    ``output`` — used only to decide whether ``$.field`` should shorthand
    into ``upstream_output["output"]`` (matches the bare-root convention
    used elsewhere in this module, e.g. ``resolve_path``)."""
    if not isinstance(output, dict):
        return False
    root = field.split(".", 1)[0]
    return root in output


def eval_condition(condition: str, upstream_output: Any) -> bool:
    """Evaluate ``$.field op literal`` or ``true``/``false`` against upstream output."""
    if condition is None:
        return True
    cond = condition.strip()
    if cond.lower() == "true":
        return True
    if cond.lower() == "false":
        return False
    m = _CONDITION_RE.fullmatch(cond)
    if not m:
        # Unknown condition form — fail closed (design §2.3: no arbitrary Python)
        raise TemplateError(f"unparseable condition '{condition}'")
    field, op, lit = m.group("field"), m.group("op"), m.group("lit").strip()
    # Resolve $.field against upstream_output (a dict/output envelope). `field`
    # may itself be a dotted path (e.g. "echo.score" against a script whose
    # output nests one level, such as the `workflow.examples.echo` demo
    # callable which wraps its input as {"echo": {...}}) — walk each
    # component rather than treating the whole dotted string as one flat key.
    val = upstream_output
    if isinstance(val, dict) and "output" in val and _dotted_root_present(field, val.get("output")):
        val = val["output"]
    for part in field.split("."):
        if isinstance(val, dict):
            if part not in val:
                if op == "exists":
                    return False
                raise TemplateError(f"condition field '{field}' not in upstream output")
            val = val[part]
        else:
            if op == "exists":
                return val is not None
            raise TemplateError(f"cannot index '{part}' on non-dict upstream output for '{field}'")
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


def validate_condition_syntax(condition: str) -> None:
    """Syntax-only check: does ``condition`` parse as ``$.field op literal`` or
    ``true``/``false``? Raises TemplateError if not. No upstream data needed —
    lets the verifier reject a malformed condition at compile time instead of
    only discovering it mid-run via eval_condition's own fail-closed raise."""
    cond = condition.strip()
    if cond.lower() in ("true", "false"):
        return
    if not _CONDITION_RE.fullmatch(cond):
        raise TemplateError(f"unparseable condition '{condition}'")


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