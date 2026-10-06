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
        "RU:Hub:sn_6acf0c01 (Home assistant)",
        "RU:Browser:desktop (Current browser 1)",
        "RU:Browser:desktop-dev (Current browser 1)",
        "LO:Browser:desktop (Local browser)",
    }
    assert sum("RU:Browser:desktop (" in item["title"] for item in children) == 1
    assert {item["icon"] for item in children if item["device_type"] == "browser"} == {"globe-outline"}
    assert all("unassigned" not in item["title"].casefold() for item in projected)


def test_legacy_browser_without_parent_uses_only_online_os_and_detects_hub():
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
    assert not any(item["title"] == "Browser device" for item in projected)
    assert next(item for item in projected if item["device_type"] == "browser")[
        "parent_device_id"
    ] == active_root["id"]
    assert next(item for item in projected if item["id"] == "member:hub-local")[
        "title"
    ] == "RU:Hub:sn_6acf0c01 (Home assistant)"


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
