import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml


@pytest.fixture
def updates(monkeypatch):
    access = SimpleNamespace(require=Mock(), invocation=Mock(return_value={
        'request_id': 'core-bound-intent', 'idempotency_key': 'core-replay-key',
    }))
    system = SimpleNamespace(request_core_update=Mock(), set_core_autoupdate=Mock())
    sdk = ModuleType('adaos.sdk')
    sdk.access, sdk.system = access, system
    adaos = ModuleType('adaos')
    adaos.sdk = sdk
    monkeypatch.setitem(sys.modules, 'adaos', adaos)
    monkeypatch.setitem(sys.modules, 'adaos.sdk', sdk)
    path = Path(__file__).resolve().parents[1] / 'handlers' / 'updates.py'
    spec = importlib.util.spec_from_file_location('tested_updates', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, access, system


def test_retry_preserves_core_identity_and_result(updates):
    module, access, system = updates
    result = {'ok': True, 'request_id': 'core-bound-intent', 'target': 'node',
              'accepted': True, 'current': {}, 'desired': {}, 'applied': False}
    system.request_core_update.return_value = result
    assert module.request_core_update() is result
    assert module.request_core_update() is result
    assert all(call.kwargs == {'request_id': 'core-bound-intent'}
               for call in system.request_core_update.call_args_list)
    access.require.assert_called_with('workspace.write')
    access.invocation.return_value = {'request_id': 'next-intent'}
    module.request_core_update()
    system.request_core_update.assert_called_with(request_id='next-intent')


@pytest.mark.parametrize('identity', [None, {}, {'request_id': ''},
    {'request_id': ' '}, {'request_id': 1}, {'request_id': 'x' * 161}])
def test_missing_identity_denies_both_mutations(updates, identity):
    module, access, system = updates
    access.invocation.return_value = identity
    with pytest.raises(ValueError, match='invocation_request_id_missing'):
        module.request_core_update()
    with pytest.raises(ValueError, match='invocation_request_id_missing'):
        module.set_core_autoupdate(True)
    system.request_core_update.assert_not_called()
    system.set_core_autoupdate.assert_not_called()


def test_authorization_precedes_identity_and_mutation(updates):
    module, access, system = updates
    access.require.side_effect = PermissionError('denied')
    for operation in (module.request_core_update, lambda: module.set_core_autoupdate(True)):
        with pytest.raises(PermissionError):
            operation()
    access.invocation.assert_not_called()
    system.request_core_update.assert_not_called()
    system.set_core_autoupdate.assert_not_called()


@pytest.mark.parametrize('enabled', [True, False])
def test_autoupdate_passes_exact_boolean(updates, enabled):
    module, _, system = updates
    assert module.set_core_autoupdate(enabled) is system.set_core_autoupdate.return_value
    system.set_core_autoupdate.assert_called_once_with(
        request_id='core-bound-intent', enabled=enabled)


@pytest.mark.parametrize('enabled', ['false', 0, None])
def test_autoupdate_rejects_coercion(updates, enabled):
    module, _, system = updates
    with pytest.raises(ValueError, match='enabled_must_be_boolean'):
        module.set_core_autoupdate(enabled)
    system.set_core_autoupdate.assert_not_called()


def test_public_mutation_contracts():
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / 'skill.yaml').read_text(encoding='utf-8'))
    for name in ('request_core_update', 'set_core_autoupdate'):
        declaration = next(t for t in manifest['tools'] if t['name'] == name)
        assert name in manifest['exports']['tools']
        assert declaration['entry'] == 'handlers.main:' + name
        assert declaration['side_effects'] == 'runtime_write'
        assert declaration['permissions'] == ['workspace.write']
        schema = declaration['input_schema']
        assert schema['additionalProperties'] is False
        assert 'target_node_id' in schema['required']
        assert not {'request_id', 'idempotency_key', 'url', 'target_rev'} & schema['properties'].keys()


"""Hermetic public-SDK boundary tests; no Core or production data required."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml


@pytest.fixture
def overview():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("overview_operational", path.with_name("operational.py"))
    operational = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(operational)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "get_system_overview")
    function.decorator_list = []
    access = SimpleNamespace(require=Mock())
    read = Mock(return_value={"subject": {"id": "member-42"},
                             "observed_at": "2026-10-02T14:00:00Z"})
    namespace = {
        "Any": object, "sdk_access": access,
        "sdk_system": SimpleNamespace(get_operational_snapshot=read),
        "_string": lambda v: str(v) if v is not None else "",
        "_mapping": lambda v: v if isinstance(v, dict) else {},
        "_canonical_item": lambda v, **kw: {
            "id": v.get("id", ""), "status": "online", "title": "Node",
            "summary": "", "kind": "node"},
        "_project_operational_dashboard": lambda _snapshot, _section: {},
        "_operational_number": operational.number,
        "_activity_content": lambda projection: "No recent user-impacting system activity."
        if not projection.get("items") else "\n".join(item.get("summary", "System activity") for item in projection["items"]),
        "_technical_content": lambda projection: "Technical details are available."
        if projection.get("available") is True else "Technical details are unavailable.",
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["get_system_overview"], access, read


@pytest.mark.parametrize("section,field", [
    ("skills", "skill_summary"), ("development", "development_delivery"),
    ("activity", "activity"), ("technical", "technical"),
])
def test_section_is_bounded_and_preserves_availability(overview, section, field):
    get, access, read = overview
    projection = {"available": False, "freshness": "unavailable", "source": "core"}
    read.return_value[field] = projection
    result = get(section=section, target_node_id="member-42")
    access.require.assert_called_once_with("workspace.read")
    read.assert_called_once_with(sections={"summary", section}, webspace_id=None, limit=40)
    assert result[field] == projection


@pytest.mark.parametrize("total,available,expected", [
    (0, True, 0), (12, True, 12), (12, False, None),
    (None, True, None), (-1, True, None), (True, True, None),
])
def test_installed_count_never_uses_applications_or_defaults(overview, total, available, expected):
    get, _, read = overview
    read.return_value["skill_summary"] = {
        "total": total, "available": available, "freshness": "stale", "source": "core"}
    read.return_value["capacity"] = {"resources": {"active_skill_total": 999}}
    result = get(section="skills")
    assert result["item"]["apps_count"] == expected
    assert result["item"]["freshness"] == "stale"
    assert result["item"]["observed_at"] == "2026-10-02T14:00:00Z"
    assert result["item"]["id"] == "member-42"


@pytest.mark.parametrize("section", [None, "all", "summary", "members"])
def test_technical_read_is_explicit(overview, section):
    get, _, read = overview
    get(section=section)
    assert "technical" not in read.call_args.kwargs["sections"]
    assert "all" not in read.call_args.kwargs["sections"]


def test_denial_precedes_read(overview):
    get, access, read = overview
    access.require.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        get(section="technical")
    read.assert_not_called()


def test_failure_is_redacted_and_matches_required_schema(overview):
    get, _, read = overview
    read.side_effect = RuntimeError("private transport details")
    result = get(section="skills")
    assert result["ok"] is False
    assert "private transport" not in str(result)
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "skill.yaml").read_text(encoding="utf-8"))
    tool = next(t for t in manifest["tools"] if t["name"] == "get_system_overview")
    assert set(tool["output_schema"]["required"]) <= result.keys()
    assert "target_node_id" in tool["input_schema"]["properties"]
    assert {"skills", "development", "activity", "technical"} <= set(tool["input_schema"]["properties"]["section"]["enum"])


@pytest.mark.parametrize("section", ["activity", "technical"])
def test_lazy_record_is_real_and_preserves_freshness(overview, section):
    get, _, read = overview
    projection = {"available": True, "freshness": "stale", "observed_at": "observed"}
    read.return_value[section] = projection
    result = get(section=section)
    assert result["item"]["freshness"] == "stale"
    assert result["item"]["content"] in {
        "No recent user-impacting system activity.",
        "Technical details are available.",
    }
    assert "1.5.0" not in result["item"]["content"]


import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml


@pytest.fixture
def runtime():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name in {"get_runtime_controls", "set_runtime_control"}]
    for node in functions:
        node.decorator_list = []
    access = SimpleNamespace(require=Mock(), invocation=Mock(return_value={"request_id": "trusted"}))
    system = SimpleNamespace(get_runtime_controls=Mock(return_value={"controls": {}, "rasa": {}}),
                             set_runtime_control=Mock(return_value={"ok": False, "accepted": False}))
    ns = {"Any": object, "sdk_access": access, "sdk_system": system,
          "_mapping": lambda v: v if isinstance(v, dict) else {}}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), ns)
    return ns, access, system


def test_read_unknown_is_not_false(runtime):
    ns, access, system = runtime
    result = asyncio.run(ns["get_runtime_controls"]())
    access.require.assert_called_once_with("workspace.read")
    assert result["item"]["rasa_running"] is None
    system.get_runtime_controls.return_value = {"controls": {"log_level": "INFO"},
        "rasa": {"running": False, "availability": "unavailable"}}
    result = asyncio.run(ns["get_runtime_controls"]())
    assert result["item"]["rasa_running"] is False
    assert result["item"]["rasa_availability"] == "unavailable"


@pytest.mark.parametrize("control", ["rasa_install", "rasa_enabled", "log_level",
    "core_auto_update", "application_auto_update_default"])
def test_mutation_preserves_identity_and_pending_result(runtime, control):
    ns, access, system = runtime
    result = asyncio.run(ns["set_runtime_control"](control, False, request_id="forged"))
    assert result is system.set_runtime_control.return_value
    system.set_runtime_control.assert_called_once_with(request_id="trusted", control=control, value=False)
    access.require.assert_called_once_with("workspace.write")


@pytest.mark.parametrize("identity", [None, {}, {"request_id": ""}, {"request_id": "x" * 161}])
def test_missing_identity_never_mutates(runtime, identity):
    ns, access, system = runtime
    access.invocation.return_value = identity
    with pytest.raises(ValueError, match="invocation_request_id_missing"):
        asyncio.run(ns["set_runtime_control"]("rasa_install"))
    system.set_runtime_control.assert_not_called()


@pytest.mark.parametrize("name", ["get_runtime_controls", "set_runtime_control"])
def test_denied_before_sdk(runtime, name):
    ns, access, system = runtime
    access.require.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        asyncio.run(ns[name](**({"control": "rasa_install"} if name.startswith("set") else {})))
    system.get_runtime_controls.assert_not_called()
    system.set_runtime_control.assert_not_called()


def test_runtime_manifest():
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "skill.yaml").read_text(encoding="utf-8"))
    for name in ("get_runtime_controls", "set_runtime_control"):
        tool = next(t for t in manifest["tools"] if t["name"] == name)
        assert name in manifest["exports"]["tools"]
        assert tool["entry"] == "handlers.main:" + name
    assert tool["side_effects"] == "runtime_write"
    assert tool["input_schema"]["additionalProperties"] is False
    assert "adaos.services" not in (root / "handlers" / "main.py").read_text(encoding="utf-8")
