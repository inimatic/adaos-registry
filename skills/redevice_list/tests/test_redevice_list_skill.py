from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4


def _load_redevice_list_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    module_name = f"test_redevice_list_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_compact_device_surfaces_used_and_served_versions() -> None:
    mod = _load_redevice_list_module()

    item = mod._compact_device(
        {
            "code": "pair-1",
            "endpoint_id": "endpoint-1",
            "display_name": "Kitchen panel",
            "state": "approved",
            "last_seen_at": 0,
            "endpoint_manifest": {
                "schema_version": "endpoint-manifest.v1",
                "endpoint_id": "endpoint-1",
                "agent_version": "0.1.1",
                "agent_version_code": 2,
            },
            "endpoint_policy": {"redevice_agent": {"version": "0.1.2", "version_code": 3}},
        }
    )

    assert item["software_version"] == "0.1.1"
    assert item["served_version"] == "0.1.2"
    assert item["version_status"] == "drift"
    assert item["content"]["version_info"]["software_version_code"] == "2"
    assert item["content"]["version_info"]["served_version_code"] == "3"


def test_webui_redevice_table_has_version_columns() -> None:
    webui = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    widgets = webui["registry"]["modals"]["redevice_list_modal"]["schema"]["widgets"]
    table = next(widget for widget in widgets if widget.get("id") == "redevice-table")
    columns = table["inputs"]["columns"]

    assert any(column.get("key") == "software_version" and column.get("label") == "Used" for column in columns)
    assert any(column.get("key") == "served_version" and column.get("label") == "Served" for column in columns)
    assert any(column.get("key") == "version_status" and column.get("label") == "Version" for column in columns)
