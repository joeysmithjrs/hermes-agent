"""Persistent workflow registry (post-Phase-3 commit 2).

Hermetic: every test runs against a tmp HERMES_HOME via the `wf_home` fixture,
so the catalog under test is always empty to begin with. CLI surfaces are
invoked as functions with hand-built Namespaces, matching test_cli.py.
"""

from __future__ import annotations

import argparse
import json

import pytest

from workflow.store import catalog
from workflow.store.catalog import CatalogError

RECIPE_YAML = """
workflow: desk_autonomy
version: 1
nodes:
  - id: seed
    kind: agent
    spec: {prompt: "review the {{ params.market }} desk"}
  - id: tally
    kind: script
    run: workflow.examples.echo
edges:
  - { from: seed, to: tally }
"""

PLAIN_YAML = """
workflow: plain_recipe
nodes:
  - id: a
    kind: agent
    spec: {prompt: "go"}
edges: []
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ---- ids + storage layout --------------------------------------------------


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", "a\\b", None, 3, "x" * 65])
def test_invalid_catalog_ids_rejected(bad):
    with pytest.raises(CatalogError):
        catalog.validate_catalog_id(bad)


def test_register_snapshots_yaml_and_writes_index(wf_home, tmp_path):
    src = _write(tmp_path, "recipe.yaml", RECIPE_YAML)
    entry = catalog.register("desk-autonomy", src, tags=["desk", "daily"], owner="joe")

    assert entry["version"] == 1
    snapshot = wf_home / "workflows" / "catalog" / "desk-autonomy" / "version_1.yaml"
    assert snapshot.read_text(encoding="utf-8") == RECIPE_YAML
    assert (wf_home / "workflows" / "catalog" / "index.yaml").exists()
    assert entry["workflow_id"] == "desk_autonomy"
    assert entry["tags"] == ["daily", "desk"]
    assert entry["owner"] == "joe"
    assert entry["registered_at"]


def test_register_snapshot_is_immutable_against_source_edits(wf_home, tmp_path):
    """A registered version must not silently become 'whatever is at that path
    today' -- that is the whole reason register snapshots instead of pointing."""
    src = tmp_path / "recipe.yaml"
    src.write_text(PLAIN_YAML, encoding="utf-8")
    catalog.register("plain", str(src))

    src.write_text(PLAIN_YAML.replace("go", "TOTALLY DIFFERENT"), encoding="utf-8")

    assert "TOTALLY DIFFERENT" not in catalog.load_version_text("plain")


def test_register_bumps_version_per_id(wf_home, tmp_path):
    src = _write(tmp_path, "recipe.yaml", PLAIN_YAML)
    assert catalog.register("plain", src)["version"] == 1
    assert catalog.register("plain", src)["version"] == 2
    assert catalog.register("other", src)["version"] == 1

    assert [e.version for e in catalog.list_entries(catalog_id="plain")] == [1, 2]
    assert catalog.get_entry("plain").version == 2
    assert catalog.get_entry("plain", 1).version == 1


def test_register_rejects_missing_and_non_mapping_files(wf_home, tmp_path):
    with pytest.raises(CatalogError):
        catalog.register("plain", str(tmp_path / "nope.yaml"))
    bad = _write(tmp_path, "list.yaml", "- just\n- a list\n")
    with pytest.raises(CatalogError):
        catalog.register("plain", bad)


def test_get_entry_unknown_id_and_version_fail_closed(wf_home, tmp_path):
    with pytest.raises(CatalogError):
        catalog.get_entry("never-registered")
    catalog.register("plain", _write(tmp_path, "r.yaml", PLAIN_YAML))
    with pytest.raises(CatalogError):
        catalog.get_entry("plain", 99)


def test_list_entries_filters_by_tag_and_latest(wf_home, tmp_path):
    src = _write(tmp_path, "r.yaml", PLAIN_YAML)
    catalog.register("a", src, tags=["desk"])
    catalog.register("a", src, tags=["desk"])
    catalog.register("b", src, tags=["desk", "nightly"])
    catalog.register("c", src, tags=["other"])

    latest = catalog.list_entries(latest_only=True)
    assert [(e.id, e.version) for e in latest] == [("a", 2), ("b", 1), ("c", 1)]
    assert [e.id for e in catalog.list_entries(tags=["desk"])] == ["a", "a", "b"]
    assert [e.id for e in catalog.list_entries(tags=["desk", "nightly"])] == ["b"]


def test_empty_catalog_reads_as_empty_not_error(wf_home):
    assert catalog.load_index() == []
    assert catalog.list_entries() == []


def test_catalog_does_not_touch_run_artifacts(wf_home, tmp_path):
    """Registering must not create/modify anything under workflows/runs or the
    sqlite run index."""
    import workflow
    from workflow.runtime.worker import FakeWorker

    env = workflow.run(workflow.compile_text(PLAIN_YAML), worker=FakeWorker())
    run_json = wf_home / "workflows" / "runs" / env["run_id"] / "run.json"
    before = run_json.read_bytes()

    catalog.register("plain", _write(tmp_path, "r.yaml", PLAIN_YAML))

    assert run_json.read_bytes() == before
    assert sorted(p.name for p in (wf_home / "workflows" / "runs").iterdir()) == [env["run_id"]]


# ---- params ----------------------------------------------------------------


def test_render_params_substitutes_only_the_params_root():
    text = "a: {{ params.market }}\nb: {{ seed.output.topic }}\n"
    out = catalog.render_params(text, {"market": "EU"})
    assert out == "a: EU\nb: {{ seed.output.topic }}\n"


def test_render_params_missing_param_fails_closed():
    with pytest.raises(CatalogError) as exc:
        catalog.render_params("m: {{ params.market }}", {"other": 1})
    assert "market" in str(exc.value)


def test_render_params_inlines_structured_values():
    out = catalog.render_params("v: {{ params.items }}", {"items": ["a", "b"]})
    assert "a" in out and "b" in out
    import yaml

    assert yaml.safe_load(out)["v"] == ["a", "b"]


def test_validate_params_required_key():
    schema = {"type": "object", "required": ["market"], "properties": {"market": {"type": "string"}}}
    assert catalog.validate_params(schema, {"market": "EU"}) is None
    assert catalog.validate_params(schema, {}) is not None
    assert catalog.validate_params(None, {}) is None


# ---- CLI surfaces ----------------------------------------------------------


def test_cli_register_and_list_catalog(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_list_catalog, _cmd_register

    src = _write(tmp_path, "recipe.yaml", RECIPE_YAML)
    rc = _cmd_register(
        argparse.Namespace(
            catalog_id="desk-autonomy",
            from_file=src,
            tags="desk,daily",
            owner="joe",
            description="the desk loop",
            param_schema='{"type": "object", "required": ["market"]}',
            as_json=False,
        )
    )
    assert rc == 0
    assert "registered desk-autonomy v1" in capsys.readouterr().out

    rc = _cmd_list_catalog(
        argparse.Namespace(tags=None, catalog_id=None, all_versions=False, as_json=True)
    )
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["id"] == "desk-autonomy"
    assert rows[0]["param_schema"]["required"] == ["market"]


def test_cli_list_catalog_empty(wf_home, capsys):
    from workflow.cli import _cmd_list_catalog

    rc = _cmd_list_catalog(
        argparse.Namespace(tags=None, catalog_id=None, all_versions=False, as_json=False)
    )
    assert rc == 0
    assert "no catalog entries" in capsys.readouterr().out


def test_cli_run_catalog_renders_params_and_runs(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_register, _cmd_run_catalog

    src = _write(tmp_path, "recipe.yaml", RECIPE_YAML)
    _cmd_register(
        argparse.Namespace(
            catalog_id="desk-autonomy", from_file=src, tags=None, owner=None,
            description=None, param_schema=None, as_json=False,
        )
    )
    capsys.readouterr()

    rc = _cmd_run_catalog(
        argparse.Namespace(
            catalog_id="desk-autonomy", version=None, params='{"market": "EU"}',
            input=None, dry_run=False, max_budget_usd=None, fake=True,
        )
    )
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert env["status"] == "succeeded"
    assert env["catalog"] == {"id": "desk-autonomy", "version": 1}
    # params also reach the run as input
    from workflow.store import checkpoint

    assert checkpoint.load_run_record(env["run_id"])["workflow_id"] == "desk_autonomy"


def test_cli_run_catalog_missing_param_is_usage_error(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_register, _cmd_run_catalog

    src = _write(tmp_path, "recipe.yaml", RECIPE_YAML)
    _cmd_register(
        argparse.Namespace(
            catalog_id="desk-autonomy", from_file=src, tags=None, owner=None,
            description=None, param_schema='{"type": "object", "required": ["market"]}',
            as_json=False,
        )
    )
    capsys.readouterr()

    rc = _cmd_run_catalog(
        argparse.Namespace(
            catalog_id="desk-autonomy", version=None, params="{}", input=None,
            dry_run=False, max_budget_usd=None, fake=True,
        )
    )
    assert rc == 3
    assert "param_schema" in capsys.readouterr().err


def test_cli_run_catalog_unknown_id_is_usage_error(wf_home, capsys):
    from workflow.cli import _cmd_run_catalog

    rc = _cmd_run_catalog(
        argparse.Namespace(
            catalog_id="nope", version=None, params=None, input=None,
            dry_run=False, max_budget_usd=None, fake=True,
        )
    )
    assert rc == 3
    assert "no catalog entry" in capsys.readouterr().err


def test_cli_run_catalog_rejects_recipe_that_fails_verify(wf_home, tmp_path, capsys):
    """A registered recipe gets no special trust -- the verifier applies."""
    from workflow.cli import _cmd_register, _cmd_run_catalog

    bad = _write(tmp_path, "bad.yaml", "workflow: bad_one\nnodes:\n  - id: a\n    kind: agent\nedges: []\n")
    _cmd_register(
        argparse.Namespace(
            catalog_id="bad-one", from_file=bad, tags=None, owner=None,
            description=None, param_schema=None, as_json=False,
        )
    )
    capsys.readouterr()

    rc = _cmd_run_catalog(
        argparse.Namespace(
            catalog_id="bad-one", version=None, params=None, input=None,
            dry_run=False, max_budget_usd=None, fake=True,
        )
    )
    assert rc == 2  # EXIT_VERIFY
    assert "PROMPT" in capsys.readouterr().err


def test_cli_run_catalog_pins_version(wf_home, tmp_path, capsys):
    from workflow.cli import _cmd_register, _cmd_run_catalog

    src = tmp_path / "r.yaml"
    src.write_text(PLAIN_YAML, encoding="utf-8")
    _cmd_register(argparse.Namespace(catalog_id="plain", from_file=str(src), tags=None,
                                     owner=None, description=None, param_schema=None, as_json=False))
    src.write_text(PLAIN_YAML.replace("plain_recipe", "plain_recipe_v2"), encoding="utf-8")
    _cmd_register(argparse.Namespace(catalog_id="plain", from_file=str(src), tags=None,
                                     owner=None, description=None, param_schema=None, as_json=False))
    capsys.readouterr()

    rc = _cmd_run_catalog(
        argparse.Namespace(catalog_id="plain", version=1, params=None, input=None,
                           dry_run=False, max_budget_usd=None, fake=True)
    )
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert env["workflow_id"] == "plain_recipe"
    assert env["catalog"]["version"] == 1
