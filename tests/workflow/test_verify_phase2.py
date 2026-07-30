"""Phase 2 verifier regression coverage — acceptance checklist §"verify".

`tests/workflow/test_verify.py` already covers the headline verifier cases
(model+provider together accepted since Phase 2; tools/max_turns accepted
since Phase 3; profile/workspace still rejected/warn-only; F8/F4 security
invariants). This file adds the angles that file does NOT cover, so it
complements rather than duplicates it:
  1. `spec.model` alone and `spec.provider` alone (not just both together)
     are each independently accepted with zero issues.
  2. The updated PHASE1_OVERRIDE rejection message text itself (not just the
     issue code) -- it must not claim an isolation boundary Phase 2 doesn't
     enforce, and must name spec.model/spec.provider as the honored fields.
  3. A fanout branch that mixes an ALLOWED field (model) with a still-
     REJECTED field (profile) in the same spec: exactly one PHASE1_OVERRIDE
     issue (for profile), none for model -- proves the branch-template
     re-check applies the *same* per-field carve-out as a top-level node, not
     a coarser all-or-nothing rule. (Phase 3 note: this used to use `tools:`
     as the still-rejected field; tools is now honored -- see
     ir.PHASE3_OVERRIDE_FIELDS -- so `profile` demonstrates it instead. A
     companion case shows model+tools+max_turns all coexisting cleanly.)
  4. The Phase 2 example fixture (`examples-phase2-live.yaml`) compiles /
     validates clean.
  5. A combined regression guard: a node that sets spec.model/spec.provider
     (now allowed) AND violates a security invariant (approve_auto +
     dual_control, or an unregistered `run:`) in the SAME workflow still
     gets rejected for the security reason -- the model/provider carve-out
     must not have loosened anything else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workflow.ir import WorkflowRejected
from workflow.verify import verify_ir
from workflow.yaml_load import load_yaml


def _verify(yaml_text: str, *, warn: bool = False):
    return verify_ir(load_yaml(yaml_text), phase1_warn_overrides=warn)


def _rejected_codes(yaml_text: str, *, warn: bool = False):
    with pytest.raises(WorkflowRejected) as exc:
        _verify(yaml_text, warn=warn)
    return [i.code for i in exc.value.issues if i.severity == "error"]


# ---------------------------------------------------------------------------
# 1. model-only / provider-only independently accepted
# ---------------------------------------------------------------------------


def test_spec_model_alone_accepted(wf_home):
    yaml_text = """
workflow: model_only
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      model: fake/model-1
edges: []
triggers:
  - { kind: manual }
"""
    vir = _verify(yaml_text, warn=False)
    assert vir.issues == [], vir.issues


def test_spec_provider_alone_accepted(wf_home):
    yaml_text = """
workflow: provider_only
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      provider: fake-provider
edges: []
triggers:
  - { kind: manual }
"""
    vir = _verify(yaml_text, warn=False)
    assert vir.issues == [], vir.issues


# ---------------------------------------------------------------------------
# 2. rejection message text (not just the code)
# ---------------------------------------------------------------------------


def test_override_only_field_message_names_model_and_provider_as_honored(wf_home):
    yaml_text = """
workflow: msgcheck
version: 1
nodes:
  - id: a
    kind: agent
    spec:
      prompt: do
      profile: trader
edges: []
triggers:
  - { kind: manual }
"""
    with pytest.raises(WorkflowRejected) as exc:
        _verify(yaml_text, warn=False)
    errs = [i for i in exc.value.issues if i.severity == "error" and i.code == "PHASE1_OVERRIDE"]
    assert len(errs) == 1, exc.value.issues
    msg = errs[0].message
    # must not claim an unenforced isolation boundary, and must name the
    # fields Phase 2 actually honors so an operator knows the difference.
    assert "spec.model" in msg and "spec.provider" in msg, msg
    assert "profile" in msg, msg


# ---------------------------------------------------------------------------
# 3. branch template: per-field carve-out, not all-or-nothing
# ---------------------------------------------------------------------------


def test_branch_model_allowed_profile_rejected_same_spec(wf_home):
    """Phase 3 note: this used to use `tools:` as the still-rejected field
    alongside the allowed `model:`. tools is now genuinely honored by the
    LiveWorker (ir.PHASE3_OVERRIDE_FIELDS) and was removed from
    OVERRIDE_ONLY_FIELDS, so it can no longer demonstrate a per-field reject.
    `profile` still can -- see test_branch_model_tools_max_turns_all_honored_
    same_spec below for the positive-only counterpart."""
    yaml_text = """
workflow: branch_mixed
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b]}
  - id: fan
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec:
        prompt: "do {{ branch }}"
        model: fake/model-1
        profile: trader
  - id: jn
    kind: join
    from: [fan]
    reduce: { type: concat }
edges:
  - { from: seed, to: fan }
  - { from: fan, to: jn }
triggers:
  - { kind: manual }
"""
    with pytest.raises(WorkflowRejected) as exc:
        _verify(yaml_text, warn=False)
    override_issues = [i for i in exc.value.issues if i.code == "PHASE1_OVERRIDE"]
    assert len(override_issues) == 1, exc.value.issues
    assert "profile" in override_issues[0].message, override_issues[0].message
    assert "model" not in override_issues[0].message.split("'")[1], override_issues[0].message


def test_branch_model_tools_max_turns_all_honored_same_spec(wf_home):
    """Positive counterpart to test_branch_model_allowed_profile_rejected_
    same_spec: within one fanout.branch.spec, model (Phase 2) plus tools and
    max_turns (Phase 3) all coexist with zero issues in strict mode -- none
    of the honored-field carve-outs are mutually exclusive."""
    yaml_text = """
workflow: branch_all_honored
version: 1
nodes:
  - id: seed
    kind: script
    run: workflow.examples.echo
    input: {items: [a, b]}
  - id: fan
    kind: fanout
    over: "{{ seed.output.echo.items }}"
    max_branches: 5
    branch:
      kind: agent
      spec:
        prompt: "do {{ branch }}"
        model: fake/model-1
        provider: fake-provider
        tools: [web_search]
        max_turns: 4
  - id: jn
    kind: join
    from: [fan]
    reduce: { type: concat }
edges:
  - { from: seed, to: fan }
  - { from: fan, to: jn }
triggers:
  - { kind: manual }
"""
    vir = _verify(yaml_text, warn=False)
    assert vir.issues == [], vir.issues


# ---------------------------------------------------------------------------
# 4. the Phase 2 example fixture compiles clean
# ---------------------------------------------------------------------------


def test_examples_phase2_live_yaml_compiles_clean(wf_home):
    from workflow import compile_file

    path = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "proposals"
        / "workflow-dispatch"
        / "examples-phase2-live.yaml"
    )
    assert path.exists(), f"fixture missing: {path}"
    vir = compile_file(str(path), phase1_warn_overrides=False)
    assert vir.issues == [], vir.issues


# ---------------------------------------------------------------------------
# 5. security invariants unaffected by the model/provider carve-out
# ---------------------------------------------------------------------------


def test_approve_auto_dual_control_still_rejected_alongside_model_override(wf_home):
    """A workflow that ALSO uses the now-allowed spec.model must still be
    rejected for the unrelated GATE_TIMEOUT security violation."""
    yaml_text = """
workflow: security_regression_gate
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do, model: fake/model-1}
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
    codes = _rejected_codes(yaml_text, warn=True)  # warn mode: model carve-out active either way
    assert "GATE_TIMEOUT" in codes, codes


def test_unregistered_run_still_rejected_alongside_provider_override(wf_home):
    """A workflow that ALSO uses the now-allowed spec.provider on an agent
    node must still reject an unregistered `run:` on a script node (F4)."""
    yaml_text = """
workflow: security_regression_run
version: 1
nodes:
  - id: a
    kind: agent
    spec: {prompt: do, provider: fake-provider}
  - id: s
    kind: script
    run: os.system
edges:
  - { from: a, to: s }
triggers:
  - { kind: manual }
"""
    codes = _rejected_codes(yaml_text, warn=True)
    assert "SCRIPT" in codes, codes
