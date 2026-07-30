"""Named persistent workspaces — ``$HERMES_HOME/workflows/workspaces/<name>/``.

A workspace is an ACTIVE (read+write) directory a workflow's agent nodes share,
deliberately separate from two things it is easy to confuse it with:

* ``store/fs.py`` run artifacts (``workflows/runs/<run_id>/``) are the AUDIT
  record — written by the driver, read-only to agents, never mutated after the
  fact. A workspace is the opposite: agents write into it, and what they write
  is expected to change.
* ``workflows/catalog/`` holds registered workflow recipes, not run data.

Layout::

    workflows/workspaces/<name>/            <- persistent, shared across runs
        seed.md                             <- (author-managed) cross-run seed
        protocol_state.yaml                 <- (agent-managed) living state
        runs/<run_id>/                      <- per-run scratch, isolated

Everything at the workspace top level survives every run, which is the whole
point: a run seeds the next one by leaving a file behind rather than by
threading prompt text through the CLI. ``runs/<run_id>/`` gives each run a
private corner, and a later run can read a previous run's corner (see
``previous_run_ids``) — cross-run review is a feature here, not a leak, because
a workspace is named and opted into by the workflow author.

Isolation: workspaces live under ``HERMES_HOME``, so a different profile (a
different ``HERMES_HOME``) sees a different set of workspaces — the same
per-profile boundary ``cron/jobs.py`` relies on. Within a home, names are
validated (``validate_workspace_name``) and every path built here is
containment-checked, so a workspace name or a relative path can never escape
the workspaces root.

Prompt-caching note: nothing in this module is read at system-prompt
construction time. The driver exposes workspace PATHS (never file bodies) to a
node's template ctx, and agents read the files with their ordinary file tools at
run time — so a workspace file changing between runs cannot invalidate a cached
prompt prefix.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .fs import workflows_root

__all__ = [
    "WorkspaceError",
    "WORKSPACE_NAME_RE",
    "validate_workspace_name",
    "workspaces_root",
    "workspace_dir",
    "workspace_runs_dir",
    "workspace_run_dir",
    "ensure_workspace",
    "previous_run_ids",
    "top_level_entries",
    "resolve_in_workspace",
    "workspace_context",
]


# Deliberately narrow: lowercase alnum plus ``_``/``-``, first char alnum. A
# workspace name becomes a directory name on every platform we support, so it
# excludes anything that is a path separator, a drive letter, a Windows
# reserved character, a leading dot, or `..`.
WORKSPACE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class WorkspaceError(ValueError):
    """Raised for an invalid workspace name or an out-of-workspace path."""


def validate_workspace_name(name: Any) -> str:
    """Return ``name`` if it is a legal workspace name, else raise.

    Fail-closed on purpose: an unvalidated name reaches ``Path`` joining, and
    ``"../../.."`` or an absolute path there would write outside HERMES_HOME.
    """
    if not isinstance(name, str) or not WORKSPACE_NAME_RE.match(name):
        raise WorkspaceError(
            f"invalid workspace name {name!r}: expected [a-z0-9][a-z0-9_-]* "
            "(max 64 chars, lowercase)"
        )
    return name


def workspaces_root() -> Path:
    return workflows_root() / "workspaces"


def workspace_dir(name: str) -> Path:
    return workspaces_root() / validate_workspace_name(name)


def workspace_runs_dir(name: str) -> Path:
    return workspace_dir(name) / "runs"


def workspace_run_dir(name: str, run_id: str) -> Path:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        raise WorkspaceError(f"invalid run_id {run_id!r} for a workspace run directory")
    return workspace_runs_dir(name) / run_id


def ensure_workspace(name: str, run_id: Optional[str] = None) -> Path:
    """Create the workspace (and, given a ``run_id``, its per-run subdir).

    Idempotent — re-entering an existing workspace is the normal case (that is
    what "persistent" means), so this never clears anything.
    """
    wdir = workspace_dir(name)
    wdir.mkdir(parents=True, exist_ok=True)
    if run_id:
        rdir = workspace_run_dir(name, run_id)
        rdir.mkdir(parents=True, exist_ok=True)
    return wdir


def previous_run_ids(name: str, *, exclude: Optional[str] = None) -> List[str]:
    """Run ids that previously used this workspace, oldest-first by mtime.

    ``exclude`` (normally the current run) is dropped so a run never lists
    itself as its own history.
    """
    runs = workspace_runs_dir(name)
    if not runs.is_dir():
        return []
    entries = [p for p in runs.iterdir() if p.is_dir() and p.name != exclude]
    entries.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return [p.name for p in entries]


def top_level_entries(name: str) -> List[str]:
    """Names of the workspace's persistent top-level entries (``runs/``
    excluded — that is per-run scratch, listed via ``previous_run_ids``).

    Names only, never contents: this feeds a node's template ctx, and file
    bodies must not travel through prompt construction (see module docstring).
    """
    wdir = workspace_dir(name)
    if not wdir.is_dir():
        return []
    return sorted(p.name for p in wdir.iterdir() if p.name != "runs")


def resolve_in_workspace(name: str, relative: str, *, run_id: Optional[str] = None) -> Path:
    """Resolve ``relative`` inside the workspace (or its per-run subdir).

    Containment-checked: a path that escapes the workspace root raises rather
    than resolving. Callers get a real ``Path``, so this is Windows-safe and
    caller-agnostic about separators.
    """
    base = workspace_run_dir(name, run_id) if run_id else workspace_dir(name)
    if relative is None or str(relative) == "":
        return base
    candidate = (base / str(relative)).resolve()
    base_resolved = base.resolve() if base.exists() else base.absolute()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        raise WorkspaceError(
            f"path {relative!r} escapes workspace {name!r} ({base_resolved})"
        ) from None
    return candidate


def workspace_context(name: str, run_id: Optional[str] = None) -> Dict[str, Any]:
    """The ctx block the driver exposes to nodes as ``{{ workspace.* }}``.

    Paths and names only — never file contents. ``previous_runs`` lets an agent
    review earlier runs of the same workspace by path, which is the cross-run
    continuity the workspace exists to provide.
    """
    ctx: Dict[str, Any] = {
        "name": validate_workspace_name(name),
        "dir": str(workspace_dir(name)),
        "files": top_level_entries(name),
        "previous_runs": previous_run_ids(name, exclude=run_id),
    }
    if run_id:
        ctx["run_dir"] = str(workspace_run_dir(name, run_id))
        ctx["run_id"] = run_id
    return ctx
