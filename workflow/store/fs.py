"""$HERMES_HOME/workflows paths + atomic writes (design §4.3, api §9).

Filesystem is the source of truth; sqlite is an index. Atomic writes use
temp-file + os.replace for crash safety. Node bodies are keyed by
``node_run_id`` (F1) — NEVER bare ``node_id``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

from hermes_constants import get_hermes_home

__all__ = [
    "workflows_root",
    "definitions_dir",
    "definition_path",
    "runs_dir",
    "run_dir",
    "node_output_path",
    "node_events_path",
    "run_json_path",
    "checkpoint_path",
    "run_output_path",
    "gate_signal_path",
    "index_path",
    "atomic_write",
    "atomic_write_json",
    "read_json",
    "ensure_run_dirs",
]


def workflows_root() -> Path:
    return get_hermes_home() / "workflows"


def definitions_dir() -> Path:
    return workflows_root() / "definitions"


def definition_path(workflow_id: str) -> Path:
    return definitions_dir() / f"{workflow_id}.json"


def runs_dir() -> Path:
    return workflows_root() / "runs"


def run_dir(run_id: str) -> Path:
    return runs_dir() / run_id


def node_dir(run_id: str, node_run_id: str) -> Path:
    return run_dir(run_id) / "nodes" / node_run_id


def node_output_path(run_id: str, node_run_id: str) -> Path:
    return node_dir(run_id, node_run_id) / "output.json"


def node_events_path(run_id: str, node_run_id: str) -> Path:
    return node_dir(run_id, node_run_id) / "events.jsonl"


def run_json_path(run_id: str) -> Path:
    return run_dir(run_id) / "run.json"


def checkpoint_path(run_id: str) -> Path:
    return run_dir(run_id) / "checkpoint.json"


def run_output_path(run_id: str) -> Path:
    return run_dir(run_id) / "run_output.json"


def gate_signal_path(run_id: str, gate_id: str) -> Path:
    return run_dir(run_id) / "gate_signals" / f"{gate_id}.json"


def index_path() -> Path:
    return workflows_root() / "index.sqlite"


def artifacts_dir(run_id: str) -> Path:
    return run_dir(run_id) / "artifacts"


def ensure_run_dirs(run_id: str) -> Path:
    rd = run_dir(run_id)
    (rd / "nodes").mkdir(parents=True, exist_ok=True)
    (rd / "artifacts").mkdir(parents=True, exist_ok=True)
    (rd / "gate_signals").mkdir(parents=True, exist_ok=True)
    return rd


def atomic_write(path: Path, data: bytes) -> None:
    """Write bytes to path atomically: temp file in same dir + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=path.suffix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    data = json.dumps(obj, indent=2, default=str).encode("utf-8")
    atomic_write(path, data)


def read_json(path: Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def store_node_output(run_id: str, node_run_id: str, output: Any) -> Path:
    """Store a node's output value keyed by node_run_id (F1)."""
    p = node_output_path(run_id, node_run_id)
    atomic_write_json(p, output)
    return p


def append_event(run_id: str, node_run_id: str, event: Dict[str, Any]) -> None:
    """Append a tool-names-only event (no arg secrets) to events.jsonl."""
    p = node_events_path(run_id, node_run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, default=str) + "\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)