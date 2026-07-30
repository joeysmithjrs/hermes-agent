"""Named, reusable prompt bodies — the ``spec.prompt: {library: ...}`` form.

NAMING (deliberate, see POST_PHASE3_SPEC.md §2): this feature is NOT called
"template". In this codebase "template" already means ``expr.py``'s
``{{ node_id.output.field }}`` interpolation, which ``verify.py`` compile-time
checks under the error code ``TEMPLATE``. Shipping a second, unrelated
"template" concept would leave an author unable to answer "does my ``{{ }}``
resolve against the template registry or against node outputs?". So the
reusable-prompt-body feature is a *library*, its YAML key is ``library:``, and
its verifier code is ``PROMPT_LIBRARY``. Both mechanisms coexist unambiguously.

A library entry is a YAML file::

    name: desk-autonomy-brief-v1
    version: 1
    description: standing brief for the desk-autonomy council
    tags: [desk]
    param_schema: {type: object, required: [profile]}
    prompt: |
      You are the {{ params.profile }} desk reviewer.
      Prior state: {{ seed.output.summary }}

Used from a workflow::

    - id: reviewer
      kind: agent
      spec:
        prompt:
          library: desk-autonomy-brief-v1
          params:
            profile: "{{ input.profile }}"

Two-stage rendering, in this order:

1. LOAD time — ``{{ params.* }}`` is substituted from the node's ``params``
   (``expr.render_params``). Missing param -> fail closed.
2. RUN time — the resulting string goes through the ordinary ``expr.render``
   against the node's ctx, so ``{{ seed.output.summary }}`` (whether it came
   from the library body or from a param value) resolves against live run data.

This module is PURE load + render: it reads files and formats strings. It
makes no model calls, spawns no agent, and mutates no run state — the agent
node's existing ``LiveWorker`` path still does all execution, with the library
merely populating ``spec.prompt`` before ``build_child_agent``.

Search order (first match wins):
  1. ``$HERMES_HOME/workflows/prompts/<name>.yaml`` — operator-owned
  2. ``workflow/prompts/builtin/<name>.yaml``      — shipped with the package
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

__all__ = [
    "PromptLibraryError",
    "PromptLibraryEntry",
    "LIBRARY_NAME_RE",
    "validate_library_name",
    "user_library_dir",
    "builtin_library_dir",
    "library_search_paths",
    "list_libraries",
    "load_library",
    "render_library_prompt",
    "resolve_prompt_spec",
    "is_library_prompt",
]


LIBRARY_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


class PromptLibraryError(ValueError):
    """Raised for an unknown/malformed library or bad params. Fail-closed:
    there is no "prompt not found -> empty prompt" path."""


@dataclass
class PromptLibraryEntry:
    name: str
    prompt: str
    version: int = 1
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    param_schema: Optional[Dict[str, Any]] = None
    source_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tags": list(self.tags),
            "param_schema": self.param_schema,
            "source_path": self.source_path,
        }


def validate_library_name(name: Any) -> str:
    if not isinstance(name, str) or not LIBRARY_NAME_RE.match(name):
        raise PromptLibraryError(
            f"invalid prompt library name {name!r}: expected [A-Za-z0-9][A-Za-z0-9_.-]* "
            "(max 64 chars)"
        )
    return name


def user_library_dir() -> Path:
    from ..store.fs import workflows_root

    return workflows_root() / "prompts"


def builtin_library_dir() -> Path:
    return Path(__file__).resolve().parent / "builtin"


def library_search_paths() -> List[Path]:
    return [user_library_dir(), builtin_library_dir()]


def list_libraries() -> List[str]:
    """Every resolvable library name, user-owned first (they shadow builtins)."""
    seen: List[str] = []
    for d in library_search_paths():
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.yaml")):
            if p.stem not in seen:
                seen.append(p.stem)
    return seen


def load_library(name: str) -> PromptLibraryEntry:
    """Load one library entry. Unknown name -> PromptLibraryError."""
    validate_library_name(name)
    for d in library_search_paths():
        p = d / f"{name}.yaml"
        if not p.is_file():
            continue
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise PromptLibraryError(f"prompt library '{name}' ({p}) is not a YAML mapping")
        body = raw.get("prompt")
        if not isinstance(body, str) or not body.strip():
            raise PromptLibraryError(
                f"prompt library '{name}' ({p}) has no non-empty string `prompt:` field"
            )
        schema = raw.get("param_schema")
        if schema is not None and not isinstance(schema, dict):
            raise PromptLibraryError(f"prompt library '{name}' param_schema must be a mapping")
        return PromptLibraryEntry(
            name=raw.get("name") or name,
            prompt=body,
            version=int(raw.get("version", 1) or 1),
            description=raw.get("description"),
            tags=list(raw.get("tags") or []),
            param_schema=schema,
            source_path=str(p),
        )
    searched = ", ".join(str(d) for d in library_search_paths())
    raise PromptLibraryError(
        f"unknown prompt library '{name}' (searched: {searched}). Add "
        f"{name}.yaml under $HERMES_HOME/workflows/prompts/, or fix the `library:` name."
    )


def is_library_prompt(prompt: Any) -> bool:
    """True for the ``{library: <name>, params: {...}}`` prompt form."""
    return isinstance(prompt, dict) and "library" in prompt


def render_library_prompt(
    name: str, params: Optional[Dict[str, Any]] = None, *, entry: Optional[PromptLibraryEntry] = None
) -> str:
    """Load ``name`` and substitute its ``{{ params.* }}`` placeholders.

    Stage 1 of two (see module docstring): the result may still contain
    ``{{ node.output.field }}`` templates for the driver to resolve at run
    time. No model call happens here.
    """
    from ..expr import TemplateError, render_params

    lib = entry or load_library(name)
    params = params or {}
    mismatch = _validate_params(lib.param_schema, params)
    if mismatch:
        raise PromptLibraryError(
            f"prompt library '{name}' params do not match its param_schema: {mismatch}"
        )
    try:
        return render_params(lib.prompt, params)
    except TemplateError as exc:
        raise PromptLibraryError(f"prompt library '{name}': {exc}") from None


def resolve_prompt_spec(prompt: Any, *, param_renderer: Optional[Any] = None) -> Any:
    """Resolve a node's ``spec.prompt`` to a string when it is a library ref.

    A plain string (or any other form, e.g. ``{"file": ...}``) is returned
    unchanged, so this is safe to call on every agent node.

    ``param_renderer`` (the driver passes ``lambda v: expr.render(v, ctx)``)
    resolves each param VALUE against live run data before substitution, so
    ``params: {directive: "{{ seed.output.topic }}"}`` works. At compile time
    the verifier passes nothing and the raw value is substituted instead —
    which leaves any ``{{ }}`` in it visible to the TEMPLATE node-reference
    check, exactly where an author wants a typo caught.
    """
    if not is_library_prompt(prompt):
        return prompt
    name = prompt.get("library")
    params = prompt.get("params") or {}
    if not isinstance(params, dict):
        raise PromptLibraryError(
            f"prompt library '{name}': `params` must be a mapping, got {type(params).__name__}"
        )
    unknown = set(prompt) - {"library", "params"}
    if unknown:
        raise PromptLibraryError(
            f"prompt library '{name}': unknown key(s) {sorted(unknown)} "
            "(expected only `library` and `params`)"
        )
    if param_renderer is not None:
        params = {k: param_renderer(v) for k, v in params.items()}
    return render_library_prompt(name, params)


def _validate_params(param_schema: Optional[Dict[str, Any]], params: Dict[str, Any]) -> Optional[str]:
    """Same degrade-gracefully policy as the driver's output-schema check:
    real validation when ``jsonschema`` is importable, required-key checking
    otherwise. Never hard-depends on the package."""
    if not param_schema:
        return None
    try:
        import jsonschema  # type: ignore
    except Exception:
        jsonschema = None  # type: ignore
    if jsonschema is not None:
        try:
            jsonschema.validate(instance=params, schema=param_schema)
            return None
        except jsonschema.exceptions.ValidationError as exc:  # type: ignore[attr-defined]
            return str(getattr(exc, "message", exc))
        except Exception as exc:
            return f"param schema validation error: {exc}"
    for req in param_schema.get("required") or []:
        if req not in params:
            return f"missing required param '{req}'"
    return None
