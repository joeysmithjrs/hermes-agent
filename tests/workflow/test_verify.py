"""Verifier tests — design §5 / test-plan §2 (verifier cases).

Covers the AUDIT F-list verifier invariants: F2 (Phase 1 override-only fields
reject / warn), F4 (run: allowlist, os.system rejected), F5 (fanout
max_branches required), F8 (approve_auto + dual_control rejected). Plus the
structural verifier cases (cycle, unreachable, missing prompt, gate without
channel/approver, unknown template var) and the residual model-id check.

Tests build a WorkflowIR from YAML text and run `verify_ir` directly (no FS,
no run) so they are pure unit tests. They still run under a hermetic
HERMES_HOME for isolation.
"""

from __future__ import annotations

import pytest


def _ir(yaml_text: str):
    """Load YAML into an unverified WorkflowIR (no persistence)."""
    from workflow.yaml_load import load_yaml

    return load_yaml(yaml_text)


def _verify(yaml_text: str, *, warn: bool = False):
    """Verify and return VerifiedIR, or raise WorkflowRejected."""
    from workflow.verify import verify_ir

    return verify_ir(_ir(yaml_text), phase1_warn_overrides=warn)


def _rejected_codes(yaml_text: str, *, warn: bool = False):
    """Return the list of Issue codes raised for a rejected workflow."""
    from workflow.ir import WorkflowRejected

    with pytest.raises(WorkflowRejected) as exc:
        _verify(yaml_text, warn=warn)
    return [i.code for i in exc.value.issues if i.severity == "error"]


# ---------------------------------------------------------------------------
# Structural cases
# ---------------------------------------------------------------------------


def test_rejects_cycle(wf_home):
    """A→B→A cycle is rejected (CYCLE)."""
    yaml = """
workflow: cyc
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do a}
  - id: b
    kind: agent
    spec: {prompt: do b}
edges:
  - { from: a, to: b }
  - { from: b, to: a }
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml)
    assert "CYCLE" in codes, codes


def test_rejects_unreachable_node(wf_home):
    """A node not reachable from any entry is rejected (unreachable).

    Unreachable requires a subgraph with no entry: a self-contained 2-cycle
    (b<->c) disconnected from the real entry `a` is unreachable from `a`, so
    both `b` and `c` are flagged. (In a finite DAG every node is reachable from
    an entry, so unreachable necessarily co-occurs with a cycle here.)
    """
    yaml = """
workflow: unreach
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: entry}
  - id: b
    kind: agent
    spec: {prompt: b}
  - id: c
    kind: agent
    spec: {prompt: c}
edges:
  - { from: b, to: c }
  - { from: c, to: b }
triggers:
  - { kind: manual }
"""
    from workflow.ir import WorkflowRejected

    with pytest.raises(WorkflowRejected) as exc:
        _verify(yaml)
    msgs = [i.message for i in exc.value.issues]
    assert any("unreachable" in m for m in msgs), msgs


def test_fanout_without_downstream_join_is_accepted(wf_home):
    """Phase 1 accepts a terminal fanout by the §5 implementation note.

    The design explicitly defers the stricter fanout-requires-downstream-join
    rule, so this behavior is a documented contract rather than a silent gap.
    """
    yaml = """
workflow: nojoin
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [alpha, beta]}
  - id: br
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec: {prompt: "p {{ branch }}"}
edges:
  - { from: seed, to: br }
triggers:
  - { kind: manual }
"""
    vir = _verify(yaml)
    # accepted; no error/warning about a missing downstream join
    codes = [i.code for i in vir.issues]
    assert "STRUCTURE" not in codes, codes


def test_rejects_agent_missing_prompt(wf_home):
    yaml = """
workflow: noprompt
version: 1
nodes:
  - id: a
    kind: agent
edges: []
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml)
    assert "PROMPT" in codes, codes


def test_rejects_gate_without_channel(wf_home):
    yaml = """
workflow: nochannel
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do}
  - id: g1
    kind: gate
gates:
  g1:
    approvers: [joe]
edges:
  - { from: a, to: g1 }
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml)
    assert "GATE" in codes, codes


def test_rejects_gate_without_approvers(wf_home):
    yaml = """
workflow: noapprover
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
edges:
  - { from: a, to: g1 }
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml)
    assert "GATE" in codes, codes


def test_rejects_unknown_template_var(wf_home):
    yaml = """
workflow: badtpl
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: "Do {{ ghost.output }}"}
edges: []
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml)
    assert "TEMPLATE" in codes, codes


# ---------------------------------------------------------------------------
# AUDIT F-list verifier invariants
# ---------------------------------------------------------------------------


def test_f5_fanout_missing_max_branches_rejected(wf_home):
    """F5: fanout without max_branches is rejected."""
    yaml = """
workflow: nomax
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a]}
  - id: br
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    branch:
      kind: agent
      spec: {prompt: "p {{ branch }}"}
  - id: jn
    kind: join
    from: [br]
    reduce: { type: concat }
edges:
  - { from: seed, to: br }
  - { from: br, to: jn }
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml)
    assert "CARDINALITY" in codes, codes


def test_f4_run_not_in_allowlist_rejected(wf_home):
    """F4: a `run:` callable not in the registry is rejected; os.system must be rejected."""
    yaml = """
workflow: badrun
version: 1
nodes:
  - id: s
    kind: script
    run: os.system
edges: []
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml)
    assert "SCRIPT" in codes, codes


def test_f4_arbitrary_dotted_name_rejected(wf_home):
    """F4: an arbitrary dotted import path (not registered) is rejected."""
    yaml = """
workflow: badrun2
version: 1
nodes:
  - id: s
    kind: script
    run: subprocess.call
edges: []
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml)
    assert "SCRIPT" in codes, codes


def test_f8_approve_auto_with_dual_control_rejected(wf_home):
    """F8: on_timeout: approve_auto + dual_control: true is hard-rejected."""
    yaml = """
workflow: autodual
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
    on_timeout: approve_auto
    dual_control: true
edges:
  - { from: a, to: g1 }
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml)
    assert "GATE_TIMEOUT" in codes, codes


def test_f8_approve_auto_without_dual_control_warns(wf_home):
    """F8: approve_auto with dual_control:false is accepted but warned (insecure)."""
    yaml = """
workflow: autonodual
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
    on_timeout: approve_auto
    dual_control: false
edges:
  - { from: a, to: g1 }
triggers:
  - { kind: manual }
"""
    vir = _verify(yaml)
    codes = [i.code for i in vir.issues]
    assert "GATE_SECURITY" in codes, codes


def test_f2_agent_override_fields_rejected_strict(wf_home):
    """F2 (reject path): an agent setting tools/model/profile/max_turns/workspace
    is rejected by default (Phase 1 inherit path cannot honor them).

    Uses a non-live tool (web_search) so only PHASE1_OVERRIDE fires, not the
    live-tool SIDE_EFFECTS rule.
    """
    for field_yaml in [
        "tools: [web_search]",
        "model: fake/model-1",
        "profile: trader",
        "max_turns: 3",
        "workspace: {root: /tmp}",
    ]:
        yaml = f"""
workflow: override
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      {field_yaml}
edges: []
triggers:
  - {{ kind: manual }}
"""
        codes = _rejected_codes(yaml, warn=False)
        assert "PHASE1_OVERRIDE" in codes, (field_yaml, codes)


def test_f2_agent_override_fields_warn_behind_flag(wf_home):
    """F2 (warn path): with phase1_warn_overrides=true the same fields downgrade
    to a warning — the boundary is loud, not silently dropped, and the workflow
    is accepted."""
    for field_yaml in [
        "tools: [web_search]",
        "model: fake/model-1",
        "profile: trader",
        "max_turns: 3",
        "workspace: {root: /tmp}",
    ]:
        yaml = f"""
workflow: override
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      {field_yaml}
edges: []
triggers:
  - {{ kind: manual }}
"""
        vir = _verify(yaml, warn=True)
        codes = [i.code for i in vir.issues]
        assert "PHASE1_OVERRIDE" in codes, (field_yaml, codes)
        # all issues are warnings (no errors raised -> VerifiedIR returned)
        assert all(i.severity == "warning" for i in vir.issues), (field_yaml, vir.issues)


def test_side_effecting_agent_missing_side_effects_rejected(wf_home):
    """F6 verifier side: an agent using a live/side-effecting tool must declare
    `side_effects: external`. (Runs under warn mode so the tools override is a
    warning, surfacing the distinct SIDE_EFFECTS error.)"""
    yaml = """
workflow: liveside
version: 1
nodes:
  - id: trader
    kind: agent
    spec:
      prompt: place trade
      tools: [exec]
edges: []
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml, warn=True)
    assert "SIDE_EFFECTS" in codes, codes


def test_model_id_not_checked_residual_debt(wf_home):
    """Residual debt (AUDIT Q7 / SUMMARY): the verifier has NO model-id registry
    check. A totally unknown model id is NOT rejected for being unknown — the
    only feedback is the F2 override warning (model is an override-only field).

    This test pins the current lenient behaviour. If a model registry is added
    later, flip this to assert the unknown-model rejection.
    """
    yaml = """
workflow: unknownmodel
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      model: totally/fake/model-999-not-real
edges: []
triggers:
  - { kind: manual }
"""
    # warn mode -> model is a PHASE1_OVERRIDE warning, accepted
    vir = _verify(yaml, warn=True)
    codes = [i.code for i in vir.issues]
    assert codes == ["PHASE1_OVERRIDE"], codes  # no MODEL / unknown-model error
