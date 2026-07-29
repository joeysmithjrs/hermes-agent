# illustrative sketch — not executable until workflow package exists
from __future__ import annotations

# from workflow import Workflow, node, gate

SCHEMA_BRIEF = {
    "type": "object",
    "required": ["bullets", "sources"],
    "properties": {
        "bullets": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
}

def build_linear_brief():
    """Mirrors examples §1 once DSL ships."""
    raise NotImplementedError("Phase 1: implement workflow.dsl")
    # with Workflow("linear_brief", defaults=node.agent(model="openrouter/x-ai/grok-4.5")) as wf:
    #     r = wf.agent("research", prompt="Research {{ input.topic }} …", tools=["web_search"], output=SCHEMA_BRIEF)
    #     w = wf.agent("write", prompt="Write briefing from {{ research.output }}", tools=["write_file"])
    #     n = wf.script("notify", run="workflow.examples.notify_telegram")
    #     r.then(w).then(n)
    #     return wf.compile()

def build_pm_skeleton():
    """See 2026-07-29-workflow-dispatch-examples.md §3."""
    raise NotImplementedError("Phase 1+")
