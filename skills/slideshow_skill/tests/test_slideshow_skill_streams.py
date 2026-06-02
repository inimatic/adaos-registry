from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

_REPO_SRC = Path(__file__).resolve().parents[5] / "src"
if _REPO_SRC.exists():
    sys.path.insert(0, str(_REPO_SRC))

from PIL import Image


def _load_slideshow_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    module_name = f"test_slideshow_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_snapshot_request_publishes_only_requested_receiver(monkeypatch):
    mod = _load_slideshow_module()

    published: list[tuple[str, object, dict[str, object] | None]] = []
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data=None, **kwargs: published.append((receiver, data, kwargs.get("_meta"))) or {"ok": True},
    )
    monkeypatch.setattr(
        mod,
        "_load_state",
        lambda: {
            "source_dir": r"C:\photos",
            "selected_codes": ["ABC123"],
            "sync": True,
            "mode": "sequential",
            "scope": "all",
            "display_mode": "fit",
            "fullscreen": True,
            "running": False,
        },
    )
    monkeypatch.setattr(
        mod,
        "_load_devices",
        lambda: [{"code": "ABC123", "state": "approved", "last_seen_at": 1}],
    )
    monkeypatch.setattr(
        mod,
        "_files_for_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("endpoint snapshot must not build slideshow files")
        ),
    )

    mod.on_webio_stream_snapshot_requested(
        {"webspace_id": "ws-1", "receiver": "slideshow_skill.endpoints"},
    )

    assert [item[0] for item in published] == ["slideshow_skill.endpoints"]
    assert published[0][2] == {"webspace_id": "ws-1"}


def test_subscription_changed_does_not_build_snapshot(monkeypatch):
    mod = _load_slideshow_module()

    published: list[tuple[str, object]] = []
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data=None, **_kwargs: published.append((receiver, data)) or {"ok": True},
    )
    monkeypatch.setattr(
        mod,
        "_publish_receiver_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("subscription changes must not publish stream snapshots")
        ),
    )

    mod.on_webio_stream_subscription_changed(
        {"webspace_id": "ws-1", "receiver": "slideshow_skill.session", "action": "subscribed"},
    )

    assert published == []


def test_subscription_changed_starts_poller_for_running_slideshow(monkeypatch):
    mod = _load_slideshow_module()

    started: list[str | None] = []
    monkeypatch.setattr(
        mod,
        "_load_state",
        lambda: {
            "source_dir": r"C:\photos",
            "selected_codes": ["ABC123"],
            "sync": True,
            "mode": "sequential",
            "scope": "all",
            "display_mode": "fit",
            "fullscreen": False,
            "running": True,
        },
    )
    monkeypatch.setattr(mod, "_ensure_polling", lambda webspace_id=None: started.append(webspace_id))

    mod.on_webio_stream_subscription_changed(
        {"webspace_id": "ws-1", "receiver": "slideshow_skill.session", "action": "subscribed"},
    )

    assert started == ["ws-1"]


def test_duplicate_snapshot_requests_are_coalesced(monkeypatch):
    mod = _load_slideshow_module()

    published: list[str] = []
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data=None, **_kwargs: published.append(receiver) or {"ok": True},
    )
    monkeypatch.setattr(
        mod,
        "_load_state",
        lambda: {
            "source_dir": r"C:\photos",
            "selected_codes": [],
            "sync": True,
            "mode": "sequential",
            "scope": "all",
            "display_mode": "fit",
            "fullscreen": True,
            "running": False,
        },
    )
    monkeypatch.setattr(mod, "_index_status", lambda *_args, **_kwargs: {"ok": True, "value": "1"})

    event = {"webspace_id": "ws-1", "receiver": "slideshow_skill.index"}
    mod.on_webio_stream_snapshot_requested(event)
    mod.on_webio_stream_snapshot_requested(event)

    assert published == ["slideshow_skill.index"]


def test_endpoint_window_prefetches_from_current_frame(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photos = [tmp_path / f"photo-{idx}.jpg" for idx in range(12)]
    for photo in photos:
        photo.write_bytes(b"jpeg")

    monkeypatch.setattr(mod, "_favorite_files_for_state", lambda *_args, **_kwargs: photos)

    state = {"current_index": 1}

    assert mod._endpoint_window(photos, state) == photos[1:5]


def test_endpoint_content_items_stop_at_inline_budget(monkeypatch, tmp_path):
    mod = _load_slideshow_module()
    monkeypatch.setattr(mod, "_INLINE_CONTENT_BUDGET_BYTES", 10)
    monkeypatch.setattr(
        mod,
        "_content_item",
        lambda path: {
            "source_name": path.name,
            "thumbnail_path": str(path),
            "cached": True,
            "thumbnail_bytes": 6,
            "data_uri": "",
        },
    )

    photos = [tmp_path / f"photo-{idx}.jpg" for idx in range(3)]
    items, content_bytes, limited = mod._content_items_for_window(photos)

    assert len(items) == 1
    assert content_bytes == 6
    assert limited is True


def test_endpoint_next_refreshes_independent_endpoint_window(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photos = [tmp_path / f"photo-{idx}.jpg" for idx in range(8)]
    for photo in photos:
        photo.write_bytes(b"jpeg")

    sent_codes: list[str | None] = []
    monkeypatch.setattr(mod, "_save_state", lambda state: state)
    monkeypatch.setattr(mod, "_files_for_state", lambda *_args, **_kwargs: photos)
    monkeypatch.setattr(
        mod,
        "_send_to_selected",
        lambda state, files, **kwargs: sent_codes.append(kwargs.get("code")) or {"ok": True},
    )

    state = {
        "selected_codes": ["A", "B"],
        "sync": False,
        "running": True,
        "current_index": 0,
        "endpoint_index_by_code": {},
        "last_event_by_code": {},
    }
    devices = [
        {
            "code": "B",
            "last_event": {
                "type": "endpoint.surface.event",
                "observed_at": 10,
                "action": "next",
                "item_ref": "content:1",
            },
        }
    ]

    updated = mod._apply_root_events(state, devices, photos, webspace_id="ws-1", broadcast=True)

    assert updated["selected_codes"] == ["A", "B"]
    assert updated["endpoint_index_by_code"] == {"B": 1}
    assert sent_codes == ["B"]


def test_endpoint_hide_item_marks_photo_hidden(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    hidden_refs: list[str] = []
    monkeypatch.setattr(mod, "_save_state", lambda state: state)
    monkeypatch.setattr(mod, "_files_for_state", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(mod, "_set_hidden", lambda _root, ref, hidden: hidden_refs.append(ref) if hidden else None)
    monkeypatch.setattr(mod, "_send_to_selected", lambda *_args, **_kwargs: {"ok": True})

    state = {
        "source_dir": str(tmp_path),
        "selected_codes": ["A"],
        "sync": True,
        "running": True,
        "current_index": 0,
        "favorites": ["content:hide-me"],
        "last_event_by_code": {},
    }
    devices = [
        {
            "code": "A",
            "last_event": {
                "type": "endpoint.surface.event",
                "observed_at": 11,
                "action": "hide_item",
                "item_ref": "content:hide-me",
            },
        }
    ]

    updated = mod._apply_root_events(state, devices, [], webspace_id="ws-1", broadcast=True)

    assert hidden_refs == ["content:hide-me"]
    assert updated["favorites"] == []


def test_service_tick_advances_running_slideshow_without_modal(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photos = [tmp_path / f"photo-{idx}.jpg" for idx in range(8)]
    for photo in photos:
        photo.write_bytes(b"jpeg")

    saved: list[dict[str, object]] = []
    sent: list[list[str]] = []
    monkeypatch.setattr(mod.time, "time", lambda: 100.0)
    monkeypatch.setattr(mod, "_save_state", lambda state: saved.append(dict(state)) or state)
    monkeypatch.setattr(
        mod,
        "_send_to_selected",
        lambda state, files, **_kwargs: sent.append([item.name for item in mod._endpoint_window(files, state)]) or {"ok": True},
    )

    state = {
        "selected_codes": ["A"],
        "sync": True,
        "running": True,
        "interval_ms": 7000,
        "last_service_tick_at": 90.0,
        "current_index": 0,
        "mode": "sequential",
        "scope": "all",
    }

    assert mod._apply_service_tick(state, photos, webspace_id="ws-1") is True
    assert state["current_index"] == 1
    assert state["last_service_tick_at"] == 100.0
    assert sent == [["photo-1.jpg", "photo-2.jpg", "photo-3.jpg", "photo-4.jpg"]]
    assert saved[-1]["current_index"] == 1


def test_endpoint_command_payload_stays_below_redevice_body_budget(tmp_path):
    mod = _load_slideshow_module()

    photo = tmp_path / "source.jpg"
    Image.effect_noise((1600, 1000), 100).convert("RGB").save(photo, "JPEG", quality=92)
    item = mod._content_item(photo)
    command = mod._build_command(
        "ABC123",
        [item],
        {
            "fullscreen": True,
            "display_mode": "fit",
            "interval_ms": 7000,
            "sync": True,
            "mode": "sequential",
            "scope": "all",
        },
        autoplay=True,
    )
    raw = json.dumps({"command": command}, separators=(",", ":")).encode("utf-8")

    assert len(command["payload"]["items"]) == 1
    assert item["thumbnail_bytes"] < 48_000
    assert len(raw) < 80_000
