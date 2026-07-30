# Workflow Dispatch specs (Joe fork)

Hermes-native **code-defined multi-agent workflow** design for `joeysmithjrs/hermes-agent`.

## Index

| Doc | Role |
|-----|------|
| [DESIGN](./2026-07-29-workflow-dispatch-design.md) | **Authority** — IR, driver, security, PM mapping |
| [API](./2026-07-29-workflow-dispatch-api.md) | DSL/YAML/CLI/tool/store contracts |
| [UPSTREAM](./2026-07-29-workflow-dispatch-upstream.md) | Fork/sync path, forbidden files |
| [EXAMPLES](./2026-07-29-workflow-dispatch-examples.md) | Linear, fanout+gate, PM desk, cron/webhook |
| [TEST PLAN](./2026-07-29-workflow-dispatch-test-plan.md) | Hermetic tests + failure matrix |
| [PHASES](./2026-07-29-workflow-dispatch-phases.md) | MVP → production plan |
| [AUDIT](./AUDIT.md) | Adversarial audit · SHIP-WITH-FIXES |
| [IMPL_DIRECTIVE](./IMPL_DIRECTIVE.md) | **Claude Code implementation law** |
| [CC_LAUNCH](./CC_LAUNCH.md) | How to spawn the multi-model swarm |
| [CC_PROMPT_SHORT](./CC_PROMPT_SHORT.md) | Compact `-p` seed |
| [minimal_workflow.py](./minimal_workflow.py) · [minimal_workflow.yaml](./minimal_workflow.yaml) | Sketches |
| [PHASE3_SURFACES](./PHASE3_SURFACES.md) | Phase 3 operator surfaces — watch/cost/chain/schedule |
| [POST_PHASE3_SPEC](./POST_PHASE3_SPEC.md) | Post-Phase-3 architecture — workspace, catalog, library, debate, supervisor, loop-back |
| [POST_PHASE3_SURFACES](./POST_PHASE3_SURFACES.md) | Post-Phase-3 user-facing surfaces + `examples-post-phase3-*` |
| [SUMMARY](./SUMMARY.md) | Status package |

## One-liner

Verified IR + deterministic driver wraps `delegate_task` leaves; control flow is not an LLM loop; gates/checkpoints/status are first-class; packaging is a new `workflow/` package default-off for upstream safety.
