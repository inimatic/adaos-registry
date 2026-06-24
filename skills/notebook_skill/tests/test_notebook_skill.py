from __future__ import annotations

import importlib
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def load_module(monkeypatch):
    mod = importlib.import_module("handlers.main")
    mod = importlib.reload(mod)
    projected = []
    streams = []
    monkeypatch.setattr(mod.ctx_subnet, "set", lambda slot, value, webspace_id=None: projected.append((slot, value, webspace_id)))
    monkeypatch.setattr(mod, "stream_publish", lambda receiver, data=None, _meta=None: streams.append((receiver, data, _meta)))
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

    mod.on_webio_stream_snapshot_requested({"receiver": "notebook_skill.notes", "webspace_id": "desktop"})

    assert streams
    receiver, payload, meta = streams[-1]
    assert receiver == "notebook_skill.notes"
    assert payload["items"][0]["id"] == "note-1"
    assert meta["webspace_id"] == "desktop"


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
    assert projected[-1][1]["editor"]["attachments"][0]["name"] == "photo.jpg"


def test_note_cards_use_first_line_title_and_remaining_preview(monkeypatch):
    mod, _projected, _streams = load_module(monkeypatch)

    created = mod.create_note({"content": "Heading\nBody line one\nBody line two"})
    item = created["snapshot"]["notes"]["items"][0]

    assert item["title"] == "Heading"
    assert item["preview"] == "Body line one\nBody line two"


def test_widget_snapshot_uses_latest_changed_note(monkeypatch):
    mod, _projected, _streams = load_module(monkeypatch)

    first = mod.create_note({"content": "First note"})
    second = mod.create_note({"content": "Second note"})
    mod.save_note({"note_id": first["note"]["id"], "content": "First note\nupdated"})
    snapshot = mod.select_note({"note_id": second["note"]["id"], "edit": True})["snapshot"]

    assert snapshot["editor"]["id"] == second["note"]["id"]
    assert snapshot["widget"]["items"][0]["id"] == first["note"]["id"]
    assert snapshot["widget"]["items"][0]["title"] == "First note"
