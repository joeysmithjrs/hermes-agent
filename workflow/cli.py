"""hermes workflow subcommands (api §4). Soft-imported from hermes_cli/main.py.

Exit codes: 0 ok / 1 runtime fail / 2 verify reject / 3 usage / 4 gate awaiting.
validate/compile/doctor always work regardless of workflow.enabled; run refuses
unless enabled (or HERMES_WORKFLOW_FAKE=1 for smoke).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_VERIFY = 2
EXIT_USAGE = 3
EXIT_GATE = 4

__all__ = ["register_subparser", "cmd_workflow"]


def register_subparser(subparsers) -> None:
    """Register the `hermes workflow` subcommand tree. Called from main.py soft-import."""
    wf_parser = subparsers.add_parser(
        "workflow",
        help="Compile, verify, and run multi-agent workflows (default-off)",
        description=(
            "Workflows are code-defined multi-agent orchestrations: code/YAML -> "
            "verified IR -> deterministic driver -> checkpointed runs. Default-off "
            "(config workflow.enabled: false). validate/compile/doctor always work."
        ),
    )
    wf_sub = wf_parser.add_subparsers(dest="workflow_command")

    # validate
    p = wf_sub.add_parser("validate", help="Verify a workflow YAML/JSON (exit 0 ok / 2 reject)")
    p.add_argument("path", help="Path to workflow YAML/JSON")
    # store_const + default=None so the config-driven `phase1_warn_overrides`
    # fallback in _cmd_validate is actually reached when the flag is absent
    # (a bare store_true yields False, which would shadow the config flag).
    p.add_argument("--warn-overrides", dest="warn_overrides", action="store_const", const=True, default=None, help="Treat Phase 1 override-only fields as warnings")
    p.set_defaults(func=_cmd_validate)

    # compile
    p = wf_sub.add_parser("compile", help="Verify + write a compiled IR JSON")
    p.add_argument("path", help="Path to workflow YAML/JSON")
    p.add_argument("-o", "--out", help="Output path (default: $HERMES_HOME/workflows/definitions/<id>.json)")
    p.add_argument("--warn-overrides", dest="warn_overrides", action="store_const", const=True, default=None, help="Treat Phase 1 override-only fields as warnings")
    p.set_defaults(func=_cmd_compile)

    # run
    p = wf_sub.add_parser("run", help="Run a workflow (refuses if workflow.enabled: false)")
    p.add_argument("path_or_id", help="Path to workflow YAML/JSON, or a run_id to resume")
    p.add_argument("--input", help="JSON input string")
    p.add_argument("--resume", dest="resume", help="Resume a run_id")
    p.add_argument("--from", dest="from_node", help="Resume from a node id")
    p.add_argument("--retry-failed", action="store_true", help="Re-run failed nodes on resume")
    p.add_argument("--dry-run", action="store_true", help="Compile + plan ready set without spawning")
    p.add_argument("--max-budget-usd", type=float, help="Override run budget cap")
    p.add_argument("--fake", action="store_true", help="Use the no-LLM FakeWorker instead of a live agent worker")
    p.set_defaults(func=_cmd_run)

    # status
    p = wf_sub.add_parser("status", help="Show a run's status")
    p.add_argument("run_id")
    p.add_argument("--watch", action="store_true", help="Tail the event log (Phase 3 polish)")
    p.set_defaults(func=_cmd_status)

    # logs
    p = wf_sub.add_parser("logs", help="Show a run's event logs")
    p.add_argument("run_id")
    p.add_argument("--node", help="Filter to a node id")
    p.add_argument("--follow", action="store_true", help="Tail (Phase 3 polish)")
    p.set_defaults(func=_cmd_logs)

    # list
    p = wf_sub.add_parser("list", help="List runs")
    p.add_argument("--status", dest="status_filter", help="Filter by status")
    p.add_argument("--workflow", dest="workflow_id", help="Filter by workflow id")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=_cmd_list)

    # cancel
    p = wf_sub.add_parser("cancel", help="Cancel a run")
    p.add_argument("run_id")
    p.set_defaults(func=_cmd_cancel)

    # doctor
    p = wf_sub.add_parser("doctor", help="Check workflow store + rebuild sqlite index")
    p.set_defaults(func=_cmd_doctor)

    # gate
    p = wf_sub.add_parser("gate", help="Decide a gate (Phase 2 runtime; records decision)")
    p.add_argument("run_id")
    p.add_argument("gate_id")
    p.add_argument("--decide", required=True, choices=["approve", "shelve", "modify"])
    p.add_argument("--note", default="")
    p.set_defaults(func=_cmd_gate)

    wf_parser.set_defaults(func=cmd_workflow)


# ---- dispatch --------------------------------------------------------------


def cmd_workflow(args) -> int:
    """Top-level dispatch when no sub-subcommand matched."""
    if not getattr(args, "workflow_command", None):
        sys.stderr.write("usage: hermes workflow <validate|compile|run|status|logs|list|cancel|doctor|gate> ...\n")
        return EXIT_USAGE
    fn = getattr(args, "func", None)
    if fn is None:
        sys.stderr.write("usage: hermes workflow <subcommand> ...\n")
        return EXIT_USAGE
    try:
        return fn(args) or EXIT_OK
    except _VerifyExit as e:
        sys.stderr.write(str(e) + "\n")
        return EXIT_VERIFY
    except PermissionError as e:
        sys.stderr.write(f"error: {e}\n")
        return EXIT_RUNTIME
    except FileNotFoundError as e:
        sys.stderr.write(f"error: {e}\n")
        return EXIT_RUNTIME
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"error: {e}\n")
        return EXIT_RUNTIME


class _VerifyExit(Exception):
    pass


# ---- subcommands -----------------------------------------------------------


def _cmd_validate(args) -> int:
    from . import compile_file
    from .ir import WorkflowRejected
    warn = args.warn_overrides
    if warn is None:
        from .config import load_workflow_config
        warn = bool(load_workflow_config().get("phase1_warn_overrides", False))
    try:
        vir = compile_file(args.path, phase1_warn_overrides=warn)
    except WorkflowRejected as e:
        sys.stderr.write(f"REJECTED\n")
        for i in e.issues:
            tag = "ERROR" if i.severity == "error" else "WARN"
            sys.stderr.write(f"  [{tag}] {i.code} {i.node or '?'}: {i.message}\n")
        return EXIT_VERIFY
    except FileNotFoundError as e:
        sys.stderr.write(f"error: {e}\n")
        return EXIT_RUNTIME
    warnings = vir.issues
    sys.stdout.write(f"OK  workflow={vir.ir.id} hash={vir.ir.hash}\n")
    for w in warnings:
        sys.stdout.write(f"  [WARN] {w.code} {w.node or '?'}: {w.message}\n")
    return EXIT_OK


def _cmd_compile(args) -> int:
    from . import compile_file
    from .ir import WorkflowRejected
    warn = args.warn_overrides
    if warn is None:
        from .config import load_workflow_config
        warn = bool(load_workflow_config().get("phase1_warn_overrides", False))
    try:
        vir = compile_file(args.path, phase1_warn_overrides=warn)
    except WorkflowRejected as e:
        for i in e.issues:
            if i.severity == "error":
                sys.stderr.write(f"REJECT [{i.code}] {i.node}: {i.message}\n")
        return EXIT_VERIFY
    out = args.out
    if not out:
        from .store import fs
        out = str(fs.definition_path(vir.ir.id))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(vir.to_json(), encoding="utf-8")
    sys.stdout.write(f"compiled -> {out}\n")
    return EXIT_OK


def _cmd_run(args) -> int:
    from . import run, resume, status
    from .config import load_workflow_config

    cfg = load_workflow_config()
    enabled = bool(cfg.get("enabled", False)) or bool(os.environ.get("HERMES_WORKFLOW_FAKE"))
    if not enabled and not args.dry_run:
        sys.stderr.write("error: workflow is disabled (workflow.enabled: false). Set enabled: true in config.yaml.\n")
        return EXIT_RUNTIME

    # --dry-run is only meaningful for a fresh run: resume() has no plan-only
    # mode (it mutates checkpoint state — gate unpark, retry resets — before
    # the walk). Silently dropping the flag here would be the worst outcome:
    # the operator asks for a free preview and gets a real, billable resume.
    # Refuse the combination loudly instead.
    if args.dry_run and args.resume:
        sys.stderr.write(
            "error: --dry-run cannot be combined with --resume. A resume has no "
            "plan-only mode; running it would execute live nodes. Use "
            "`hermes workflow status <run_id>` to inspect a run without executing it.\n"
        )
        return EXIT_USAGE

    input_data: Dict[str, Any] = {}
    if args.input:
        try:
            input_data = json.loads(args.input)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"error: --input is not valid JSON: {e}\n")
            return EXIT_USAGE

    # Skip worker construction for a plan-only invocation. Note that run()
    # also skips its own worker default when dry_run is set, so the runtime
    # provider/credential path is never touched for a --dry-run.
    worker = None
    if not args.dry_run:
        fake_requested = bool(getattr(args, "fake", False)) or bool(os.environ.get("HERMES_WORKFLOW_FAKE"))
        if fake_requested:
            from .runtime.worker import FakeWorker

            worker = FakeWorker()
            sys.stderr.write("workflow: using FakeWorker (no live LLM)\n")
        else:
            from .runtime.live import LiveWorker

            worker = LiveWorker()
            sys.stderr.write(
                "workflow: using LiveWorker (live agent nodes; model inherited from runtime unless spec.model is set)\n"
            )

    if args.resume:
        env = resume(
            args.resume,
            worker=worker,
            retry_failed=args.retry_failed,
            from_node=args.from_node,
            max_budget_usd=args.max_budget_usd,
        )
    else:
        env = run(
            args.path_or_id,
            input=input_data,
            worker=worker,
            dry_run=args.dry_run,
            max_budget_usd=args.max_budget_usd,
        )
    sys.stdout.write(json.dumps(env, indent=2, default=str) + "\n")
    if env.get("status") == "awaiting_gate":
        return EXIT_GATE
    return EXIT_OK


def _cmd_status(args) -> int:
    from . import status
    env = status(args.run_id)
    sys.stdout.write(json.dumps(env, indent=2, default=str) + "\n")
    return EXIT_OK


def _cmd_logs(args) -> int:
    from .store import fs
    run_id = args.run_id
    rd = fs.run_dir(run_id)
    if not rd.exists():
        sys.stderr.write(f"error: no run {run_id}\n")
        return EXIT_RUNTIME
    found = False
    nodes_dir = rd / "nodes"
    if nodes_dir.exists():
        for nr_dir in sorted(nodes_dir.iterdir()):
            if not nr_dir.is_dir():
                continue
            ev = nr_dir / "events.jsonl"
            if not ev.exists():
                continue
            if args.node:
                # match node_id via the envelope/node naming
                if args.node not in nr_dir.name:
                    continue
            for line in ev.read_text(encoding="utf-8").splitlines():
                sys.stdout.write(f"{nr_dir.name}: {line}\n")
                found = True
    if not found:
        sys.stdout.write("(no events)\n")
    return EXIT_OK


def _cmd_list(args) -> int:
    from .store import index
    rows = index.list_runs(status=args.status_filter, workflow_id=args.workflow_id, limit=args.limit)
    if not rows:
        sys.stdout.write("(no runs)\n")
        return EXIT_OK
    for r in rows:
        sys.stdout.write(f"{r['run_id']}\t{r['status']}\t{r['workflow_id']}\t{r.get('started') or ''}\t${r.get('cost_usd', 0):.4f}\n")
    return EXIT_OK


def _cmd_cancel(args) -> int:
    from . import cancel
    res = cancel(args.run_id)
    sys.stdout.write(json.dumps(res, indent=2, default=str) + "\n")
    return EXIT_OK


def _cmd_doctor(args) -> int:
    from .store import fs, index
    root = fs.workflows_root()
    sys.stdout.write(f"workflows root: {root}\n")
    sys.stdout.write(f"  exists: {root.exists()}\n")
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        sys.stdout.write("  created.\n")
    # rebuild index from FS (FS is source of truth)
    n = index.rebuild_index()
    sys.stdout.write(f"  index rebuilt: {n} runs indexed (sqlite at {fs.index_path()})\n")
    # verify hermes_state hardening import (F11)
    try:
        from hermes_state import apply_wal_with_fallback, _on_disk_journal_mode, _apply_macos_checkpoint_barrier
        sys.stdout.write("  hermes_state sqlite hardening: OK (F11 reuse confirmed)\n")
    except ImportError as e:
        sys.stdout.write(f"  hermes_state sqlite hardening: MISSING ({e})\n")
        return EXIT_RUNTIME
    return EXIT_OK


def _cmd_gate(args) -> int:
    from . import decide_gate
    res = decide_gate(args.run_id, args.gate_id, args.decide, note=args.note)
    sys.stdout.write(json.dumps(res, indent=2, default=str) + "\n")
    sys.stdout.write(
        f"decision recorded; run `hermes workflow run --resume {args.run_id}` to continue past the gate\n"
    )
    return EXIT_OK
