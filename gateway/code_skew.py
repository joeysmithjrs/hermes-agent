"""Detect when the gateway is running stale code after a hot ``git pull``.

The gateway is a single long-lived process; its ``sys.modules`` is frozen at
boot. If the checkout is updated underneath it (a manual ``git pull``, or the
window before ``hermes update``'s graceful restart fires), a first-time lazy
import on a new code path can resolve a freshly-pulled consumer module against a
stale cached dependency -> ImportError (see
``tests/test_stale_utils_module_import.py`` for the exact failure).

We snapshot the checkout revision at gateway startup and compare on demand, so
risky callers (e.g. ``/model`` switching) can refuse with a clear "restart the
gateway" message instead of crashing on a cryptic import error.

If the revision can't be read (non-git install, IO error), the boot snapshot
stays ``None`` and skew detection no-ops — it never produces a false positive.
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_boot_fingerprint: str | None = None


def _fingerprint() -> str | None:
    """Current checkout fingerprint, reusing the CLI's git-rev reader.

    ``hermes_cli.main`` is always already imported in a gateway process (it's
    the entry point), so this import is free and avoids duplicating the
    worktree-aware ref resolution.
    """
    try:
        from hermes_cli.main import _read_git_revision_fingerprint

        return _read_git_revision_fingerprint(_PROJECT_ROOT)
    except Exception:
        return None


def record_boot_fingerprint() -> None:
    """Snapshot the checkout revision at gateway startup (idempotent)."""
    global _boot_fingerprint
    if _boot_fingerprint is None:
        _boot_fingerprint = _fingerprint()


def _short(fingerprint: str) -> str:
    """Render a ``git:<ref>:<sha>`` fingerprint as a compact label."""
    sha = fingerprint.rsplit(":", 1)[-1]
    if sha and sha != "unresolved" and len(sha) > 10:
        return sha[:10]
    return sha or fingerprint


def current_disk_rev_short(project_root: Path | None = None) -> str | None:
    """Return the checkout's CURRENT revision (short form), independent of
    any recorded boot fingerprint.

    Unlike :func:`detect_code_skew` (which compares against THIS process's
    own boot snapshot), this is a stateless "what does disk say right now"
    read. Out-of-process callers that don't share the gateway's boot
    snapshot — ``hermes update``'s post-restart verification, ``hermes
    doctor``, ``hermes gateway status`` — use this to compute the
    "expected" revision to compare a gateway's persisted
    ``code_boot_rev`` (see ``gateway/status.py::write_runtime_status``)
    against.
    """
    try:
        from hermes_cli.main import _read_git_revision_fingerprint

        current = _read_git_revision_fingerprint(project_root or _PROJECT_ROOT)
    except Exception:
        return None
    return _short(current) if current else None


def detect_code_skew() -> tuple[str, str] | None:
    """Return ``(boot_rev, disk_rev)`` short labels if the checkout drifted
    since boot, else ``None``."""
    if _boot_fingerprint is None:
        return None
    current = _fingerprint()
    if current is None or current == _boot_fingerprint:
        return None
    return _short(_boot_fingerprint), _short(current)


def boot_fingerprint_short() -> str | None:
    """Return this process's own boot revision (short form), or ``None``.

    Used by callers that want to *publish* what this gateway booted on
    (e.g. ``write_runtime_status(code_boot_rev=...)``) rather than compare
    it against the current checkout.
    """
    if _boot_fingerprint is None:
        return None
    return _short(_boot_fingerprint)


def skew_help_message(boot_rev: str, disk_rev: str) -> str:
    """Render a detailed, actionable code-skew message.

    Shared by the reactive ``/model`` guard (``slash_commands._model_switch_
    skew_guard``) and the proactive background watcher's critical log line,
    so a user sees the same wording (and the same recovery commands)
    regardless of which path caught the drift. Includes a ``git log`` range
    so an operator can see exactly what changed underneath the process,
    not just that *something* did.
    """
    return (
        f"This gateway is running code from {boot_rev} but the checkout on "
        f"disk is now {disk_rev}. Switching models would risk a stale-module "
        f"crash — restart the gateway to load the new code: hermes gateway restart\n"
        f"  See what changed:  git log {boot_rev}..{disk_rev} --oneline\n"
        f"  Or restart directly:  systemctl --user restart hermes-gateway  "
        f"(or: launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway)"
    )
