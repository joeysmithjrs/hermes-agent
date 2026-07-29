# Workflow Dispatch — Test Plan

**Companion to:** design + api · 2026-07-29

Follow Hermes practice: hermetic `HERMES_HOME` temp dirs, no live network unless marked, assertions on contracts not brittle snapshots.

---

## 1. Layers

| Layer | Where | What |
|-------|--------|------|
| Unit | `tests/workflow/` | IR, verify, expr, checkpoint atomicity, reduce policies |
| Integration | tmp HERMES_HOME | Driver + fake worker (no real LLM) |
| E2E (optional) | opt-in | one real agent node with tiny max_turns |
| CLI | CliRunner / subprocess | validate/run/status/gate/resume |

---

## 2. Unit cases

### Verifier
- [ ] rejects cycle (A→B→A)  
- [ ] accepts map-over-list graph  
- [ ] rejects unreachable node  
- [ ] rejects join with <1 logical fanout source  
- [ ] rejects fanout without downstream join/map (warning or error per design)  
- [ ] rejects agent missing prompt  
- [ ] rejects unknown model id (mocked registry)  
- [ ] rejects gate without channel/approver  
- [ ] rejects `dangerously_skip` / equivalent flags in IR  
- [ ] budget worst-case fanout × branch cost exceeds max → reject  
- [ ] template var unknown → reject  
- [ ] conditional edge bad port → reject  

### Expr / templates
- [ ] `{{ a.output.x }}` resolution  
- [ ] `$.n > 0` true/false  
- [ ] missing path → false or error (document pick; test both)  

### Store
- [ ] atomic checkpoint replace survives kill mid-write (simulate)  
- [ ] run_id uniqueness in index  
- [ ] list/filter by status  

### Reducers
- [ ] `top_k` ordering stable  
- [ ] `concat` preserves branch ids  
- [ ] `first_k` short-circuits barrier (if implemented)  

---

## 3. Integration — FakeWorkerDriver

Replace LLM with `FakeWorker`:
- returns canned envelope by node_id  
- can sleep / raise / exceed budget  

### Flows
- [ ] linear 3-node success → RunEnvelope.succeeded  
- [ ] mid script fail → partial + resume retries script only  
- [ ] fanout 3 branches join → 3 node_run ids  
- [ ] gate parks run (status awaiting_gate); `decide_gate(approve)` continues  
- [ ] gate shelve → downstream skipped  
- [ ] cancel while running → cancelled  
- [ ] crash after node success before checkpoint → resume does not dup side effects if checkpoint rules held  
- [ ] schema mismatch on agent output → node failed  
- [ ] dry_run compiles & plans ready set without spawning  

---

## 4. CLI tests

- [ ] `validate` good yaml exit 0  
- [ ] `validate` bad yaml exit 2  
- [ ] `run` writes `runs/<id>/`  
- [ ] `status` json stable keys  
- [ ] `gate` wrong status → error  
- [ ] soft-import: removing workflow package doesn’t crash `hermes --help`  

---

## 5. Worker / delegate seam

- [ ] inherit path calls delegate_task (mock) with rendered prompt  
- [ ] override path constructs agent with model/tools (mock AIAgent)  
- [ ] pause kill-switch respected if siblingophobic delegate pauses  
- [ ] depth limits not violated (workflow driver shouldn’t nest workflow inside agent without policy)  

---

## 6. Security tests

- [ ] script subprocess cannot read outside workspace (tmp root)  
- [ ] network denied by default for scripts  
- [ ] dual_control gate cannot be auto-approved unless flag + warning path  

---

## 7. Failure matrix (must document expected)

| Case | Expected |
|------|----------|
| parent cancelled | children cancelled/skipped |
| one fanout branch fails, join=all | join failed or partial per policy (define `reduce.on_fail`) |
| gate timeout shelve | run ends no exec |
| budget trip at node | node failed BUDGET; run gate or stop |

**Open implementer choice:** `reduce.on_fail: fail_join | ignore_branch` — pick default `fail_join` for v1; test both if feature-flagged.

---

## 8. E2E (manual / nightly)

1. workflow_enabled true  
2. linear agent with `max_turns: 2` mocked tools  
3. confirm envelopes + cost field present  
4. telegram notify optional soak  

---

## 9. Perf smoke (not gating)

- 20-node linear fake workers < 2s  
- fanout 10 fake < 5s wall with max_parallel_nodes=4  

---

## 10. Definition of done (MVP)

All unit + integration FakeWorker suites green on CI with hermetic env; CLI help lists `workflow` when package present; master switch default false so default installs unchanged.
