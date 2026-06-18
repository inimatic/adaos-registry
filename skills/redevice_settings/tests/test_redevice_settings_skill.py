from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import uuid4


def _load_redevice_settings_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    module_name = f"test_redevice_settings_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_normalize_device_and_sections_surface_agent_versions() -> None:
    mod = _load_redevice_settings_module()
    item = mod._normalize_device(
        {
            "ref": "redevice:endpoint-1",
            "identity": {"endpoint_id": "endpoint-1", "pair_code": "pair-1"},
            "policy": {"effective_name": "Kitchen panel"},
            "observation": {"connection_state": "online", "online": True, "last_seen_at": 0},
            "runtime": {"active_app": {"app_id": "demo"}},
            "diagnostics": {
                "endpoint_manifest": {
                    "schema_version": "endpoint-manifest.v1",
                    "endpoint_id": "endpoint-1",
                    "agent_version": "0.1.1",
                    "agent_version_code": 2,
                },
                "endpoint_policy": {"redevice_agent": {"version": "0.1.2", "version_code": 3}},
            },
        }
    )
    sections = mod._section_rows(item)
    overview = {row["id"]: row for row in sections["overview"]}
    about = {row["id"]: row for row in sections["about"]}

    assert item["software_version"] == "0.1.1"
    assert item["served_version"] == "0.1.2"
    assert item["version_status"] == "drift"
    assert overview["agent_version"]["description"] == "0.1.1"
    assert "served 0.1.2" in overview["agent_version"]["subtitle"]
    assert about["version"]["details"]["served_version_code"] == "3"


def test_normalize_device_maps_legacy_android_capabilities() -> None:
    mod = _load_redevice_settings_module()
    item = mod._normalize_device(
        {
            "ref": "redevice:endpoint-1",
            "identity": {"endpoint_id": "endpoint-1", "pair_code": "pair-1"},
            "policy": {"effective_name": "Kitchen tablet"},
            "observation": {"connection_state": "online", "online": True, "last_seen_at": 0},
            "diagnostics": {
                "diagnostic_report": {
                    "network_online": True,
                    "battery_level": 0.7,
                    "charging": True,
                    "capabilities": {
                        "audio.input": {"available": True, "quality": "unknown"},
                        "audio.output": {"available": True, "quality": "ok"},
                        "network.bluetooth": {"available": True, "quality": "unknown"},
                        "screen": {"available": True, "readability": "confirmed"},
                    },
                },
                "endpoint_manifest": {
                    "zone_id": "ru",
                    "assistant_name": "Homepoint",
                    "hub_id": "hub-1",
                    "endpoint_id": "endpoint-1",
                },
            },
        }
    )
    sections = mod._section_rows(item)
    overview = {row["id"]: row for row in sections["overview"]}
    audio = {row["id"]: row for row in sections["audio"]}
    bluetooth = {row["id"]: row for row in sections["bluetooth"]}

    assert item["subnet"]["assistant_name"] == "Homepoint"
    assert overview["subnet"]["description"] == "Homepoint"
    assert audio["input"]["description"] == "unknown"
    assert audio["output"]["description"] == "ok"
    assert bluetooth["available"]["description"] == "unknown"


def test_normalize_device_keeps_diagnostics_compact() -> None:
    mod = _load_redevice_settings_module()
    item = mod._normalize_device(
        {
            "ref": "redevice:endpoint-1",
            "identity": {"endpoint_id": "endpoint-1", "pair_code": "pair-1"},
            "policy": {"effective_name": "Kitchen tablet"},
            "observation": {"connection_state": "online", "online": True, "last_seen_at": 0},
            "diagnostics": {
                "endpoint_manifest": {
                    "schema_version": "endpoint-manifest.v1",
                    "large_nested": {f"k{idx}": f"value-{idx}" for idx in range(40)},
                },
                "diagnostic_report": {
                    "huge_text": "x" * 1000,
                    "large_list": [{"value": idx} for idx in range(40)],
                },
            },
        }
    )

    diagnostics = item["diagnostics"]
    assert diagnostics["endpoint_manifest"]["schema_version"] == "endpoint-manifest.v1"
    assert "_truncated_fields" in diagnostics["endpoint_manifest"]["large_nested"]
    assert diagnostics["diagnostic_report"]["huge_text"].endswith("...")
    assert len(diagnostics["diagnostic_report"]["large_list"]) == 8


def test_normalize_device_infers_online_from_recent_root_last_seen() -> None:
    mod = _load_redevice_settings_module()
    item = mod._normalize_device(
        {
            "code": "SNX68P2A",
            "endpoint_id": "redevice-1",
            "state": "consumed",
            "raw": {
                "code": "SNX68P2A",
                "endpoint_id": "redevice-1",
                "state": "consumed",
                "last_seen_at": mod.time.time(),
                "endpoint_policy": {"hub_id": "sn_92ffc943", "trust_level": "limited"},
            },
        }
    )

    assert item["online"] is True
    assert item["online_state"] == "online"
    assert item["last_seen"] == "0s"


def test_first_selected_skips_revoked_endpoint_when_online_available(monkeypatch) -> None:
    mod = _load_redevice_settings_module()
    selected: dict[str, str] = {"desktop": "redevice:old-endpoint"}
    monkeypatch.setattr(mod, "_selected_by_ws", lambda: dict(selected))
    monkeypatch.setattr(mod, "_set_memory_dict", lambda key, value: selected.update(value))

    items = [
        {
            "ref": "redevice:old-endpoint",
            "code": "FMRS7WTB",
            "lifecycle_state": "revoked",
            "online": False,
            "commandable": False,
        },
        {
            "ref": "redevice:new-endpoint",
            "code": "SNX68P2A",
            "lifecycle_state": "consumed",
            "online": True,
            "commandable": True,
        },
    ]

    assert mod._first_selected(items, "desktop") == "redevice:new-endpoint"
    assert selected["desktop"] == "redevice:new-endpoint"


def test_settings_command_refuses_offline_selected_endpoint(monkeypatch) -> None:
    mod = _load_redevice_settings_module()
    snapshot = {
        "selected": {
            "ref": "redevice:old-endpoint",
            "code": "LCCX54KP",
            "online": False,
            "online_state": "offline",
        },
        "items": [],
    }
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(mod, "_build_snapshot", lambda webspace_id=None: snapshot)
    monkeypatch.setattr(mod, "_publish", lambda webspace_id=None: snapshot)
    monkeypatch.setattr(mod, "_set_memory_dict", lambda key, value: writes.append((key, value)))

    result = mod.send_redevice_settings_command(action="open_wifi", webspace_id="desktop")

    assert result["ok"] is False
    assert result["result"]["error"] == "endpoint_offline"
    assert result["result"]["code"] == "LCCX54KP"
    assert "state" not in result
    assert writes[-1][1]["result"]["error"] == "endpoint_offline"


def test_settings_command_requires_selected_endpoint(monkeypatch) -> None:
    mod = _load_redevice_settings_module()
    snapshot = {"selected": {}, "items": []}
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(mod, "_build_snapshot", lambda webspace_id=None: snapshot)
    monkeypatch.setattr(mod, "_publish", lambda webspace_id=None: snapshot)
    monkeypatch.setattr(mod, "_set_memory_dict", lambda key, value: writes.append((key, value)))

    result = mod.send_redevice_settings_command(action="open_wifi", webspace_id="desktop")

    assert result["ok"] is False
    assert result["result"]["error"] == "endpoint_required"
    assert "state" not in result
    assert writes[-1][1]["result"]["error"] == "endpoint_required"


def test_refresh_returns_small_ack_not_stream_snapshot(monkeypatch) -> None:
    mod = _load_redevice_settings_module()
    snapshot = {
        "ok": True,
        "selected_ref": "redevice:endpoint-1",
        "selected": {"ref": "redevice:endpoint-1", "code": "FMRS7WTB", "title": "Kitchen tablet"},
        "items": [{"ref": "redevice:endpoint-1"}],
        "sections": {"about": [{"details": {"large": "payload"}}]},
        "count": 1,
        "updated_at": "2026-06-18T10:00:00+00:00",
    }
    monkeypatch.setattr(mod, "_publish", lambda webspace_id=None: snapshot)

    result = mod.refresh_redevice_settings_state(webspace_id="desktop")

    assert result == {
        "ok": True,
        "status": "refreshed",
        "receiver": "redevice_settings.state",
        "selected_ref": "redevice:endpoint-1",
        "selected_code": "FMRS7WTB",
        "selected_title": "Kitchen tablet",
        "count": 1,
        "updated_at": "2026-06-18T10:00:00+00:00",
    }
