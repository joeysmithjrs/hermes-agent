"""The shipped example workflows must actually compile and run.

Docs rot silently; an example that no longer verifies is worse than no example,
because it is the first thing an author copies. These run the same two commands
each example header advertises -- `validate` (compile + verify) and a
FakeWorker run -- over every `examples-*.yaml` in the proposal directory, so a
rule added to the verifier that invalidates a documented shape fails here rather
than in someone's terminal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "docs" / "proposals" / "workflow-dispatch"
EXAMPLES = sorted(EXAMPLES_DIR.glob("examples-*.yaml"))


def _ids(paths):
    return [p.name for p in paths]


@pytest.mark.parametrize("path", EXAMPLES, ids=_ids(EXAMPLES))
def test_example_compiles(wf_home, path):
    from workflow import compile_text

    vir = compile_text(path.read_text(encoding="utf-8"))
    assert vir.ir.nodes


POST_PHASE3_EXAMPLES = sorted(EXAMPLES_DIR.glob("examples-post-phase3-*.yaml"))


@pytest.mark.parametrize("path", POST_PHASE3_EXAMPLES, ids=_ids(POST_PHASE3_EXAMPLES))
def test_post_phase3_example_runs_under_the_fake_worker(wf_home, path):
    """The `HERMES_WORKFLOW_FAKE=1 hermes workflow run ...` line in each header
    has to be true: every node reaches a terminal state and none of the
    documented `{{ }}` references blow up mid-run."""
    import workflow
    from workflow.runtime.worker import FakeWorker

    vir = workflow.compile_text(path.read_text(encoding="utf-8"))
    env = workflow.run(vir, worker=FakeWorker())

    assert env["status"] == "succeeded", (path.name, env)
    assert env["failed"] == []


def test_every_shipped_prompt_library_loads_and_renders(wf_home):
    from workflow.prompts.library import builtin_library_dir, load_library

    names = sorted(p.stem for p in builtin_library_dir().glob("*.yaml"))
    assert names, "no builtin prompt libraries found"
    for name in names:
        entry = load_library(name)
        assert entry.prompt.strip()
        assert entry.name == name
