# Workflow Dispatch — Design Spec

**Status:** Draft for review · **Repo:** `joeysmithjrs/hermes-agent` fork, branch `feat/workflow-dispatch`
**Date:** 2026-07-29 · **Phase:** SPEC ONLY (no production runner ships here)
**Authority doc.** Companion docs: `…-api.md`, `…-upstream.md`, `…-examples.md`, `…-test-plan.md`, `…-phases.md`.

---

## 1. Problem & scope

Hermes can already delegate a *single hop* of work to a child agent
(`delegate_task`) and run scheduled jobs (`cron`). What it cannot do is express
a **multi-node orchestration** — fan-out research, fan-in scoring, a hard human
gate, then a frozen execution plan with parallel monitors — as **code/data**,
verify it before running, checkpoint it, resume it after a crash, and drive it
from a tool call, cron, or webhook without the main chat LLM re-deriving the
graph every hop.

**Goal:** add core **orchestration primitives** so Hermes runs code-defined
multi-agent workflows ("Claude Code style" in the *explicit-structure* sense —
not a reimplementation of Claude Code).

**In scope (design):** an intermediate representation (IR), a Python DSL and
YAML that compile to it, a static verifier/linter, a runtime/driver, a
persistence + checkpoint model, a standard completion envelope, per-node
granularity, invocation surfaces (tool / CLI / cron / webhook), security, and
observability.

**Out of scope (this spec phase):** production implementation & merge, replacing
kanban or `delegate_task`, mid-conversation tool-schema mutation, a BPMN GUI,
making the main chat LLM the DAG interpreter every hop.

### Design principles (non-negotiable)

1. **Control flow is code, not prose.** A workflow is a verified IR a
   deterministic driver walks. The LLM only runs *inside* leaf agent nodes.
   Rationale: a model re-deriving the graph every step is untestable, expensive,
   and non-resumable. (Reference repo calls this "fat engine, thin skill.")
2. **Prompt cache is sacred.** Nothing this feature ships may mutate the parent
   conversation's toolset or system prompt mid-turn. The conversation-facing
   surface is **one** service-gated tool whose availability is fixed for the life
   of a conversation.
3. **Smallest footprint first.** Per the Footprint Ladder (AGENTS.md), the
   capability ships as a **new package `workflow/` + a `hermes workflow` CLI
   subcommand + skill (rung 2)** and an **optional, default-off, service-gated
   tool (rung 3)**. It must *not* require edits to `run_agent.py`'s hot path.
4. **Reuse the leaf executor.** Worker agent nodes run through the existing
   `delegate_task` machinery (background async, depth limits, pause kill-switch)
   rather than a parallel spawning implementation. New code wraps; it does not
   fork the conversation loop.
5. **The board is not the bus (for now).** The reference repo uses Kanban cards
   as the inter-agent bus and audit log. That is elegant but couples this feature
   to the kanban dispatcher's promotion semantics. Our v1 persistence is a
   dedicated, self-contained store under `$HERMES_HOME/workflows/`. A kanban
   *adapter* is a deliberate later option (Phase 3), not the foundation — see §10.

---

## 2. Primitives — the IR

A small, orthogonal set. Everything serializes to JSON (so YAML/DSL/checkpoints
share one schema).

### 2.1 Core objects

```
Workflow      := { id, version, name, nodes: [Node], edges: [Edge],
                  triggers: [Trigger], defaults: NodeDefaults, gates: {name: Gate} }
Node          := { id, kind, ...kind-specific fields, spec: NodeSpec }
Edge          := { from, to, condition?: Expr, port?: string }
NodeSpec      := the per-agent granularity block (§6): model, prompt, tools,
                 profile, timeout, budget, max_turns, workspace, input, output
Trigger       := CronTrigger | WebhookTrigger | ManualTrigger
Gate          := { id, channel, notify, approvers, timeout, on_timeout,
                  auto_actions: [GateAction] }
```

### 2.2 Node kinds (only those justified)

| kind | runs | fan? | why it exists |
|------|------|------|---------------|
| `agent` | one leaf/orchestrator agent run | no | the LLM-bearing unit; executes via `delegate_task` |
| `script` | deterministic Python/shell, no model | no | transforms, validators, format glue (DD-memo packing, score sums) |
| `fanout` | expands one input into N branch inputs | out | "spawn N directives"; declares cardinality + a per-branch `NodeSpec` |
| `join` | waits for N upstreams, reduces | in | fan-in / barrier; declares a reducer (all, first-k, majority, custom) |
| `gate` | blocks until a dual-control decision arrives | — | the hard human checkpoint; emits `awaiting_gate` status |
| `map` | syntactic sugar: fanout+identical agent+join over a list | both | the common "apply this agent to every item" pattern |
| `cron_trigger` | schedule that starts a run | — | lifecycle entry |
| `webhook_trigger` | external event that starts a run or signals a gate | — | lifecycle entry / gate signal |

**Rejected kinds (deliberate omissions):** a generic `parallel` node (use
multiple `agent` edges into a `join`), a `loop`/`while` node (v1 graphs are
acyclic; iteration is expressed as `map` over a materialized list — see §4 on
cycles), and an `llm-router` node (routing is a `script` or `agent` node that
emits a port label consumed by conditional edges — keeps the IR honest about
"who decided").

### 2.3 Edges & ports

Edges carry an optional `condition` (a tiny expression language: `$.field op
literal`, referencing the upstream node's output envelope) and an optional
`port`. A node may declare named output ports (e.g. `pass`/`fail`/`timeout`);
conditional edges route on them. This is how DQ "kill" branches and gate
`approve`/`shelve`/`modify` outcomes are expressed — without making the graph
interpreter call an LLM.

### 2.4 Identifiers

- `run_id` — `wf_<short>`, created at start. Namespaced per profile (see §8).
- `attempt_id` — increments on each (re)start of the run; checkpoint lines
  reference `(run_id, attempt_id)`.
- `node_run_id` — `<run_id>__<node_id>__<attempt>`; one per node execution.
  Idempotency key for resume.

### 2.5 Status enum

```
pending            # not yet ready (waiting on upstream)
ready              # upstream done, driver may start it
running            # in flight
succeeded          # completed, envelope written
failed             # terminal failure (see error model)
skipped            # conditionally pruned or gate-shortcut
cancelled          # run or parent cancelled
awaiting_gate      # blocked on a gate decision
```

Node lifecycle: `pending → ready → running → {succeeded|failed|awaiting_gate|skipped|cancelled}`.
Run lifecycle: `pending → running → {succeeded|failed|partial|cancelled}`.

### 2.6 Standard completion envelope

Every node emits one envelope into the artifact store. The run emits one too.

```jsonc
// NodeRunEnvelope
{
  "node_run_id": "wf_..__prepare__1",
  "node_id": "prepare",
  "kind": "agent",
  "status": "succeeded",            // from the enum
  "attempt": 1,
  "started_at": "…", "ended_at": "…",
  "cost_usd": 0.012,                 // aggregated for this node
  "port": "pass",                    // chosen output port (null if single)
  "output": { … node output_schema value … },
  "error": null,                     // ErrorObject when status in {failed,cancelled}
  "artifact_ref": "runs/wf_../nodes/prepare/output.json",  // $HERMES_HOME/workflows/...
  "events": ["events/wf_..__prepare__1.jsonl"]   // event-log refs
}
```

```jsonc
// RunEnvelope  (terminal)
{
  "run_id": "wf_…", "attempt_id": 2, "workflow_id": "pm_desk",
  "status": "partial",             // succeeded | failed | partial | cancelled
  "succeeded": ["seed","directive","dq","dd"],   // node ids
  "failed":  ["eval"],
  "skipped": ["exec"],
  "awaiting_gate": null,
  "started_at": "…", "ended_at": "…",
  "cost_usd": 0.43,
  "final_output_ref": "runs/wf_…/run_output.json",
  "resume_hint": "re-run with --resume wf_… to continue from checkpoint"
}
```

`partial` means ≥1 node succeeded, ≥1 failed, and the graph is recoverable
(resume can retry the failed node). `failed` means an unrecoverable node failed
or the run was rejected by the verifier. `succeeded` means the terminal node(s)
succeeded.

---

## 3. Authoring: Python DSL + YAML

Both compile to the **same IR** (`Workflow.compile()` → a validated `IRGraph`
dataclass tree). YAML is the persistence/authoring format; the DSL is for
agents/humans generating workflows programmatically. They share one schema, so
"compile then verify" is identical.

### 3.1 DSL sketch (illustrative — final API in `-api.md`)

```python
from workflow import Workflow, node, gate, expr as _

with Workflow("pm_desk", defaults=node.agent(model="anthropic/claude-sonnet-5")) as wf:
    ctx = wf.agent("prepare", prompt="Load desk_state…", output=DESK_STATE_SCHEMA)

    seed = wf.script("seed", run=seed_branches, output={"branches": list})

    # fan-out N directive branches, fan-in to a DQ node
    dirs = wf.fanout("directive", over="branches", branch=node.agent(
        prompt=_.template("directive {{branch.template}}"),
        tools=["web_search","read_file"]))
    dq = wf.join("dq", from_=dirs, reduce="top_k", k=3, run=dq_script)

    dd = wf.fanout("dd", over="candidates", branch=node.agent(prompt=DD_PROMPT))
    eval = wf.join("eval", from_=dd, reduce="scorecards", run=eval_script)

    plan = wf.agent("plan", prompt=PLAN_PROMPT, output=PLAN_SCHEMA)

    # dual-control gate before any execution
    g = wf.gate("freeze", channel="telegram", approvers=["joe"],
                on_timeout="shelve")
    plan.then(g, port="proposes_live")     # only live proposals hit the gate

    exec = wf.agent("exec", prompt=EXEC_PROMPT, profile="trader-paper",
                    tools=["trade_paper"], budget_usd=1.0)
    mon = wf.fanout("monitor", over="watchers", branch=node.agent(
        prompt=MONITOR_PROMPT, tools=["tape","oracle","macro"]))
    wf.join("scribe", from_=[exec, *mon], reduce="postmortem", run=scribe)

    wf.trigger("cron", schedule="0 7 * * *")
```

### 3.2 YAML (same IR)

```yaml
workflow: pm_desk
version: 1
defaults: { model: anthropic/claude-sonnet-5 }
triggers:
  - cron: { schedule: "0 7 * * *" }
nodes:
  - id: prepare
    kind: agent
    spec: { prompt: "Load desk_state…", output: { $ref: "schemas/desk_state.json" } }
  - id: seed
    kind: script
    run: pm_desk.seed_branches
  - id: directive
    kind: fanout
    over: "{{ seed.branches }}"
    branch: { kind: agent, spec: { prompt: "directive {{ branch.template }}",
            tools: [web_search, read_file] } }
  - id: dq
    kind: join
    from: [directive]
    reduce: { type: top_k, k: 3 }
    run: pm_desk.dq_script
edges:
  - { from: prepare, to: seed }
  - { from: seed, to: directive }
  - { from: directive, to: dq }
  - { from: dq, to: dd }      # dd fanout omitted for brevity
gates:
  freeze: { channel: telegram, approvers: [joe], on_timeout: shelve }
```

### 3.3 Verify before execute

`Workflow.compile()` runs the **verifier** (§5) and returns a `VerifiedIR` or
raises `WorkflowRejected` with a list of `Issue(severity, code, node, message)`.
**No run starts from an unverified graph.** This is the single most important
invariant: the driver only ever walks a verified IR.

---

## 4. Execution model

### 4.1 What it is NOT

Not an orchestrator LLM that loops, calling subagents ad hoc, deciding the next
node each turn. That re-derives the graph every hop, is untestable, and breaks
resumability.

### 4.1 What it IS

A **deterministic driver** (`workflow.runtime.Driver`) that:

1. Loads the verified IR + the run record (or creates one).
2. Computes `ready` nodes (all upstreams `succeeded`, conditions satisfied).
3. Starts each `ready` node per its kind:
   - `agent` → calls the **worker shim** (§7) which runs the leaf via
     `delegate_task` semantics (background async, depth-bounded).
   - `script` → calls the registered Python callable in a sandboxed subprocess
     (no model).
   - `fanout` → materializes the branch list from upstream output, stamps N
     child node-runs, each an `agent`/`script` per `branch`.
   - `join` → registers a barrier keyed on the upstream node-run set; fires the
     reducer when all arrive (or when `reduce` policy says so, e.g. `first_k`,
     `majority`).
   - `gate` → emits `awaiting_gate`, parks the run, and registers a signal
     listener (CLI / webhook / gateway reply). The driver loop does **not** busy
     poll; it sleeps on a condition variable woken by the signal path or cron
     tick.
4. After each node completes, writes the envelope + event log, recomputes
   `ready`, checkpoints, repeats until terminal.

### 4.2 Why wrap `delegate_task`, not reimplement

`delegate_task(goal, context, tasks, max_iterations, role, background,
parent_agent)` already gives us: leaf vs orchestrator roles, **background async
batch fan-out** ("a batch dispatched as ONE async unit … joins on every child …
pushes a SINGLE completion event"), depth limiting (`delegation.max_spawn_depth`),
and an operator **pause kill-switch** (`is_spawn_paused()`). Reusing it means the
workflow runtime inherits tested concurrency, cancellation, and cost plumbing.
The runtime adds what `delegate_task` lacks: graph structure, fan-in barriers,
gates, checkpoints, per-node persistence, and conditional routing.

**The seam (open integration point, resolved with a default in §7):**
`delegate_task`'s public surface does **not** accept per-call model/prompt/tools/
profile/workspace — those come from role→profile resolution and the child agent's
config. To honor §6 per-node granularity without forking the conversation loop we
introduce a thin **`workflow.worker`** shim that constructs the child `AIAgent`
with explicit overrides (the same construction `_run_single_child` already uses
internally) and runs it. `delegate_task` remains the path for nodes that *want*
to inherit the parent profile; the worker shim is the path for nodes that
*override*. Both share the result-aggregation and event-emission code.

### 4.3 Persistence

```
$HERMES_HOME/workflows/
  definitions/<workflow_id>.json        # compiled verified IR (content-addressed by hash)
  runs/<run_id>/
    run.json                            # RunRecord: status, attempt_id, node states
    checkpoint.json                     # last-committed node-run statuses (atomic write)
    nodes/<node_id>/output.json         # node output envelope value
    nodes/<node_id>/events.jsonl        # per-node event log
    artifacts/                          # large blobs (DD memos, source graphs)
    gate_signals/<gate_id>.json         # pending/decided gate state
    run_output.json                     # terminal RunEnvelope
  index.sqlite                          # run registry: run_id, workflow_id, status,
                                        #   started, ended, cost; queryable for `status`
```

Choice: **filesystem is the source of truth** for run/node/envelope bodies
(simple, diff-able, debuggable by hand); **sqlite is an index** for listing/
filtering (mirrors the hermes_state.py SessionDB pattern of SQLite-over-files).
This avoids a heavy DB migration and keeps each run a self-contained directory —
easy to `tar`, `rsync`, or inspect. Atomic writes via temp-file + `os.replace`
for crash safety; checkpoint write is the commit point.

### 4.4 Resume after crash

`hermes workflow resume <run_id>` reloads `run.json` + `checkpoint.json`:
- Re-create the IR from `definitions/<workflow_id>.json` (verified at compile;
  re-verified at load).
- Skip nodes already `succeeded` (read their `output.json`).
- Re-queue `running` nodes as `ready` (their envelope wasn't committed →
  idempotent retry; `script` nodes must be idempotent or declare `attempts`)
  **or** `failed` nodes per resume policy (`--retry-failed` vs `--from <node>`).
- Re-arm `awaiting_gate` from `gate_signals/`.

Idempotency contract: `script` and `agent` nodes must tolerate re-execution.
`agent` nodes are naturally idempotent if their output is a memo (re-running
yields equivalent text); `script` nodes that mutate external state declare a
`attempts: N` budget and are checkpointed only on success. The verifier rejects
non-idempotent `script` nodes without an explicit `idempotent: true|attempts`
declaration adjacent to mutating calls.

### 4.5 Status query

`hermes workflow status <run_id>` → reads `index.sqlite` (fast path) and falls
back to `runs/<run_id>/run.json` (authoritative). Returns the `RunEnvelope` plus
a per-node status table. `--watch` tails the active run's event log.

---

## 5. Verifier / linter

Runs at compile and at run-load. A `WorkflowRejected` with zero `ready`-able
plans is fatal — **the driver never walks an unverified graph.** Checks, by
category:

| category | checks |
|---|---|
| **structure** | acyclic (or an explicit `map`/gate cycle construct — §4 note); single entry; ≥1 terminal; no unreachable nodes; every `join` has ≥2 upstreams or is a `map` reducer; no `fanout` without a downstream `join`/`map` |
| **references** | every `over:`/`from:`/`run:`/`output:` resolves to a real node/callable/schema; no unknown model ids (checked against the provider registry); `port` targets exist |
| **gates** | every `gate` has a `channel` + ≥1 `approver`; `on_timeout` ∈ {block, shelve, approve-auto}; a gate with `auto` action requires `dual_control: false` and emits a lint *warning* (security) |
| **budget** | declared per-node `budget_usd` and a run-level `max_budget_usd`; verifier sums the *worst-case* fanout cardinality × branch cost and rejects runs that could exceed the cap without a gate before the expensive node |
| **sandbox** | `script` nodes declare an `allow` tool/env list; mutating scripts flagged; no node may reference `dangerously-skip-approvals`-style flags by default |
| **prompts** | every `agent` node has a non-empty `prompt`; template variables (`{{ }}`) all resolve against upstream outputs or run inputs |

Cycles: v1 graphs are **acyclic**. Iteration is `map` over a *materialized list*
(produced by an upstream `script`/`agent` node), not a `while`. True cyclic
pipelines (the PM loop's "scribe → back into Prepare") are expressed as a
**trigger chain**: the terminal `scribe` node emits a webhook/cron signal that
starts a *new run* of the same workflow with the prior run's output as input.
This keeps a single run acyclic and resumable while still modeling the feedback
loop — and it maps cleanly to the PM pipeline's explicit "back into Prepare
context" arrow (see §11).

---

## 6. Per-agent granularity (`NodeSpec`)

Each `agent` (and `fanout`/`map` branch) node carries:

```jsonc
{
  "model": "anthropic/claude-sonnet-5",      // provider/model id; verified
  "provider": "anthropic",                    // optional, derived from model
  "prompt": "…",                              // system+task; supports {{ }} templating
  "skills": ["dd_memo"],                      // optional skill list for the worker
  "tools": ["web_search","read_file"],        // allowlist; deny = "_all minus […]"
  "profile": "trader-paper",                  // optional isolated HERMES_HOME slice
  "timeout_s": 1200, "max_turns": 40,
  "budget_usd": 0.50,                         // hard per-node cap (circuit-breaks)
  "workspace": "runs/<run_id>/ws/prepare",    // isolated dir; scratch or persistent
  "input":  { …mapping from upstream outputs… },   // JSON-schema-validated
  "output": { "$ref": "schemas/desk_state.json" }  // JSON schema; envelope validated
}
```

**Profile isolation:** when `profile` is set, the worker runs under a separate
`HERMES_HOME` slice (`get_hermes_home()` already scopes per profile —
`_apply_profile_override()` in `hermes_cli/main.py`). Workers that share state
use a `persistent` workspace dir threaded across stages (mirrors the reference
repo's `workspace_kind: dir`); ephemeral nodes use `scratch`.

**Input/output contracts:** `input` maps upstream envelopes into the node's
prompt context; `output` is a JSON schema the envelope is validated against
before the node is marked `succeeded` (a schema mismatch → `failed` with a
clear error, not silent garbage downstream). This is the "eval-parseable
artifact" requirement from the PM pipeline made structural.

---

## 7. Worker execution seam (resolution of the open point)

Two execution paths, sharing event-emission and result-aggregation code:

- **Inherit path:** node has no `model`/`profile`/`tools` override → call
  `delegate_task(goal=<prompt rendered>, context=<mapped inputs>, role="leaf",
  background=True, parent_agent=<run's agent ctx>)`. Reuses all existing
  concurrency, pause, depth-limit machinery. Zero new spawning code.
- **Override path:** node sets `model`/`profile`/`tools` → `workflow.worker`
  constructs a child `AIAgent` with explicit kwargs (model, toolset allowlist,
  profile-scoped `HERMES_HOME`, workspace, max_turns, budget watcher) and runs
  it. This uses the *same low-level construction* `tools/delegate_tool.py
  _run_single_child` uses internally — so no second spawning implementation,
  just a thin, well-tested construction site that the existing code already
  exercises.

**Recommendation (default):** ship the inherit path in Phase 1 (zero core
changes — `delegate_task` is the worker). Add the override path in Phase 2 via a
small, additive `workflow.worker` module that calls existing `AIAgent`
construction helpers. Neither path edits `run_agent.py`. If, and only if, the
override path turns out to require a kwarg `delegate_task` doesn't expose, add it
as an **optional kwarg** (additive, ignored when absent) — the least invasive
core touch, feature-flagged behind `workflow.enabled`.

---

## 8. Security

- **Dual-control gates.** A `gate` node is the one hard boundary between
  research/deliberation and execution. Default-on for any edge labeled
  `proposes_live` (or any node with `tools` containing a declared "live" tag).
  No gate auto-approves unless `dual_control: false` is *explicitly* set *and* the
  verifier emits a security warning. Mirrors the reference repo's "never make it
  auto-approve."
- **Secret scavenging.** Workers run with the *minimum* toolset. The verifier
  rejects a node whose `tools` overlap a secret-bearing tool
  (`credential_files`, env readers) unless the node is a named "secrets
  preparer" and gated. Per-node `budget_usd` caps blast radius.
- **Tool allowlists.** Each node's `tools` is an allowlist; `deny` is
  `_all minus [...]`. No node inherits the parent's full toolset by default —
  the worker shim constructs an explicit, minimal set.
- **No blind `dangerously-skip`.** Such flags are forbidden in the IR; the
  verifier hard-rejects them. Approvals flow through the `gate` primitive only.
- **Tenant/path isolation.** `run_id` is namespaced to the active profile
  (`get_hermes_home()/workflows/runs/...`), so concurrent runs across profiles
  never collide. Within a profile, run dirs are unique per `run_id`; the sqlite
  index enforces it. Worker workspaces are per-node, per-run — never shared
  across runs (persistent workspaces are *within* a run).
- **Sandbox for `script` nodes.** `script` runs in a subprocess with an explicit
  `allow` env/PATH and a working dir = the node's workspace; network is denied
  unless `allow: [network]` is declared and the node is pre-gate. Mutating
  scripts require `idempotent`/`attempts` declarations (§4.4).

---

## 9. Observability

- **Event log per node** (`nodes/<node_id>/events.jsonl`): start, model call,
  tool invocations (names only, not args-with-secrets), cost tick, end. Append-
  only; `hermes workflow logs <run_id>` streams/tails it; `hermes workflow logs
  <run_id> --node eval` filters.
- **Cost aggregation per run:** summed from per-node `cost_usd` into the
  `RunEnvelope`; also written to the sqlite index for `hermes workflow list
  --cost`. A run-level `max_budget_usd` circuit-breaks (pauses run → emits a
  gate asking to continue or stop), mirroring the reference repo's `cost_gate`.
- **Telegram notify** on `gate`/`failed`/`completed` (channel from the gate or
  run-level `notify`), via the existing gateway delivery path — reuses
  `gateway/delivery.py`, no new notifier.

---

## 10. Relationship to kanban (deliberate deferral)

The reference repo uses the Kanban board as the inter-agent bus: "a card with
parents starts in `todo` and auto-promotes to `ready` only when every parent is
`done`" — one rule gives parallelism + sequencing. We deliberately do **not**
build on that for v1, for three reasons:

1. **Coupling.** Building on kanban ties this feature to the dispatcher's
   promotion semantics and board schema; a kanban refactor upstream would break
   us. Our self-contained `workflows/` store has no such dependency.
2. **Resume/debug surface.** A per-run directory + envelope files is hand-
   debuggable; a board is not.
3. **Gateway-independence.** The kanban dispatcher lives in the gateway; we want
   workflows runnable from CLI/cron/webhook *without* a gateway.

A **kanban adapter** is reserved for Phase 3: an optional `persistence_backend:
kanban` that writes node states as cards on a named board, for users who want the
board as the audit log / want dispatcher-driven worker assignment. It is a
*projection* of our IR state onto kanban, not a replacement for the IR.

---

## 11. Alignment with the PM Autonomy Pipeline

Mapping from the vault note's canonical loop to primitives:

| PM stage | Primitive(s) | Notes |
|---|---|---|
| Prepare context | `agent` `prepare`, output `desk_state` schema | input mapping loads failed-thesis hashes, positions, calendar, infra health |
| Pseudorandom seed | `script` `seed` (deterministic, no model) | "seed influences exploration; does not override DQ" → script output feeds fanout, not a gate |
| Determine directive (N branches) | `fanout` `directive` over `branches`, branch=`agent` | each directive template = a branch `NodeSpec`; collapses toward DQ |
| Disqualification (≤3) | `join` `dq` reduce `top_k` k=3, run=dq_script | hard+soft DQ encoded in the script; outputs `Candidate` objects + DQ notes |
| Deep due diligence | `fanout` `dd` over `candidates`, branch=`agent` | DD memo artifact; `output` schema = eval-parseable structured fields |
| Eval fan-out | `join` `eval` reduce `scorecards` | independent scorers; "majority should still die" → default-reject bias in reducer |
| Execution plan (frozen) | `agent` `plan`, output `PLAN_SCHEMA`; edge `proposes_live`→`gate` | "frozen means exec does not freestyle" → exec node has no `directive` tool, only trade tools |
| Dual-control | `gate` `freeze` channel=telegram, approvers=[joe], on_timeout=shelve | maps the vault's "Approve if live" / dual-control rows directly |
| Execution ∥ Monitoring | `agent` `exec` ∥ `fanout` `monitor` over `watchers` | parallel; monitor may abort/reduce/hold per plan — encoded as gate-conditional edges back to exec |
| Post-exec scribing & notify | `join` `scribe` from [exec, *monitor], reduce `postmortem` | writes back to Prepare via **trigger chain** (scribe emits cron/webhook → new run) |
| Autonomy boundaries table | per-node `tools` allowlists + `gate` defaults | "Change thesis mid-flight: No" → no node has a `replan` tool after `plan` |

The dual-control gate and the monitor watchers are first-class IR nodes, not
afterthoughts — which is exactly the vault's "dual-control on size/submit/UMA
until proven boring."

---

## 12. Decisions summary (resolved defaults)

| # | Question | Decision | Rationale |
|---|---|---|---|
| D1 | Where does control flow live? | Verified IR + deterministic driver | testable, resumable, cache-safe |
| D2 | Packaging | new `workflow/` pkg + `hermes workflow` CLI + skill; optional default-off tool | Footprint Ladder rung 2+3 |
| D3 | Worker executor | wrap `delegate_task` (inherit) + thin `workflow.worker` (override) | reuse tested spawning; no core edits |
| D4 | Persistence | filesystem + sqlite index under `$HERMES_HOME/workflows/` | self-contained, debuggable, no DB migration |
| D5 | Cycles | acyclic v1; loops = trigger-chain to a new run | resumable; matches PM feedback loop |
| D6 | Kanban | self-contained store v1; kanban adapter Phase 3 | avoid dispatcher coupling |
| D7 | Gate auto-approve | forbidden by default; explicit `dual_control:false` + lint warning | security boundary |

Open questions are collected in `SUMMARY.md`; everything above is resolved with a
reasoned default an implementer can proceed on.
