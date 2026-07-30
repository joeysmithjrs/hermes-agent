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
    p.add_argument("--input", help="JSON input string (merged with --from-run's derived input, if given; explicit --input wins on key collision)")
    p.add_argument("--resume", dest="resume", help="Resume a run_id")
    p.add_argument("--from", dest="from_node", help="Resume from a node id")
    p.add_argument("--retry-failed", action="store_true", help="Re-run failed nodes on resume")
    p.add_argument("--dry-run", action="store_true", help="Compile + plan ready set without spawning")
    p.add_argument("--max-budget-usd", type=float, help="Override run budget cap")
    p.add_argument("--fake", action="store_true", help="Use the no-LLM FakeWorker instead of a live agent worker")
    p.add_argument("--from-run", dest="from_run", help="Seed --input from a prior run's final output (ergonomic inline form of `hermes workflow chain`)")
    p.add_argument("--select", help="With --from-run: dotted path into the source run's final envelope (default: the whole envelope)")
    p.add_argument("--as", dest="as_key", default="from_run", help="With --from-run: key to wrap the selected value under (default: from_run)")
    p.add_argument("--allow-incomplete", action="store_true", help="With --from-run: allow chaining off a source run that has not reached a terminal status")
    p.set_defaults(func=_cmd_run)

    # status
    p = wf_sub.add_parser("status", help="Show a run's status")
    p.add_argument("run_id")
    p.add_argument("--watch", action="store_true", help="Poll until the run reaches a terminal state (delegates to `hermes workflow watch`)")
    p.add_argument("--interval", type=float, default=2.0, help="With --watch: polling interval in seconds (default 2.0)")
    p.add_argument("--timeout", type=float, default=0.0, help="With --watch: give up after N seconds (default 0 = wait forever)")
    p.add_argument("--json", dest="as_json", action="store_true", help="With --watch: emit the final envelope as JSON instead of the human status stream")
    p.set_defaults(func=_cmd_status)

    # watch
    p = wf_sub.add_parser("watch", help="Poll a run's status until it reaches a terminal state")
    p.add_argument("run_id")
    p.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds (default 2.0)")
    p.add_argument("--timeout", type=float, default=0.0, help="Give up after N seconds (default 0 = wait forever)")
    p.add_argument("--json", dest="as_json", action="store_true", help="Emit the final envelope as JSON instead of the human status stream")
    p.set_defaults(func=_cmd_watch)

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
    p.add_argument("--cost", action="store_true", help="Show a per-run cost/token breakdown plus a TOTAL line")
    p.set_defaults(func=_cmd_list)

    # chain
    p = wf_sub.add_parser("chain", help="Start a new run seeded from a prior run's final output")
    p.add_argument("source_run_id", help="run_id to read the final envelope from")
    p.add_argument("target", metavar="TARGET_WORKFLOW_PATH", help="Path to the workflow YAML/JSON to start")
    p.add_argument("--select", help="Dotted path into the source run's final envelope (default: the whole envelope)")
    p.add_argument("--as", dest="as_key", default="from_run", help="Key to wrap the selected value under in the target's input (default: from_run)")
    p.add_argument("--input", help="Additional JSON input, merged with the derived input (explicit --input wins on key collision)")
    p.add_argument("--allow-incomplete", action="store_true", help="Allow chaining off a source run that has not reached a terminal status")
    p.add_argument("--dry-run", action="store_true", help="Compile + plan ready set without spawning")
    p.add_argument("--fake", action="store_true", help="Use the no-LLM FakeWorker instead of a live agent worker")
    p.set_defaults(func=_cmd_chain)

    # schedule
    p = wf_sub.add_parser("schedule", help="Print (or register) a cron recipe that runs a workflow on a schedule")
    p.add_argument("path", help="Path to workflow YAML/JSON")
    p.add_argument("--cron", help="Schedule, e.g. '0 9 * * *' or '30m' (default: the workflow's own `triggers: [{kind: cron, schedule: ...}]`)")
    p.add_argument("--input", help="JSON input string passed to the scheduled `hermes workflow run`")
    p.add_argument("--name", help="Job name (default: derived from the workflow id)")
    p.add_argument("--register", action="store_true", help="Actually create the cron job via `hermes cron create` (default: print-only, zero side effects)")
    p.add_argument("--fake", action="store_true", help="Pass --fake through to the scheduled `hermes workflow run`")
    p.set_defaults(func=_cmd_schedule)

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
        sys.stderr.write("usage: hermes workflow <validate|compile|run|status|watch|logs|list|cancel|doctor|gate|chain|schedule> ...\n")
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

    explicit_input: Dict[str, Any] = {}
    if args.input:
        try:
            explicit_input = json.loads(args.input)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"error: --input is not valid JSON: {e}\n")
            return EXIT_USAGE

    # --from-run: the ergonomic inline form of `hermes workflow chain`. Both
    # surfaces resolve through workflow/chain.py's resolve_chain_input() —
    # there is exactly one implementation of "read a source run, derive an
    # input dict" (see workflow/chain.py docstring).
    from_run = getattr(args, "from_run", None)
    if from_run:
        if args.resume:
            sys.stderr.write("error: --from-run cannot be combined with --resume.\n")
            return EXIT_USAGE
        from .chain import resolve_chain_input, ChainSourceNotTerminal

        try:
            input_data = resolve_chain_input(
                from_run,
                select=getattr(args, "select", None),
                as_key=getattr(args, "as_key", None) or "from_run",
                extra_input=explicit_input,
                allow_incomplete=bool(getattr(args, "allow_incomplete", False)),
            )
        except ChainSourceNotTerminal as e:
            sys.stderr.write(f"error: {e}\n")
            return EXIT_RUNTIME
        except FileNotFoundError as e:
            sys.stderr.write(f"error: {e}\n")
            return EXIT_RUNTIME
    else:
        input_data = explicit_input

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
    if getattr(args, "watch", False):
        return _run_watch(
            args.run_id,
            interval=getattr(args, "interval", 2.0),
            timeout=getattr(args, "timeout", 0.0),
            as_json=getattr(args, "as_json", False),
        )
    env = status(args.run_id)
    sys.stdout.write(json.dumps(env, indent=2, default=str) + "\n")
    return EXIT_OK


# Terminal for the WATCH loop's purposes: nothing further happens without
# external action (a resume, a gate decision, an operator). Deliberately
# broader than chain.py's DONE_STATUSES, which asks a stricter question
# ("is the final envelope safe to treat as final data") — see that module's
# docstring for why the two sets differ.
_WATCH_TERMINAL_STATUSES = {"succeeded", "failed", "partial", "cancelled", "paused", "awaiting_gate"}


def _cmd_watch(args) -> int:
    return _run_watch(args.run_id, interval=args.interval, timeout=args.timeout, as_json=args.as_json)


def _run_watch(run_id: str, *, interval: float, timeout: float, as_json: bool) -> int:
    """Poll `status(run_id)` until it reaches a terminal state, then exit.

    Never executes nodes — this is read-only polling of the checkpointed
    RunState (FS-authoritative via `status()`). Interrupt-safe: a
    KeyboardInterrupt prints the last known status and returns 130 instead of
    a traceback.
    """
    from . import status as _status
    import time

    start = time.monotonic()
    last_sig: Optional[tuple] = None
    last_env: Optional[Dict[str, Any]] = None
    try:
        while True:
            env = _status(run_id)
            last_env = env
            st = env.get("status")
            sig = (
                st,
                len(env.get("succeeded") or []),
                len(env.get("failed") or []),
                len(env.get("skipped") or []),
            )
            # Human mode: print only when status or the succeeded/failed/
            # skipped counts actually change, so a long watch doesn't spam.
            if not as_json and sig != last_sig:
                cost = float(env.get("cost_usd", 0) or 0.0)
                sys.stdout.write(
                    f"[{run_id}] status={st} succeeded={sig[1]} failed={sig[2]} "
                    f"skipped={sig[3]} cost=${cost:.4f}\n"
                )
                sys.stdout.flush()
            last_sig = sig
            if st in _WATCH_TERMINAL_STATUSES:
                break
            if timeout and (time.monotonic() - start) >= timeout:
                sys.stderr.write(
                    f"error: still running after {timeout:g}s (run_id={run_id}, last status={st})\n"
                )
                return EXIT_RUNTIME
            time.sleep(max(0.05, interval))
    except KeyboardInterrupt:
        st = last_env.get("status") if last_env else "unknown"
        sys.stderr.write(f"interrupted; last known status={st}\n")
        return 130

    if as_json:
        sys.stdout.write(json.dumps(last_env, indent=2, default=str) + "\n")
    final_status = last_env.get("status")
    if final_status == "succeeded":
        return EXIT_OK
    if final_status == "awaiting_gate":
        return EXIT_GATE
    return EXIT_RUNTIME


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
    if not getattr(args, "cost", False):
        # Default output format is byte-identical to pre-Phase-3: tests parse it.
        for r in rows:
            sys.stdout.write(f"{r['run_id']}\t{r['status']}\t{r['workflow_id']}\t{r.get('started') or ''}\t${r.get('cost_usd', 0):.4f}\n")
        return EXIT_OK

    # --cost: per-run cost/token breakdown + a TOTAL line. Cost is already
    # indexed in sqlite; tokens are not (the F11 index is a cost-only cache),
    # so pull them per-run from the FS-backed run record via status(). A run
    # whose token counters are unavailable (old checkpoint, sqlite-only
    # fallback, or the read simply fails) degrades to "-" — never a fake 0,
    # which would misreport "this run used no tokens".
    from . import status as _status
    total_cost = 0.0
    total_in = 0
    total_out = 0
    any_unknown = False
    for r in rows:
        cost = float(r.get("cost_usd", 0) or 0.0)
        total_cost += cost
        tin: Optional[int] = None
        tout: Optional[int] = None
        try:
            env = _status(r["run_id"])
            tin = env.get("tokens_in")
            tout = env.get("tokens_out")
        except Exception:
            pass
        if tin is None or tout is None:
            any_unknown = True
        else:
            total_in += tin
            total_out += tout
        sys.stdout.write(
            f"{r['run_id']}\t{r['status']}\t{r['workflow_id']}\t{r.get('started') or ''}\t"
            f"${cost:.4f}\ttokens_in={tin if tin is not None else '-'}\ttokens_out={tout if tout is not None else '-'}\n"
        )
    suffix = "+ (some runs missing token data)" if any_unknown else ""
    sys.stdout.write(f"TOTAL\t\t\t\t${total_cost:.4f}\ttokens_in={total_in}{suffix}\ttokens_out={total_out}{suffix}\n")
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


def _cmd_chain(args) -> int:
    """`hermes workflow chain SOURCE_RUN_ID TARGET_WORKFLOW_PATH ...`

    Thin translation into the exact same code path as `hermes workflow run
    --from-run` (both bottom out in workflow/chain.py's resolve_chain_input,
    and both are handled here by _cmd_run) — no duplicated enable-gate,
    worker-construction, or dry-run logic between the two CLI surfaces.
    """
    run_args = argparse.Namespace(
        path_or_id=args.target,
        input=args.input,
        resume=None,
        from_node=None,
        retry_failed=False,
        dry_run=args.dry_run,
        max_budget_usd=None,
        fake=args.fake,
        from_run=args.source_run_id,
        select=args.select,
        as_key=args.as_key,
        allow_incomplete=args.allow_incomplete,
    )
    return _cmd_run(run_args)


def _cmd_schedule(args) -> int:
    """`hermes workflow schedule WORKFLOW_PATH --cron "SCHEDULE" ...`

    A THIN wrapper that emits (or, with --register, actually creates) a cron
    recipe calling `hermes workflow run`. This is NOT a second scheduler:
    there is no polling loop and no state of its own — `--register` shells
    out once to the existing `hermes cron` CLI (cron/jobs.py create_job via
    hermes_cli.subcommands.cron) and then this function is done.
    """
    import re
    import shlex

    from . import compile_file
    from .ir import WorkflowRejected

    # Validate the workflow compiles BEFORE printing/registering anything —
    # scheduling a workflow that can't compile is a guaranteed 3am failure.
    try:
        vir = compile_file(args.path)
    except WorkflowRejected as e:
        for i in e.issues:
            if i.severity == "error":
                sys.stderr.write(f"REJECT [{i.code}] {i.node}: {i.message}\n")
        return EXIT_VERIFY

    cron_schedule = args.cron
    if not cron_schedule:
        # Fall back to the workflow's own `triggers: [{kind: cron, schedule: ...}]`.
        for t in vir.ir.triggers:
            if t.kind == "cron" and t.schedule:
                cron_schedule = t.schedule
                break
    if not cron_schedule:
        sys.stderr.write(
            "error: no --cron given and the workflow declares no "
            "`triggers: [{kind: cron, schedule: ...}]`.\n"
        )
        return EXIT_USAGE

    if args.input:
        try:
            json.loads(args.input)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"error: --input is not valid JSON: {e}\n")
            return EXIT_USAGE

    workflow_path = str(Path(args.path).resolve())
    name = args.name or f"workflow-{vir.ir.id}"

    # Review HIGH #3: `name` is interpolated into a FILESYSTEM PATH below
    # (`$HERMES_HOME/scripts/<name>.sh`), and `Path.__truediv__` happily
    # accepts `..` components and absolute paths. `--name ../../foo` therefore
    # wrote the generated wrapper script outside $HERMES_HOME entirely. The
    # downstream `hermes cron create` rejected the traversal so no job was
    # registered, but the file had already been written by then — a partial
    # failure that still drops an executable script at an attacker-chosen
    # path. Validate before the value is ever used as a path component.
    if (
        not name
        or name in (".", "..")
        or any(sep in name for sep in ("/", "\\"))
        or Path(name).is_absolute()
        or name.startswith(".")
    ):
        sys.stderr.write(
            f"error: --name {name!r} is not a valid job name. It becomes a script "
            "filename under $HERMES_HOME/scripts/, so it must not contain path "
            "separators, start with '.', or be an absolute path.\n"
        )
        return EXIT_USAGE

    # The exact `hermes workflow run` invocation the scheduled job will make.
    run_argv = [sys.executable, "-m", "hermes_cli.main", "workflow", "run", workflow_path]
    if args.input:
        run_argv += ["--input", args.input]
    if args.fake:
        run_argv.append("--fake")
    run_cmd_str = shlex.join(run_argv)

    script_name = f"{name}.sh"
    script_body = "#!/usr/bin/env bash\nset -euo pipefail\n" + run_cmd_str + "\n"

    # A 5(-or-6)-field whitespace-separated schedule is plausibly raw cron
    # syntax and gets a literal crontab line too; hermes' own natural-language
    # schedules ("30m", "every 2h") have no honest crontab equivalent — printing
    # one anyway would be exactly the kind of guaranteed-wrong 3am recipe this
    # command exists to prevent, so it is intentionally omitted for those.
    is_cron_syntax = bool(re.match(r"^\s*(\S+\s+){4,5}\S+\s*$", cron_schedule))

    if not args.register:
        sys.stdout.write(f"# hermes cron recipe for workflow={vir.ir.id}\n")
        sys.stdout.write(f"# 1) save the script (chmod +x) to $HERMES_HOME/scripts/{script_name}:\n")
        for line in script_body.splitlines():
            sys.stdout.write(f"#   {line}\n")
        sys.stdout.write("# 2) register it:\n")
        sys.stdout.write(
            f"hermes cron create {shlex.quote(cron_schedule)} --script {shlex.quote(script_name)} "
            f"--no-agent --name {shlex.quote(name)}\n"
        )
        if is_cron_syntax:
            sys.stdout.write("# equivalent raw crontab line (if you'd rather not use `hermes cron`):\n")
            sys.stdout.write(f"# {cron_schedule} {run_cmd_str}\n")
        else:
            sys.stdout.write(
                f"# note: {cron_schedule!r} is a hermes-native schedule spec, not raw cron syntax — "
                "no crontab-equivalent line is shown; use `hermes cron create` above.\n"
            )
        return EXIT_OK

    # --register: actually create the job by shelling out to the existing
    # `hermes cron` CLI (real flags confirmed in hermes_cli/subcommands/cron.py
    # + cron/jobs.py create_job: schedule, --script, --no-agent, --name all
    # exist today). `--script` requires the file to already live under
    # $HERMES_HOME/scripts/, so write it there first.
    try:
        from hermes_constants import get_hermes_home

        scripts_dir = get_hermes_home() / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script_path = scripts_dir / script_name
        # Review HIGH #3, belt and braces: the `--name` validation above is the
        # real gate, but it lives ~70 lines from this write. Re-assert
        # containment against the RESOLVED path immediately before writing, so
        # this stays safe if the name ever reaches here by another route.
        if scripts_dir.resolve() not in script_path.resolve().parents:
            sys.stderr.write(
                f"error: refusing to write the generated job script outside "
                f"{scripts_dir} (resolved to {script_path.resolve()}).\n"
            )
            return EXIT_USAGE
        script_path.write_text(script_body, encoding="utf-8")
        try:
            script_path.chmod(0o755)
        except OSError:
            pass  # best-effort (e.g. unsupported on some Windows filesystems)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(
            f"error: could not write the cron script under $HERMES_HOME/scripts/: {e}\n"
            "falling back to print-only — nothing was registered.\n"
        )
        return EXIT_RUNTIME

    import subprocess

    cron_argv = [
        sys.executable, "-m", "hermes_cli.main", "cron", "create", cron_schedule,
        "--script", script_name, "--no-agent", "--name", name,
    ]
    try:
        result = subprocess.run(cron_argv, capture_output=True, text=True)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"error: failed to shell out to `hermes cron create`: {e}\n")
        return EXIT_RUNTIME
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.returncode != 0:
        if result.stderr:
            sys.stderr.write(result.stderr)
        sys.stderr.write("error: `hermes cron create` failed; the job was not registered.\n")
        return EXIT_RUNTIME
    return EXIT_OK
