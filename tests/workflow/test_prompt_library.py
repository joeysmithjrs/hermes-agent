"""Prompt library — `spec.prompt: {library: <name>, params: {...}}`.

Post-Phase-3 commit 3. Covers the naming split that motivated the feature
(`library:` is NOT `template:`), pure load+render with no model calls,
fail-closed on an unknown library, and the two-stage rendering contract
(params substituted at load, `{{ node.output.* }}` resolved at run time).
"""

from __future__ import annotations

import pytest

from workflow.prompts import library as lib
from workflow.prompts.library import PromptLibraryError

BRIEF = """
name: desk-brief-v1
version: 2
description: a desk brief
tags: [desk]
param_schema:
  type: object
  required: [profile]
prompt: |
  You are the {{ params.profile }} reviewer.
  Prior summary: {{ seed.output.summary }}
"""


@pytest.fixture
def user_lib(wf_home):
    d = wf_home / "workflows" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "desk-brief-v1.yaml").write_text(BRIEF, encoding="utf-8")
    return d


# ---- load ------------------------------------------------------------------


def test_load_library_reads_fields(user_lib):
    entry = lib.load_library("desk-brief-v1")
    assert entry.name == "desk-brief-v1"
    assert entry.version == 2
    assert entry.tags == ["desk"]
    assert "{{ params.profile }}" in entry.prompt
    assert entry.source_path.endswith("desk-brief-v1.yaml")


def test_unknown_library_fails_closed(wf_home):
    with pytest.raises(PromptLibraryError) as exc:
        lib.load_library("nope-v1")
    assert "unknown prompt library" in str(exc.value)


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", None, 5])
def test_invalid_library_names_rejected(bad):
    with pytest.raises(PromptLibraryError):
        lib.validate_library_name(bad)


def test_malformed_library_rejected(wf_home):
    d = wf_home / "workflows" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "empty-v1.yaml").write_text("name: empty-v1\nprompt: ''\n", encoding="utf-8")
    with pytest.raises(PromptLibraryError):
        lib.load_library("empty-v1")


def test_builtin_library_is_resolvable(wf_home):
    entry = lib.load_library("debate-participant-v1")
    assert "debate" in entry.tags
    assert "{{ params.topic }}" in entry.prompt
    assert "debate-participant-v1" in lib.list_libraries()


def test_user_library_shadows_builtin(wf_home):
    d = wf_home / "workflows" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "debate-participant-v1.yaml").write_text(
        "name: debate-participant-v1\nprompt: mine\n", encoding="utf-8"
    )
    assert lib.load_library("debate-participant-v1").prompt == "mine"


# ---- render (stage 1: params only) -----------------------------------------


def test_render_substitutes_params_and_leaves_node_templates(user_lib):
    out = lib.render_library_prompt("desk-brief-v1", {"profile": "macro"})
    assert "You are the macro reviewer." in out
    assert "{{ seed.output.summary }}" in out  # stage 2 is the driver's job


def test_render_missing_required_param_fails_closed(user_lib):
    with pytest.raises(PromptLibraryError):
        lib.render_library_prompt("desk-brief-v1", {})


def test_render_unknown_placeholder_fails_closed(wf_home):
    d = wf_home / "workflows" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x-v1.yaml").write_text("name: x-v1\nprompt: 'hi {{ params.who }}'\n", encoding="utf-8")
    with pytest.raises(PromptLibraryError) as exc:
        lib.render_library_prompt("x-v1", {"other": 1})
    assert "who" in str(exc.value)


def test_resolve_prompt_spec_passes_through_non_library_forms():
    assert lib.resolve_prompt_spec("plain") == "plain"
    assert lib.resolve_prompt_spec({"file": "p.md"}) == {"file": "p.md"}


def test_resolve_prompt_spec_rejects_unknown_keys(user_lib):
    with pytest.raises(PromptLibraryError):
        lib.resolve_prompt_spec({"library": "desk-brief-v1", "params": {"profile": "x"}, "oops": 1})


def test_renderer_makes_no_model_calls(user_lib, monkeypatch):
    """The library is pure load+render: the agent path must not be touched."""
    import workflow.runtime.live as live

    def explode(*a, **kw):
        raise AssertionError("prompt library must not construct an agent")

    monkeypatch.setattr(live, "build_runtime_parent", explode)
    lib.render_library_prompt("desk-brief-v1", {"profile": "macro"})


# ---- verifier integration --------------------------------------------------


LIB_WORKFLOW = """
id: lib_demo
nodes:
  - id: seed
    kind: agent
    spec: {prompt: "produce a summary"}
  - id: reviewer
    kind: agent
    spec:
      prompt:
        library: desk-brief-v1
        params:
          profile: macro
edges:
  - { from: seed, to: reviewer }
"""


def test_verify_accepts_library_prompt(user_lib):
    from workflow import compile_text

    vir = compile_text(LIB_WORKFLOW)
    # the IR keeps the library REF (not the expanded body): the recipe stays
    # readable and the library stays the single source of truth
    node = [n for n in vir.ir.nodes if n.id == "reviewer"][0]
    assert node.spec.prompt == {"library": "desk-brief-v1", "params": {"profile": "macro"}}


def test_verify_rejects_unknown_library(wf_home):
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    with pytest.raises(WorkflowRejected) as exc:
        compile_text(LIB_WORKFLOW.replace("desk-brief-v1", "does-not-exist"))
    codes = {i.code for i in exc.value.issues if i.severity == "error"}
    assert "PROMPT_LIBRARY" in codes


def test_verify_rejects_missing_library_param(user_lib):
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    text = LIB_WORKFLOW.replace("        params:\n          profile: macro\n", "")
    with pytest.raises(WorkflowRejected) as exc:
        compile_text(text)
    codes = {i.code for i in exc.value.issues if i.severity == "error"}
    assert "PROMPT_LIBRARY" in codes


def test_verify_checks_node_refs_inside_the_library_body(wf_home):
    """A typo'd node id inside a library body is caught at compile time, in the
    workflow that uses it -- the library is checked in context, not blindly."""
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    d = wf_home / "workflows" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "typo-v1.yaml").write_text(
        "name: typo-v1\nprompt: 'use {{ nosuchnode.output.x }}'\n", encoding="utf-8"
    )
    text = """
id: typo_demo
nodes:
  - id: a
    kind: agent
    spec:
      prompt: {library: typo-v1}
edges: []
"""
    with pytest.raises(WorkflowRejected) as exc:
        compile_text(text)
    codes = {i.code for i in exc.value.issues if i.severity == "error"}
    assert "TEMPLATE" in codes


# ---- driver integration (stage 2) ------------------------------------------


def test_driver_renders_library_prompt_before_the_worker_sees_it(user_lib):
    import workflow
    from workflow.ir import Node
    from workflow.runtime.worker import FakeWorker

    seen = {}

    class RecordingWorker(FakeWorker):
        def run_node(self, node: Node, ctx):
            seen[node.id] = node.spec.prompt if node.spec else None
            if node.id == "seed":
                return {"output": {"summary": "the desk is calm"}, "cost_usd": 0.0}
            return super().run_node(node, ctx)

    vir = workflow.compile_text(LIB_WORKFLOW)
    env = workflow.run(vir, worker=RecordingWorker())

    assert env["status"] == "succeeded"
    prompt = seen["reviewer"]
    assert "You are the macro reviewer." in prompt          # stage 1: params
    assert "Prior summary: the desk is calm" in prompt      # stage 2: node ref
    assert "{{" not in prompt


def test_library_params_may_reference_node_outputs(wf_home):
    import workflow
    from workflow.ir import Node
    from workflow.runtime.worker import FakeWorker

    d = wf_home / "workflows" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "topic-v1.yaml").write_text(
        "name: topic-v1\nprompt: 'debate {{ params.topic }}'\n", encoding="utf-8"
    )
    text = """
id: param_ref
nodes:
  - id: seed
    kind: agent
    spec: {prompt: "pick a topic"}
  - id: arguer
    kind: agent
    spec:
      prompt:
        library: topic-v1
        params:
          topic: "{{ seed.output.topic }}"
edges:
  - { from: seed, to: arguer }
"""
    seen = {}

    class RecordingWorker(FakeWorker):
        def run_node(self, node: Node, ctx):
            seen[node.id] = node.spec.prompt if node.spec else None
            if node.id == "seed":
                return {"output": {"topic": "rates"}, "cost_usd": 0.0}
            return super().run_node(node, ctx)

    env = workflow.run(workflow.compile_text(text), worker=RecordingWorker())
    assert env["status"] == "succeeded"
    assert seen["arguer"] == "debate rates"


def test_library_prompt_works_inside_a_map_branch(user_lib):
    import workflow
    from workflow.ir import Node
    from workflow.runtime.worker import FakeWorker

    text = """
id: lib_map
nodes:
  - id: seed
    kind: agent
    spec: {prompt: "summarize the desk"}
  - id: fan
    kind: map
    over: "{{ input.items }}"
    max_branches: 3
    branch:
      kind: agent
      spec:
        prompt:
          library: desk-brief-v1
          params:
            profile: "{{ branch }}"
edges:
  - { from: seed, to: fan }
"""
    seen = []

    class RecordingWorker(FakeWorker):
        def run_node(self, node: Node, ctx):
            if node.id == "seed":
                return {"output": {"summary": "calm"}, "cost_usd": 0.0}
            if node.kind == "agent":
                seen.append(node.spec.prompt)
            return super().run_node(node, ctx)

    env = workflow.run(
        workflow.compile_text(text),
        input={"items": ["macro", "credit"]},
        worker=RecordingWorker(),
    )
    assert env["status"] == "succeeded"
    # branches may complete in any order under max_parallel_nodes > 1
    rendered = "\n".join(seen)
    assert "You are the macro reviewer." in rendered
    assert "You are the credit reviewer." in rendered
    assert "Prior summary: calm" in rendered


def test_expr_and_library_are_distinct_concepts(user_lib):
    """`template:` is still expr.py's {{ }} interpolation (verifier code
    TEMPLATE); the reusable-body feature is `library:` (code PROMPT_LIBRARY).
    A node using `template:` as a prompt key gets no library treatment."""
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    text = LIB_WORKFLOW.replace("library: desk-brief-v1", "template: desk-brief-v1")
    # `{template: ...}` is not a library ref: it stays an opaque non-string
    # prompt, so it compiles but never resolves to the library body.
    vir = compile_text(text)
    node = [n for n in vir.ir.nodes if n.id == "reviewer"][0]
    assert node.spec.prompt["template"] == "desk-brief-v1"

    # and dsl.expr.template remains the unimplemented interpolation helper --
    # this feature did not overload that name
    import workflow.dsl as dsl

    with pytest.raises(NotImplementedError):
        dsl.expr.template("x")
