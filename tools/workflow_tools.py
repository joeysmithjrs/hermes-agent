"""
Conversation surface for the `workflow` package (Phase 2/3, default OFF).

Three read/dispatch tools — `workflow_run`, `workflow_status`, and (Phase 3)
`workflow_chain` — let an agent start, inspect, and sequence code-defined
workflows from chat. All are gated by
``check_workflow_tool_requirements`` on ``workflow.tool_enabled`` AND
``workflow.enabled`` in config.yaml, so they are absent from the schema unless
the operator has opted in twice. The gate is a ``check_fn``, which the registry
evaluates when it assembles the session's tool definitions — the toolset is
therefore fixed at session start and never mutates mid-turn (no prompt-cache
thrash).

Deliberately NOT exposed: gate decisions. An agent that can approve its own
gates is not a gate — approvals stay on `hermes workflow gate` and the
human-authenticated surfaces.

Also deliberately NOT exposed: a blocking `watch`. Polling until a run reaches
a terminal state is an operator affordance (`hermes workflow watch`), not a
chat one — a tool call that blocks for minutes burns the turn and tells the
model nothing it cannot get by calling `workflow_status` again later.
"""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def check_workflow_tool_requirements() -> bool:
    """True only when the operator enabled BOTH the runner and its tool surface.

    Reads config through ``workflow.config.load_workflow_config`` (which goes
    via ``hermes_cli.config.load_config``) — never a raw yaml.safe_load of
    config.yaml. Any failure means "not available", so a broken/absent config
    hides the tools rather than exposing an unconfigured runner.
    """
    try:
        from workflow.config import load_workflow_config

        cfg = load_workflow_config()
        return bool(cfg.get("tool_enabled", False)) and bool(cfg.get("enabled", False))
    except Exception as e:  # noqa: BLE001 - availability probe must never raise
        logger.debug("workflow tool availability check failed: %s", e)
        return False


WORKFLOW_RUN_SCHEMA = {
    "name": "workflow_run",
    "description": (
        "Start a code-defined multi-agent workflow from a YAML/JSON definition, "
        "or resume a parked/crashed run. Workflows are deterministic graphs of "
        "agent/script/fanout/join/gate nodes; the driver — not an LLM — owns "
        "control flow. Runs cost real tokens: each agent node spawns a child "
        "agent. Use dry_run first to see the plan without spawning anything. "
        "A run that stops with status 'awaiting_gate' is waiting for a HUMAN "
        "approval and cannot be advanced from this tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the workflow YAML/JSON definition. Required unless resuming.",
            },
            "resume": {
                "type": "string",
                "description": "run_id of an existing run to resume from its checkpoint instead of starting a new one.",
            },
            "input": {
                "type": "object",
                "description": "Input mapping made available to nodes as {{ input.* }}.",
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Compile + verify + report the initial ready set WITHOUT spawning any "
                    "node. Costs nothing. Only valid for a fresh run — a resume has no "
                    "plan-only mode and combining the two is rejected."
                ),
            },
            "retry_failed": {
                "type": "boolean",
                "description": "On resume: re-run nodes that previously failed.",
            },
        },
        "required": [],
    },
}

WORKFLOW_STATUS_SCHEMA = {
    "name": "workflow_status",
    "description": (
        "Inspect workflow runs. action='get' returns one run's envelope "
        "(status, succeeded/failed/skipped node ids, cost, awaiting_gate); "
        "action='list' returns recent runs. Read-only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "list"],
                "description": "'get' one run (needs run_id) or 'list' recent runs.",
            },
            "run_id": {"type": "string", "description": "Run id for action='get'."},
            "status": {"type": "string", "description": "For action='list': filter by run status."},
            "workflow_id": {"type": "string", "description": "For action='list': filter by workflow id."},
            "limit": {"type": "integer", "description": "For action='list': max rows (default 20)."},
        },
        "required": [],
    },
}


WORKFLOW_CHAIN_SCHEMA = {
    "name": "workflow_chain",
    "description": (
        "Start a new workflow run whose input is derived from a FINISHED run's "
        "final envelope (trigger-chaining: run B consumes run A's result). "
        "Costs real tokens — the new run's agent nodes spawn child agents. "
        "Use dry_run to see the plan without spawning. Refuses to chain off a "
        "run that is still in progress or parked at a gate, because that "
        "snapshot is not final yet."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source_run_id": {
                "type": "string",
                "description": "run_id of the FINISHED run to take input from.",
            },
            "path": {
                "type": "string",
                "description": "Path to the target workflow YAML/JSON definition to start.",
            },
            "select": {
                "type": "string",
                "description": (
                    "Dotted path into the source run's final envelope to pass along "
                    "(e.g. 'succeeded' or 'cost_usd'). Omit to pass the whole envelope. "
                    "A path that does not resolve passes null rather than failing."
                ),
            },
            "as_key": {
                "type": "string",
                "description": "Input key to bind the selected value to (default 'from_run').",
            },
            "input": {
                "type": "object",
                "description": "Extra input keys, merged last (they win on collision).",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Compile + report the ready set WITHOUT spawning. Costs nothing.",
            },
        },
        "required": ["source_run_id", "path"],
    },
}


def _err(message: str) -> str:
    return json.dumps({"error": message})


def workflow_run(
    path: Optional[str] = None,
    resume: Optional[str] = None,
    input: Optional[Dict[str, Any]] = None,  # noqa: A002 - matches the IR's field name
    dry_run: bool = False,
    retry_failed: bool = False,
) -> str:
    """Start or resume a workflow run. Returns the RunEnvelope as JSON."""
    if not check_workflow_tool_requirements():
        return _err(
            "workflow tools are disabled. Set workflow.enabled: true and "
            "workflow.tool_enabled: true in config.yaml."
        )
    if not path and not resume:
        return _err("Provide 'path' (a workflow definition) or 'resume' (a run_id).")
    if dry_run and resume:
        # Never silently drop dry_run here: the schema promises "costs
        # nothing", and an agent trusting that promise would otherwise
        # trigger a real, billable resume with real side effects.
        return _err(
            "dry_run is not supported with resume — a resume has no plan-only mode and "
            "would execute live nodes. Use workflow_status(action='get', run_id=...) to "
            "inspect the run instead."
        )

    try:
        import workflow as wf
    except Exception as e:  # noqa: BLE001
        return _err(f"workflow package unavailable: {e}")

    try:
        if resume:
            env = wf.resume(resume, retry_failed=bool(retry_failed))
        else:
            env = wf.run(path, input=dict(input or {}), dry_run=bool(dry_run))
    except PermissionError as e:
        return _err(str(e))
    except FileNotFoundError as e:
        return _err(f"not found: {e}")
    except Exception as e:  # noqa: BLE001 - surface verifier/runtime errors as tool errors
        return _err(f"workflow run failed: {e}")

    if isinstance(env, dict) and env.get("status") == "awaiting_gate":
        env = dict(env)
        env["note"] = (
            "This run is parked awaiting HUMAN approval. It cannot be advanced "
            "from chat — a person must run: hermes workflow gate "
            f"{env.get('run_id')} {env.get('awaiting_gate')} --decide approve"
        )
    return json.dumps(env, indent=2, default=str)


def workflow_status(
    action: str = "get",
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    workflow_id: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Read a run envelope, or list recent runs. Returns JSON."""
    if not check_workflow_tool_requirements():
        return _err(
            "workflow tools are disabled. Set workflow.enabled: true and "
            "workflow.tool_enabled: true in config.yaml."
        )
    try:
        import workflow as wf
    except Exception as e:  # noqa: BLE001
        return _err(f"workflow package unavailable: {e}")

    if action == "list":
        try:
            from workflow.store import index

            rows = index.list_runs(status=status, workflow_id=workflow_id, limit=int(limit or 20))
        except Exception as e:  # noqa: BLE001
            return _err(f"could not list runs: {e}")
        return json.dumps({"runs": rows}, indent=2, default=str)

    if not run_id:
        return _err("action='get' requires 'run_id'.")
    try:
        return json.dumps(wf.status(run_id), indent=2, default=str)
    except FileNotFoundError:
        return _err(f"no run {run_id}")
    except Exception as e:  # noqa: BLE001
        return _err(f"could not read run {run_id}: {e}")


def workflow_chain(
    source_run_id: Optional[str] = None,
    path: Optional[str] = None,
    select: Optional[str] = None,
    as_key: str = "from_run",
    input: Optional[Dict[str, Any]] = None,  # noqa: A002 - matches the IR's field name
    dry_run: bool = False,
) -> str:
    """Start a run whose input is derived from a finished run. Returns JSON."""
    if not check_workflow_tool_requirements():
        return _err(
            "workflow tools are disabled. Set workflow.enabled: true and "
            "workflow.tool_enabled: true in config.yaml."
        )
    if not source_run_id or not path:
        return _err("workflow_chain requires both 'source_run_id' and 'path'.")

    try:
        import workflow as wf
        from workflow.chain import ChainSourceNotTerminal, resolve_chain_input
    except Exception as e:  # noqa: BLE001
        return _err(f"workflow package unavailable: {e}")

    try:
        # allow_incomplete is deliberately NOT exposed to the model. Overriding
        # the terminal-status guard is an operator decision made with the run
        # in front of them (`hermes workflow chain --allow-incomplete`); an
        # agent doing it from chat would be freezing a snapshot the source run
        # itself still considers unfinished.
        chained_input = resolve_chain_input(
            source_run_id,
            select=select,
            as_key=as_key or "from_run",
            extra_input=dict(input or {}),
        )
    except ChainSourceNotTerminal as e:
        return _err(
            f"{e} Wait for it to finish (workflow_status(action='get', "
            f"run_id='{source_run_id}')), then chain."
        )
    except FileNotFoundError:
        return _err(f"no run {source_run_id}")
    except Exception as e:  # noqa: BLE001
        return _err(f"could not build chained input from {source_run_id}: {e}")

    try:
        env = wf.run(path, input=chained_input, dry_run=bool(dry_run))
    except PermissionError as e:
        return _err(str(e))
    except FileNotFoundError as e:
        return _err(f"not found: {e}")
    except Exception as e:  # noqa: BLE001
        return _err(f"chained workflow run failed: {e}")

    if isinstance(env, dict):
        env = dict(env)
        env["chained_from"] = source_run_id
        if env.get("status") == "awaiting_gate":
            env["note"] = (
                "This run is parked awaiting HUMAN approval. It cannot be advanced "
                "from chat — a person must run: hermes workflow gate "
                f"{env.get('run_id')} {env.get('awaiting_gate')} --decide approve"
            )
    return json.dumps(env, indent=2, default=str)


# --- Registry ---
# The `workflow` toolset is in no default bundle in toolsets.py; combined with
# the check_fn above, the tools stay out of every schema until the operator
# opts in. No run_agent.py / toolsets.py edits are needed — tools/registry.py
# auto-discovers any tools/*.py that calls registry.register().
from tools.registry import registry  # noqa: E402

registry.register(
    name="workflow_run",
    toolset="workflow",
    schema=WORKFLOW_RUN_SCHEMA,
    handler=lambda args, **kw: workflow_run(
        path=args.get("path"),
        resume=args.get("resume"),
        input=args.get("input"),
        dry_run=args.get("dry_run", False),
        retry_failed=args.get("retry_failed", False),
    ),
    check_fn=check_workflow_tool_requirements,
    emoji="🔀",
)

registry.register(
    name="workflow_status",
    toolset="workflow",
    schema=WORKFLOW_STATUS_SCHEMA,
    handler=lambda args, **kw: workflow_status(
        action=args.get("action", "get"),
        run_id=args.get("run_id"),
        status=args.get("status"),
        workflow_id=args.get("workflow_id"),
        limit=args.get("limit", 20),
    ),
    check_fn=check_workflow_tool_requirements,
    emoji="🔎",
)

registry.register(
    name="workflow_chain",
    toolset="workflow",
    schema=WORKFLOW_CHAIN_SCHEMA,
    handler=lambda args, **kw: workflow_chain(
        source_run_id=args.get("source_run_id"),
        path=args.get("path"),
        select=args.get("select"),
        as_key=args.get("as_key", "from_run"),
        input=args.get("input"),
        dry_run=args.get("dry_run", False),
    ),
    check_fn=check_workflow_tool_requirements,
    emoji="⛓️",
)
