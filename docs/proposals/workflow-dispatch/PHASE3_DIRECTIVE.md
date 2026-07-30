# DIRECTIVE — Workflow Dispatch Phase 3: full implementation + validation

**Orchestrator:** you — Claude Code **Opus** (`--model opus`) on Joe's Claude Pro OAuth.  
**Subagents:** Task agents **sonnet** — `impl-sonnet`, `test-sonnet`, `review-sonnet` (via `--agents` JSON). Parallelize impl vs tests vs review; you integrate, own architecture, open PR.

**Branch / worktree:** `feat/workflow-dispatch-phase3`  
`/home/hermes/research/hermes-workflow-phase3`  
Base: `origin/main` @ post Phase 2 merge `bb72f747a`.

**Repo:** `joeysmithjrs/hermes-agent` only. Open PR → `main`. **DO NOT MERGE.**

**Git user already set:** Joe Smith / `17200287+joeysmithjrs@users.noreply.github.com`

---

## 0. Mission

Ship **Phase 3 productization** on top of working Phase 1+2:
map sugar, richer reducers, failure policies, real cost rollup, bounded parallel fanout, watch/status UX, output schema enforcement, optional tool subset, trigger-chain / schedule sugar, optional kanban projection, notify polish — hermetic tests green, PR open.

Phase 1+2 must stay green (conditionals, gates unpark, live inherit/override model, checkpoint-before-dispatch, no silent FakeWorker, frozen script registry).

---

## 1. Non-negotiables (carry forward)

1. Control flow remains deterministic driver + verified IR — LLM only inside agent leaves.  
2. Model: unset inherits parent; `spec.model` / `spec.provider` override via existing `_build_child_agent` / LiveWorker path.  
3. No silent FakeWorker — live unless `--fake` / `HERMES_WORKFLOW_FAKE=1`.  
4. Checkpoint `"running"` **before** blocking work; parallelism must not break this.  
5. Config via `hermes_cli.config.load_config()` only. Path I/O `encoding="utf-8"`.  
6. Footprint: prefer `workflow/`; conversation tools default-off, session-start only; avoid `run_agent.py` hot path.  
7. Script `run:` stay allowlisted/frozen.

---

## 2. Phase 3 scope — implement the FULL set

### A. Graph sugar & control flow (REQUIRED)

| Feature | Spec |
|---------|------|
| **`map` node** | Sugar: expand list → identical branch NodeSpec → join/reduce. Compiles to fanout+join semantics or first-class driver kind that is observably equivalent. YAML example + verifier. |
| **Richer reducers** | At least: `concat`, `top_k` (existing), **`first_k`**, **`majority`** (define clear tie-break), optional scored `best` if cheap. Document in api/examples. Short-circuit reducers **cancel/skip remaining branches** when policy says so (or document if cooperative only). |
| **`on_fail` policy** | Per-node and/or edge: `fail_run` \| `skip_downstream` \| `continue` \| `retry` (bounded). Default preserves safe current behavior. Tests for each. |
| **Failed-upstream cascade** | Downstream of **failed** must not hang `pending` forever — map to skip/fail per policy. |

### B. Operator UX & cost (REQUIRED)

| Feature | Spec |
|---------|------|
| **Cost rollup** | Aggregate child agent token/cost into node envelopes + run `cost_usd`. Fix Phase 2 under-report when LiveWorker has token counters in result. |
| **`hermes workflow list --cost`** and/or status fields showing rolled cost. |
| **`hermes workflow watch RUN_ID`** | Poll/stream node status until terminal (or timeout flag). |
| **Budget pause UX** | On `max_budget_usd`: clear status (`paused` or failed+code), notify if configured, resume policy documented + tested. |
| **`max_parallel_nodes` for real** | Bounded concurrent execution of ready nodes / fanout branches. **Must preserve checkpoint-before-dispatch** and deterministic-enough tests (thread and/or async pool OK; no lost node_run_ids). |

### C. Correctness under LLMs (REQUIRED)

| Feature | Spec |
|---------|------|
| **Output JSON Schema** | If `spec.output` / schema present on agent or script, validate on succeed; fail node with clear code on mismatch. |
| **Optional `spec.tools` subset** | When set: child toolsets ⊆ parent valid tools (names). When unset: inherit all (current). Enables cheap models without tool endpoints. Verifier: reject unknown tool names. Tests with mock builder kwargs. |
| **Optional `max_turns`** | If easy to pass through to child max_iterations, honor; else document still deferred but prefer implement. |

### D. Surfaces & integration (NICE-TO-HAVE but IN SCOPE — ship, don't skip without note)

| Feature | Spec |
|---------|------|
| **Trigger-chain helper** | CLI or API: start run B with input from run A's final envelope / selected fields. Cron recipe remains valid; add `hermes workflow chain` or `run --from-run` style. |
| **Schedule sugar** | Thin wrapper: `hermes workflow schedule` that prints or registers cron recipe calling `hermes workflow run` (can shell out to `hermes cron` if stable API exists). Not a second scheduler. |
| **Webhook recipe hardening** | Doc + optional script under docs/proposals; reuse existing gateway webhook. |
| **Kanban projection (optional, default off)** | Config `workflow.kanban_projection: false`. When true, mirror run/node status to kanban cards **without** using kanban as execution bus. If Hermes kanban API is too sprawling, ship a **thin stub + interface** with one hermetic test and honest docs — but try real projection first. |
| **Notify polish** | Presets: on gate / fail / success / budget; templates; don't crash run if delivery fails (already). |
| **Conversation tools** | If Phase 2 tools exist, extend with status/watch/chain as needed; still `workflow.tool_enabled` default false. |

### E. Docs

Update phases.md Phase 3 status; IMPLEMENTATION_LOG Phase 3 section; examples YAML for `map`, on_fail, tool subset, schema; cron/chain recipes.

---

## 3. Acceptance checklist (ALL before PR)

```
[ ] pytest tests/workflow -q  (+ any new tests/tools) GREEN
[ ] map sugar: example validates + FakeWorker E2E
[ ] reducers first_k + majority tests
[ ] on_fail policies tested
[ ] failed upstream does not hang downstream
[ ] cost_usd increases when Fake/Live child reports tokens (unit with stub result)
[ ] max_parallel_nodes>1 fanout completes correctly + checkpoint durable under concurrent branches (stress/unit)
[ ] output schema reject on bad agent/script output
[ ] spec.tools subset applied to child construction (kwargs assertion)
[ ] watch CLI exits on terminal status
[ ] chain/from-run or schedule sugar has at least smoke/doc+unit
[ ] Phase 1+2 regression: conditionals, gate unpark, no silent fake, F4, CARDINALITY
[ ] ruff/footgun clean on touched files
[ ] PR open, NOT merged; body = checklist + residuals
```

---

## 4. Order of work

1. Scout current `workflow/` (driver, verify, live, cli, ir, scripts).  
2. Sonnet: map + reducers + on_fail + cascade.  
3. Sonnet: parallel fanout + cost rollup.  
4. Sonnet: schema + tools subset.  
5. Sonnet: watch/list/cost CLI + chain/schedule.  
6. Kanban projection / notify polish.  
7. Sonnet tests continuous.  
8. Sonnet adversarial review.  
9. Opus: integrate, fix, docs, commit, push, `gh pr create`, print URL.

---

## 5. Stop condition

Checklist green + PR open. Prefer complete limbs over stubs; if kanban truly impossible without sacred-core edits, stub interface + document residual — **everything else must be real**.

BEGIN NOW. Opus orchestrates; Sonnet implements/tests/reviews.
