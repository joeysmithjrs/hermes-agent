# Workflow Dispatch — Implementation Phases

**Companion to:** design + api + tests · 2026-07-29

---

## Phase 0 — Spec freeze (this folder)

**Done when:** design/api/examples/test/upstream/SUMMARY accepted by Joe.  
**No runtime code required.**

---

## Phase 1 — MVP runner (fork only / feature-flagged)

### Scope
- Package `workflow/` with IR, YAML load, verifier (structure+refs+gates basic)
- Filesystem store + sqlite index — **node bodies keyed by `node_run_id`, not `node_id`** (fanout branches must not collide)
- Driver supporting: `agent` (inherit/`delegate_task` only — honors `prompt`→`goal` + `context` ONLY), `script`, linear edges, simple `fanout`+`join` (reduce concat/top_k) with `max_branches` enforced + per-branch budget check
- CLI: validate, compile, run, status, logs, resume, cancel
- `config.workflow.enabled` default **false**
- Hermetic unit + FakeWorker integration tests

### Phase 1 inherit-path limitation (must be explicit)
The Phase 1 worker is the **inherit path only** (`delegate_task`, in-process
threads). It **cannot honor** `model`, `tools`, `profile`, `max_turns`,
`workspace`, or `side_effects` isolation — `delegate_task`'s signature takes none
of these and threads share one process-wide `HERMES_HOME`. Therefore the Phase 1
verifier **rejects** (or warns behind `workflow.phase1_warn_overrides`) any agent
node that sets an override-only field, so a `tools` allowlist or `profile` tag is
never silently dropped. `tools`/`profile`/model enforcement and true per-node
profile isolation are **Phase 2** (override path / subprocess). Side-effecting
agent-node resume safety (`side_effects: external`) and `max_branches` runtime
capping ARE in Phase 1 (they are verifier + driver concerns, not worker-shim).

### Out of Phase 1
- conversation tool
- per-node model/tools/profile override path (worker shim) — and with it, tool-allowlist & profile-isolation *enforcement*
- webhook/cron native fields
- kanban adapter
- map sugar
- budget worst-case fanout verifier (can be warn-only)

### Acceptance
1. `linear_brief` yaml runs end-to-end with FakeWorker (note: its `tools:` are *stored* not *enforced* in Phase 1 — see inherit limitation).
2. Fanout 3 → join produces 3 branch envelopes, each under its own `nodes/<node_run_id>/output.json` (no path collision).
3. Kill -9 driver mid-run; `resume` completes without double-finalizing succeeded nodes; a `side_effects: external` agent node resumes to `failed` (not auto-requeued) — verified with a FakeWorker that records a side-effect call count.
4. Fanout `over:` a list exceeding `max_branches` → node `failed` code `CARDINALITY`, no overspawn.
5. Verifier rejects an agent node setting `tools`/`profile`/`model` in Phase 1 (or warns behind flag).
6. `hermes --help` works if `workflow` import fails.
7. Zero files changed in `run_agent.py`.  

### Exit criteria for merge to fork `main`
Phase 1 tests green; docs link from README optional section behind flag.

---

## Phase 2 — Granularity + gates + surfaces

**Status: SHIPPED (partial — scoped honestly below).** See `IMPLEMENTATION_LOG.md`
§Phase 2 and `PHASE2_SURFACES.md`.

### Scope — what actually shipped
- ✅ **Live worker + model override path.** `workflow/runtime/live.py` —
  `LiveWorker` runs agent nodes as real `AIAgent` children through
  `tools/delegate_tool.py`'s canonical construction path. No `spec.model` →
  inherits the runtime parent's model/provider; `spec.model` → that node only
  runs on the override; `spec.provider` → fresh credentials resolved via the
  same `resolve_runtime_provider` the CLI/gateway use. `effective_model` /
  `effective_provider` land on the node-run and in node events.
- ✅ **No silent FakeWorker.** FakeWorker requires `HERMES_WORKFLOW_FAKE=1` or
  `--fake`; otherwise a live worker is built and a missing runtime parent
  fails loud. This reverses the Phase 1 default at every layer including
  `Driver.__init__`.
- ✅ **Gate unpark.** `resume()` resolves the on-disk decision: `approve` →
  gate succeeds on the `approve` port and downstream continues; `shelve` →
  gate skipped on the `shelve` port and downstream never executes; no
  decision → stays `awaiting_gate` (an open gate can never become
  `succeeded`).
- ✅ **Notifications.** Best-effort, through Hermes's existing
  `send_message_tool` path, on terminal status and on gate park. A delivery
  failure is logged into the run's event log and never fails a run.
- ✅ **Optional toolset `workflow`** — `workflow_run` / `workflow_status`,
  default-off behind `workflow.tool_enabled` + `workflow.enabled`, fixed at
  session start via `check_fn` (no mid-turn toolset mutation).
- ✅ **Cron + webhook recipes** documented with a working example script
  (`PHASE2_SURFACES.md`, `examples-phase2-cron.sh`).
- ✅ **Output JSON schema validation** (opt-in per node via `spec.output`).
- ✅ **`max_budget_usd` circuit breaker** — trips to `paused` with
  `pause_reason: BUDGET`, breaks the loop, documented resume policy.
- ✅ **Failed-upstream cascade** (latent Phase 1 debt) — downstream of a
  failed node is marked `skipped` with a reason instead of hanging `pending`;
  `--retry-failed` un-sticks the cascade.

### Explicitly NOT shipped in Phase 2 (still rejected by the verifier)
`spec.tools`, `spec.profile`, `spec.max_turns`, `spec.workspace` remain
**rejected** (or warn-only behind `workflow.phase1_warn_overrides`). The live
child inherits the parent's toolsets; there is no per-node tool narrowing or
profile isolation yet, so the verifier refuses to accept a field it cannot
enforce rather than silently dropping a `tools:` allowlist. Claiming
isolation we don't enforce is the one failure mode worth being loud about.

Also deferred: native `triggers:` dispatch (cron/webhook are host-surface
recipes, not a second scheduler), `gate` decisions as a conversation tool
(deliberate — an agent that can approve its own gates is not a gate), and
`modify` gate decisions (requires re-authoring the definition; the runtime
says so rather than pretending).

### Acceptance
1. ✅ Gate parks run; approve continues; shelve skips exec (proved with
   side-effect counters that the downstream node is never *invoked*).
2. ✅ Per-node model override visible in node events (mock).
3. ✅ Tool call from isolated session starts run without cache thrash
   (toolset fixed at session start via `check_fn`).
4. ⚠️ Webhook HMAC rejection path — **not re-tested here by design.** Phase 2
   reuses the existing gateway webhook route rather than adding a second HMAC
   implementation, so the rejection path is covered by that route's own
   tests. A duplicate implementation would be new attack surface for no new
   capability.

---

## Phase 3 — Hardening + adapters

### Scope
- `map` sugar  
- richer reducers (`first_k`, `majority`) + `on_fail` policy  
- kanban **projection** adapter (optional)  
- cost aggregation dashboard hooks / `workflow list --cost`  
- trigger-chain helper (scribe → enqueue next run)  
- Observability polish (`--watch`)  
- Upstream PR packaging  

### Acceptance
1. PM desk skeleton yaml validates and runs with fakes through gate.  
2. Kanban adapter optional off-by-default.  
3. Upstream-oriented PR description + footprint ladder narrative ready.  

---

## Phase 4 — Production PM desk (application layer)

Not the primitive itself — builds **on** the runner:

- real `pm_desk.*` scripts & prompts  
- Polymarket tools allowlists  
- dual-control live trading profiles  
- monitors as fanout watchers  

Acceptance tied to [[PM Autonomy Pipeline]] Phase 0–2 in vault, not to merging core.

---

## Suggested week plan (single eng + agent assist)

| Week | Output |
|------|--------|
| 1 | IR+verify+yaml+linear FakeWorker |
| 2 | fanout/join+store+resume+CLI |
| 3 | gate+notify+tests |
| 4 | worker overrides+toolset+docs polish |

---

## Risk register

| Risk | Mitigation |
|------|------------|
| `delegate_task` lacks overrides | Phase 1 inherit only; Phase 2 shim |
| Gateway required for usefulness | CLI-first; gateway optional |
| Upstream rejects package in core | plugin extraction path ready |
| Agents misuse tool to burn $ | default tool off; run budget caps |
| Non-idempotent scripts | verify require flags; fake tests |

---

## Definition of “Hermes-native”

- Uses Hermes profile homes, gateway delivery, config.yaml, hermetic test style  
- Does not require external orchestrator binary  
- Composable from cron/webhook/tool/CLI  
- Appears as `hermes workflow` to operators  
