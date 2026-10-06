import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml


@pytest.fixture
def usage(monkeypatch):
    access = SimpleNamespace(require=Mock())
    subscriptions = SimpleNamespace(get_codex_usage_snapshot=Mock(return_value={
        "status": "ready", "used_tokens": 0, "used_7d_tokens": 24,
        "used_30d_tokens": 130, "limit_tokens": 100,
        "updated_at": "2026-10-02T13:00:00Z",
    }))
    sdk = ModuleType("adaos.sdk")
    sdk.access, sdk.subscriptions = access, subscriptions
    adaos = ModuleType("adaos")
    adaos.sdk = sdk
    monkeypatch.setitem(sys.modules, "adaos", adaos)
    monkeypatch.setitem(sys.modules, "adaos.sdk", sdk)
    path = Path(__file__).resolve().parents[1] / "handlers" / "usage.py"
    spec = importlib.util.spec_from_file_location("tested_usage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, access, subscriptions.get_codex_usage_snapshot


def test_usage_uses_native_windows_and_bounded_quota(usage):
    module, access, read = usage
    result = module.get_subscription_usage(webspace_id="space")
    access.require.assert_called_once_with("workspace.read")
    read.assert_called_once_with(webspace_id="space", refresh=False, timeout=8.0)
    metrics = result["usage_arc"]["metrics"]
    assert [m["used_tokens"] for m in metrics] == [24, 130, 0]
    assert [m["value"] for m in metrics] == pytest.approx([24 / 130 * 100, 100, 0])
    assert result["usage_arc"]["value"] == "130 tokens"


@pytest.mark.parametrize("unknown", [None, -1, True, "100", float("nan"), float("inf"), 10**400])
def test_unknown_usage_never_becomes_zero(usage, unknown):
    module, _, read = usage
    read.return_value.update(used_7d_tokens=unknown, limit_tokens=unknown)
    metrics = module.get_subscription_usage()["usage_arc"]["metrics"]
    assert metrics[0]["used_tokens"] is None
    assert metrics[0]["display"] == "Unavailable"
    assert metrics[0]["value"] is None
    assert metrics[1]["value"] == 100


def test_quota_does_not_affect_ai_usage_comparison(usage):
    module, _, read = usage
    read.return_value["limit_tokens"] = 0
    result = module.get_subscription_usage()
    assert result["usage_arc"]["metrics"][0]["display"] == "24 tokens"
    assert result["usage_arc"]["metrics"][0]["value"] == pytest.approx(24 / 130 * 100)


def test_stale_values_keep_timestamp_and_no_money_is_inferred(usage):
    module, _, read = usage
    read.return_value.update(status="stale", cost={"status": "partial", "estimated_usd": 12})
    result = module.get_subscription_usage(refresh=True)
    assert result["status"] == "stale"
    assert result["updated_at"] == read.return_value["updated_at"]
    assert "Stale" in result["usage_arc"]["description"]
    assert result["usage_arc"]["metrics"][0]["used_tokens"] == 24
    assert "cost" not in result and "USD" not in str(result)
    assert read.call_args.kwargs["refresh"] is True


def test_denial_precedes_provider_io(usage):
    module, access, read = usage
    access.require.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        module.get_subscription_usage()
    read.assert_not_called()


@pytest.mark.parametrize("snapshot", [None, [], {"status": []}, {"status": "bad"}, {"status": "unavailable", "used_tokens": 50}])
def test_unavailable_and_invalid_snapshots(usage, snapshot):
    module, _, read = usage
    read.return_value = snapshot
    result = module.get_subscription_usage()
    assert result["status"] == "unavailable"
    assert not result["ok"]
    assert all(m["value"] is None for m in result["usage_arc"]["metrics"])


def test_provider_exception_is_not_exposed(usage):
    module, _, read = usage
    read.side_effect = RuntimeError("private provider diagnostic")
    result = module.get_subscription_usage()
    assert result["status"] == "unavailable"
    assert "private" not in str(result)


def test_owned_tool_and_causal_route():
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "skill.yaml").read_text(encoding="utf-8"))
    tool = next(t for t in manifest["tools"] if t["name"] == "get_subscription_usage")
    assert tool["permissions"] == ["workspace.read"]
    assert tool["side_effects"] == "read_only"
    assert tool["application_access"]["permission"] == "workspace.read"
    assert tool["name"] in manifest["exports"]["tools"]
    route = next(r for r in manifest["data_routes"] if r["tool"] == tool["name"])
    assert route["read_policy"]["preserve_last_value"] is True
    assert "explicit_refresh" in route["read_policy"]["triggers"]
