# /goal — Adversarial audit of Workflow Dispatch specs (GLM only)

**Status:** ACTIVE until AUDIT.md + patched specs (if needed) are on disk.  
**Model constraint:** You MUST use only `z-ai/glm-5.2`. Do not escalate to Opus/Sonnet/other models. If tools tempt model switch, refuse and stay on GLM.  
**Budget:** `--max-budget-usd 5` hard. Prefer finishing AUDIT + critical patches over polish.  
**Scope:** SPEC AUDIT + CORRECTIONS ONLY. No full runtime implementation.

## Inputs (read fully)

Worktree: `/home/hermes/research/hermes-workflow-dispatch` branch `feat/workflow-dispatch`

```
docs/proposals/workflow-dispatch/
  GOAL.md
  README.md
  SUMMARY.md
  2026-07-29-workflow-dispatch-design.md    # authority
  2026-07-29-workflow-dispatch-api.md
  2026-07-29-workflow-dispatch-upstream.md
  2026-07-29-workflow-dispatch-examples.md
  2026-07-29-workflow-dispatch-test-plan.md
  2026-07-29-workflow-dispatch-phases.md
  minimal_workflow.py / .yaml
```

Hermes source (cite real paths):  
`AGENTS.md`, `tools/` delegate_task, `cron/`, kanban-related, `hermes_cli/main.py` registration patterns, tool registry / toolsets, gateway webhook if relevant.

Reference (optional, ideas only): `/home/hermes/research/hermes-multi-agent-workflow` engine/docs.

## Audit axes (be adversarial)

1. **Hermes-primitive reuse / DRY**  
   Are we reinventing kanban dispatcher, cron chaining (`context_from`, `script`, `no_agent`), `delegate_task`, session DB, webhook router, profile isolation? Where should we *compose* instead of fork? Where is a new primitive truly justified?

2. **Efficiency**  
   Persistence shape, event log volume, fanout cardinality bombs, double execution on resume, prompt/cache impact, number of profile processes, sqlite vs FS overhead.

3. **Correctness / contradictions**  
   Internal inconsistencies across design/api/examples/phases. Missing error paths. Join/fanout edge cases. Gate + dual-control holes. Status enum gaps. Cycle story (trigger-chain) holes.

4. **Upstream safety**  
   Touch-surface honesty vs AGENTS.md footprint ladder. Soft-import CLI. Default-off. Anything that will make rebase hell.

5. **Security**  
   Script sandbox claims vs reality; secrets; tool allowlists; auto-approve paths; multi-tenant path isolation.

6. **PM desk fitness**  
   Does §11 mapping actually work for dual-control, monitors, DQ≤3, paper vs live?

7. **Implementability**  
   Could Phase 1 ship without resolving a hidden ambiguity? List blockers.

## Method

1. Read all proposal docs first (and critical Hermes source).  
2. Write findings as you go into `docs/proposals/workflow-dispatch/AUDIT.md` with severity:  
   `P0 blocker · P1 should-fix · P2 nice · P3 nit`  
3. For each P0/P1: propose a concrete correction.  
4. **Apply corrections** by editing the authoritative design/api/examples/phases/test docs (patch in place). Keep a short changelog section in AUDIT.md (what changed where).  
5. Do not bloat — prefer surgical patches over rewrites.  
6. If a section is solid, say so briefly (avoid discard-rewrite aesthetics).

## Deliverables

1. **`docs/proposals/workflow-dispatch/AUDIT.md`** (required)  
   - Executive verdict (ship / ship-with-fixes / rethink)  
   - Findings table  
   - DRY/reuse map vs existing Hermes primitives  
   - Efficiency risks  
   - Changelog of patches applied  
   - Residual questions  

2. Patched specs as needed (same folder).  

3. Update **SUMMARY.md** with audit stamp + new residual questions.

## Stop when

AUDIT.md complete, P0s fixed or explicitly accepted with rationale, SUMMARY updated. Prefer staying under $5.

## Tone

Staff-eng review. Blunt. Cite files/sections. No fluff.
