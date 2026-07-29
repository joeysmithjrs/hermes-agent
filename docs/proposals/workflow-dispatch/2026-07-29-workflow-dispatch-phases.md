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

### Scope
- `workflow.worker` override path (model/prompt/tools/profile/workspace)  
- `gate` node + CLI `workflow gate` + optional Telegram parse hook  
- Optional toolset `workflow` + `workflow_run`/`status`/`gate`  
- Cron recipe documented; optional thin scheduler hook  
- Webhook start + gate signal (reuse gateway secret model)  
- Output JSON schema validation  
- `max_budget_usd` circuit breaker  
- Budget/ports verifier completeness  

### Acceptance
1. Gate parks run; approve continues; shelve skips exec.  
2. Per-node model override visible in node events (mock).  
3. Tool call from isolated session starts run without cache thrash (toolset fixed at session start).  
4. Webhook HMAC rejection path tested.  

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
