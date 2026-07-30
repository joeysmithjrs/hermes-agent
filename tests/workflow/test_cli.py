"""CLI tests — test-plan §4 + acceptance (#6 soft-import, disabled-run refuses).

Invokes the argparse-based `workflow.cli` subcommand functions directly with
hand-built `argparse.Namespace` objects (no subprocess), so the tests stay
hermetic and fast. Soft-import is proven two ways:
  (a) `register_subparser` exists, is callable, and registers `workflow` on a
      throwaway subparsers without raising;
  (b) the soft-import hunk in `hermes_cli/main.py` is wrapped in try/except so a
      registration/import failure never breaks `hermes --help`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

GOOD_YAML = """
workflow: cli_good
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
  - id: b
    kind: script
    run: workflow.examples.echo
    input: {text: "hi"}
edges:
  - { from: a, to: b }
triggers:
  - { kind: manual }
"""

BAD_YAML = """
workflow: cli_bad
version: 1
nodes:
  - id: a
    kind: agent
edges: []
triggers:
  - { kind: manual }
"""


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_validate_good_yaml_exit_0(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_validate

    path = _write(tmp_path, "good.yaml", GOOD_YAML)
    args = argparse.Namespace(path=path, warn_overrides=True)
    rc = _cmd_validate(args)
    assert rc == 0, rc
    out = capsys.readouterr().out
    assert "OK" in out


def test_validate_bad_yaml_exit_2(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_validate

    path = _write(tmp_path, "bad.yaml", BAD_YAML)
    args = argparse.Namespace(path=path, warn_overrides=True)
    rc = _cmd_validate(args)
    assert rc == 2, rc
    err = capsys.readouterr().err
    assert "REJECTED" in err or "PROMPT" in err


def test_run_writes_run_dir(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_run
    from workflow.store import fs

    path = _write(tmp_path, "good.yaml", GOOD_YAML)
    args = argparse.Namespace(
        path_or_id=path,
        input=None,
        resume=None,
        from_node=None,
        retry_failed=False,
        dry_run=False,
        max_budget_usd=None,
    )
    rc = _cmd_run(args)
    assert rc == 0, rc
    out = capsys.readouterr().out
    env = json.loads(out)
    rid = env["run_id"]
    # runs/<id>/run.json exists
    assert fs.run_json_path(rid).exists(), f"missing run dir for {rid}"


def test_status_stable_keys(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_run, _cmd_status

    path = _write(tmp_path, "good.yaml", GOOD_YAML)
    run_args = argparse.Namespace(
        path_or_id=path,
        input=None,
        resume=None,
        from_node=None,
        retry_failed=False,
        dry_run=False,
        max_budget_usd=None,
    )
    _cmd_run(run_args)
    rid = json.loads(capsys.readouterr().out)["run_id"]

    status_args = argparse.Namespace(run_id=rid, watch=False)
    rc = _cmd_status(status_args)
    assert rc == 0, rc
    env = json.loads(capsys.readouterr().out)
    for key in ("run_id", "workflow_id", "status"):
        assert key in env, (key, env)
    assert env["workflow_id"] == "cli_good"


def test_list_lists_run(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_run, _cmd_list

    path = _write(tmp_path, "good.yaml", GOOD_YAML)
    _cmd_run(argparse.Namespace(
        path_or_id=path, input=None, resume=None, from_node=None,
        retry_failed=False, dry_run=False, max_budget_usd=None))
    capsys.readouterr()  # drain

    rc = _cmd_list(argparse.Namespace(status_filter=None, workflow_id=None, limit=50))
    assert rc == 0, rc
    out = capsys.readouterr().out
    assert "cli_good" in out


# ---------------------------------------------------------------------------
# Soft-import (acceptance #6: `hermes --help` works if workflow import fails)
# ---------------------------------------------------------------------------


def test_register_subparser_exists_and_registers(wf_home):
    """register_subparser exists, is callable, and registers `workflow` cleanly."""
    import workflow.cli as cli

    assert callable(cli.register_subparser)
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    cli.register_subparser(sub)
    # `workflow` subcommand is registered and parseable
    ns = parser.parse_args(["workflow", "validate", "x.yaml"])
    assert getattr(ns, "workflow_command", None) == "validate"


def test_soft_import_hunk_in_main_is_guarded():
    """Acceptance #6 (behavioural guard): the workflow registration block in
    `hermes_cli/main.py` is wrapped in try/except so a failed import/registration
    never breaks `hermes --help`.

    We assert the guard structurally: the `from workflow.cli import ...` line is
    preceded by a `try:` and followed by an `except Exception` within a few
    lines. (main.py is ~15k lines; constructing the full CLI in-process is not
    hermetic, so we verify the guard that protects `--help` instead.)
    """
    main_path = Path(__file__).resolve().parents[2] / "hermes_cli" / "main.py"
    src = main_path.read_text(encoding="utf-8")
    needle = "from workflow.cli import register_subparser"
    idx = src.index(needle)
    # look at the surrounding ~10 lines
    block = src[idx - 200 : idx + 400]
    assert "try:" in block, "workflow registration is not wrapped in try"
    assert "except Exception" in block, "workflow registration has no except Exception guard"
    # the import line itself is present
    assert needle in src


def test_disabled_run_refuses_nonzero(tmp_path, monkeypatch, capsys):
    """A `run` when workflow.enabled:false (and no FAKE env) returns a clear
    nonzero exit (EXIT_RUNTIME), NOT a crash/traceback."""
    from workflow.cli import _cmd_run
    from tests.workflow.conftest import make_home

    make_home(tmp_path, monkeypatch, enabled=False, fake=False)
    path = _write(tmp_path, "good.yaml", GOOD_YAML)
    args = argparse.Namespace(
        path_or_id=path,
        input=None,
        resume=None,
        from_node=None,
        retry_failed=False,
        dry_run=False,
        max_budget_usd=None,
    )
    rc = _cmd_run(args)
    assert rc != 0, rc  # nonzero, not a crash
    err = capsys.readouterr().err
    assert "disabled" in err.lower(), err


def test_dry_run_works_even_when_disabled(tmp_path, monkeypatch, capsys):
    """dry_run is a compile+plan step; it must work even when enabled:false
    (validate/compile/dry-run never need the live run gate)."""
    from workflow.cli import _cmd_run
    from tests.workflow.conftest import make_home

    make_home(tmp_path, monkeypatch, enabled=False, fake=False)
    path = _write(tmp_path, "good.yaml", GOOD_YAML)
    args = argparse.Namespace(
        path_or_id=path,
        input=None,
        resume=None,
        from_node=None,
        retry_failed=False,
        dry_run=True,
        max_budget_usd=None,
    )
    rc = _cmd_run(args)
    assert rc == 0, rc
    out = capsys.readouterr().out
    env = json.loads(out)
    assert env["status"] == "dry_run", env
    assert "a" in env["ready"], env


def test_gate_cli_records_decision(wf_home, tmp_path, capsys):
    """`workflow gate` (Phase 2 stub) records the decision on disk and reports
    the Phase-2 note. Proves the gate CLI surface exists."""
    from workflow import compile_text, run, decide_gate
    from workflow.cli import _cmd_gate
    from workflow.runtime.worker import FakeWorker
    from workflow.store import fs

    gate_yaml = """
workflow: gatecli
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
  - id: g1
    kind: gate
gates:
  g1:
    channel: "#ops"
    approvers: [joe]
edges:
  - { from: a, to: g1 }
triggers:
  - { kind: manual }
"""
    vir = compile_text(gate_yaml, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=FakeWorker())
    rid = env["run_id"]

    args = argparse.Namespace(run_id=rid, gate_id="g1", decide="approve", note="ok")
    rc = _cmd_gate(args)
    assert rc == 0, rc
    out = capsys.readouterr().out
    # _cmd_gate emits the JSON object followed by a trailing Phase-2 note line,
    # so parse only the leading JSON object via raw_decode.
    res, _end = json.JSONDecoder().raw_decode(out.lstrip())
    assert res["decision"] == "approve"
    # decision persisted on disk
    sig = fs.read_json(fs.gate_signal_path(rid, "g1"))
    assert sig["decision"] == "approve"
