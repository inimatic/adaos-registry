from __future__ import annotations

import importlib
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[5]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from adaos.services.skill.validation import validate_webui_file_contract


def load_module(monkeypatch, memory=None, skills_root: Path | None = None, base_dir: Path | None = None):
    sys.modules.pop("handlers.main", None)
    sys.modules.pop("handlers", None)
    mod = importlib.import_module("handlers.main")
    mod = importlib.reload(mod)
    streams = []
    memory = {} if memory is None else memory
    monkeypatch.setattr(mod, "stream_publish", lambda receiver, data=None, _meta=None: streams.append((receiver, data, _meta)))
    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    if skills_root is not None:
        monkeypatch.setattr(mod, "_skills_root", lambda: skills_root)
    runtime_base = base_dir or (skills_root.parent / "base" if skills_root is not None else None)
    if runtime_base is not None:
        fake_ctx = SimpleNamespace(
            settings=SimpleNamespace(
                app_base="https://inimatic.com",
                api_base="https://ru.api.inimatic.com",
                root_token="root-secret",
                subnet_id="sn_test",
                default_hub="sn_test",
            ),
            paths=SimpleNamespace(
                base_dir=lambda: runtime_base,
                skills_workspace_dir=lambda: skills_root or SKILL_ROOT.parent,
            ),
        )
        monkeypatch.setattr(mod, "get_ctx", lambda: fake_ctx)
        monkeypatch.setattr(
            mod,
            "load_config",
            lambda ctx=None: SimpleNamespace(zone_id="ru", subnet_id="sn_test", node_id="node_test"),
        )
    return mod, streams


def test_snapshot_lists_files_and_lazy_tree(monkeypatch, tmp_path):
    root = tmp_path / "left"
    nested = root / "docs"
    nested.mkdir(parents=True)
    (root / "alpha.txt").write_text("hello", encoding="utf-8")
    (nested / "note.md").write_text("# Note", encoding="utf-8")
    mod, streams = load_module(monkeypatch)

    reset = mod.reset_drive({"root": str(root), "webspace_id": "test"})
    assert reset["ok"] is True
    left = reset["snapshot"]["panels"]["left"]
    items = {item["name"]: item for item in left["items"]}
    assert items["alpha.txt"]["extension"] == "txt"
    assert items["alpha.txt"]["size"] == "5 B"
    assert items["alpha.txt"]["modified_label"]
    assert items["docs"]["is_dir"] is True

    expanded = mod.expand_tree({"panel": "left", "path": "", "webspace_id": "test"})
    assert expanded["ok"] is True
    assert {item["name"] for item in expanded["children"]} >= {"alpha.txt", "docs"}
    assert expanded["snapshot"]["panels"]["left"]["tree_view"]["root"]["children"]
    assert streams[-1][0] == "adaos_drive.browser"
    assert streams[-1][2]["webspace_id"] == "test"


def test_path_traversal_is_rejected(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    mod, _streams = load_module(monkeypatch)
    mod.reset_drive({"root": str(root), "webspace_id": "test"})

    with pytest.raises(ValueError):
        mod.open_folder({"panel": "left", "path": "../outside", "webspace_id": "test"})

    with pytest.raises(ValueError):
        mod.make_folder({"panel": "left", "name": "..\\escape", "webspace_id": "test"})


def test_activate_item_opens_folders_and_parent_rows(monkeypatch, tmp_path):
    root = tmp_path / "root"
    nested = root / "scenarios"
    nested.mkdir(parents=True)
    (nested / "scenario.yaml").write_text("name: demo\n", encoding="utf-8")
    mod, _streams = load_module(monkeypatch)
    mod.reset_drive({"root": str(root), "webspace_id": "test"})

    opened = mod.activate_item({"panel": "left", "path": "scenarios", "webspace_id": "test"})
    assert opened["snapshot"]["panels"]["left"]["path"] == "scenarios"

    selected = mod.activate_item({"panel": "left", "path": "scenarios/scenario.yaml", "webspace_id": "test"})
    assert selected["snapshot"]["panels"]["left"]["selected_path"] == "scenarios/scenario.yaml"

    parent = mod.activate_item({"panel": "left", "path": "__parent__", "webspace_id": "test"})
    assert parent["snapshot"]["panels"]["left"]["path"] == ""


def test_sources_are_shared_across_drive_webspaces(monkeypatch, tmp_path):
    left = tmp_path / "left"
    extra = tmp_path / "extra"
    left.mkdir()
    extra.mkdir()
    mod, _streams = load_module(monkeypatch)

    mod.reset_drive({"root": str(left), "webspace_id": "desktop"})
    added = mod.add_source({"label": "Extra", "path": str(extra), "panel": "right", "webspace_id": "desktop"})
    source_id = added["source"]["id"]

    home = mod.get_snapshot({"webspace_id": "Homepoint"})["snapshot"]
    assert source_id in {item["id"] for item in home["sources"]}
    assert {item["webspace_id"] for item in home["source_options"]} == {"Homepoint"}

    selected = mod.select_source({"panel": "right", "source_id": source_id, "webspace_id": "Homepoint"})
    assert selected["snapshot"]["selectors"]["right_source"]["current"] == source_id


def test_select_source_recovers_from_option_payload(monkeypatch, tmp_path):
    root = tmp_path / "root"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    mod, _streams = load_module(monkeypatch)

    mod.reset_drive({"root": str(root), "webspace_id": "Homepoint"})
    source_id = mod._source_payload("External", external)["id"]
    selected = mod.select_source(
        {
            "panel": "right",
            "source_id": source_id,
            "source_label": "External",
            "source_path": str(external),
            "webspace_id": "Homepoint",
        }
    )

    assert selected["source_id"] == source_id
    assert selected["snapshot"]["selectors"]["right_source"]["current"] == source_id
    assert source_id in {item["id"] for item in selected["snapshot"]["sources"]}


def test_copy_rename_upload_preview_and_link(monkeypatch, tmp_path):
    for key in (
        "ADAOS_PUBLIC_APP_URL",
        "ADAOS_PUBLIC_APP_BASE",
        "ADAOS_APP_BASE_URL",
        "PUBLIC_ADAOS_BASE",
    ):
        monkeypatch.delenv(key, raising=False)
    left = tmp_path / "left"
    right = tmp_path / "right"
    skills_root = tmp_path / "skills"
    left.mkdir()
    right.mkdir()
    (left / "alpha.txt").write_text("hello from left", encoding="utf-8")
    upload = tmp_path / "upload.txt"
    upload.write_text("uploaded", encoding="utf-8")
    mod, _streams = load_module(monkeypatch, skills_root=skills_root)
    mod.reset_drive({"root": str(left), "webspace_id": "test"})
    mod.add_source({"label": "Right", "path": str(right), "panel": "right", "webspace_id": "test"})

    mod.select_item({"panel": "left", "path": "alpha.txt", "webspace_id": "test"})
    copied = mod.copy_to_other_panel({"panel": "left", "to_panel": "right", "webspace_id": "test"})
    assert copied["ok"] is True
    assert (right / "alpha.txt").read_text(encoding="utf-8") == "hello from left"

    mod.select_item({"panel": "right", "path": "alpha.txt", "webspace_id": "test"})
    renamed = mod.rename_selected({"panel": "right", "new_name": "renamed.txt", "webspace_id": "test"})
    assert renamed["ok"] is True
    assert (right / "renamed.txt").exists()

    uploaded = mod.upload_to_panel(
        {
            "panel": "right",
            "artifact_ref": {"path": str(upload), "name": "uploaded.txt"},
            "webspace_id": "test",
        }
    )
    assert uploaded["ok"] is True
    assert (right / "uploaded.txt").read_text(encoding="utf-8") == "uploaded"

    mod.select_item({"panel": "left", "path": "alpha.txt", "webspace_id": "test"})
    preview = mod.preview_in_other_panel({"panel": "left", "webspace_id": "test"})
    assert preview["preview"]["mode"] == "text"
    assert "hello from left" in preview["preview"]["content"]

    registrations = []
    monkeypatch.setattr(mod, "_register_root_drive_link", lambda payload: registrations.append(dict(payload)) or {"ok": True, "link": {"public_token": payload["public_token"]}})

    link = mod.create_guest_link({"panel": "left", "webspace_id": "test"})
    assert link["ok"] is True
    assert registrations
    assert registrations[0]["subnet_id"] == "sn_test"
    assert registrations[0]["zone"] == "ru"
    assert registrations[0]["skill"] == "adaos_drive"
    assert "hub_token" in registrations[0]
    assert registrations[0]["resource_kind"] == "file"
    assert registrations[0]["capabilities"] == ["read", "preview", "download"]
    assert link["link"]["download_url"].startswith("https://inimatic.com/?intent=drive.view&zone=ru&public_token=")
    assert link["link"]["view_url"].startswith("https://inimatic.com/?intent=drive.view&zone=ru&public_token=")
    assert "subnet_id=" not in link["link"]["download_url"]
    assert link["link"]["root_download_url"].startswith("https://ru.api.inimatic.com/v1/drive/public-links/")
    assert not (skills_root / ".runtime" / "adaos_drive" / "v0.0" / "data" / "files" / "public_links").exists()

    downloaded = mod.download_selected({"panel": "left", "path": "alpha.txt", "webspace_id": "test"})
    assert downloaded["ok"] is True
    assert downloaded["link"]["open_url"].startswith("https://ru.api.inimatic.com/v1/drive/public-links/")
    assert "download=1" in downloaded["link"]["open_url"]


def test_guest_link_can_share_folder_readonly(monkeypatch, tmp_path):
    root = tmp_path / "root"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "note.md").write_text("# Note\n", encoding="utf-8")
    registrations = []
    mod, _streams = load_module(monkeypatch, skills_root=tmp_path / "skills", base_dir=tmp_path / "base")
    monkeypatch.setattr(
        mod,
        "_register_root_drive_link",
        lambda payload: registrations.append(dict(payload)) or {"ok": True, "link": {"public_token": payload["public_token"]}},
    )

    mod.reset_drive({"root": str(root), "webspace_id": "test"})
    link = mod.create_guest_link({"panel": "left", "path": "docs", "webspace_id": "test"})

    assert link["ok"] is True
    assert link["link"]["resource_kind"] == "folder"
    assert link["link"]["readonly"] is True
    assert link["link"]["capabilities"] == ["read", "list", "preview", "download"]
    assert link["link"]["url"].startswith("https://inimatic.com/?intent=drive.view&zone=ru&public_token=")
    assert link["link"]["list_url"].startswith("https://ru.api.inimatic.com/v1/drive/public-links/")
    assert registrations[0]["resource_kind"] == "folder"
    assert registrations[0]["mime_type"] == "inode/directory"
    assert registrations[0]["download_url"].startswith("https://inimatic.com/?intent=drive.view&zone=ru&public_token=")
    public_links = mod.list_public_links()
    assert public_links["ok"] is True
    assert public_links["links"][0]["resource_kind"] == "folder"


def test_guest_link_public_app_base_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAOS_PUBLIC_APP_URL", "downloads.example.test/root/")
    mod, _streams = load_module(monkeypatch, skills_root=tmp_path / "skills", base_dir=tmp_path / "base")

    url = mod.sdk_navigation.build_url(
        mod.sdk_navigation.drive_download_destination("pub_1234567890abcdef", zone="ru"),
        base_url=mod._public_app_base_url(),
    )

    assert url == "https://downloads.example.test/root?intent=drive.download&zone=ru&public_token=pub_1234567890abcdef"
    assert "subnet_id=" not in url


def test_guest_link_public_view_base_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAOS_PUBLIC_APP_URL", "downloads.example.test/root/")
    mod, _streams = load_module(monkeypatch, skills_root=tmp_path / "skills", base_dir=tmp_path / "base")

    url = mod.sdk_navigation.build_url(
        mod.sdk_navigation.drive_view_destination("pub_1234567890abcdef", zone="ru"),
        base_url=mod._public_app_base_url(),
    )

    assert url == "https://downloads.example.test/root?intent=drive.view&zone=ru&public_token=pub_1234567890abcdef"
    assert "subnet_id=" not in url


def test_guest_link_builder_falls_back_when_sdk_helper_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAOS_PUBLIC_APP_URL", "https://inimatic.com")
    root = tmp_path / "root"
    root.mkdir()
    (root / "alpha.txt").write_text("hello", encoding="utf-8")
    mod, _streams = load_module(monkeypatch, skills_root=tmp_path / "skills", base_dir=tmp_path / "base")
    monkeypatch.delattr(mod.sdk_navigation, "drive_download_destination", raising=False)
    monkeypatch.delattr(mod.sdk_navigation, "drive_view_destination", raising=False)
    monkeypatch.setattr(
        mod,
        "_register_root_drive_link",
        lambda payload: {"ok": True, "link": {"public_token": payload["public_token"]}},
    )

    url = mod._build_drive_app_download_url("pub_1234567890abcdef", "ru")
    mod.reset_drive({"root": str(root), "webspace_id": "test"})
    link = mod.create_guest_link({"panel": "left", "path": "alpha.txt", "webspace_id": "test"})

    assert url == "https://inimatic.com/?intent=drive.download&zone=ru&public_token=pub_1234567890abcdef"
    assert link["ok"] is True
    assert link["link"]["download_url"].startswith("https://inimatic.com/?intent=drive.view&zone=ru&public_token=")
    assert "subnet_id=" not in url
    assert "subnet_id=" not in link["link"]["download_url"]


def test_guest_link_returns_pending_status_when_root_registration_fails(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "alpha.txt").write_text("hello", encoding="utf-8")
    mod, _streams = load_module(monkeypatch, skills_root=tmp_path / "skills", base_dir=tmp_path / "base")
    monkeypatch.setattr(
        mod,
        "_register_root_drive_link",
        lambda payload: {
            "ok": False,
            "status": "pending_root_registration",
            "error": "root_registration_http_error",
            "detail": "unauthorized",
        },
    )

    mod.reset_drive({"root": str(root), "webspace_id": "test"})
    link = mod.create_guest_link({"panel": "left", "path": "alpha.txt", "webspace_id": "test"})

    assert link["ok"] is True
    assert link["link"]["registration_status"] == "pending_root_registration"
    assert link["link"]["registration_error"] == "unauthorized"
    assert link["snapshot"]["last_link"]["registration_status"] == "pending_root_registration"


def test_rehydrate_keeps_newer_in_memory_folder_state(monkeypatch, tmp_path):
    memory = {}
    root = tmp_path / "root"
    (root / "scenarios").mkdir(parents=True)
    (root / "scenarios" / "scenario.yaml").write_text("name: demo\n", encoding="utf-8")
    mod, _streams = load_module(monkeypatch, memory=memory)

    mod.reset_drive({"root": str(root), "webspace_id": "test"})
    stale_state = deepcopy(mod._load_state("test"))
    opened = mod.open_folder({"panel": "left", "path": "scenarios", "webspace_id": "test"})
    assert opened["snapshot"]["panels"]["left"]["path"] == "scenarios"
    stale_state["sequence"] = 0
    memory[mod._state_key("test")] = stale_state

    rehydrated = mod.rehydrate({"webspace_id": "test"})

    assert rehydrated["snapshot"]["panels"]["left"]["path"] == "scenarios"
    assert rehydrated["snapshot"]["_stream_require_revision"] is True
    assert rehydrated["snapshot"]["_stream_rev"] >= opened["snapshot"]["_stream_rev"]


def test_webui_contract_is_valid():
    assert validate_webui_file_contract(SKILL_ROOT, skill_name="adaos_drive") == []
