"""workflow — code/YAML -> verified IR -> deterministic driver -> checkpointed runs.

Public API (api §8):
    compile_file, run, status, resume, cancel, decide_gate, Workflow, verify
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .ir import (
    Gate,
    Node,
    NodeSpec,
    Edge,
    Trigger,
    WorkflowIR,
    Issue,
    VerifiedIR,
    WorkflowRejected,
)
from .yaml_load import load_yaml_file
from .verify import verify, verify_ir
from .config import load_workflow_config
from .runtime.driver import run as _run, resume as _resume, Driver
from .runtime.worker import Worker, FakeWorker, DelegateWorker
from .store import fs, index, checkpoint
from .dsl import Workflow, node, gate, expr  # stub (Phase 2)

__all__ = [
    "compile_file",
    "compile_text",
    "run",
    "status",
    "resume",
    "cancel",
    "decide_gate",
    "verify",
    "verify_ir",
    "Workflow",
    "node",
    "gate",
    "expr",
    "WorkflowIR",
    "VerifiedIR",
    "WorkflowRejected",
    "Issue",
    "Node",
    "NodeSpec",
    "Edge",
    "Gate",
    "Trigger",
    "Worker",
    "FakeWorker",
    "DelegateWorker",
]


def compile_file(path: str, *, phase1_warn_overrides: Optional[bool] = None) -> VerifiedIR:
    """Load YAML/JSON from path, verify it, return a VerifiedIR (or raise WorkflowRejected).

    ``phase1_warn_overrides``: if None, read from config (workflow.phase1_warn_overrides).
    """
    p = Path(path)
    if p.suffix in (".json",):
        data = json.loads(p.read_text(encoding="utf-8"))
        ir = WorkflowIR.from_dict(data)
    else:
        ir = load_yaml_file(path)
    if phase1_warn_overrides is None:
        cfg = load_workflow_config()
        phase1_warn_overrides = bool(cfg.get("phase1_warn_overrides", False))
    vir = verify_ir(ir, phase1_warn_overrides=phase1_warn_overrides)
    # stamp content hash
    ir.hash = ir.content_hash()
    # persist the verified definition
    fs.definitions_dir().mkdir(parents=True, exist_ok=True)
    fs.atomic_write_json(fs.definition_path(ir.id), vir.to_dict())
    return vir


def compile_text(text: str, *, phase1_warn_overrides: bool = False) -> VerifiedIR:
    """Compile YAML text (convenience for tests). Persists the definition so resume works."""
    from .yaml_load import load_yaml

    ir = load_yaml(text)
    vir = verify_ir(ir, phase1_warn_overrides=phase1_warn_overrides)
    ir.hash = ir.content_hash()
    # persist the verified definition (resume re-loads from here)
    fs.definitions_dir().mkdir(parents=True, exist_ok=True)
    fs.atomic_write_json(fs.definition_path(ir.id), vir.to_dict())
    return vir


def run(
    vir_or_path: Any,
    *,
    input: Optional[Dict[str, Any]] = None,
    worker: Optional[Worker] = None,
    dry_run: bool = False,
    max_parallel_nodes: Optional[int] = None,
    max_budget_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """Execute a verified IR (or a path to one) with a worker (default FakeWorker)."""
    cfg = load_workflow_config()
    if not dry_run and not cfg.get("enabled", False):
        # HERMES_WORKFLOW_FAKE=1 forces FakeWorker but still requires enabled for live runs;
        # for test/CLI smoke we allow FakeWorker when HERMES_WORKFLOW_FAKE set.
        import os

        if not os.environ.get("HERMES_WORKFLOW_FAKE"):
            raise PermissionError(
                "workflow is disabled (config workflow.enabled: false). "
                "Enable it in config.yaml or use `hermes workflow validate/compile`."
            )
    if isinstance(vir_or_path, (str, Path)):
        vir = compile_file(str(vir_or_path))
    else:
        vir = vir_or_path
    worker = worker or FakeWorker()
    return _run(
        vir,
        input=input,
        worker=worker,
        dry_run=dry_run,
        max_parallel_nodes=max_parallel_nodes or int(cfg.get("max_parallel_nodes", 4)),
        max_budget_usd=max_budget_usd if max_budget_usd is not None else float(cfg.get("max_budget_usd", 10.0)),
    )


def status(run_id: str) -> Dict[str, Any]:
    """Read a run's RunEnvelope. FS (run.json) is authoritative; sqlite is a fast path."""
    rec = checkpoint.load_run_record(run_id)
    if not rec:
        # fall back to sqlite index
        row = index.get_run(run_id)
        if row:
            return {
                "run_id": row["run_id"],
                "workflow_id": row["workflow_id"],
                "status": row["status"],
                "started_at": row["started"],
                "ended_at": row["ended"],
                "cost_usd": row["cost_usd"],
            }
        raise FileNotFoundError(f"no run {run_id}")
    # build envelope from state
    from .runtime.driver import RunState
    state = RunState.from_dict(rec)
    return {
        "run_id": state.run_id,
        "attempt_id": state.attempt_id,
        "workflow_id": state.workflow_id,
        "status": state.status,
        "succeeded": list(state.succeeded),
        "failed": list(state.failed),
        "skipped": list(state.skipped),
        "awaiting_gate": state.awaiting_gate,
        "started_at": state.started_at,
        "ended_at": state.ended_at,
        "cost_usd": state.cost_usd,
    }


def resume(run_id: str, *, worker: Optional[Worker] = None, retry_failed: bool = False, from_node: Optional[str] = None) -> Dict[str, Any]:
    # Gate the lib path the same way run() does (review P2): otherwise a
    # programmatic caller could resume+execute runs while workflow.enabled=false.
    import os

    cfg = load_workflow_config()
    if not cfg.get("enabled", False) and not os.environ.get("HERMES_WORKFLOW_FAKE"):
        raise PermissionError(
            "workflow is disabled (config workflow.enabled: false); resume cannot execute nodes."
        )
    return _resume(run_id, worker=worker, retry_failed=retry_failed, from_node=from_node)


def cancel(run_id: str) -> Dict[str, Any]:
    """Mark a run cancelled (writes the run record)."""
    rec = checkpoint.load_run_record(run_id)
    if not rec:
        raise FileNotFoundError(f"no run {run_id}")
    rec["status"] = "cancelled"
    rec["ended_at"] = _now()
    checkpoint.write_run_record(run_id, rec)
    index.upsert_run(run_id, rec.get("workflow_id", ""), "cancelled", ended=rec["ended_at"], cost_usd=float(rec.get("cost_usd", 0.0) or 0.0))
    return {"run_id": run_id, "status": "cancelled"}


def decide_gate(run_id: str, gate_id: str, decision: str, *, note: str = "") -> Dict[str, Any]:
    """Record a gate decision. Phase 1 stub: writes the signal, returns Phase-2 note.

    The gate *runtime* (unparking the driver) is a Phase 2 deliverable; this
    records the decision on disk so Phase 2 can resume from it.
    """
    if decision not in ("approve", "shelve", "modify"):
        raise ValueError(f"invalid gate decision '{decision}' (approve|shelve|modify)")
    fs.atomic_write_json(fs.gate_signal_path(run_id, gate_id), {"gate_id": gate_id, "decision": decision, "note": note, "status": "decided"})
    return {"run_id": run_id, "gate_id": gate_id, "decision": decision, "note": note, "phase2": "gate runtime unpark is a Phase 2 deliverable"}


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())