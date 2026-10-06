import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml


@pytest.fixture
def builder(monkeypatch):
    access = SimpleNamespace(require=Mock())
    read = Mock(return_value={
        "status": "ready", "reason": "", "observed_at": "2026-10-02T15:00:00Z",
        "freshness": "current",
        "application": {"title": "Fixture application", "phase": "automation", "revision": "024"},
        "builder": {"available": True, "webspace_id": "builder-related"},
        "preview": {"available": True, "webspace_id": "preview-related"},
    })
    sdk = ModuleType("adaos.sdk")
    sdk.access = access
    sdk.applications = SimpleNamespace(get_current_builder_application=read)
    root = ModuleType("adaos")
    root.sdk = sdk
    monkeypatch.setitem(sys.modules, "adaos", root)
    monkeypatch.setitem(sys.modules, "adaos.sdk", sdk)
    path = Path(__file__).resolve().parents[1] / "handlers" / "builder.py"
    spec = importlib.util.spec_from_file_location("tested_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.get_current_builder_application, access, read


def test_authorized_projection_uses_related_destinations(builder):
    get, access, read = builder
    result = get()
    access.require.assert_called_once_with("workspace.read")
    read.assert_called_once_with()
    assert result["title"] == "Fixture application"
    assert result["stage"] == "automation"
    assert result["revision"] == "024"
    assert result["freshness"] == "current"
    assert result["preview"]["webspace_id"] == "preview-related"
    assert result["observed_at"] == read.return_value["observed_at"]


def test_denial_precedes_sdk_read(builder):
    get, access, read = builder
    access.require.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        get()
    read.assert_not_called()


@pytest.mark.parametrize("revision, expected", [(24, "24"), (0, "0"), ("024", "024"), (None, "Unavailable"), (True, "Unavailable"), ({}, "Unavailable"), ("", "Unavailable")])
def test_revision_preserves_sdk_value_without_fabrication(builder, revision, expected):
    get, _, read = builder
    read.return_value["application"]["revision"] = revision
    assert get()["revision"] == expected


@pytest.mark.parametrize("application", [None, [], {}, {"title": [], "phase": False}])
def test_absent_or_malformed_application_never_uses_candidates(builder, application):
    get, _, read = builder
    read.return_value.update(application=application, reason="No active application", candidates=[{"title": "Do not select"}])
    result = get()
    assert result["title"] == "Unavailable"
    assert result["revision"] == "Unavailable"
    assert result["stage"] in ("Unavailable", "No active application")
    # The trusted host may still be available without an active application.
    assert result["builder"]["available"] is True


@pytest.mark.parametrize("freshness", [None, "fresh", "stale", "offline", {}, []])
def test_unknown_freshness_is_unavailable(builder, freshness):
    get, _, read = builder
    read.return_value["freshness"] = freshness
    assert get()["freshness"] == "unavailable"


def test_display_projection_is_bounded_and_matches_output_schema(builder):
    import jsonschema

    get, _, read = builder
    read.return_value["application"] = {k: "x" * 10000 for k in ("title", "phase", "revision")}
    result = get()
    assert all(len(result[k]) == 512 for k in ("title", "stage", "revision"))
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "skill.yaml").read_text(encoding="utf-8"))
    schema = next(t for t in manifest["tools"] if t["name"] == "get_current_builder_application")["output_schema"]
    jsonschema.validate(result, schema)
    read.side_effect = RuntimeError("unavailable")
    jsonschema.validate(get(), schema)


@pytest.mark.parametrize("status", ["ambiguous", "unavailable", "invalid", None])
def test_nonready_selection_preserves_only_explicit_builder_destination(builder, status):
    get, _, read = builder
    read.return_value["status"] = status
    result = get()
    if status in {"ambiguous", "unavailable"}:
        assert result["builder"] == {"available": True, "webspace_id": "builder-related"}
        assert result["preview"] == {"available": True, "webspace_id": "preview-related"}
        assert result["title_i18n"]["key"].startswith("web_desktop.development.builder.")
    else:
        assert all(result[k] == {"available": False, "webspace_id": ""} for k in ("builder", "preview"))


@pytest.mark.parametrize("destination", [None, [], {}, {"available": "true", "webspace_id": "x"}, {"available": True, "webspace_id": ""}])
def test_malformed_destination_fails_closed(builder, destination):
    get, _, read = builder
    read.return_value["preview"] = destination
    assert get()["preview"] == {"available": False, "webspace_id": ""}


def test_failed_read_clears_prior_destinations_and_redacts_exception(builder):
    get, _, read = builder
    assert get()["builder"]["available"]
    read.side_effect = RuntimeError("private transport diagnostic")
    result = get()
    assert result["builder"]["available"] is False
    assert "private" not in str(result)


def test_manifest_contract_is_complete():
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "skill.yaml").read_text(encoding="utf-8"))
    name = "get_current_builder_application"
    declaration = next(t for t in manifest["tools"] if t["name"] == name)
    assert name in manifest["exports"]["tools"]
    assert declaration["entry"] == "handlers.main:" + name
    assert declaration["permissions"] == ["workspace.read"]
    assert declaration["side_effects"] == "read_only"
    assert declaration["input_schema"]["additionalProperties"] is False
    route = next(r for r in manifest["data_routes"] if r["tool"] == name)
    assert route["read_policy"]["preserve_last_value"] is False
    assert route["budget"]["max_payload_bytes"] == 8192
