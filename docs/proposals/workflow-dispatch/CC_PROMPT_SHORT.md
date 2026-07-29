# Implement Hermes Workflow Dispatch — launch prompt (short)

You are implementing **Phase 1** of Hermes workflow dispatch on fork
`joeysmithjrs/hermes-agent`. Specs are audited SHIP-WITH-FIXES.

**Primary brief (full law):**  
`docs/proposals/workflow-dispatch/IMPL_DIRECTIVE.md`

**Also read first:**  
`docs/proposals/workflow-dispatch/AUDIT.md`,  
`docs/proposals/workflow-dispatch/2026-07-29-workflow-dispatch-phases.md` (Phase 1 only),  
design §1–5 amplifier facts as needed.

## Hard rules
- Deterministic driver + verified IR; control flow is NOT an LLM.
- New package `workflow/`; default-off; soft CLI import.
- Agent leaves via `delegate_task` inherit path (FakeWorker in tests).
- AUDIT F1–F11: node_run_id store, max_branches, run allowlist, side_effects external no-requeue, P1 reject of tools/model/profile overrides inherit can’t honor, uuid run_ids.
- **Forbidden:** `run_agent.py` edits; core toolset default-on; kanban-as-bus.

## Multi-model
Orchestrator: `z-ai/glm-5.2`. Use Task agents when useful:
- scout-glm, think-inkling, build-terra, code-kimi, reason-grok  
Models: as in `~/.claude/agents/*` and IMPL_DIRECTIVE §2.

## Done when
Phase 1 acceptance checklist green, commits on `feat/workflow-dispatch`, PR opened, **not** merged. Return PR URL + pytest summary.
