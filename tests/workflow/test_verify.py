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
    """F2 (reject path): an agent setting profile/workspace is rejected by
    default (no execution path enforces them yet -- ir.OVERRIDE_ONLY_FIELDS).

    Phase 3 note: this used to also cover tools/max_turns. Both are now
    genuinely honored (LiveWorker resolves spec.tools to toolsets and passes
    spec.max_turns as max_iterations -- ir.PHASE3_OVERRIDE_FIELDS), so they
    were removed from OVERRIDE_ONLY_FIELDS and no longer belong in this list;
    see test_phase3_tools_valid_names_compile_clean /
    test_phase3_max_turns_compiles_clean below for their positive coverage,
    and model/provider were already carved out in Phase 2 (see
    test_phase2_model_provider_not_rejected).
    """
    for field_yaml in [
        "profile: trader",
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
    """F2 (warn path): with phase1_warn_overrides=true the same still-override-
    only fields (profile/workspace) downgrade to a warning — the boundary is
    loud, not silently dropped, and the workflow is accepted.

    Phase 3 note: tools/max_turns were dropped from this list -- see
    test_f2_agent_override_fields_rejected_strict's docstring."""
    for field_yaml in [
        "profile: trader",
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


# ---------------------------------------------------------------------------
# Phase 3 — spec.tools / spec.max_turns are genuinely honored
# ---------------------------------------------------------------------------


def test_phase3_tools_valid_names_compile_clean(wf_home):
    """Phase 3: spec.tools is resolved by the LiveWorker to a minimal covering
    toolset and passed to the child agent builder (ir.PHASE3_OVERRIDE_FIELDS),
    so it is no longer in OVERRIDE_ONLY_FIELDS. A list of recognized tool /
    toolset names must compile clean in strict mode: no PHASE1_OVERRIDE (it's
    honored now) and no TOOLS_UNKNOWN (the names are real)."""
    yaml = """
workflow: toolsvalid
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      tools: [web_search, read_file]
edges: []
triggers:
  - { kind: manual }
"""
    vir = _verify(yaml, warn=False)
    assert vir.issues == [], vir.issues


def test_phase3_max_turns_compiles_clean(wf_home):
    """Phase 3: spec.max_turns is threaded through to the child's
    max_iterations (ir.PHASE3_OVERRIDE_FIELDS), so it is no longer in
    OVERRIDE_ONLY_FIELDS -- strict mode accepts it with zero issues."""
    yaml = """
workflow: maxturnsvalid
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      max_turns: 3
edges: []
triggers:
  - { kind: manual }
"""
    vir = _verify(yaml, warn=False)
    assert vir.issues == [], vir.issues


def test_phase3_tools_and_max_turns_together_compile_clean(wf_home):
    """Both new Phase 3 honored fields together, alongside the Phase 2 honored
    model/provider pair, produce zero issues in strict mode -- the carve-outs
    stack rather than being mutually exclusive."""
    yaml = """
workflow: toolsandmaxturns
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      model: fake/model-1
      provider: fake-provider
      tools: [web_search]
      max_turns: 5
edges: []
triggers:
  - { kind: manual }
"""
    vir = _verify(yaml, warn=False)
    assert vir.issues == [], vir.issues


def test_phase3_unknown_tool_name_rejected(wf_home):
    """Phase 3: an unrecognized tool name in spec.tools is rejected at compile
    time with TOOLS_UNKNOWN (design: "the verifier validates the names and
    rejects unknown ones").

    The name-validity check is implemented to skip entirely when the
    toolsets registry cannot be imported (so a missing/broken registry never
    turns into a spurious hard-reject of otherwise-valid workflows). Skip
    gracefully here rather than assuming the registry is loadable in every
    environment this suite runs in.
    """
    try:
        import toolsets  # noqa: F401  -- the tool/toolset name registry
    except ImportError:
        pytest.skip("toolsets registry not importable in this environment")

    yaml = """
workflow: badtool
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      tools: [this_tool_definitely_does_not_exist_xyz]
edges: []
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml, warn=False)
    assert "TOOLS_UNKNOWN" in codes, codes


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
    check. A totally unknown model id is NOT rejected for being unknown, nor
    does it produce any F2 override issue any more — Phase 2 honors
    spec.model via the LiveWorker override path (ir.PHASE2_OVERRIDE_FIELDS),
    so it is no longer an OVERRIDE_ONLY_FIELDS member.

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
    vir = _verify(yaml, warn=True)
    codes = [i.code for i in vir.issues]
    assert codes == [], codes  # no MODEL / unknown-model error, no PHASE1_OVERRIDE either


def test_phase2_model_provider_not_rejected(wf_home):
    """Phase 2: spec.model AND spec.provider together are accepted with zero
    issues (strict mode, no phase1_warn_overrides) — the LiveWorker override
    path (ir.PHASE2_OVERRIDE_FIELDS) genuinely honors both fields now, so
    they no longer belong in OVERRIDE_ONLY_FIELDS."""
    yaml = """
workflow: phase2override
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      model: fake/model-1
      provider: fake-provider
edges: []
triggers:
  - { kind: manual }
"""
    vir = _verify(yaml, warn=False)
    assert vir.issues == [], vir.issues
