from pathlib import Path
import sys
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from handlers import main  # noqa: E402


@pytest.mark.parametrize("browser_id", ["current-browser", "current-browser::page_test"])
def test_preferences_reads_current_browser_label_without_device_inventory(monkeypatch, browser_id):
    access = Mock()
    monkeypatch.setattr(main.sdk_access, "require", access)
    monkeypatch.setattr(main.sdk_profile, "get_profile", Mock(return_value={"preferences": {}}))
    monkeypatch.setattr(main.sdk_control_plane, "get_workspace_object", Mock(return_value={"title": "DEV: desktop"}))
    inventory = Mock(side_effect=AssertionError("Preferences must not enumerate inventory"))
    monkeypatch.setattr(main, "list_devices", inventory)
    monkeypatch.setattr(main.sdk_control_plane, "list_device_objects", inventory)
    monkeypatch.setattr(main.sdk_control_plane, "list_browser_session_objects", inventory)
    monkeypatch.setattr(main.sdk_system, "get_operational_snapshot", inventory)
    monkeypatch.setattr(main.sdk_access_links, "list_browser_links", inventory)
    links = Mock(side_effect=lambda identity: {
        "id": identity,
        "endpoint_display_name": "Chrome on Windows" if identity == "current-browser" else "Old page name",
        "device_display_name": "Physical computer",
    })
    monkeypatch.setattr(main.sdk_access_links, "get_browser_link", links)

    result = main.get_preferences(
        webspace_id="desktop-dev",
        controller_endpoint_id=f"browser:{browser_id}:surface:test:webspace:desktop-dev",
    )

    assert result["ok"] is True
    assert result["item"]["deviceLabel"] == "Chrome on Windows"
    assert result["item"]["workspaceLabel"] == "DEV: desktop"
    assert result["items"] == [result["item"]]
    access.assert_called_once_with("workspace.read")
    inventory.assert_not_called()
    assert links.call_args_list[0].args == (browser_id,)
    assert links.call_count == (2 if "::page_" in browser_id else 1)


@pytest.mark.parametrize("endpoint", [None, "browser:missing:surface:test"])
def test_preferences_keeps_current_browser_fallback_without_inventory(monkeypatch, endpoint):
    monkeypatch.setattr(main.sdk_access, "require", Mock())
    monkeypatch.setattr(main.sdk_profile, "get_profile", Mock(return_value={}))
    inventory = Mock(side_effect=AssertionError("Preferences must not enumerate inventory"))
    monkeypatch.setattr(main, "list_devices", inventory)
    link = Mock(return_value=None)
    monkeypatch.setattr(main.sdk_access_links, "get_browser_link", link)

    result = main.get_preferences(controller_endpoint_id=endpoint)

    assert result["ok"] is True
    assert result["item"]["deviceLabel"] == "Current browser"
    inventory.assert_not_called()
    assert link.call_count == (1 if endpoint else 0)


def _browser(
    ref: str,
    *,
    webspace: str,
    zone: str,
    origin: str,
    name: str,
    current: bool = False,
    last_seen: str = "2026-10-02T20:00:00Z",
) -> dict:
    return {
        "id": ref,
        "title": ref,
        "summary": "browser",
        "kind": "browser_session",
        "status": "online",
        "source": "access_links",
        "last_seen": last_seen,
        "version": "",
        "location": "",
        "current": current,
        "device_type": "browser",
        "connection": "connected",
        "route_mode": "",
        "workspace_ids": [webspace],
        "owner": "",
        "device_display_name": "ZVERZVE-A1BNQF7",
        "endpoint_display_name": name,
        "browser_zone": zone,
        "browser_origin": origin,
        "os_name": "Windows",
        "parent_device_ref": "member:hub-local",
    }


def test_device_tree_projects_physical_os_with_addressable_endpoints():
    node = {
        "id": "member:hub-local",
        "title": "ZVERZVE-A1BNQF7",
        "summary": "hub",
        "kind": "member",
        "status": "online",
        "source": "device",
        "last_seen": "2026-10-02T20:00:00Z",
        "version": "",
        "location": "",
        "current": True,
        "device_type": "member",
        "connection": "connected",
        "route_mode": "hub",
        "workspace_ids": [],
        "owner": "",
        "hostname": "ZVERZVE-A1BNQF7",
        "node_names": ["Homepoint"],
        "node_id": "hub-local",
        "device_ref": "member:hub-local",
    }
    items = [
        node,
        _browser(
            "browser:ru-browser::page_old",
            webspace="desktop",
            zone="ru",
            origin="https://inimatic.com",
            name="Current browser 1",
            last_seen="2026-10-02T19:00:00Z",
        ),
        _browser(
            "browser:ru-browser::page_current",
            webspace="desktop",
            zone="ru",
            origin="https://inimatic.com",
            name="Current browser 1",
            current=True,
        ),
        _browser(
            "browser:ru-browser::page_dev",
            webspace="desktop-dev",
            zone="ru",
            origin="https://inimatic.com",
            name="Current browser 1",
        ),
        _browser(
            "browser:lo-browser::page_local",
            webspace="desktop",
            zone="lo",
            origin="http://127.0.0.1:8100",
            name="Local browser",
        ),
    ]

    projected = main._device_tree_items(
        items,
        subnet={"subnet_id": "sn_6acf0c01", "display_name": "Home assistant"},
    )

    root = next(item for item in projected if item["kind"] == "physical_device")
    assert root["title"] == "ZVERZVE-A1BNQF7"
    assert root["icon"] == "desktop-outline"
    children = [item for item in projected if item.get("parent_device_id") == root["id"]]
    assert {item["title"] for item in children} == {
        "RU:Hub:sn_6acf0c01 (Homepoint)",
        "RU:Browser:desktop (Current browser 1)",
        "RU:Browser:desktop-dev (Current browser 1)",
        "LO:Browser:desktop (Local browser)",
    }
    assert sum("RU:Browser:desktop (" in item["title"] for item in children) == 1
    assert {item["icon"] for item in children if item["device_type"] == "browser"} == {"globe-outline"}
    assert all("unassigned" not in item["title"].casefold() for item in projected)


def test_browser_transport_and_page_endpoints_collapse_to_one_representation():
    transport = _browser(
        "browser:shared-browser::webrtc",
        webspace="desktop",
        zone="ru",
        origin="https://inimatic.com",
        name="Dev browser",
        current=True,
        last_seen="2026-10-13T13:47:02Z",
    )
    page = _browser(
        "browser:shared-browser::page_current",
        webspace="desktop",
        zone="ru",
        origin="https://inimatic.com",
        name="Dev browser",
        last_seen="2026-10-13T13:47:00Z",
    )

    collapsed = main._collapse_browser_representations([transport, page])
    projected = main._device_tree_items(
        [transport, page],
        subnet={"subnet_id": "sn_40443596", "display_name": "sn_40443596"},
    )

    assert [item["id"] for item in collapsed] == ["browser:shared-browser::page_current"]
    assert sum(item["kind"] == "physical_device" for item in projected) == 1
    assert sum(item["device_type"] == "browser" for item in projected) == 1


def test_list_devices_syncs_and_projects_registered_desktop_endpoint(monkeypatch):
    monkeypatch.setattr(main.sdk_access, "require", Mock())
    registered_endpoint = {
        "id": "device:redevice:desktop-1",
        "title": "AdaOS Desktop",
        "summary": "desktop_client | windows",
        "kind": "redevice",
        "runtime": {
            "status": "online",
            "connection_state": "online",
            "route_mode": "root_command_poll",
            "device_kind": "redevice",
        },
        "actual_state": {
            "hostname": "WORKSTATION",
            "device_ref": "redevice:desktop-1",
            "os_name": "windows",
        },
    }
    list_objects = Mock(
        side_effect=lambda **kwargs: [registered_endpoint]
        if kwargs.get("kind") == "redevice"
        else []
    )
    monkeypatch.setattr(main.sdk_control_plane, "list_device_objects", list_objects)
    monkeypatch.setattr(main.sdk_control_plane, "list_browser_session_objects", Mock(return_value=[]))
    monkeypatch.setattr(main.sdk_access_links, "list_browser_links", Mock(return_value=[]))
    monkeypatch.setattr(main.sdk_system, "get_operational_snapshot", Mock(return_value={}))
    endpoint_list = Mock(return_value=[])
    monkeypatch.setattr(main.sdk_device_access, "list_endpoint_devices", endpoint_list)
    monkeypatch.setattr(main, "_REDEVICE_LAST_SYNC", 0.0)

    result = main.list_devices(status="active")

    assert result["ok"] is True
    endpoint = next(
        item
        for item in result["items"]
        if item["id"] == "device:redevice:desktop-1"
    )
    assert endpoint["title"] == "RU:Client:desktop-1 (AdaOS Desktop)"
    assert endpoint["device_type"] == "redevice"
    assert endpoint["parent_device_id"] == "physical-device:workstation"
    endpoint_list.assert_called_once_with("redevice", sync_registry=True)
    assert list_objects.call_args_list[1].kwargs["kind"] == "redevice"


def test_registered_desktop_endpoint_replaces_its_browser_transport(monkeypatch):
    monkeypatch.setattr(main.sdk_access, "require", Mock())
    registered_endpoint = {
        "id": "device:redevice:desktop-1",
        "title": "AdaOS Desktop",
        "kind": "redevice",
        "runtime": {
            "status": "online",
            "connection_state": "online",
            "device_kind": "redevice",
        },
        "actual_state": {
            "hostname": "WORKSTATION",
            "device_ref": "redevice:desktop-1",
        },
    }
    browser_transport = {
        "id": "browser:desktop-1",
        "title": "AdaOS Desktop",
        "kind": "browser_session",
        "runtime": {"status": "online", "connection_state": "connected"},
        "actual_state": {"device_ref": "browser:desktop-1"},
    }
    monkeypatch.setattr(
        main.sdk_control_plane,
        "list_device_objects",
        Mock(
            side_effect=lambda **kwargs: [registered_endpoint]
            if kwargs.get("kind") == "redevice"
            else [browser_transport]
        ),
    )
    monkeypatch.setattr(main.sdk_control_plane, "list_browser_session_objects", Mock(return_value=[]))
    monkeypatch.setattr(main.sdk_access_links, "list_browser_links", Mock(return_value=[]))
    monkeypatch.setattr(main.sdk_system, "get_operational_snapshot", Mock(return_value={}))
    monkeypatch.setattr(main.sdk_device_access, "list_endpoint_devices", Mock(return_value=[]))
    monkeypatch.setattr(main, "_REDEVICE_LAST_SYNC", 0.0)

    result = main.list_devices(status="active")

    endpoints = [item for item in result["items"] if item["kind"] != "physical_device"]
    assert [item["id"] for item in endpoints] == ["device:redevice:desktop-1"]


def test_legacy_browser_without_parent_stays_unknown_and_detects_hub():
    active = {
        "id": "member:hub-local",
        "title": "ZVERZVE-A1BNQF7",
        "status": "online",
        "device_type": "member",
        "hostname": "ZVERZVE-A1BNQF7",
        "node_id": "member:hub-local",
    }
    offline = {
        "id": "member:phone",
        "title": "HUAWEI DRA-LX9",
        "status": "offline",
        "device_type": "member",
        "node_id": "member:phone",
    }
    browser = _browser(
        "browser:legacy::page_current",
        webspace="desktop",
        zone="",
        origin="",
        name="Current browser 1",
    )
    browser.update(
        device_display_name="",
        os_name="",
        parent_device_ref="",
    )

    projected = main._device_tree_items(
        [active, offline, browser],
        subnet={"subnet_id": "sn_6acf0c01", "display_name": "Home assistant"},
        subject={"id": "hub:hub-local"},
    )

    active_root = next(item for item in projected if item["title"] == "ZVERZVE-A1BNQF7")
    assert any(item["title"] == "Browser device" for item in projected)
    assert next(item for item in projected if item["device_type"] == "browser")[
        "parent_device_id"
    ] != active_root["id"]
    assert next(item for item in projected if item["id"] == "member:hub-local")[
        "title"
    ] == "RU:Hub:sn_6acf0c01 (ZVERZVE-A1BNQF7)"


def test_management_owned_rename_targets_durable_browser_endpoint(monkeypatch):
    require = Mock()
    rename = Mock(return_value={"ok": True, "device_ref": "browser:ru-browser"})
    monkeypatch.setattr(main.sdk_access, "require", require)
    monkeypatch.setattr(main.sdk_device_access, "rename_device", rename)

    result = main.rename_device(
        "browser:ru-browser::page_current",
        "Current browser 1",
    )

    require.assert_called_once_with("workspace.write")
    rename.assert_called_once_with("browser:ru-browser", "Current browser 1")
    assert result["ok"] is True


def test_page_representation_inherits_durable_browser_endpoint_name(monkeypatch):
    monkeypatch.setattr(
        main.sdk_access_links,
        "get_browser_link",
        Mock(
            return_value={
                "id": "browser-parent",
                "display_name": "Current browser 1",
                "endpoint_display_name": "Current browser 1",
            }
        ),
    )

    item = main._browser_link_item(
        {
            "id": "browser-parent::page_current",
            "online": True,
            "access_class": "client",
            "last_webspace_id": "desktop",
        }
    )

    assert item["endpoint_display_name"] == "Current browser 1"
    assert item["title"].startswith("Current browser 1")


def test_profile_avatar_contract_accepts_the_declared_form_field(monkeypatch):
    require = Mock()
    digest = "sha256:" + "a" * 64
    receipt = {
        "ref": digest,
        "digest": digest,
        "size_bytes": 8,
        "owner_ref": "skill:web_desktop_runtime_skill",
        "media_type": "image/png",
    }
    monkeypatch.setattr(main.sdk_access, "require", require)
    monkeypatch.setattr(main, "put_upload", Mock(return_value=receipt))

    result = main.upload_profile_avatar(
        "avatar.png",
        "avatar_ref",
        "image/png",
        8,
        digest,
    )

    require.assert_called_once_with("workspace.write")
    assert result == receipt


@pytest.mark.parametrize("device_ref", ["", "physical-device:zverzve-a1bnqf7"])
def test_physical_device_heading_is_not_renamed_as_an_endpoint(monkeypatch, device_ref):
    monkeypatch.setattr(main.sdk_access, "require", Mock())
    rename = Mock()
    monkeypatch.setattr(main.sdk_device_access, "rename_device", rename)
    assert main.rename_device(device_ref, "New name")["ok"] is False
    rename.assert_not_called()


def test_remote_browser_does_not_inherit_hub_host():
    node = {"id": "member:hub", "device_type": "member", "hostname": "ZVERZVE",
            "node_names": ["node1"], "route_mode": "hub", "title": "ZVERZVE"}
    browser = _browser("browser:remote::page", webspace="desktop", zone="ru", origin="https://example.test", name="Remote")
    browser.update(parent_device_ref="", device_display_name="Notebook")
    items = main._device_tree_items([node, browser], subnet={"subnet_id": "subnet", "display_name": "devhub"})
    endpoint = next(i for i in items if i["id"] == browser["id"])
    root = next(i for i in items if i["id"] == endpoint["parent_device_id"])
    assert root["title"] == "Notebook"
    assert next(i for i in items if i["id"] == node["id"])["title"] == "RU:Hub:subnet (node1)"


@pytest.mark.parametrize("checked", [True, False])
def test_participation_values_round_trip_without_consent_defaults(monkeypatch, checked):
    profile = {"preferences": {}}
    monkeypatch.setattr(main.sdk_access, "require", Mock())
    monkeypatch.setattr(main.sdk_profile, "get_profile", lambda: profile)
    monkeypatch.setattr(main.sdk_profile, "update_preferences", lambda patch, **kw: profile["preferences"].update(patch))
    result = main.update_preferences({"participateDevelopment": checked, "sendDiagnostics": checked}, webspace_id="desktop")
    assert result["ok"]
    for _ in range(2):
        item = main.get_preferences(webspace_id="desktop")["item"]
        assert item["participateDevelopment"] is checked
        assert item["sendDiagnostics"] is checked


def test_workspace_name_uses_request_scope_for_read_save_and_refresh(monkeypatch):
    titles = {"desktop": "Desktop", "other-tab": "Other"}
    monkeypatch.setattr(main.sdk_access, "require", Mock())
    monkeypatch.setattr(main.sdk_profile, "get_profile", lambda: {"preferences": {"workspace_label": "default"}})
    monkeypatch.setattr(main.sdk_control_plane, "get_workspace_object", lambda ws: {"title": titles[ws]})
    profile_write = Mock()
    monkeypatch.setattr(main.sdk_profile, "update_preferences", profile_write)
    async def rename(ws, title):
        titles[ws] = title
        return {"id": ws, "title": title}
    monkeypatch.setattr(main, "webspace_rename", rename)
    assert main.get_preferences(webspace_id="desktop")["workspaceLabel"] == "Desktop"
    result = main.update_preferences({"workspaceLabel": "Renamed"}, webspace_id="desktop")
    assert result["ok"]
    assert result["item"]["workspaceLabel"] == "Renamed"
    assert main.get_preferences(webspace_id="desktop")["workspaceLabel"] == "Renamed"
    assert main.get_preferences(webspace_id="other-tab")["workspaceLabel"] == "Other"
    assert not main.update_preferences({"workspaceLabel": "Wrong"})["ok"]
    profile_write.assert_not_called()


def test_participation_write_denied_before_profile_mutation(monkeypatch):
    def deny(permission):
        assert permission == "workspace.write"
        raise PermissionError("workspace.write denied")

    monkeypatch.setattr(main.sdk_access, "require", deny)
    write = Mock()
    monkeypatch.setattr(main.sdk_profile, "update_preferences", write)
    with pytest.raises(PermissionError):
        main.update_preferences({"participateDevelopment": True, "sendDiagnostics": True})
    write.assert_not_called()
