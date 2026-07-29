# DIRECTIVE — Implement Hermes Workflow Dispatch (Phase 1 → working CLI)

**To:** Claude Code multi-model swarm (orchestrator + specialized agents)  
**From:** Joe / Hermes operator  
**Repo:** `joeysmithjrs/hermes-agent`  
**Date:** 2026-07-29  
**Mode:** IMPLEMENTATION (not another redesign). Spec is audited **SHIP-WITH-FIXES**.

---

## 0. One-sentence mission

Ship a **Hermes-native, default-off** `workflow/` package: **code/YAML → verified IR → deterministic driver → checkpointed runs**, with leaves via **`delegate_task` inherit path**, CLI `hermes workflow …`, FakeWorker tests green — **no `run_agent.py` edits**.

---

## 1. Worktree / branch / start state

| Item | Value |
|------|--------|
| Preferred worktree | Create fresh from latest `main` OR reuse `/home/hermes/research/hermes-workflow-dispatch` |
| Branch | `feat/workflow-dispatch` (rebase onto current `origin/main` first — main has CCR multi-model merge `ffc08cd3`) |
| Spec package (authority) | `docs/proposals/workflow-dispatch/` **in this worktree** |
| Live install reference | `/opt/hermes-agent` (read-only patterns; do not scoopshodder-edit bare main without branch) |

### First git actions

```bash
cd <worktree>
git fetch origin
git checkout feat/workflow-dispatch 2>/dev/null || git checkout -b feat/workflow-dispatch origin/main
git rebase origin/main   # resolve only soft-import conflicts if any
# ensure specs present at docs/proposals/workflow-dispatch/
# if missing, copy from /home/hermes/research/hermes-workflow-dispatch/docs/proposals/workflow-dispatch/
```

Specs **must** exist before coding. Canonical paths (ignore ghosts under gitignored `docs/superpowers/`):

```
docs/proposals/workflow-dispatch/
  2026-07-29-workflow-dispatch-design.md   # AUTHORITY + audit patches inlined
  2026-07-29-workflow-dispatch-api.md
  2026-07-29-workflow-dispatch-phases.md
  2026-07-29-workflow-dispatch-test-plan.md
  2026-07-29-workflow-dispatch-examples.md
  2026-07-29-workflow-dispatch-upstream.md
  AUDIT.md                                  # F1–F11 MUST be satisfied by code
  SUMMARY.md
  minimal_workflow.yaml
```

---

## 2. Model routing (use the multi-model CCR setup)

**Orchestrator (parent Claude Code process):** `z-ai/glm-5.2`  
Launch:

```bash
claude --model z-ai/glm-5.2 --dangerously-skip-permissions ...
# or: claude-glm ...
```

**Subagents (Task tool / agent definitions under `~/.claude/agents/`):**

| Agent file | Model | Use for |
|------------|-------|---------|
| `scout-glm` | `z-ai/glm-5.2` | Cheap codebase map, “where is X?” |
| `think-inkling` | `thinkingmachines/inkling` | IR edge cases, status machines, resume invariants |
| `reason-grok` | `x-ai/grok-4.5` | Adversarial review of design compliance / PR risk |
| `build-terra` | `gpt-5.6-terra` | Core package implementation |
| `code-kimi` | `moonshotai/kimi-k3` | Tests + glue + CLI polishing |
| `code-kimi-code` | `moonshotai/kimi-k2.7-code` | Dense codegen bursts if needed |

### Swarm protocol (orchestrator-enforced)

1. **Scout (glm)** — map hermes CLI registration, `delegate_task`, `hermes_state` sqlite helpers, config.yaml patterns. Return file:line map only.  
2. **Think (inkling)** — confirm Phase 1 state machine + store layout against design §2/§4 + AUDIT F1/F6/F7. Write short `IMPLEMENTATION_CHECKLIST.md` in specs folder if gaps.  
3. **Build (terra)** — implement `workflow/` package + soft CLI register.  
4. **Code/tests (kimi)** — `tests/workflow/` hermetic FakeWorker suite.  
5. **Reason (grok)** — adversarial pass: AGENTS.md footprint, AUDIT F-list, no sacred-file touch. Produce `REVIEW_NOTES.md` or inline PR body section.  
6. **Orchestrator** — integrate, run pytest, fix, commit, optionally open PR (**do not merge** unless Joe says).

Do **not** burn Opus. Stay on OpenRouter ids above. Terra bare id is `gpt-5.6-terra` (not `openai/gpt-5.6-terra` as catalog model — CCR rewrites upstream).

---

## 3. Non-negotiables (fail the job if violated)

1. **Control flow is code, not an LLM loop.** Driver walks verified IR.  
2. **No edits to `run_agent.py`** in Phase 1.  
3. **No mid-conversation tool schema mutation.** Conversation tool is Phase 2 / default-off.  
4. **Default-off:** `workflow.enabled: false` (or equivalent).  
5. **Packaging:** new top-level `workflow/` package + soft-import CLI (Footprint Ladder rung 2).  
6. **AUDIT must be code-true:**
   - **F1** store key = `node_run_id` (not bare `node_id` under fanout)  
   - **F2** Phase 1 inherit only; verifier **rejects** override-only fields (`model`/`tools`/`profile`/workspace enforcement that inherit can’t do) — optional warn flag OK  
   - **F4** `run:` allowlist / registry only (no arbitrary import of `os.system`)  
   - **F5** `max_branches` required + runtime `CARDINALITY`  
   - **F6** `side_effects: external` → no auto-requeue on resume  
   - **F7** run status includes `awaiting_gate` / `paused` (even if gate runtime is stub/P2, enums + types exist)  
   - **F8** reject `approve_auto` when `dual_control: true`  
   - **F11** sqlite hardening: reuse pattern/`hermes_state` helpers — don’t invent bold NFS code  
7. **Reuse** `delegate_task` for agent leaves (inherit path). Do not reimplement a second agent loop.  
8. **Acyclic v1**; loops = new run (trigger chain later).  
9. **Hermetic tests** with tmp `HERMES_HOME`.  

---

## 4. Phase 1 scope (build this — stop at acceptance)

### 4.1 Package layout (target)

```
workflow/
  __init__.py
  ir.py                 # dataclasses WorkflowIR, Node, Edge, NodeSpec, ...
  yaml_load.py
  dsl.py                # optional if time; YAML is MVP-must
  verify.py
  expr.py               # tiny template + condition language
  runtime/
    driver.py
    worker.py           # inherit path shim → delegate_task / FakeWorker interface
    scripts.py          # allowlisted callables
    events.py
  store/
    fs.py
    index.py
    checkpoint.py
  cli.py                # hermes workflow ***
  schemas/              # node_run + run envelopes JSON schema (can be lightweight)
tests/workflow/
  ...
docs/proposals/workflow-dispatch/   # already exists; add IMPLEMENTATION_LOG.md
```

### 4.2 Node kinds in Phase 1 runtime

| kind | Required P1 |
|------|-------------|
| `agent` | Yes — inherit → FakeWorker in tests; real `delegate_task` behind interface |
| `script` | Yes — allowlisted registry |
| `fanout` | Yes |
| `join` | Yes — `reduce.type` in `{concat, top_k}` (+ file-defined simple variants) |
| `gate` | Enums + verify rules **yes**; full park/resume runtime **nice if small**, else stub status + CLI undoc until Phase 2 |
| `map` | No (Phase 3) |

### 4.3 CLI (must)

```
hermes workflow validate PATH
hermes workflow compile PATH -o ...
hermes workflow run PATH_OR_ID [--input JSON] [--resume RUN] [--from NODE] [--retry-failed] [--dry-run]
hermes workflow status RUN_ID
hermes workflow logs RUN_ID [--node ID]
hermes workflow list
hermes workflow cancel RUN_ID
hermes workflow doctor
# gate CLI can be stub with clear "Phase 2" message if gate runtime deferred
```

Registration: soft-import from `hermes_cli/main.py` — if `workflow` import fails, `hermes --help` still works.

### 4.4 Persistence

```
$HERMES_HOME/workflows/
  definitions/<workflow_id>.json
  runs/<run_id>/
    run.json
    checkpoint.json
    nodes/<node_run_id>/output.json
    nodes/<node_run_id>/events.jsonl
    run_output.json
  index.sqlite
```

- `run_id`: **uuid4-based** (AUDIT residual F17 — do it now)  
- Atomic checkpoint via temp + `os.replace`  
- FS source of truth; sqlite is index  

### 4.5 Config

```yaml
# config.yaml
workflow:
  enabled: false
  max_budget_usd: 10.0
  max_parallel_nodes: 4
  # optional: phase1_warn_overrides: false  # false = reject overrides
```

No new required `HERMES_*` env vars for behavior.

### 4.6 Acceptance checklist (exit criteria)

Copy to PR body when green:

- [ ] `pytest tests/workflow -q` green hermetic  
- [ ] Linear yaml end-to-end FakeWorker → RunEnvelope succeeded  
- [ ] Fanout 3 → join → **3 distinct** `node_run_id` output paths  
- [ ] Resume after kill: no double-finalize of succeeded; `side_effects: external` → failed/INTERRUPTED not auto-rerun  
- [ ] oversize fanout → CARDINALITY  
- [ ] Verifier rejects Phase 1 override-only agent fields (tools/model/profile)  
- [ ] Verifier rejects unregistered `run:`  
- [ ] Verifier rejects `approve_auto` + `dual_control:true`  
- [ ] `hermes workflow validate docs/proposals/workflow-dispatch/minimal_workflow.yaml` works when enabled path imports  
- [ ] `hermes --help` OK if workflow package broken/missing  
- [ ] `git diff origin/main --stat` nearly all under `workflow/` + `tests/workflow/` + docs + **one soft-import hunk** in `hermes_cli/main.py`  
- [ ] **Zero** `run_agent.py` changes  

---

## 5. Implementation order (don’t freestyle)

### Slice A — IR + verify + yaml (1)
- Dataclasses matching design/api  
- YAML loader  
- Verifier (structure, refs, gates basic, max_branches, dual_control/approve_auto, run allowlist, P1 override reject, templates `{{ node.output.x }}`)  
- Unit tests for verifier only  

### Slice B — store + envelopes (2)
- FS layout + checkpoint + index  
- NodeRunEnvelope / RunEnvelope write/read  
- Resume load logic pure functions tested  

### Slice C — driver + FakeWorker (3)
- Ready-set computation  
- Linear + fanout/join  
- CARDINALITY / side_effects resume policy  
- Integration tests  

### Slice D — CLI soft-import (4)
- `workflow/cli.py`  
- main.py try/except register  
- smoke: validate/run/status against FakeWorker (env var `HERMES_WORKFLOW_FAKE=1` or inject);  

### Slice E — dual-use worker interface (5)
- `Worker` protocol: `FakeWorker` + `DelegateWorker`  
- `DelegateWorker` calls `delegate_task` with rendered prompt/context  
- Prefer not requiring live LLM in default CI  

### Slice F — adversarial + docs (6)
- Grok review  
- `IMPLEMENTATION_LOG.md` decisions  
- Commit(s) on `feat/workflow-dispatch`  
- Open PR to `main` of **fork only** (draft OK). **Do not merge** without Joe. Deploy bounces Hermes.

---

## 6. Hermes reuse map (DRY)

| Need | Use |
|------|-----|
| Leaf agent | `tools/delegate_tool.py::delegate_task` |
| Async batch | existing bg path under tools/async_delegation |
| Sqlite hardening ideas | `hermes_state.py` helpers/patterns |
| CLI patterns | other soft-import modules in `hermes_cli/` |
| Config | existing yaml config load pattern |
| Cron later | shell-out `hermes workflow run` via `script:` + `no_agent` — **document only** in P1 |

**Do not** build on kanban dispatcher as the bus (deferred; design §10).

---

## 7. Explicit out-of-scope (say no)

- Conversation tools / toolset in default bundle  
- Per-node model override subprocess workers (Phase 2)  
- Live Telegram gate UX (Phase 2)  
- Kanban adapter  
- PM desk real trading prompts (Phase 4 app)  
- Upstream Nous PR (fork first)  
- Redesigning the IR  

If something is blocked on residual SUMMARY questions 7–13, pick the **design/AUDIT default** and log it in IMPLEMENTATION_LOG.md — don’t stall.

---

## 8. Test standards (Hermes house style)

- Temp `HERMES_HOME` per test  
- Assert **invariants/contracts**, not brittle snapshots of model lists  
- FakeWorker records call counts for side-effects resume test  
- No network in unit/integration unless marked  

Primary file: follow  
`docs/proposals/workflow-dispatch/2026-07-29-workflow-dispatch-test-plan.md`  
including audit-added verifier cases.

---

## 9. Commit / PR policy

- Imperative commits, focused  
- Example: `feat(workflow): IR + verifier + FakeWorker driver (phase 1)`  
- PR title: `feat: workflow dispatch phase 1 (IR + driver + CLI)`  
- PR body: acceptance checklist above + AUDIT F coverage table  
- Author ok as agent on Joe’s fork  

**Do not** merge; Joe will merge when green (deploy restarts gateway).

---

## 10. Done definition for this directive

Claude Code may stop when:

1. Phase 1 acceptance checklist is all green on the workstation, **and**  
2. Changes committed on `feat/workflow-dispatch`, **and**  
3. PR opened (URL printed), **and**  
4. Short summary returned: files touched, test command output, residual debt → Phase 2 list.

---

## 11. Operator escalate / human gates

- Need live LLM spend for DelegateWorker smoke beyond FakeWorker: optional, mark optional job  
- Any temptation to edit `run_agent.py` → **stop and write a design note instead**  
- Budget: prefer FakeWorker completeness over burning tokens on real subagents  

---

## Appendix A — Minimal yaml to keep green

Use / extend `docs/proposals/workflow-dispatch/minimal_workflow.yaml`.  
For fanout: tests may use inline yaml fixtures under `tests/workflow/fixtures/`.

## Appendix B — Forbidden path recap

```
run_agent.py
agent/prompt_builder.py   # no schema swap
kanban schema as foundation
new _HERMES_CORE_TOOLS permanent liability
```

## Appendix C — Reading order (orchestrator + agents)

1. `AUDIT.md` (10 min)  
2. `phases.md` Phase 1  
3. `design.md` §1–§5, §12  
4. `api.md` package layout + CLI  
5. `upstream.md` touch list  
6. `test-plan.md`  
7. Hermes: `AGENTS.md` Footprint Ladder, `delegate_tool.py` `delegate_task` signature  

---

**End of directive.** Implement Phase 1. Use multi-model subagents as specified. Ship working `hermes workflow` on this fork.
