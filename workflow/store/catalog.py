"""Persistent workflow registry — ``$HERMES_HOME/workflows/catalog/``.

A catalog entry is a named, versioned, parameterizable *recipe*: the YAML text
of a workflow plus the metadata needed to find and run it again later. It sits
next to ``store/fs.py`` (run artifacts) and ``store/index.py`` (the sqlite run
index) and touches neither — registering a workflow can never perturb a run.

Layout::

    workflows/catalog/index.yaml              <- one line per (id, version)
    workflows/catalog/<id>/version_<v>.yaml   <- immutable snapshot of the YAML

Registration SNAPSHOTS the source file rather than storing a pointer to it.
A pointer would mean "run the recipe I registered" silently becomes "run
whatever is at that path today" — the version number would then be a label on
something that can change under it. The original path is kept as
``source_path`` for provenance only; nothing ever reads it back.

Security: an entry stores YAML text, never Python. ``run_catalog`` compiles it
through the ordinary ``workflow.compile_text`` -> ``verify_ir`` path, so a
registered recipe is subject to exactly the same verifier rules (F3 gating, F4
``run:`` allowlist, F5 cardinality...) as a hand-run file. Ids are validated so
they cannot escape the catalog root.

Parameterization: a recipe may contain ``{{ params.<key> }}`` placeholders,
substituted by ``render_params`` BEFORE compile. Only that one root is touched
— ordinary ``{{ node.output.field }}`` templates pass through untouched for the
driver to resolve at run time. An unknown/missing param fails closed rather
than rendering an empty string.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .fs import atomic_write, workflows_root

__all__ = [
    "CatalogError",
    "CatalogEntry",
    "CATALOG_ID_RE",
    "catalog_root",
    "catalog_index_path",
    "catalog_entry_dir",
    "catalog_version_path",
    "validate_catalog_id",
    "load_index",
    "list_entries",
    "get_entry",
    "next_version",
    "register",
    "load_version_text",
    "render_params",
    "validate_params",
]


# Same shape as a workflow id: a directory name on every supported platform.
CATALOG_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


class CatalogError(ValueError):
    """Raised for an invalid catalog id, a missing entry, or bad params."""


class CatalogEntry(dict):
    """A catalog index row. A plain dict subclass so it serializes to YAML/JSON
    unchanged; the attribute accessors are convenience for call sites."""

    @property
    def id(self) -> str:
        return self["id"]

    @property
    def version(self) -> int:
        return int(self["version"])

    @property
    def tags(self) -> List[str]:
        return list(self.get("tags") or [])


# ---- paths -----------------------------------------------------------------


def catalog_root() -> Path:
    return workflows_root() / "catalog"


def catalog_index_path() -> Path:
    return catalog_root() / "index.yaml"


def validate_catalog_id(catalog_id: Any) -> str:
    if not isinstance(catalog_id, str) or not CATALOG_ID_RE.match(catalog_id):
        raise CatalogError(
            f"invalid catalog id {catalog_id!r}: expected [A-Za-z0-9][A-Za-z0-9_.-]* "
            "(max 64 chars)"
        )
    return catalog_id


def catalog_entry_dir(catalog_id: str) -> Path:
    return catalog_root() / validate_catalog_id(catalog_id)


def catalog_version_path(catalog_id: str, version: int) -> Path:
    v = int(version)
    if v < 1:
        raise CatalogError(f"invalid catalog version {version!r}: must be >= 1")
    return catalog_entry_dir(catalog_id) / f"version_{v}.yaml"


# ---- index -----------------------------------------------------------------


def load_index() -> List[CatalogEntry]:
    """Read the catalog index. A missing index is an empty catalog, not an
    error — the first ``register`` creates it."""
    p = catalog_index_path()
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if isinstance(raw, list):  # tolerate a bare list form
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get("entries") or []
    else:
        raise CatalogError(f"malformed catalog index at {p}: expected a mapping or list")
    return [CatalogEntry(r) for r in rows if isinstance(r, dict)]


def _write_index(entries: List[Dict[str, Any]]) -> None:
    body = yaml.safe_dump({"entries": entries}, sort_keys=False, allow_unicode=True)
    atomic_write(catalog_index_path(), body.encode("utf-8"))


def list_entries(
    *, tags: Optional[List[str]] = None, catalog_id: Optional[str] = None, latest_only: bool = False
) -> List[CatalogEntry]:
    """Entries matching the filters, sorted by (id, version).

    ``tags`` matches entries carrying ALL of the given tags. ``latest_only``
    collapses each id to its highest version.
    """
    rows = load_index()
    if catalog_id:
        rows = [r for r in rows if r.get("id") == catalog_id]
    if tags:
        want = set(tags)
        rows = [r for r in rows if want.issubset(set(r.get("tags") or []))]
    rows.sort(key=lambda r: (str(r.get("id")), int(r.get("version", 0))))
    if latest_only:
        best: Dict[str, CatalogEntry] = {}
        for r in rows:
            rid = str(r.get("id"))
            if rid not in best or int(r.get("version", 0)) > int(best[rid].get("version", 0)):
                best[rid] = r
        rows = [best[k] for k in sorted(best)]
    return rows


def get_entry(catalog_id: str, version: Optional[int] = None) -> CatalogEntry:
    """One entry. ``version=None`` means the highest registered version."""
    validate_catalog_id(catalog_id)
    rows = [r for r in load_index() if r.get("id") == catalog_id]
    if not rows:
        raise CatalogError(
            f"no catalog entry '{catalog_id}' (register one with "
            "`hermes workflow register --id <id> --from-file <path>`)"
        )
    if version is None:
        return max(rows, key=lambda r: int(r.get("version", 0)))
    for r in rows:
        if int(r.get("version", 0)) == int(version):
            return r
    known = sorted(int(r.get("version", 0)) for r in rows)
    raise CatalogError(f"catalog entry '{catalog_id}' has no version {version} (have: {known})")


def next_version(catalog_id: str) -> int:
    rows = [r for r in load_index() if r.get("id") == catalog_id]
    return (max((int(r.get("version", 0)) for r in rows), default=0)) + 1


# ---- register / load -------------------------------------------------------


def register(
    catalog_id: str,
    source_path: str,
    *,
    tags: Optional[List[str]] = None,
    owner: Optional[str] = None,
    description: Optional[str] = None,
    param_schema: Optional[Dict[str, Any]] = None,
) -> CatalogEntry:
    """Snapshot ``source_path``'s YAML as the next version of ``catalog_id``.

    The source is parsed (``yaml.safe_load``) purely to reject a file that
    isn't a YAML mapping before it lands in the catalog — a recipe that cannot
    possibly compile has no business being registered. It is stored VERBATIM
    (comments and formatting intact), not re-serialized.
    """
    validate_catalog_id(catalog_id)
    src = Path(source_path)
    if not src.exists():
        raise CatalogError(f"no such workflow file: {source_path}")
    text = src.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise CatalogError(f"{source_path} is not a YAML mapping; cannot register it as a workflow")
    if param_schema is not None and not isinstance(param_schema, dict):
        raise CatalogError("param_schema must be a JSON Schema object (mapping)")

    version = next_version(catalog_id)
    dest = catalog_version_path(catalog_id, version)
    atomic_write(dest, text.encode("utf-8"))

    entry = CatalogEntry(
        {
            "id": catalog_id,
            "version": version,
            "workflow_id": parsed.get("id") or parsed.get("workflow"),
            "path": str(dest.relative_to(workflows_root())).replace("\\", "/"),
            "source_path": str(src),
            "description": description,
            "owner": owner,
            "tags": sorted(set(tags or [])),
            "param_schema": param_schema,
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    rows = [dict(r) for r in load_index()]
    rows.append(dict(entry))
    _write_index(rows)
    return entry


def load_version_text(catalog_id: str, version: Optional[int] = None) -> str:
    """The registered YAML text for an entry (the immutable snapshot)."""
    entry = get_entry(catalog_id, version)
    p = catalog_version_path(catalog_id, entry.version)
    if not p.exists():
        raise CatalogError(
            f"catalog entry '{catalog_id}' v{entry.version} is indexed but its snapshot "
            f"is missing at {p} (catalog corrupt; re-register it)"
        )
    return p.read_text(encoding="utf-8")


# ---- params ----------------------------------------------------------------


def render_params(text: str, params: Optional[Dict[str, Any]]) -> str:
    """Substitute ``{{ params.<key> }}`` placeholders in a recipe.

    ONLY that root is touched: ``{{ seed.output.topic }}`` and friends are left
    for the driver to resolve at run time. A placeholder with no matching param
    raises rather than rendering empty — a workflow silently missing its market
    or budget is worse than one that refuses to start.

    The substitution itself lives in ``expr.render_params`` (shared with the
    prompt library); this wrapper only re-raises as a ``CatalogError`` so a bad
    ``--params`` reads as a catalog problem to the CLI.
    """
    from ..expr import TemplateError, render_params as _render_params

    try:
        return _render_params(text, params)
    except TemplateError as exc:
        raise CatalogError(str(exc)) from None


def validate_params(param_schema: Optional[Dict[str, Any]], params: Optional[Dict[str, Any]]) -> Optional[str]:
    """Check ``params`` against an entry's ``param_schema``.

    Returns None when it matches (or there is nothing to check), else a short
    message. Uses ``jsonschema`` when importable and degrades to a required-key
    /type check otherwise — the same "never hard-depend on a new package"
    policy as the driver's output-schema validation.
    """
    if not param_schema:
        return None
    params = params or {}
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
