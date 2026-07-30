"""Real-process durability regression coverage for pre-dispatch checkpoints."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from workflow.runtime.driver import resume
from workflow.runtime.worker import FakeWorker
from workflow.store import checkpoint, fs


class _SidecarRecordingWorker(FakeWorker):
    """Fake worker that durably records every external-effect call at entry."""

    def __init__(self, sidecar_path: Path, *, sleep_s: float = 0.0) -> None:
        super().__init__(sleep_s=sleep_s, side_effect_nodes={"effect"})
        self.sidecar_path = sidecar_path

    def run_node(self, node, ctx):
        if node.id in self.side_effect_nodes:
            calls = fs.read_json(self.sidecar_path, default={}) or {}
            calls[node.id] = calls.get(node.id, 0) + 1
            fs.atomic_write_json(self.sidecar_path, calls)
        return super().run_node(node, ctx)


def _wait_for_sidecar_count(sidecar_path: Path, node_id: str, expected: int, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        calls = fs.read_json(sidecar_path, default={}) or {}
        if calls.get(node_id) == expected:
            return
        time.sleep(0.02)
    raise AssertionError(f"{node_id} did not reach sidecar call count {expected}")


def test_external_running_transition_is_durable_across_sigkill(wf_home, tmp_path):
    """SIGKILL during a real blocking external call preserves F6's no-replay rule."""
    sidecar_path = tmp_path / "side_effect_calls.json"
    harness_path = tmp_path / "pass2_harness.py"
    harness_path.write_text(
        """
import os
from pathlib import Path

from workflow import compile_text
from workflow.runtime.driver import Driver
from workflow.runtime.worker import FakeWorker
from workflow.store import fs

YAML = '''
workflow: pass2_external_effect
version: 1
nodes:
  - id: effect
    kind: agent
    side_effects: external
    spec: {prompt: "perform external effect"}
triggers:
  - {kind: manual}
'''


class RecordingWorker(FakeWorker):
    def __init__(self, sidecar_path):
        super().__init__(sleep_s=4.0, side_effect_nodes={"effect"})
        self.sidecar_path = Path(sidecar_path)

    def run_node(self, node, ctx):
        if node.id in self.side_effect_nodes:
            calls = fs.read_json(self.sidecar_path, default={}) or {}
            calls[node.id] = calls.get(node.id, 0) + 1
            fs.atomic_write_json(self.sidecar_path, calls)
        return super().run_node(node, ctx)


vir = compile_text(YAML)
fs.atomic_write_json(fs.definition_path(vir.ir.id), vir.to_dict())
driver = Driver(vir, worker=RecordingWorker(os.environ["PASS2_SIDECAR_PATH"]))
print(driver.run_id, flush=True)
driver.execute()
""".strip(),
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(wf_home)
    env["PASS2_SIDECAR_PATH"] = str(sidecar_path)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(repo_root), env.get("PYTHONPATH"))))
    process = subprocess.Popen(
        [sys.executable, str(harness_path)],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        run_id = process.stdout.readline().strip()
        assert run_id, process.stderr.read() if process.stderr is not None else "harness emitted no run id"
        _wait_for_sidecar_count(sidecar_path, "effect", 1)
        os.kill(process.pid, signal.SIGKILL)
        assert process.wait(timeout=5) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    before_resume = fs.read_json(sidecar_path)
    assert before_resume == {"effect": 1}
    interrupted = checkpoint.load_run_record(run_id)
    effect_before_resume = next(nr for nr in interrupted["node_runs"].values() if nr["node_id"] == "effect")
    assert effect_before_resume["status"] == "running"

    default_worker = _SidecarRecordingWorker(sidecar_path)
    default_resume = resume(run_id, worker=default_worker)
    after_default = checkpoint.load_run_record(run_id)
    effect_after_default = next(nr for nr in after_default["node_runs"].values() if nr["node_id"] == "effect")
    assert effect_after_default["status"] == "failed"
    assert effect_after_default["error"]["code"] == "INTERRUPTED"
    assert default_resume["status"] == "failed"
    assert fs.read_json(sidecar_path) == before_resume, "default resume must not replay the external effect"

    retry_resume = resume(run_id, worker=_SidecarRecordingWorker(sidecar_path), retry_failed=True)
    after_retry = checkpoint.load_run_record(run_id)
    effect_after_retry = next(nr for nr in after_retry["node_runs"].values() if nr["node_id"] == "effect")
    assert effect_after_retry["status"] == "succeeded"
    assert retry_resume["status"] == "succeeded"
    assert fs.read_json(sidecar_path) == {"effect": 2}
