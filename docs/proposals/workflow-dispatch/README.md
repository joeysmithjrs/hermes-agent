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
| [examples/](./minimal_workflow.py) · (./minimal_workflow.yaml) | Sketch py/yaml |
| [SUMMARY](./SUMMARY.md) | Status package |
| [GOAL](../GOAL_workflow_dispatch.md) | Original brief |

## One-liner

Verified IR + deterministic driver wraps `delegate_task` leaves; control flow is not an LLM loop; gates/checkpoints/status are first-class; packaging is a new `workflow/` package default-off for upstream safety.
