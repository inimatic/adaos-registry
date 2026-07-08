from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from adaos.services.skill.validation import validate_webui_file_contract


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def load_module(monkeypatch, memory=None):
    mod = importlib.import_module("handlers.main")
    mod = importlib.reload(mod)
    projected = []
    streams = []
    memory = {} if memory is None else memory
    monkeypatch.setattr(
        mod,
        "_project_notebook_snapshot",
        lambda value, webspace_id=None: projected.append(("notebook.snapshot", value, webspace_id)),
    )
    monkeypatch.setattr(mod, "stream_publish", lambda receiver, data=None, _meta=None: streams.append((receiver, data, _meta)))
    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    return mod, projected, streams


def test_create_save_and_select_note_updates_projection_and_stream(monkeypatch):
    mod, projected, streams = load_module(monkeypatch)

    created = mod.create_note({"content": "first", "webspace_id": "desktop"})
    assert created["ok"] is True
    note_id = created["note"]["id"]

    saved = mod.save_note({"note_id": note_id, "content": "second", "webspace_id": "desktop"})
    assert saved["ok"] is True
    assert saved["note"]["content"] == "second"

    selected = mod.select_note({"note_id": "note-1", "edit": True, "webspace_id": "desktop"})
    assert selected["ok"] is True
    assert projected[-1][0] == "notebook.snapshot"
    assert projected[-1][1]["editor"]["id"] == "note-1"
    assert projected[-1][1]["editing_note_id"] == "note-1"
    assert streams[-1][0] == "notebook_skill.notes"
    assert streams[-1][2]["webspace_id"] == "desktop"


def test_delete_selected_note_falls_back_to_remaining_note(monkeypatch):
    mod, _projected, _streams = load_module(monkeypatch)

    created = mod.create_note({"content": "Temp"})
    deleted = mod.delete_note({"note_id": created["note"]["id"]})

    assert deleted["ok"] is True
    snapshot = mod.get_notebook_snapshot()["snapshot"]
    assert snapshot["selected_note_id"] == "note-1"
    assert snapshot["editor"]["content"] == ""


def test_snapshot_request_republishes_note_list(monkeypatch):
    mod, _projected, streams = load_module(monkeypatch)
    mod.save_note({"note_id": "note-1", "content": "Stream title\nStream body", "source": "editor_change"})

    mod.on_webio_stream_snapshot_requested({"receiver": "notebook_skill.notes", "webspace_id": "desktop"})

    assert streams
    receiver, payload, meta = streams[-1]
    assert receiver == "notebook_skill.notes"
    assert payload["_stream_require_revision"] is True
    assert isinstance(payload["_stream_rev"], int)
    assert payload["items"][0]["id"] == "note-1"
    assert payload["editor"]["content"] == "Stream title\nStream body"
    assert payload["widget"]["items"][0]["title"] == "Stream title"
    assert payload["widget"]["items"][0]["text"] == "Stream body"
    assert meta["webspace_id"] == "desktop"


def test_snapshot_request_reloads_durable_state_before_publish(monkeypatch):
    memory = {}
    mod, _projected, streams = load_module(monkeypatch, memory=memory)
    mod.save_note({"note_id": "note-1", "content": "Old title\nOld body", "source": "editor_change", "webspace_id": "desktop"})
    stored = json.loads(json.dumps(memory[mod._memory_key("desktop")]))
    stored["notes"]["note-1"]["content"] = "Fresh title\nFresh body"
    stored["notes"]["note-1"]["updated_at"] += 10
    stored["notes"]["note-1"]["version"] += 1
    memory[mod._memory_key("desktop")] = stored
    mod._STATE["notes"]["note-1"]["content"] = "Old title\nOld body"
    mod._LOADED_WEBSPACES.add("desktop")

    mod.on_webio_stream_snapshot_requested({"receiver": "notebook_skill.notes", "webspace_id": "desktop"})

    assert streams[-1][1]["editor"]["content"] == "Fresh title\nFresh body"
    assert streams[-1][1]["widget"]["items"][0]["title"] == "Fresh title"
    assert streams[-1][1]["widget"]["items"][0]["text"] == "Fresh body"


def test_actions_default_to_desktop_webspace(monkeypatch):
    mod, projected, streams = load_module(monkeypatch)

    result = mod.save_note({"note_id": "note-1", "content": "Desktop note"})

    assert result["ok"] is True
    assert projected[-1][2] == "desktop"
    assert streams[-1][2]["webspace_id"] == "desktop"
    assert projected[-1][1]["widget"]["items"][0]["title"] == "Desktop note"


def test_save_note_ignores_unresolved_content_placeholders(monkeypatch):
    mod, _projected, _streams = load_module(monkeypatch)

    mod.save_note({"note_id": "note-1", "content": "Keep me"})

    result = mod.save_note({"note_id": "note-1", "content": "$state.notebookEditorContent"})

    assert result["ok"] is False
    assert result["error"] == "content_required"
    assert mod.get_notebook_snapshot()["snapshot"]["editor"]["content"] == "Keep me"


def test_save_note_rejects_stale_empty_state_but_allows_editor_clear(monkeypatch):
    mod, _projected, _streams = load_module(monkeypatch)

    mod.save_note({"note_id": "note-1", "content": "Keep me"})

    stale = mod.save_note({"note_id": "note-1", "content": ""})

    assert stale["ok"] is False
    assert stale["error"] == "stale_empty_content"
    assert mod.get_notebook_snapshot()["snapshot"]["editor"]["content"] == "Keep me"

    cleared = mod.save_note({"note_id": "note-1", "content": "", "source": "editor_change"})

    assert cleared["ok"] is True
    assert cleared["note"]["content"] == ""


def test_notebook_state_rehydrates_from_skill_memory(monkeypatch):
    memory = {}
    mod, _projected, _streams = load_module(monkeypatch, memory=memory)

    mod.save_note({"note_id": "note-1", "content": "Persisted title\nPersisted body", "webspace_id": "desktop"})

    mod, projected, streams = load_module(monkeypatch, memory=memory)
    snapshot = mod.get_notebook_snapshot({"webspace_id": "desktop"})["snapshot"]

    assert snapshot["editor"]["content"] == "Persisted title\nPersisted body"
    assert snapshot["notes"]["items"][0]["title"] == "Persisted title"
    assert snapshot["notes"]["items"][0]["preview"] == "Persisted body"
    assert snapshot["widget"]["items"][0]["text"] == "Persisted body"
    assert projected[-1][1]["editor"]["content"] == "Persisted title\nPersisted body"
    assert streams[-1][1]["items"][0]["preview"] == "Persisted body"


def test_notebook_state_persists_to_projected_webspaces(monkeypatch):
    memory = {}
    mod, _projected, _streams = load_module(monkeypatch, memory=memory)

    mod.save_note({"note_id": "note-1", "content": "Shared title\nShared body", "webspace_id": "desktop"})

    for ws in ["desktop-dev", "desktop", "default"]:
        stored = memory[mod._memory_key(ws)]
        assert stored["notes"]["note-1"]["content"] == "Shared title\nShared body"


def test_notebook_state_rehydrates_from_freshest_webspace_alias(monkeypatch):
    memory = {}
    mod, _projected, _streams = load_module(monkeypatch, memory=memory)
    mod.save_note({"note_id": "note-1", "content": "Old alias\nOld body", "webspace_id": "desktop-dev"})
    old_state = json.loads(json.dumps(memory[mod._memory_key("desktop-dev")]))
    fresh_state = json.loads(json.dumps(old_state))
    fresh_state["notes"]["note-1"]["content"] = "Fresh alias\nFresh body"
    fresh_state["notes"]["note-1"]["updated_at"] += 100
    fresh_state["notes"]["note-1"]["version"] += 1
    fresh_state["updated_at"] += 100
    memory[mod._memory_key("desktop-dev")] = old_state
    memory[mod._memory_key("default")] = old_state
    memory[mod._memory_key("desktop")] = fresh_state

    mod, projected, streams = load_module(monkeypatch, memory=memory)
    snapshot = mod.get_notebook_snapshot({"webspace_id": "desktop-dev"})["snapshot"]

    assert snapshot["editor"]["content"] == "Fresh alias\nFresh body"
    assert snapshot["widget"]["items"][0]["title"] == "Fresh alias"
    assert projected[-1][2] == "desktop-dev"
    assert streams[-1][2]["webspace_id"] == "desktop-dev"
    for ws in ["desktop-dev", "desktop", "default"]:
        assert memory[mod._memory_key(ws)]["notes"]["note-1"]["content"] == "Fresh alias\nFresh body"


def test_lifecycle_persist_rehydrates_before_writing_memory(monkeypatch):
    memory = {}
    mod, _projected, _streams = load_module(monkeypatch, memory=memory)
    mod.save_note({"note_id": "note-1", "content": "Durable title\nDurable body", "webspace_id": "desktop"})

    mod, _projected, _streams = load_module(monkeypatch, memory=memory)
    result = mod.notebook_persist_state({"webspace_id": "desktop"})

    assert result["ok"] is True
    for ws in ["desktop-dev", "desktop", "default"]:
        assert memory[mod._memory_key(ws)]["notes"]["note-1"]["content"] == "Durable title\nDurable body"


def test_yjs_reload_completion_reprojects_notebook_snapshot(monkeypatch):
    memory = {}
    mod, projected, _streams = load_module(monkeypatch, memory=memory)
    monkeypatch.setattr(mod, "_RELOAD_REPUBLISH_DELAYS", ())
    mod.save_note({"note_id": "note-1", "content": "Reloaded title\nReloaded body", "webspace_id": "desktop"})
    projected.clear()

    mod.on_yjs_control_completed({"action": "reload", "webspace_id": "desktop-dev", "ok": True, "accepted": True})

    assert projected[-1][2] == "desktop-dev"
    assert projected[-1][1]["widget"]["items"][0]["title"] == "Reloaded title"
    assert projected[-1][1]["widget"]["items"][0]["text"] == "Reloaded body"


def test_notebook_manifest_subscribes_to_yjs_reload_events():
    text = (SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8")

    assert "node.yjs.control.completed" in text


def test_notebook_back_action_does_not_save_empty_state():
    webui = json.loads((SKILL_ROOT / "webui.json").read_text(encoding="utf-8"))
    widgets = webui["registry"]["modals"]["notebook_modal"]["schema"]["widgets"]
    back = next(item for item in widgets if item["id"] == "notebook-back")

    assert all(action.get("target") != "notebook_skill.save_note" for action in back["actions"])


def test_notebook_ui_reads_editor_and_widget_from_stream():
    webui = json.loads((SKILL_ROOT / "webui.json").read_text(encoding="utf-8"))
    ui = webui["interface"]
    desktop_widget = next(item for item in webui["widgets"] if item["id"] == "notebook_skill_last_note")
    modal = webui["registry"]["modals"]["notebook_modal"]
    modal_schema = modal["schema"]
    modal_interface = modal_schema["interface"]
    modal_widgets = modal_schema["widgets"]
    editor = next(item for item in modal_widgets if item["id"] == "notebook-editor")
    attachments = next(item for item in modal_widgets if item["id"] == "notebook-attachments")
    modal_list = next(item for item in modal_widgets if item["id"] == "notebook-notes")

    assert "notebook.latest" in ui["views"]
    assert ui["views"]["notebook.note.edit"]["params"]["note_id"]["required"] is True
    assert "notebook.note.edit" in modal["implements"]
    assert modal_interface["defaultRoute"] == "notes.list"
    assert modal_interface["history"] == {"url": True, "mode": "push"}
    assert modal_interface["routes"]["note.edit"]["state"] == {
        "notebookViewMode": "edit",
        "notebookSelectedNoteId": "$params.note_id",
    }
    assert modal_interface["domain"]["defaultState"] == "notes.list"
    assert modal_interface["domain"]["states"]["note.edit"] == {
        "kind": "entity",
        "route": "note.edit",
        "view": "notebook.note.edit",
        "entity": {
            "type": "note",
            "idParam": "note_id",
            "idStateKey": "notebookSelectedNoteId",
        },
        "state": {
            "notebookViewMode": "edit",
        },
    }
    assert modal_interface["ownership"]["domainState"] == {
        "owner": "skill:notebook_skill",
        "store": "skill_memory",
        "projection": "webio:notebook_skill.notes",
    }
    assert modal_interface["ownership"]["routeState"]["keys"] == [
        "notebookViewMode",
        "notebookSelectedNoteId",
        "notebookEditorContent",
    ]
    assert modal_interface["ownership"]["persistence"]["ack"] == "tool:notebook_skill.save_note"
    assert desktop_widget["dataSource"] == {
        "kind": "stream",
        "receiver": "notebook_skill.notes",
        "path": "widget",
    }
    assert desktop_widget["inputs"]["detailsPresentation"] == "body"
    assert desktop_widget["inputs"]["cardShell"] is True
    assert "hideTitle" not in desktop_widget["inputs"]
    assert desktop_widget["actions"][0]["type"] == "navigate"
    assert desktop_widget["actions"][0]["params"]["to"] == "notebook.note.edit"
    assert desktop_widget["actions"][0]["params"]["params"]["note_id"] == "$event.id"
    assert desktop_widget["actions"][1]["type"] == "callSkill"
    assert modal_list["actions"][0]["type"] == "navigateModal"
    assert modal_list["actions"][0]["params"] == {
        "route": "note.edit",
        "params": {"note_id": "$event.id"},
    }
    assert editor["dataSource"] == {
        "kind": "stream",
        "receiver": "notebook_skill.notes",
        "path": "editor",
    }
    assert attachments["dataSource"] == {
        "kind": "stream",
        "receiver": "notebook_skill.notes",
        "path": "editor",
    }
    assert attachments["inputs"]["collectionKey"] == "attachments"
    assert attachments["inputs"]["detailsPath"] == "url"
    assert [button["id"] for button in attachments["inputs"]["buttons"]] == ["open", "download"]
    assert attachments["actions"][0]["type"] == "openUrl"
    assert attachments["actions"][0]["params"]["url"] == "$event.url"
    assert attachments["actions"][1]["params"]["url"] == "$event.download_url"


def test_notebook_webui_contract_is_valid():
    assert validate_webui_file_contract(SKILL_ROOT, skill_name="notebook_skill") == []


def test_send_note_to_telegram_uses_root_outbox_contract(monkeypatch):
    mod, _projected, _streams = load_module(monkeypatch)
    sent = {}

    class Response:
        status_code = 200
        content = b"{}"
        text = "{}"

        def json(self):
            return {"ok": True}

    def fake_post(url, **kwargs):
        sent["url"] = url
        sent["json"] = kwargs["json"]
        return Response()

    monkeypatch.setenv("TG_CHAT_ID", "42")
    monkeypatch.setattr("requests.post", fake_post)
    mod.save_note({"note_id": "note-1", "content": "Send this"})

    result = mod.send_note_to_telegram({"note_id": "note-1", "root_base": "https://root.example"})

    assert result["ok"] is True
    assert sent["url"] == "https://root.example/io/tg/send"
    assert sent["json"]["messages"][0]["type"] == "text"
    assert sent["json"]["chat_id"] == "42"


def test_attach_note_file_updates_editor_and_widget(monkeypatch):
    mod, projected, _streams = load_module(monkeypatch)
    created = mod.create_note({"content": "with file", "webspace_id": "desktop"})
    note_id = created["note"]["id"]

    result = mod.attach_note_file({
        "note_id": note_id,
        "kind": "photo",
        "artifact_ref": {"filename": "photo.jpg", "path": "/files/photo.jpg", "mime": "image/jpeg"},
        "file": {"name": "photo.jpg", "size_bytes": 123, "mime": "image/jpeg"},
        "webspace_id": "desktop",
    })

    assert result["ok"] is True
    assert result["attachment"]["kind"] == "photo"
    assert result["attachment"]["url"] == "/api/skills/notebook_skill/files/content/uploads/photos/photo.jpg"
    assert result["attachment"]["download_url"] == "/api/skills/notebook_skill/files/content/uploads/photos/photo.jpg?download=1"
    assert projected[-1][1]["editor"]["attachments"][0]["name"] == "photo.jpg"


def test_attach_note_upload_accepts_sanitized_upload_payload(monkeypatch):
    mod, projected, _streams = load_module(monkeypatch)
    created = mod.create_note({"content": "with upload", "webspace_id": "desktop"})
    note_id = created["note"]["id"]

    result = mod.attach_note_upload({
        "note_id": note_id,
        "kind": "photo",
        "upload": {
            "name": "photo.gif",
            "size_bytes": 123,
            "mime": "image/gif",
            "sha256": "a" * 64,
            "purpose": "photos",
        },
        "artifact_ref": {
            "artifact_id": "skill_file:notebook_skill:photos:" + "a" * 16,
            "name": "photo.gif",
            "purpose": "photos",
            "relative_path": "uploads/photos/photo.gif",
            "mime": "image/gif",
        },
        "webspace_id": "desktop",
        "side_effect_class": "local_write",
    })

    assert result["ok"] is True
    assert result["attachment"]["kind"] == "photo"
    assert result["attachment"]["name"] == "photo.gif"
    assert result["attachment"]["artifact_ref"]["artifact_id"] == "skill_file:notebook_skill:photos:aaaaaaaaaaaaaaaa"
    assert result["attachment"]["artifact_ref"]["relative_path"] == "uploads/photos/photo.gif"
    assert result["attachment"]["url"] == "/api/skills/notebook_skill/files/content/uploads/photos/photo.gif"
    assert result["attachment"]["download_url"] == "/api/skills/notebook_skill/files/content/uploads/photos/photo.gif?download=1"
    assert result["attachment"]["summary"] == "image/gif | 123 B"
    assert projected[-1][1]["editor"]["attachments"][0]["mime"] == "image/gif"
    assert projected[-1][1]["editor"]["attachments"][0]["url"] == "/api/skills/notebook_skill/files/content/uploads/photos/photo.gif"


def test_note_cards_use_first_line_title_and_remaining_preview(monkeypatch):
    mod, _projected, _streams = load_module(monkeypatch)

    created = mod.create_note({"content": "Heading\nBody line one\nBody line two"})
    item = created["snapshot"]["notes"]["items"][0]

    assert item["title"] == "Heading"
    assert item["content"] == "Heading\nBody line one\nBody line two"
    assert item["text"] == "Body line one\nBody line two"
    assert item["preview"] == "Body line one\nBody line two"
    assert item["description"] == "Body line one\nBody line two"


def test_widget_snapshot_uses_latest_changed_note(monkeypatch):
    mod, _projected, _streams = load_module(monkeypatch)

    first = mod.create_note({"content": "First note"})
    second = mod.create_note({"content": "Second note"})
    mod.save_note({"note_id": first["note"]["id"], "content": "First note\nupdated"})
    snapshot = mod.select_note({"note_id": second["note"]["id"], "edit": True})["snapshot"]

    assert snapshot["editor"]["id"] == second["note"]["id"]
    assert snapshot["widget"]["items"][0]["id"] == first["note"]["id"]
    assert snapshot["widget"]["items"][0]["title"] == "First note"
    assert snapshot["widget"]["items"][0]["text"] == "updated"
    assert snapshot["widget"]["items"][0]["content"] == "First note\nupdated"
