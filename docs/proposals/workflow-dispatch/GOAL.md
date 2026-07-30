# /goal — Hermes-native Workflow Dispatch (Claude Code–style orchestration primitives)

**Goal status:** ACTIVE until a complete, reviewer-ready DESIGN SPEC is written to disk.  
**This run is SPEC ONLY** — do not implement production code beyond tiny illustrative stubs if they clarify the design. Primary deliverable is documents + optional yarn-level examples.

**Repository:** Joe’s fork `joeysmithjrs/hermes-agent`  
**Worktree:** `/home/hermes/research/hermes-workflow-dispatch` branch `feat/workflow-dispatch`  
**Upstream sensitivity:** Design so master/upstream sync stays easy. Prefer **new package / plugin / thin CLI surface / optional tool**, not invasive rewrites of `run_agent.py` conversation loop mid-turn (prompt cache sacred).

---

## Product goal

Add **core orchestration primitives** so Hermes can run **code-defined multi-agent workflows** (“Claude Code style” in the *orchestrate workers via explicit structure* sense — not “reimplement Claude Code”).

Key idea:
- **Agents = subagents / profiles / isolated runtimes**
- **Control flow = code (Python DSL or declarative YAML→compiled plan)** not an LLM re-deriving the graph every step
- A **checker/linter/verifier** validates plans before run
- Runtime **persists stage results**, hands off to next nodes, ends in a **standard completion record**
- Runnable from: **(1) tool call in conversation**, **(2) cron**, **(3) webhook/event**
- **Checkpointing / status** first-class
- **Per-node model + prompt (+ toolset + profile)** overrides

Reference architecture inspiration (study, don’t vendor as dependency):
- `/home/hermes/research/hermes-multi-agent-workflow` — fat engine, thin skill, Kanban fan-in/out, human gate
- Vault note: `/home/hermes/the-journey/Build In Public/Experiments/Ideas/Branches/PM Autonomy Pipeline.md`
- Existing Hermes: `delegate_task`, kanban dispatcher, cron (`context_from`, `script`, `no_agent`), webhooks

---

## Non-goals (this SPEC phase)

- Full production implementation and merge
- Replacing kanban or killing `delegate_task`
- Mid-conversation tool schema mutation that breaks prompt cache
- Building a full BPMN GUI
- Making the main chat LLM the DAG interpreter every hop

---

## Deliverables (must all exist before you stop)

Write under the worktree:

1. **`docs/superpowers/specs/2026-07-29-workflow-dispatch-design.md`**  
   Complete design spec (authority doc).

2. **`docs/superpowers/specs/2026-07-29-workflow-dispatch-api.md`**  
   Public surfaces: Python DSL sketch, YAML schema, CLI commands, tool schema (if any), completion JSON schema, checkpoint schema, error model.

3. **`docs/superpowers/specs/2026-07-29-workflow-dispatch-upstream.md`**  
   File ownership map: NEW paths favored; list of forbidden/critical-touch files; merge/sync strategy with Nous upstream; plugin vs core decision.

4. **`docs/superpowers/specs/2026-07-29-workflow-dispatch-examples.md`**  
   At least:
   - linear 3-stage research → write → notify  
   - fan-out research → fan-in eval → human/dual-control gate → exec  
   - PM-desk shaped skeleton (map stages from PM Autonomy Pipeline)  
   - cron + webhook invocation examples  

5. **`docs/superpowers/specs/2026-07-29-workflow-dispatch-test-plan.md`**  
   Unit/integration/e2e plan, hermetic tests, failure cases (partial node fail, parent cancel, idempotent resume).

6. **`docs/superpowers/specs/2026-07-29-workflow-dispatch-phases.md`**  
   Phased implementation plan (MVP → durable → full surfaces) with acceptance criteria per phase.

7. Optional but preferred: **`docs/superpowers/specs/examples/minimal_workflow.py`** and **`minimal_workflow.yaml`** as **non-runnable or lightly-typed sketches** matching the DSL (spec clarity).

8. A short **`docs/superpowers/specs/README.md`** index linking all of the above.

When done, leave a one-page **SUMMARY.md** at  
`docs/superpowers/specs/SUMMARY.md` with status DONE, file list, residual open questions.

---

## Design requirements (must address stretch goals)

### A. Primitives
Define minimal IR (intermediate representation), e.g. concepts like:
- Workflow / Graph / Node / Edge  
- Node kinds: `agent`, `script`, `gate`, `fanout`, `barrier`/`join`, `map`, `cron_trigger`, `webhook_trigger` (only those justified)  
- Artifact store / results references  
- Run ID, attempt ID, node run ID  
- Status enum: pending, ready, running, succeeded, failed, skipped, cancelled, awaiting_gate  
- Standard **completion envelope** (schemas for success/fail/partial)

Prefer small orthogal set over kitchen sink.

### B. Code-first authoring
Propose Python DSL that an agent (or human) can generate:

```python
# illustrative only — invent the real API carefully
with Workflow("pm_desk") as wf:
    ctx = node.agent("prepare_context", model="...", prompt=..., tools=...)
    ...
```

And/or YAML that compiles to the same IR.  
**Lint/verify** before execute: acyclic checks (or explicit loop construct), unknown models, missing prompts, unreachable nodes, join without fanout, gate without notify channel, budget caps, sandbox tool allowlists.

### C. Execution model
- **Not** “orchestrator LLM loops calling subagents ad hoc” as the primary engine  
- **Yes** driver/runtime walks verified IR, spawns workers, waits, checkpoints  
- Workers may be: in-process leaf agent runs, profile spawns, or process boundaries  
- Map onto / extend: `delegate_task`, kanban dispatcher, or a **new workflow runner service** — pick with rationale  
- Persistence: filesystem under `$HERMES_HOME/workflows/` and/or sqlite — specify  
- Resume from checkpoint after crash  
- Status query API/CLI: `hermes workflow status RUN_ID`

### D. Invocation surfaces
1. **Tool** e.g. `workflow_run` / `workflow_status` (cache-safe: doesn’t mutate parent tools mid-turn)  
2. **CLI** `hermes workflow …`  
3. **Cron** job type or cron wrapping CLI  
4. **Webhook** event → start run or signal gate  

### E. Per-agent granularity
Each agent node specifies where applicable:
- model + provider  
- system/prompt (+ optional skill list)  
- toolsets / allow/deny  
- profile or isolated HERMES_HOME slice  
- timeout, max budget USD, max turns  
- workspace dir  
- input artifact mapping / output schema (JSON schema preferred)

### F. Upstream-friendly packaging
**Strong preference order:**
1. New package directory e.g. `workflow/` or `hermes_workflow/` + CLI subcommand registration that can soft-fail if optional  
2. Optional core tool behind config flag / toolset `workflow` disabled by default  
3. Plugin under `plugins/` only if it can call public APIs  

Document every needed hook into existing:
- `hermes_cli/main.py` (command registration)  
- tool registry  
- gateway (only if needed for webhook)  
- cron  

Avoid rewriting `run_agent.py` hot path unless unavoidable; if unavoidable, minimize and feature-flag.

### G. Alignment with PM Autonomy Pipeline
Explicit mapping table from:
`Prepare → seed → directive → DQ → DD → eval → plan → exec ∥ monitor → scribe`  
to proposed primitives (including dual-control gate and monitors).

### H. Security
- Gates dual-control  
- Secret scavenging  
- Tool allowlists per node  
- No blind `dangerously-skip` as default  
- Tenant/path isolation for multi-run  

### I. Observability
- Event log per node  
- `hermes workflow logs RUN`  
- Optional Telegram notify on gate/fail/complete  
- Cost aggregation per run  

---

## Method of work (how you should operate)

1. Read `AGENTS.md` footprint ladder and cache rules.  
2. Skim existing: `tools/delegate_tool`/`delegate_task`, `cron/`, `kanban` related code, gateway webhook bits — cite real paths.  
3. Skim `/home/hermes/research/hermes-multi-agent-workflow/docs/01-architecture.md` and `engine/engine.py`.  
4. Draft the DESIGN DOC fully before API doc.  
5. Be concrete: name modules, tables/files, function signatures, config keys.  
6. Call out open questions only at the end; resolve what you can with reasoned defaults.  
7. Do **not** spend turns implementing a full runner. Spec + sketches only.  
8. Write all files to disk; don’t leave the answer only in chat.  

## Stop condition

You may stop when:
- All deliverable files exist and are internally consistent  
- SUMMARY.md says DONE  
- A skilled Hermes contributor could implement Phase 1 from the docs alone without redesigning architecture  

**Budget:** respect `--max-budget-usd 10`. Prefer finishing the docs early over perfect polish if budget tightens — complete DESIGN + API + phases first, then examples/tests/upstream.

## Quality bar

Write like an internal RFC: sharp, implementable, tradeoff-aware, no motivational fluff.
