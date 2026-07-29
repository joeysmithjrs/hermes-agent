# SUMMARY — Workflow Dispatch Spec Package

**Status:** DONE (spec package complete)  
**Date:** 2026-07-29  
**Branch/worktree:** `feat/workflow-dispatch` @ `/home/hermes/research/hermes-workflow-dispatch`  
**Fork:** `joeysmithjrs/hermes-agent`

## What happened with Claude Code

- Launched with `--max-budget-usd 10`, `--max-turns 80`, backgrounded.
- Spent **~$12.21** total (router mixed glm-5.2 + claude-opus-4-8) and stopped with `error_max_budget_usd` / `budget_exhausted`.
- Delivered a strong **authority design doc** only (`2026-07-29-workflow-dispatch-design.md`, ~553 lines).
- Companion docs completed by Hermes (Joe session) from that design for a coherent package.

## File list

| File | Author |
|------|--------|
| `docs/superpowers/GOAL_workflow_dispatch.md` | Hermes brief |
| `docs/superpowers/specs/2026-07-29-workflow-dispatch-design.md` | Claude Code (primary) |
| `docs/superpowers/specs/2026-07-29-workflow-dispatch-api.md` | Hermes completion |
| `docs/superpowers/specs/2026-07-29-workflow-dispatch-upstream.md` | Hermes completion |
| `docs/superpowers/specs/2026-07-29-workflow-dispatch-examples.md` | Hermes completion |
| `docs/superpowers/specs/2026-07-29-workflow-dispatch-test-plan.md` | Hermes completion |
| `docs/superpowers/specs/2026-07-29-workflow-dispatch-phases.md` | Hermes completion |
| `docs/superpowers/specs/examples/minimal_workflow.py` | Hermes sketch |
| `docs/superpowers/specs/examples/minimal_workflow.yaml` | Hermes sketch |
| `docs/superpowers/specs/README.md` | Hermes index |
| `docs/superpowers/specs/SUMMARY.md` | this file |
| `cc_workflow_spec_run.json` | CC run envelope |

## Architecture in one paragraph

Workflows compile (Python DSL or YAML) to a **verified acyclic IR**. A **deterministic Driver** walks ready nodes, checkpoints under `$HERMES_HOME/workflows/`, and runs leaves via **`delegate_task` (inherit)** or a thin **`workflow.worker` override shim** (per-node model/tools/profile). Control flow is **not** an orchestrator LLM. **Gates** dual-control side effects. Invocation: CLI + optional tool + cron wrapper + webhook. Packaging: new **`workflow/`** package, soft CLI register, toolset default off — **no `run_agent.py` hot path**.

## Residual open questions

1. Exact cron job schema extension vs shell wrapper long-term  
2. `reduce.on_fail` default when one fanout leg dies (`fail_join` recommended)  
3. Whether override path needs any optional `delegate_task` kwarg vs pure AIAgent construct  
4. Telegram gate verb parsing ownership (gateway vs skill)  
5. Profile billing: child spend attribution into RunEnvelope cost field  
6. Accept/amend CC design D3–D6 after Joe review  

## Suggested next action

Joe review **design §12 decisions** + **PM mapping §11**. Then Phase 1 implementation on `feat/workflow-dispatch` (IR+verify+FakeWorker+CLI).

## Implementability check

A skilled Hermes contributor can implement **Phase 1** from DESIGN+API+TEST+PHASES without redesigning architecture. **DONE.**
