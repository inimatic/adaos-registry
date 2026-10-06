"""Hermetic checks of operational data and authorization boundaries."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("operational", ROOT / "handlers/operational.py")
operational = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operational)


def test_missing_telemetry_is_not_zero_or_an_example():
    result = operational.project({}, "metrics")
    assert all(m["value"] is None and m["display"] == "Unavailable" for m in result["hardware_metrics"])
    assert result["center"]["value"] == "—"
    assert result["center"]["label"] == "CPU unavailable"


@pytest.mark.parametrize("percent", [True, -1, 101, float("nan"), float("inf"), "37"])
def test_invalid_measurements_are_unavailable(percent):
    result = operational.project({"resources": {"available": True, "cpu": {"percent": percent}}}, "metrics")
    assert result["hardware_metrics"][0]["value"] is None


def test_stale_zero_is_real_and_keeps_observation_time():
    result = operational.project({"resources": {"available": True, "freshness": "stale", "observed_at": "2026-10-02T10:00:00Z", "cpu": {"percent": 0}}}, "metrics")
    assert result["hardware_metrics"][0]["value"] == 0
    assert result["hardware_metrics"][0]["display"] == "0%"
    assert result["center"] == {"value": "0%", "label": "CPU"}
    assert result["subtitle"] == "2026-10-02T10:00:00Z"


def test_byte_capacities_are_humanized_for_dashboard_copy():
    result = operational.project({
        "resources": {
            "available": True,
            "freshness": "live_sample",
            "cpu": {"percent": 37},
            "memory": {"percent": 50, "used_bytes": 8_000_000_000, "total_bytes": 16_000_000_000},
            "disk": {"percent": 60, "used_bytes": 150_000_000_000, "total_bytes": 250_000_000_000},
        }
    }, "metrics")
    assert result["hardware_metrics"][1]["display"] == "50% · 8.0 GB / 16.0 GB"
    assert result["hardware_metrics"][2]["display"] == "60% · 150.0 GB / 250.0 GB"
    assert result["center"] == {"value": "37%", "label": "CPU"}


def test_subnet_and_delivery_require_authoritative_availability():
    assert operational.project({}, "subnet")["subnet_id"] is None
    result = operational.project({"subnet": {"available": True, "subnet_id": "test-subnet", "display_name": "Test", "freshness": "current"}, "development_delivery": {"available": True, "delivered": 0, "accepted": 0, "freshness": "current"}}, "subnet")
    assert result["subnet_id"] == "test-subnet"
    assert "0 delivered, 0 accepted" in result["delivery_status"]


def test_empty_and_missing_application_collections_differ():
    assert operational.project({}, "applications")["value"] == "Unavailable"
    assert operational.project({"applications": []}, "applications")["value"] == 0


def test_update_projection_keeps_release_and_only_supported_transition_controls():
    result = operational.project({
        "update": {
            "state": "countdown",
            "message": "Restart is scheduled",
            "reason": "root.release:0.1.1133",
            "runtime_channel": "stable",
            "runtime_version": "0.1.1132",
        }
    }, "update")
    assert result["subtitle"] == "stable | 0.1.1132"
    assert [button["id"] for button in result["buttons"]] == [
        "refuse_update", "defer_update_5m", "defer_update_15m", "cancel_update",
    ]


def test_development_update_projection_has_no_operator_update_controls():
    result = operational.project({
        "update": {
            "state": "succeeded", "runtime_channel": "dev",
            "runtime_version": "0.1.1132", "development": True,
        }
    }, "update")
    assert result["subtitle"] == "dev | 0.1.1132"
    assert result["development"] is True
    assert result["buttons"] == []


def overview_function(snapshot):
    """Exercise the exported handler without installing the origin runtime."""
    tree = ast.parse((ROOT / "handlers/main.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "get_system_overview")
    fn.decorator_list = []
    access = SimpleNamespace(require=Mock())
    system = SimpleNamespace(get_operational_snapshot=Mock(return_value=snapshot))
    namespace = {
        "Any": object, "sdk_access": access, "sdk_system": system,
        "_string": lambda value: str(value or "").strip(),
        "_mapping": operational.record,
        "_canonical_item": lambda value, **kwargs: {
            "id": "", "status": "unknown", "title": "", "summary": "",
            **operational.record(value),
        },
        "_operational_number": operational.number,
        "_project_operational_dashboard": operational.project,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["get_system_overview"], access, system


def test_overview_missing_counts_are_not_reported_as_zero_or_healthy():
    overview, access, system = overview_function({"ok": True})
    result = overview(section="all")
    access.require.assert_called_once_with("workspace.read")
    assert system.get_operational_snapshot.call_args.kwargs["limit"] == 40
    assert "technical" not in system.get_operational_snapshot.call_args.kwargs["sections"]
    assert result["metrics"]["capacity"]["value"] == "Unavailable"
    assert result["metrics"]["services"]["value"] == "Unavailable"
    assert result["metrics"]["incidents"]["value"] == "--"
    assert result["metrics"]["connections"]["value"] == "--"
    assert result["metrics"]["update"]["color"] == "warning"
    assert result["metrics"]["update"]["description"] == "Update status unavailable"


def test_overview_explicit_empty_counts_remain_zero():
    overview, _, _ = overview_function({
        "ok": True, "services": [], "incidents": [], "connections": [],
        "capacity": {"resources": {"active_skill_total": 0, "active_scenario_total": 0}},
    })
    result = overview(section="all")
    for key in ("capacity", "services", "incidents", "connections"):
        assert result["metrics"][key]["value"] == 0


@pytest.mark.parametrize("percent", [True, -1, 101, float("nan"), float("inf"), "bad"])
def test_overview_rejects_invalid_telemetry_before_dashboard_projection(percent):
    overview, _, _ = overview_function({"resources": {"available": True, "cpu": {"percent": percent}}})
    result = overview(section="metrics")
    assert result["metrics"]["cpu"]["value"] == "--"
    assert result["hardware_metrics"][0]["value"] is None


def test_node_dashboard_exposes_flat_hardware_contract_for_arc_chart():
    overview, _, _ = overview_function({
        "ok": True,
        "subject": {"id": "node-1", "status": "online"},
        "skill_summary": {"available": True, "total": 7},
        "resources": {
            "available": True,
            "freshness": "live_sample",
            "observed_at": "2026-10-04T10:00:00Z",
            "cpu": {"percent": 47.2},
            "memory": {"percent": 76.5},
            "disk": {"percent": 93.7},
        },
        "incidents": [],
    })

    result = overview(section="node_dashboard", target_node_id="node-1")

    assert result["hardware"]["hardware_metrics"] == result["hardware_metrics"]
    assert result["center"] == {"value": "47.2%", "label": "CPU"}
    assert result["subtitle"] == "2026-10-04T10:00:00Z"
    assert [metric["value"] for metric in result["hardware_metrics"]] == [47.2, 76.5, 93.7]


@pytest.mark.parametrize("snapshot", [None, [], {"ok": False, "error": "target_member_unavailable"}])
def test_failed_snapshot_rejects_read_instead_of_replacing_last_known_data(snapshot):
    overview, _, _ = overview_function(snapshot)
    result = overview(section="metrics")
    assert result["ok"] is False
    assert result["error"] == "system_overview_failed"


def test_overview_denial_precedes_snapshot_read():
    overview, access, system = overview_function({})
    access.require.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        overview(section="all")
    system.get_operational_snapshot.assert_not_called()


def test_device_inventory_is_filtered_and_bounded_at_the_sdk_boundary():
    tree = ast.parse((ROOT / "handlers/main.py").read_text(encoding="utf-8"))
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "list_devices"
    )
    calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "list_device_objects"
    ]

    assert len(calls) == 1
    keywords = {item.arg: item.value for item in calls[0].keywords}
    assert {"status", "include_detached", "limit"} <= keywords.keys()
    cap = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name)
                and target.id == "_MAX_DEVICE_INVENTORY_PAGE_SIZE"
                for target in node.targets)
    )
    assert ast.literal_eval(cap) == 500
    expected = {
        "status": 'status_token if status_token in {"active", "offline"} else None',
        "include_detached": 'status_token == "offline"',
        "limit": "min(_MAX_DEVICE_INVENTORY_PAGE_SIZE, max(40, _bounded_limit(limit) * 4))",
    }
    for name, expression in expected.items():
        assert ast.dump(keywords[name]) == ast.dump(ast.parse(expression, mode="eval").body)


def rename_function(access, system):
    tree = ast.parse((ROOT / "handlers/main.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "rename_subnet")
    fn.decorator_list = []
    namespace = {"sdk_access": access, "sdk_system": system, "Any": object}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["rename_subnet"]


def test_subnet_rename_authorizes_and_returns_sdk_receipt():
    access = SimpleNamespace(require=Mock())
    receipt = {"ok": True, "applied": True, "display_name": "New name"}
    system = SimpleNamespace(rename_local_subnet=Mock(return_value=receipt))
    assert rename_function(access, system)(" New name ") is receipt
    access.require.assert_called_once_with("workspace.write")
    system.rename_local_subnet.assert_called_once_with("New name")


def test_subnet_rename_denied_before_io():
    access = SimpleNamespace(require=Mock(side_effect=PermissionError("denied")))
    system = SimpleNamespace(rename_local_subnet=Mock())
    with pytest.raises(PermissionError):
        rename_function(access, system)("Name")
    system.rename_local_subnet.assert_not_called()


@pytest.mark.parametrize("name", ["", "  ", "x" * 121, None])
def test_subnet_rename_rejects_invalid_input(name):
    system = SimpleNamespace(rename_local_subnet=Mock())
    with pytest.raises(ValueError):
        rename_function(SimpleNamespace(require=Mock()), system)(name)
    system.rename_local_subnet.assert_not_called()


def identify_function(access, device_access):
    tree = ast.parse((ROOT / "handlers/main.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "identify_device")
    fn.decorator_list = []
    namespace = {
        "sdk_access": access,
        "sdk_device_access": device_access,
        "_string": lambda value: str(value or "").strip(),
        "Any": object,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["identify_device"]


def test_identify_device_authorizes_and_uses_core_device_access():
    access = SimpleNamespace(require=Mock())
    receipt = {"ok": True, "request_id": "identify-1"}
    device_access = SimpleNamespace(identify_device=Mock(return_value=receipt))

    result = identify_function(access, device_access)(
        " browser:phone ", webspace_id=" desktop "
    )

    assert result is receipt
    access.require.assert_called_once_with("workspace.read")
    device_access.identify_device.assert_called_once_with(
        "browser:phone", webspace_id="desktop"
    )


def test_identify_device_rejects_empty_ref_before_io():
    device_access = SimpleNamespace(identify_device=Mock())
    with pytest.raises(ValueError, match="device_ref_required"):
        identify_function(SimpleNamespace(require=Mock()), device_access)("  ")
    device_access.identify_device.assert_not_called()
