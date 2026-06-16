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
