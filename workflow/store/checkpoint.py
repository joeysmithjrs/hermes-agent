"""Atomic checkpoint commit (design §4.3). temp-file + os.replace."""

from __future__ import annotations

from typing import Any, Dict

from . import fs

__all__ = ["commit_checkpoint", "load_checkpoint", "load_run_record", "write_run_record"]


def commit_checkpoint(run_id: str, state: Dict[str, Any]) -> None:
    """Atomically write the checkpoint (the commit point of a run step)."""
    fs.atomic_write_json(fs.checkpoint_path(run_id), state)


def load_checkpoint(run_id: str) -> Dict[str, Any]:
    return fs.read_json(fs.checkpoint_path(run_id), default={}) or {}


def write_run_record(run_id: str, record: Dict[str, Any]) -> None:
    fs.atomic_write_json(fs.run_json_path(run_id), record)


def load_run_record(run_id: str) -> Dict[str, Any]:
    return fs.read_json(fs.run_json_path(run_id), default={}) or {}