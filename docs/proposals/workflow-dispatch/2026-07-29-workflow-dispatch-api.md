# Workflow Dispatch — Public API Spec

**Companion to:** `2026-07-29-workflow-dispatch-design.md` (authority)  
**Status:** Draft · 2026-07-29  
**Audience:** Implementers and agent authors (including Hermes itself generating workflows).

---

## 1. Package layout (import surface)

```
workflow/                          # NEW top-level package (repo-root sibling to agent/, tools/, cron/)
  __init__.py                      # exports Workflow, node, gate, expr, compile_*, verify, run, status
  ir.py                            # dataclasses: WorkflowIR, Node, Edge, NodeSpec, Gate, Trigger, ...
  dsl.py                           # Python DSL builder
  yaml_load.py                     # YAML → IR
  verify.py                        # Verifier / linter
  runtime/
    driver.py                      # Driver main loop
    worker.py                      # AIAgent construction / delegate_task shim
    scripts.py                     # sandboxed script runner
    gates.py                       # gate park + signal handlers
    events.py                      # event log append helpers
  store/
    fs.py                          # $HERMES_HOME/workflows paths, atomic writes
    index.py                       # sqlite run registry
    checkpoint.py                  # checkpoint commit
  cli.py                           # hermes workflow subcommands
  tool.py                          # optional conversation tool (default-off)
  schemas/                         # JSON schemas for envelopes
    node_run_envelope.json
    run_envelope.json
    error_object.json
```

Install: ordinary package import inside Hermes tree. No new pip dep beyond PyYAML if not already present.

---

## 2. JSON / IR schemas

### 2.1 WorkflowIR

```jsonc
{
  "id": "pm_desk",                 // stable workflow id
  "version": 1,
  "name": "Prediction Markets Desk",
  "hash": "sha256:…",              // content hash of compiled IR (set by compiler)
  "defaults": { /* NodeSpec partial */ },
  "nodes": [ /* Node */ ],
  "edges": [ /* Edge */ ],
  "triggers": [ /* Trigger */ ],
  "gates": { "freeze": { /* Gate */ } },
  "max_budget_usd": 5.0,
  "notify": { "channel": "telegram", "on": ["gate","failed","completed"] }
}
```

### 2.2 Node

```jsonc
{
  "id": "prepare",                 // [a-z][a-z0-9_]{0,63}
  "kind": "agent",                 // agent|script|fanout|join|gate|map|cron_trigger|webhook_trigger
  "spec": { /* NodeSpec */ },      // required for agent; optional for others
  "run": "pm_desk.seed_branches",  // dotted callable for script|join reducer
  "over": "{{ seed.branches }}",   // fanout/map source path
  "from": ["directive"],           // join upstreams (or use edges)
  "reduce": { "type": "top_k", "k": 3 },
  "branch": { /* Node nested — agent/script template for fanout/map */ },
  "ports": ["pass", "fail"],       // optional named outputs
  "attempts": 1,                   // re-exec budget
  "idempotent": true
}
```

### 2.3 Edge

```jsonc
{
  "from": "dq",
  "to": "dd",
  "port": "pass",                  // optional: only if upstream emits this port
  "condition": "$.candidates.length > 0"   // optional tiny expr
}
```

### 2.4 NodeSpec (per-agent granularity)

```jsonc
{
  "model": "anthropic/claude-sonnet-4",
  "provider": null,                // derived if omitted
  "prompt": "…",                   // string or { "file": "prompts/prepare.md" }
  "skills": ["polymarket"],
  "tools": ["web_search", "read_file"],
  "deny_tools": [],
  "profile": null,                 // optional profile name → HERMES_HOME slice
  "timeout_s": 1200,
  "max_turns": 40,
  "budget_usd": 0.5,
  "workspace": { "kind": "scratch|dir", "path": null },
  "input": { "desk_state": "{{ prepare.output }}" },
  "output": { "$ref": "schemas/desk_state.json" }
}
```

### 2.5 Gate

```jsonc
{
  "id": "freeze",
  "channel": "telegram",
  "approvers": ["joe"],
  "timeout_s": 86400,
  "on_timeout": "shelve",          // shelve|block|approve_auto
  "dual_control": true,
  "notify": true
}
```

### 2.6 Triggers

```jsonc
// Cron
{ "kind": "cron", "schedule": "0 7 * * *", "input": {} }

// Webhook
{ "kind": "webhook", "name": "pm-desk-start", "events": ["manual", "source.alert"],
  "secret_env": "WF_PM_DESK_SECRET" }

// Manual / tool / CLI
{ "kind": "manual" }
```

### 2.7 NodeRunEnvelope / RunEnvelope

See design §2.6. Canonical JSON Schema files live under `workflow/schemas/`.

### 2.8 ErrorObject

```jsonc
{
  "code": "NODE_TIMEOUT|BUDGET|SCHEMA|SCRIPT|AGENT|GATE_TIMEOUT|CANCELLED|VERIFY",
  "message": "human readable",
  "retriable": true,
  "details": {}
}
```

### 2.9 Expression language (edges / templates)

Minimal subset:

| Form | Meaning |
|------|---------|
| `{{ node_id.field }}` | template pop from envelope / output |
| `{{ node_id.output.x }}` | nested |
| `$.field op literal` | condition on **immediate upstream** output (`op` ∈ `==,!=,>,>=,<,<=,in,exists`) |
| `true` / `false` | literal |

No arbitrary Python in conditions. Script nodes handle rich logic.

---

## 3. Python DSL

```python
# workflow/dsl.py — public names
from workflow import Workflow, node, gate, expr as _

with Workflow("example", defaults=node.agent(model="openrouter/x-ai/grok-4.5")) as wf:
    a = wf.agent("research", prompt="…", tools=["web_search"], output=SCHEMA)
    b = wf.script("pack", run="pkg.fn", input={"memo": "{{ research.output }}"})
    g = wf.gate("approve", channel="telegram", approvers=["owner"])
    a.then(b).then(g)
    c = wf.agent("publish", prompt="…")
    g.on("approve", c)            # port-routed edge
    g.on("shelve", wf.script("log_shelve", run="pkg.shelve"))
    wf.trigger_cron("0 * * * *")

ir = wf.compile()                 # VerifiedIR | raises WorkflowRejected
ir.to_json_path("definitions/example.json")
```

### Builder methods (required)

| Method | Returns |
|--------|---------|
| `wf.agent(id, **NodeSpec)` | NodeRef |
| `wf.script(id, run=…, **kw)` | NodeRef |
| `wf.fanout(id, over=…, branch=…)` | NodeRef |
| `wf.join(id, from_=…, reduce=…, run=…)` | NodeRef |
| `wf.map(id, over=…, branch=…, reduce=…)` | NodeRef |
| `wf.gate(id, **Gate)` | GateRef |
| `NodeRef.then(other, port=None, when=None)` | NodeRef |
| `GateRef.on(port, target)` | NodeRef |
| `wf.trigger_cron(schedule, input=None)` | self |
| `wf.trigger_webhook(name, events=…)` | self |
| `wf.compile()` | `VerifiedIR` |
| `Workflow.load_yaml(path).compile()` | `VerifiedIR` |

---

## 4. CLI (`hermes workflow …`)

Registration: soft-import `workflow.cli` from `hermes_cli/main.py` behind try/except so missing package never breaks Hermes.

```
hermes workflow validate PATH.yaml|PATH.py
hermes workflow compile  PATH -o $HERMES_HOME/workflows/definitions/ID.json
hermes workflow run      PATH_OR_ID [--input JSON] [--resume RUN_ID]
                         [--from NODE] [--retry-failed]
                         [--max-budget-usd F] [--dry-run]
hermes workflow status   RUN_ID [--watch]
hermes workflow logs     RUN_ID [--node ID] [--follow]
hermes workflow list     [--status S] [--workflow ID] [--limit N]
hermes workflow cancel   RUN_ID
hermes workflow gate     RUN_ID GATE_ID --decide approve|shelve|modify [--note "..."]
hermes workflow doctor   # paths, sqlite, permissions
```

Exit codes: `0` ok · `1` runtime fail · `2` verify reject · `3` usage · `4` gate awaiting (run parked).

---

## 5. Conversation tool (optional, default-off)

**Toolset:** `workflow` (not in core default bundle).  
**Enable:** `hermes tools enable workflow` or config:

```yaml
workflow:
  enabled: true
  tool_enabled: true     # expose to agent; still compile+verify on every run
```

### Tools

```jsonc
// workflow_run
{
  "name": "workflow_run",
  "parameters": {
    "workflow": "id or path",
    "input": {},
    "resume": "run_id?",
    "dry_run": false
  }
}
// returns RunEnvelope or { status: "awaiting_gate", run_id, gate_id }

// workflow_status
{ "name": "workflow_status", "parameters": { "run_id": "…" } }

// workflow_gate  (agent may not self-approve if dual_control; typically human)
{ "name": "workflow_gate", "parameters": {
    "run_id": "…", "gate_id": "…", "decision": "approve|shelve|modify", "note": ""
}}
```

**Cache safety:** tools appear only when toolset enabled at session start. Running a workflow must not rewrite parent tools/system prompt.

---

## 6. Cron integration

Two options (both Phase 2; Phase 1 uses shell):

**A. Wrap CLI (no cron core change):**
```bash
hermes cron create "0 7 * * *" --name pm-desk \
  --command 'hermes workflow run pm_desk --input {}'
```
(If cron only supports agent prompts today, Phase 1 uses a tiny `script:` + `no_agent: true` that shells out.)

**B. Native job field (Phase 2):**
```yaml
# cron job record extension
workflow_id: pm_desk
workflow_input: {}
```
Scheduler calls `workflow.runtime.run(...)`. Prefer A first for upstream-friendliness.

---

## 7. Webhook integration

- start: `POST /webhooks/workflow/<name>` → `workflow_run`
- gate signal: `POST /webhooks/workflow-gate/<run_id>/<gate_id>` body `{ "decision": "approve", "note": "" }`
- Reuse gateway webhook HMAC + `hermes webhook subscribe` OR dedicated routes under `workflow.enabled`.

---

## 8. Python library API

```python
from workflow import compile_file, run, status, resume, cancel, decide_gate

vir = compile_file("pm_desk.yaml")          # VerifiedIR
env = run(vir, input={}, dry_run=False)     # RunEnvelope (or parks on gate)
env = resume("wf_abc", retry_failed=True)
st  = status("wf_abc")
decide_gate("wf_abc", "freeze", "approve", note="lgtm")
cancel("wf_abc")
```

---

## 9. Store paths

```
$HERMES_HOME/workflows/
  definitions/<workflow_id>.json
  runs/<run_id>/run.json
  runs/<run_id>/checkpoint.json
  runs/<run_id>/nodes/<node_id>/output.json
  runs/<run_id>/nodes/<node_id>/events.jsonl
  runs/<run_id>/artifacts/…
  runs/<run_id>/gate_signals/<gate_id>.json
  runs/<run_id>/run_output.json
  index.sqlite
```

Atomic write: write temp → `os.replace`.

---

## 10. Config (`config.yaml`)

```yaml
workflow:
  enabled: false              # master switch
  tool_enabled: false
  max_budget_usd: 10.0        # default run cap
  max_parallel_nodes: 4
  default_node_timeout_s: 1800
  store_dir: null             # default $HERMES_HOME/workflows
  allow_network_scripts: false
```

---

## 11. Error model (driver)

| Situation | Node status | Run status | Retriable |
|-----------|-------------|------------|-----------|
| verify fail pre-run | — | failed | no |
| agent timeout | failed | partial/failed | yes |
| budget trip | failed (BUDGET) | awaiting_gate or paused | soft |
| schema invalid output | failed | partial | yes |
| gate timeout shelve | skipped downstream | succeeded/partial | no |
| crash mid-node | running→ready on resume | running | yes |

---

## 12. Compatibility notes

- Does not change `delegate_task` required args in Phase 1.
- Phase 2 may add optional kwargs to internal construction helpers only.
- Kanban not required to run.
