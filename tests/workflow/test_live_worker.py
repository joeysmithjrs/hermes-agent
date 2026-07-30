"""LiveWorker model-semantics proofs — Phase 2 acceptance checklist §"model
inherit / model override kwargs".

Maps to the acceptance checklist:
  1. Inherit proof: no spec.model -> child_builder gets model=None (so
     `_build_child_agent` applies `model or parent_agent.model`) AND the
     returned envelope's effective_model equals the parent's .model.
  2. Override proof: spec.model set (no spec.provider) -> child_builder gets
     model=<override>, all override_* credential kwargs stay None (same
     provider/credentials as the parent), effective_model == override.
  3. Cross-provider override: spec.model + spec.provider set -> fresh
     credentials resolved via hermes_cli.runtime_provider.resolve_runtime_provider
     and passed through as override_* kwargs; effective_provider == override.
  4. resolve_effective_model() pure-function unit tests (no spec, model-only,
     provider-only, both).
  5. Non-agent node kind -> trivial ok envelope, child_builder never called.
  6. build_runtime_parent() raises RuntimeParentError (never returns None)
     when no model resolves anywhere.
  7. Driver-level plumbing: effective_model/effective_provider land on the
     persisted NodeRun and survive a checkpoint round-trip.

Every test uses a stub child_builder/child_runner (or a dummy parent object)
-- no network, no real AIAgent, no real LLM.
"""

from __future__ import annotations

import pytest

from workflow.ir import Node, NodeSpec
from workflow.runtime.live import (
    LiveWorker,
    RuntimeParentError,
    build_runtime_parent,
    resolve_effective_model,
)


@pytest.fixture(autouse=True)
def _clear_parent_cache():
    """build_runtime_parent caches parents in a module-global keyed by
    (model, provider). Clear it before AND after every test so tests never
    leak a cached parent (or a cached RuntimeParentError-avoidance) into a
    sibling test."""
    import workflow.runtime.live as live_mod

    live_mod._PARENT_CACHE.clear()
    yield
    live_mod._PARENT_CACHE.clear()


class _DummyParent:
    """Stand-in for a real AIAgent runtime parent -- only exposes the
    attributes resolve_effective_model / LiveWorker actually read."""

    def __init__(self, model="parent/default-model", provider="parent-provider"):
        self.model = model
        self.provider = provider


class _RecordingBuilder:
    """Stub child_builder: records the kwargs it was called with, returns a
    sentinel object (never a real AIAgent)."""

    def __init__(self):
        self.calls = []
        self.sentinel = object()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.sentinel

    @property
    def last(self):
        return self.calls[-1]


def _stub_runner(cost_usd=0.0, summary="child done"):
    def _run(task_index, goal, child=None, parent_agent=None):
        return {"cost_usd": cost_usd, "summary": summary}

    return _run


def _agent_node(node_id="a", *, model=None, provider=None, prompt="do the thing"):
    return Node(id=node_id, kind="agent", spec=NodeSpec(prompt=prompt, model=model, provider=provider))


# ---------------------------------------------------------------------------
# 1. Inherit proof
# ---------------------------------------------------------------------------


def test_inherit_no_spec_model_passes_none_and_reports_parent_model():
    parent = _DummyParent(model="parent/default-model", provider="parent-provider")
    builder = _RecordingBuilder()
    runner = _stub_runner()
    worker = LiveWorker(parent_agent=parent, child_builder=builder, child_runner=runner)

    node = _agent_node()  # no spec.model / spec.provider
    result = worker.run_node(node, {"input": {}})

    # half 1: the child_builder is asked to inherit (model=None so
    # _build_child_agent applies `model or parent_agent.model` itself)
    assert builder.last["model"] is None, builder.last

    # half 2: the envelope reports the actually-resolved model, which IS the
    # parent's model (not None -- the driver/NodeRun must see a real value)
    assert result["effective_model"] == parent.model
    assert result["output"]["effective_model"] == parent.model
    assert result["effective_provider"] == parent.provider


def test_inherit_credential_overrides_are_none():
    """Inherit path must not fabricate any override_* credential kwargs."""
    parent = _DummyParent()
    builder = _RecordingBuilder()
    worker = LiveWorker(parent_agent=parent, child_builder=builder, child_runner=_stub_runner())

    worker.run_node(_agent_node(), {"input": {}})
    for key in ("override_provider", "override_base_url", "override_api_key", "override_api_mode"):
        assert builder.last[key] is None, (key, builder.last)


# ---------------------------------------------------------------------------
# 2. Override proof (model only, same provider/credentials)
# ---------------------------------------------------------------------------


def test_override_model_only_keeps_parent_provider_credentials():
    parent = _DummyParent(model="parent/default-model", provider="parent-provider")
    builder = _RecordingBuilder()
    worker = LiveWorker(parent_agent=parent, child_builder=builder, child_runner=_stub_runner())

    node = _agent_node(model="some-other-model")  # no provider override
    result = worker.run_node(node, {"input": {}})

    assert builder.last["model"] == "some-other-model", builder.last
    for key in ("override_provider", "override_base_url", "override_api_key", "override_api_mode"):
        assert builder.last[key] is None, (key, builder.last)
    assert result["effective_model"] == "some-other-model"
    # provider was NOT overridden -> effective_provider still the parent's
    assert result["effective_provider"] == parent.provider


# ---------------------------------------------------------------------------
# 3. Cross-provider override
# ---------------------------------------------------------------------------


def test_cross_provider_override_passes_resolved_credentials(monkeypatch):
    resolved = {
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "or-test-key",
        "api_mode": "chat",
    }
    calls = []

    def fake_resolve_runtime_provider(*, requested=None, target_model=None):
        calls.append({"requested": requested, "target_model": target_model})
        return dict(resolved)

    import hermes_cli.runtime_provider as rtp

    monkeypatch.setattr(rtp, "resolve_runtime_provider", fake_resolve_runtime_provider)

    parent = _DummyParent(model="parent/default-model", provider="parent-provider")
    builder = _RecordingBuilder()
    worker = LiveWorker(parent_agent=parent, child_builder=builder, child_runner=_stub_runner())

    node = _agent_node(model="m", provider="openrouter")
    result = worker.run_node(node, {"input": {}})

    assert len(calls) == 1, "resolve_runtime_provider must be called exactly once for a provider override"
    assert builder.last["model"] == "m"
    assert builder.last["override_provider"] == resolved["provider"]
    assert builder.last["override_base_url"] == resolved["base_url"]
    assert builder.last["override_api_key"] == resolved["api_key"]
    assert builder.last["override_api_mode"] == resolved["api_mode"]
    assert result["effective_provider"] == "openrouter"
    assert result["effective_model"] == "m"


def test_same_provider_as_parent_still_resolves_fresh_credentials(monkeypatch):
    """An explicit spec.provider equal to the parent's own provider is still
    an explicit ask for that provider's own credential resolution (per the
    live.py module docstring) -- it must not be silently treated as inherit."""
    calls = []

    def fake_resolve_runtime_provider(*, requested=None, target_model=None):
        calls.append(requested)
        return {"provider": "parent-provider", "base_url": "u", "api_key": "k", "api_mode": "m"}

    import hermes_cli.runtime_provider as rtp

    monkeypatch.setattr(rtp, "resolve_runtime_provider", fake_resolve_runtime_provider)

    parent = _DummyParent(model="parent/default-model", provider="parent-provider")
    builder = _RecordingBuilder()
    worker = LiveWorker(parent_agent=parent, child_builder=builder, child_runner=_stub_runner())

    node = _agent_node(provider="parent-provider")  # no spec.model
    worker.run_node(node, {"input": {}})

    assert len(calls) == 1
    assert builder.last["override_provider"] == "parent-provider"


# ---------------------------------------------------------------------------
# 4. resolve_effective_model pure-function unit tests
# ---------------------------------------------------------------------------


def test_resolve_effective_model_no_spec():
    node = Node(id="a", kind="agent", spec=None)
    parent = _DummyParent(model="p-model", provider="p-provider")
    assert resolve_effective_model(node, parent) == ("p-model", "p-provider")


def test_resolve_effective_model_no_override_fields():
    node = _agent_node()  # spec present, model/provider unset
    parent = _DummyParent(model="p-model", provider="p-provider")
    assert resolve_effective_model(node, parent) == ("p-model", "p-provider")


def test_resolve_effective_model_model_only():
    node = _agent_node(model="override-model")
    parent = _DummyParent(model="p-model", provider="p-provider")
    assert resolve_effective_model(node, parent) == ("override-model", "p-provider")


def test_resolve_effective_model_provider_only():
    node = _agent_node(provider="override-provider")
    parent = _DummyParent(model="p-model", provider="p-provider")
    assert resolve_effective_model(node, parent) == ("p-model", "override-provider")


def test_resolve_effective_model_both():
    node = _agent_node(model="override-model", provider="override-provider")
    parent = _DummyParent(model="p-model", provider="p-provider")
    assert resolve_effective_model(node, parent) == ("override-model", "override-provider")


# ---------------------------------------------------------------------------
# 5. Non-agent node kind -> trivial ok envelope, child_builder never called
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["script", "join", "fanout", "map", "gate"])
def test_non_agent_node_kind_is_trivial_ok_and_does_not_call_builder(kind):
    parent = _DummyParent()
    builder = _RecordingBuilder()
    worker = LiveWorker(parent_agent=parent, child_builder=builder, child_runner=_stub_runner())

    node = Node(id="n", kind=kind)
    result = worker.run_node(node, {"input": {}})

    assert result == {"output": {"ok": True, "node": "n"}, "cost_usd": 0.0}
    assert builder.calls == [], "child_builder must never be called for a non-agent node kind"


# ---------------------------------------------------------------------------
# 6. build_runtime_parent raises RuntimeParentError, never returns None
# ---------------------------------------------------------------------------


def test_build_runtime_parent_raises_when_no_model_resolves(wf_home, monkeypatch):
    import hermes_cli.config as hc_config

    monkeypatch.setattr(hc_config, "load_config", lambda: {})

    with pytest.raises(RuntimeParentError):
        build_runtime_parent()


def test_build_runtime_parent_error_mentions_fake_fallback(wf_home, monkeypatch):
    """The error must be actionable: it names the --fake / HERMES_WORKFLOW_FAKE
    escape hatch so a caller isn't left with just a bare traceback."""
    import hermes_cli.config as hc_config

    monkeypatch.setattr(hc_config, "load_config", lambda: {})

    with pytest.raises(RuntimeParentError, match="HERMES_WORKFLOW_FAKE"):
        build_runtime_parent()


# ---------------------------------------------------------------------------
# 7. Driver-level plumbing: effective_model/effective_provider persisted +
#    survive a checkpoint round-trip.
# ---------------------------------------------------------------------------

LIVE_MODEL_YAML = """
workflow: live_model_plumbing
version: 1
nodes:
  - id: inherited
    kind: agent
    spec: {prompt: "inherit please"}
  - id: overridden
    kind: agent
    spec: {prompt: "override please", model: "explicit/model-x"}
edges: []
triggers:
  - { kind: manual }
"""


def test_driver_persists_effective_model_and_survives_checkpoint_roundtrip(wf_home):
    from workflow import compile_text, run
    from workflow.runtime.driver import RunState
    from workflow.store import checkpoint

    parent = _DummyParent(model="parent/default-model", provider="parent-provider")
    worker = LiveWorker(parent_agent=parent, child_builder=_RecordingBuilder(), child_runner=_stub_runner())

    vir = compile_text(LIVE_MODEL_YAML, phase1_warn_overrides=True)
    env = run(vir, input={}, worker=worker)
    assert env["status"] == "succeeded", env

    rec = checkpoint.load_run_record(env["run_id"])
    by_node = {nrd["node_id"]: nrd for nrd in rec["node_runs"].values()}
    assert by_node["inherited"]["effective_model"] == "parent/default-model"
    assert by_node["inherited"]["effective_provider"] == "parent-provider"
    assert by_node["overridden"]["effective_model"] == "explicit/model-x"
    # override was model-only -> provider still inherited from the parent
    assert by_node["overridden"]["effective_provider"] == "parent-provider"

    # checkpoint round-trip: from_dict(to_dict()) must not lose these fields
    state = RunState.from_dict(rec)
    roundtripped = RunState.from_dict(state.to_dict())
    for node_id in ("inherited", "overridden"):
        orig_nr = next(nr for nr in state.node_runs.values() if nr.node_id == node_id)
        rt_nr = next(nr for nr in roundtripped.node_runs.values() if nr.node_id == node_id)
        assert rt_nr.effective_model == orig_nr.effective_model
        assert rt_nr.effective_provider == orig_nr.effective_provider
