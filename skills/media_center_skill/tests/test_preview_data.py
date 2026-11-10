"""Synthetic SDK consumer tests; Core profile resolution/ingress is not emulated."""
import copy
import sys
from pathlib import Path

import pytest
from adaos.sdk.data import preview_binding, skill_env

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from media_center import preview
from media_center.catalog import MediaCenterRepository, default_db_path



def profiles():
    # Synthetic public SDK values, independent of sibling project packaging.
    common = {"schema": "adaos.builder.binding_profile.v1",
              "owner": "project:media_center", "logical_schema_ref": "adaos.media.catalog.v1",
              "capabilities": ["media.catalog"], "expires_at": None}
    items = []
    for profile_id, mode, source, read, write, sensitivity, redaction in [
        ("demo-fixture", "fixture", "fixture:media_center:demo-v1", "fixture", "none", "internal", "none"),
        ("isolated-sandbox", "sandbox", "sandbox:media_center:local", "sandbox", "sandbox", "internal", "none"),
        ("local-library-readonly", "live_readonly", "skill-data:media_center_skill:workspace", "scoped_live", "none", "personal", "required")]:
        mapping = [{"logical_ref": "media.catalog",
                    "implementation_ref": "skill:media_center_skill" + (":fixture" if mode == "fixture" else ""),
                    "status": "fixture_only" if mode == "fixture" else "mapped"}]
        items.append(dict(common, profile_id=profile_id, mode=mode, source_ref=source,
                          read_policy=read, write_policy=write, sensitivity=sensitivity,
                          redaction=redaction, implementation_mappings=mapping))
    return {"default_profile_id": "demo-fixture", "profiles": items}


def verified_fixture():
    return dict(copy.deepcopy(profiles()["profiles"][0]),
                schema="adaos.builder.materialized_data_binding.v1",
                runtime_source="isolated_preview", provider_opened=False, ready=True)




@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("field", [
    "schema", "profile_id", "mode", "source_ref", "owner", "read_policy",
    "write_policy", "implementation_mappings", "capabilities", "logical_schema_ref",
    "runtime_source", "provider_opened", "ready", "sensitivity", "redaction", "expires_at"])
def test_fixture_binding_mismatch_never_opens_root(monkeypatch, tmp_path, field, missing):
    value = verified_fixture()
    if missing:
        del value[field]
    else:
        value[field] = "wrong"
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: value)
    opened = []
    monkeypatch.setattr(skill_env, "skill_data_root", lambda: opened.append(True) or tmp_path)
    with pytest.raises(PermissionError):
        MediaCenterRepository()
    assert not opened
    assert not list(tmp_path.iterdir())


def test_missing_binding_cannot_bootstrap_fixture(monkeypatch, tmp_path):
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: None)
    monkeypatch.setattr(skill_env, "skill_data_root", lambda: tmp_path)
    with pytest.raises(PermissionError):
        preview.seed(object(), verified_fixture())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mapping", [[], [None], preview.MAPPING * 2,
    [{"logical_ref": "media.catalog", "implementation_ref": "skill:other", "status": "fixture_only"}]])
def test_fixture_rejects_missing_extra_or_foreign_mappings(monkeypatch, tmp_path, mapping):
    value = dict(verified_fixture(), implementation_mappings=mapping)
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: value)
    monkeypatch.setattr(skill_env, "skill_data_root", lambda: pytest.fail("opened invalid fixture root"))
    with pytest.raises(PermissionError, match="binding_mismatch"):
        MediaCenterRepository()


def test_fixture_count_determinism_and_isolation(monkeypatch, tmp_path):
    selected = {"binding": None, "root": tmp_path / "live"}
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: selected["binding"])
    monkeypatch.setattr(skill_env, "skill_data_root", lambda: selected["root"])
    # An inherited process override must never select a preview root.
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "live" / "db" / "media_center.sqlite3"))
    live = MediaCenterRepository()
    with live.connect() as connection:
        live_before = connection.execute("SELECT * FROM catalog_items").fetchall()
    selected.update(binding=verified_fixture(), root=tmp_path / "fixture")
    with pytest.raises(PermissionError, match="root_mismatch"):
        live.connect()
    assert default_db_path() == tmp_path / "fixture/db/media_center.sqlite3"
    fixture = MediaCenterRepository()
    def rows(repo):
        with repo.connect() as connection:
            return [tuple(row) for row in connection.execute("SELECT * FROM catalog_items ORDER BY id")]
    before = rows(fixture)
    assert len(before) == 8
    with fixture.connect() as connection:
        titles = [r[0] for r in connection.execute("SELECT title FROM catalog_items ORDER BY resource_id")]
    assert titles == list(preview.TITLES)
    assert "Aurora Station" in titles
    assert rows(MediaCenterRepository()) == before
    with pytest.raises(PermissionError, match="root_mismatch"):
        MediaCenterRepository(live.db_path)
    selected.update(binding=profiles()["profiles"][1], root=tmp_path / "sandbox")
    sandbox = MediaCenterRepository()
    assert rows(sandbox) == []
    with sandbox.connect() as connection:
        connection.execute("INSERT INTO meta(key,value) VALUES('sandbox-test','kept')")
    selected.update(binding=profiles()["profiles"][2], root=tmp_path / "live")
    assert rows(MediaCenterRepository()) == [tuple(r) for r in live_before]
    selected.update(binding=verified_fixture(), root=tmp_path / "fixture")
    assert rows(MediaCenterRepository()) == before
    selected["root"] = tmp_path / "fixture-again"
    assert rows(MediaCenterRepository()) == before


def test_live_readonly_never_initializes_or_mutates_catalog(monkeypatch, tmp_path):
    from media_center.read_models import ReadModelUnavailable
    monkeypatch.delenv("MEDIA_CENTER_DB_PATH", raising=False)
    monkeypatch.setattr(skill_env, "skill_data_root", lambda: tmp_path)
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: profiles()["profiles"][2])
    with pytest.raises(ReadModelUnavailable):
        MediaCenterRepository()
    assert not (tmp_path / "db").exists()
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: None)
    MediaCenterRepository()
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: profiles()["profiles"][2])
    repository = MediaCenterRepository()
    with repository.connect() as connection:
        import sqlite3
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO meta(key,value) VALUES('denied','denied')")


def test_fixture_is_visible_on_first_interactive_library_read(monkeypatch, tmp_path):
    import importlib.util
    value = verified_fixture()
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: value)
    monkeypatch.setattr(skill_env, "skill_data_root", lambda: tmp_path)
    spec = importlib.util.spec_from_file_location("preview_test_handlers", SKILL_ROOT / "handlers/main.py")
    handlers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handlers)
    result = handlers.library(auto_scan=False)
    assert result["ok"] is True
    assert len(result["items"]) == 8, result
    assert any(item["title"] == "Aurora Station" for item in result["items"])
    def forbidden(*args, **kwargs):
        pytest.fail("preview reached a live worker or provider")
    monkeypatch.setattr(handlers, "background_runtime", forbidden)
    monkeypatch.setattr(handlers, "_coordinator", forbidden)
    for value in profiles()["profiles"]:
        active = dict(value, schema="adaos.builder.materialized_data_binding.v1",
                      runtime_source="isolated_preview", provider_opened=False, ready=True)
        monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: active)
        handlers.on_sys_ready(None)
        handlers._start_live_runtime()
        handlers.on_agent_catalog_changed({})
        handlers.on_playback_observed({"item_id": "live-item", "state": "playing"})
        assert handlers._invoke_agent("list_roots")[0] is None
        assert handlers._discover_resources()[0] == []
    handlers.dispose_read_models()


def test_interactive_repository_lookup_never_reenters_fixture_bootstrap(
    monkeypatch, tmp_path
):
    """A read remains write-free even when hot reload splits ContextVars."""
    import importlib.util

    value = verified_fixture()
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: value)
    monkeypatch.setattr(skill_env, "skill_data_root", lambda: tmp_path)
    spec = importlib.util.spec_from_file_location(
        "preview_read_boundary_handlers", SKILL_ROOT / "handlers/main.py"
    )
    handlers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handlers)

    preview.prepare_fixture_read()
    calls = []
    original = preview.initialize_fixture

    def observed(repository, selected):
        calls.append((repository, selected))
        return original(repository, selected)

    monkeypatch.setattr(preview, "initialize_fixture", observed)
    repository = handlers._repository()
    coordinator = handlers._coordinator(repository)

    assert calls == []
    assert coordinator.home(profile_id="default", limit=6)["items"]
    handlers.dispose_read_models()


def test_read_denied_does_not_open_data_root(monkeypatch, tmp_path):
    value = dict(verified_fixture(), read_policy="none")
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: value)
    def forbidden():
        pytest.fail("denied binding reached skill_data_root")
    monkeypatch.setattr(skill_env, "skill_data_root", forbidden)
    with pytest.raises(PermissionError, match="preview_data_profile_read_denied"):
        MediaCenterRepository()


@pytest.mark.parametrize("fixture, expected_state, expected_count", [
    (True, "ready", 8),
    (False, "empty", 0),
])
def test_preview_snapshot_projects_bound_catalog_state(
    monkeypatch, tmp_path, fixture, expected_state, expected_count
):
    import importlib.util
    import adaos.sdk.io as sdk_io

    selected = verified_fixture() if fixture else profiles()["profiles"][1]
    monkeypatch.setattr(preview_binding, "preview_data_binding", lambda: selected)
    monkeypatch.setattr(skill_env, "skill_data_root", lambda: tmp_path)
    monkeypatch.delenv("MEDIA_CENTER_DB_PATH", raising=False)
    published = []
    monkeypatch.setattr(sdk_io, "stream_variable_publish",
                        lambda receiver, value, **kwargs: published.append(value))
    spec = importlib.util.spec_from_file_location("preview_snapshot_handlers", SKILL_ROOT / "handlers/main.py")
    handlers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handlers)
    monkeypatch.setattr(handlers, "_agent_sync_status", lambda: {"state": "updating"})
    catalog = handlers.MediaCatalogCoordinator(MediaCenterRepository())

    assert handlers._publish_library_snapshot(catalog) is True
    state = published[0]["collection_state"]
    assert state["state"] == expected_state
    assert state["available_count"] == expected_count
    assert state["configured"] is True
