"""Named persistent workspaces (post-Phase-3 commit 1).

Contract tests, not snapshots: name validation fails closed, paths resolve
inside the workspace and nowhere else, per-run dirs are isolated while the top
level persists across runs, and a verified workflow can reference
`{{ workspace.* }}` only when it actually declares one.
"""

from __future__ import annotations

import pytest

from workflow.store import workspace as ws
from workflow.store.workspace import WorkspaceError


# ---- name validation (fail-closed) ----------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "..",
        "../escape",
        "desk/autonomy",
        "desk\\autonomy",
        "Desk-Autonomy",  # uppercase
        "_leading",
        ".hidden",
        "a" * 65,
        None,
        7,
    ],
)
def test_invalid_workspace_names_raise(bad):
    with pytest.raises(WorkspaceError):
        ws.validate_workspace_name(bad)


@pytest.mark.parametrize("good", ["desk-autonomy", "a", "w0", "audit_desk-2"])
def test_valid_workspace_names(good):
    assert ws.validate_workspace_name(good) == good


def test_workspace_dir_rejects_traversal_name(wf_home):
    with pytest.raises(WorkspaceError):
        ws.workspace_dir("../../etc")


# ---- create / path resolution ---------------------------------------------


def test_ensure_workspace_creates_top_level_and_run_dir(wf_home):
    ws.ensure_workspace("desk-autonomy", "wf_run1")
    top = wf_home / "workflows" / "workspaces" / "desk-autonomy"
    assert top.is_dir()
    assert (top / "runs" / "wf_run1").is_dir()


def test_ensure_workspace_is_idempotent_and_preserves_content(wf_home):
    ws.ensure_workspace("desk-autonomy", "wf_run1")
    seed = ws.workspace_dir("desk-autonomy") / "seed.md"
    seed.write_text("carry me across runs\n", encoding="utf-8")

    # a second run re-enters the same workspace
    ws.ensure_workspace("desk-autonomy", "wf_run2")

    assert seed.read_text(encoding="utf-8") == "carry me across runs\n"
    assert (ws.workspace_runs_dir("desk-autonomy") / "wf_run2").is_dir()


def test_resolve_in_workspace_contains_paths(wf_home):
    ws.ensure_workspace("desk-autonomy", "wf_run1")
    p = ws.resolve_in_workspace("desk-autonomy", "protocol_state.yaml")
    assert p.parent == ws.workspace_dir("desk-autonomy")

    run_p = ws.resolve_in_workspace("desk-autonomy", "notes.md", run_id="wf_run1")
    assert run_p.parent == ws.workspace_run_dir("desk-autonomy", "wf_run1")


def test_resolve_in_workspace_rejects_escape(wf_home):
    ws.ensure_workspace("desk-autonomy", "wf_run1")
    with pytest.raises(WorkspaceError):
        ws.resolve_in_workspace("desk-autonomy", "../../../etc/passwd")


def test_workspace_run_dir_rejects_bad_run_id(wf_home):
    with pytest.raises(WorkspaceError):
        ws.workspace_run_dir("desk-autonomy", "../elsewhere")


# ---- isolation + cross-run continuity -------------------------------------


def test_two_workspaces_are_isolated(wf_home):
    ws.ensure_workspace("desk-autonomy", "wf_a")
    ws.ensure_workspace("other-desk", "wf_b")
    (ws.workspace_dir("desk-autonomy") / "seed.md").write_text("mine", encoding="utf-8")

    assert ws.top_level_entries("desk-autonomy") == ["seed.md"]
    assert ws.top_level_entries("other-desk") == []
    assert ws.previous_run_ids("other-desk") == ["wf_b"]


def test_previous_run_ids_excludes_current_run(wf_home):
    ws.ensure_workspace("desk-autonomy", "wf_first")
    ws.ensure_workspace("desk-autonomy", "wf_second")

    assert ws.previous_run_ids("desk-autonomy", exclude="wf_second") == ["wf_first"]
    assert set(ws.previous_run_ids("desk-autonomy")) == {"wf_first", "wf_second"}


def test_workspace_context_shape(wf_home):
    ws.ensure_workspace("desk-autonomy", "wf_first")
    (ws.workspace_dir("desk-autonomy") / "seed.md").write_text("seed", encoding="utf-8")
    ctx = ws.workspace_context("desk-autonomy", "wf_second")

    assert ctx["name"] == "desk-autonomy"
    assert ctx["dir"] == str(ws.workspace_dir("desk-autonomy"))
    assert ctx["run_dir"] == str(ws.workspace_run_dir("desk-autonomy", "wf_second"))
    assert ctx["files"] == ["seed.md"]  # `runs/` excluded, contents never included
    assert ctx["previous_runs"] == ["wf_first"]


# ---- verifier + driver integration ----------------------------------------


WORKSPACE_YAML = """
id: ws_demo
workspace: desk-autonomy
nodes:
  - id: seed
    kind: agent
    prompt: "review {{ workspace.dir }} (previous runs: {{ workspace.previous_runs }})"
edges: []
"""


def test_verify_accepts_workspace_reference_when_declared(wf_home):
    from workflow import compile_text

    vir = compile_text(WORKSPACE_YAML)
    assert vir.ir.workspace == "desk-autonomy"


def test_verify_rejects_workspace_reference_without_declaration(wf_home):
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    yaml_text = WORKSPACE_YAML.replace("workspace: desk-autonomy\n", "")
    with pytest.raises(WorkflowRejected) as exc:
        compile_text(yaml_text)
    codes = {i.code for i in exc.value.issues if i.severity == "error"}
    assert "TEMPLATE" in codes


def test_verify_rejects_illegal_workspace_name(wf_home):
    from workflow import compile_text
    from workflow.ir import WorkflowRejected

    yaml_text = WORKSPACE_YAML.replace("desk-autonomy", "../etc")
    with pytest.raises(WorkflowRejected) as exc:
        compile_text(yaml_text)
    codes = {i.code for i in exc.value.issues if i.severity == "error"}
    assert "WORKSPACE" in codes


def test_run_materializes_workspace_and_renders_paths(wf_home):
    import workflow
    from workflow.runtime.worker import FakeWorker

    seen = {}

    def capture(ctx):
        seen["prompt_ctx"] = ctx.get("workspace")
        return {"ok": True}

    vir = workflow.compile_text(WORKSPACE_YAML)
    env = workflow.run(vir, worker=FakeWorker(outputs={"seed": capture}))

    assert env["status"] == "succeeded"
    assert ws.workspace_dir("desk-autonomy").is_dir()
    assert ws.workspace_run_dir("desk-autonomy", env["run_id"]).is_dir()
    assert seen["prompt_ctx"]["name"] == "desk-autonomy"
    assert seen["prompt_ctx"]["run_id"] == env["run_id"]


def test_second_run_sees_first_run_in_workspace_history(wf_home):
    import workflow
    from workflow.runtime.worker import FakeWorker

    vir = workflow.compile_text(WORKSPACE_YAML)
    first = workflow.run(vir, worker=FakeWorker())

    # a run leaves something behind at the workspace top level -> the next run
    # picks it up without any CLI plumbing (cross-run seeding)
    (ws.workspace_dir("desk-autonomy") / "protocol_state.yaml").write_text(
        "round: 1\n", encoding="utf-8"
    )

    seen = {}

    def capture(ctx):
        seen["ws"] = ctx.get("workspace")
        return {"ok": True}

    second = workflow.run(vir, worker=FakeWorker(outputs={"seed": capture}))

    assert second["run_id"] != first["run_id"]
    assert first["run_id"] in seen["ws"]["previous_runs"]
    assert second["run_id"] not in seen["ws"]["previous_runs"]
    assert "protocol_state.yaml" in seen["ws"]["files"]


def test_no_workspace_declared_means_no_ctx_root(wf_home):
    import workflow
    from workflow.runtime.worker import FakeWorker

    seen = {}

    def capture(ctx):
        seen["ws"] = ctx.get("workspace", "<absent>")
        return {"ok": True}

    yaml_text = """
id: no_ws
nodes:
  - id: seed
    kind: agent
    prompt: "plain"
edges: []
"""
    vir = workflow.compile_text(yaml_text)
    workflow.run(vir, worker=FakeWorker(outputs={"seed": capture}))

    assert seen["ws"] == "<absent>"
    assert not (wf_home / "workflows" / "workspaces").exists()


def test_live_worker_context_carries_workspace_paths_not_bodies(wf_home):
    """Prompt-caching safety: the child gets workspace PATHS in its context
    payload, never file bodies -- so a workspace file changing between runs
    cannot perturb anything that gets cached."""
    import json

    from workflow.ir import Node, NodeSpec
    from workflow.runtime.live import LiveWorker

    ws.ensure_workspace("desk-autonomy", "wf_run1")
    (ws.workspace_dir("desk-autonomy") / "seed.md").write_text("SECRET BODY", encoding="utf-8")

    captured = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return object()

    def fake_runner(idx, goal, child=None, parent_agent=None):
        return {"summary": "ok"}

    worker = LiveWorker(
        parent_agent=object(), child_builder=fake_builder, child_runner=fake_runner
    )
    node = Node(id="a", kind="agent", spec=NodeSpec(prompt="go"))
    worker.run_node(node, {"input": {"x": 1}, "workspace": ws.workspace_context("desk-autonomy", "wf_run1")})

    payload = json.loads(captured["context"])
    assert payload["workspace"]["dir"] == str(ws.workspace_dir("desk-autonomy"))
    assert "SECRET BODY" not in captured["context"]
