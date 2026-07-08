from __future__ import annotations

import importlib.util
import json
import sys
import time
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


def test_stream_publish_dedupes_volatile_updated_at(monkeypatch):
    mod = _load_slideshow_module()

    published: list[tuple[str, object]] = []
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data=None, **_kwargs: published.append((receiver, data)) or {"ok": True},
    )

    mod._publish("slideshow_skill.index", {"ok": True, "value": "1", "updated_at": "one"}, "ws-1")
    mod._publish("slideshow_skill.index", {"ok": True, "value": "1", "updated_at": "two"}, "ws-1")

    assert [item[0] for item in published] == ["slideshow_skill.index"]


def test_session_snapshot_defers_media_and_endpoint_lookup(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photo = tmp_path / "photo with spaces.jpg"
    photo.write_bytes(b"jpeg")
    published: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data=None, **_kwargs: published.append((receiver, data)) or {"ok": True},
    )
    monkeypatch.setattr(
        mod,
        "_load_state",
        lambda: {
            "source_dir": str(tmp_path),
            "selected_codes": ["ABC123"],
            "sync": True,
            "mode": "sequential",
            "scope": "all",
            "display_mode": "fit",
            "fullscreen": True,
            "running": False,
            "current_index": 0,
        },
    )
    monkeypatch.setattr(mod, "_files_for_state", lambda *_args, **_kwargs: [photo])
    monkeypatch.setattr(mod, "_favorite_count", lambda _root: 7)
    monkeypatch.setattr(mod, "_is_favorite", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(mod, "_ensure_polling", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mod,
        "_load_devices",
        lambda: (_ for _ in ()).throw(AssertionError("session snapshot must not query endpoints")),
    )
    monkeypatch.setattr(
        mod,
        "_widget_thumbnail",
        lambda _path: (_ for _ in ()).throw(AssertionError("session snapshot must not build thumbnails")),
    )
    monkeypatch.setattr(
        mod,
        "_fullscreen_media_descriptor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("session snapshot must not build fullscreen media")
        ),
    )
    monkeypatch.setattr(
        mod,
        "_schedule_fullscreen_prewarm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("session snapshot must not schedule media prewarm")
        ),
    )

    mod.on_webio_stream_snapshot_requested({"webspace_id": "ws-1", "receiver": "slideshow_skill.session"})

    assert [item[0] for item in published] == ["slideshow_skill.session"]
    payload = published[0][1]
    assert payload["media_deferred"] is True
    assert payload["image"]["reason"] == "snapshot_reconnect"
    assert payload["label"] == "ABC123"
    assert payload["description"] == "1 photos, 7 favorites"


def test_stopped_poll_does_not_publish_session_or_query_files(monkeypatch):
    mod = _load_slideshow_module()

    published: list[str] = []
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
            "running": False,
        },
    )
    monkeypatch.setattr(mod, "_load_devices", lambda: [{"code": "ABC123", "state": "approved", "last_seen_at": 1}])
    monkeypatch.setattr(
        mod,
        "_files_for_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stopped poll must not query slideshow files")
        ),
    )
    monkeypatch.setattr(
        mod,
        "_session_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stopped poll must not build session payload")
        ),
    )
    monkeypatch.setattr(mod, "_endpoint_payload", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(mod, "stream_publish", lambda receiver, data=None, **_kwargs: published.append(receiver))

    mod._poll_once("ws-1")

    assert published == ["slideshow_skill.endpoints"]


def test_start_index_job_reports_running_status_with_previous_count(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    memory: dict[str, object] = {}
    published: list[tuple[str, dict[str, object]]] = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    monkeypatch.setattr(mod.threading, "Thread", FakeThread)
    monkeypatch.setattr(mod, "_index_meta", lambda _root: {"photo_count": 32500})
    monkeypatch.setattr(mod, "_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "_memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(
        mod,
        "_publish",
        lambda receiver, payload, *_args, **_kwargs: published.append((receiver, payload)) or {"ok": True},
    )

    status = mod._start_index_job(tmp_path, webspace_id="ws-1")

    assert status["status"] == "running"
    assert status["display_count"] == 32500
    assert status["value"] == "32 500"
    assert published[-1][0] == "slideshow_skill.index"


def test_index_status_keeps_fresh_running_status_without_local_thread(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    memory = {
        mod._INDEX_STATUS_KEY: {
            "ok": True,
            "status": "running",
            "source_dir": str(tmp_path),
            "visited_files": 123,
            "indexed_count": 10,
            "photo_count": 100,
            "display_count": 100,
            "folder_count": 2,
            "updated_at": mod._now(),
        }
    }
    writes: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "_index_thread", None)
    monkeypatch.setattr(mod, "_index_meta", lambda _root: {})
    monkeypatch.setattr(mod, "_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "_memory_set", lambda _key, value: writes.append(dict(value)))

    status = mod._index_status(tmp_path)

    assert status["status"] == "running"
    assert status["value"] == "100"
    assert writes == []


def test_index_status_marks_stale_running_status_interrupted(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    memory = {
        mod._INDEX_STATUS_KEY: {
            "ok": True,
            "status": "running",
            "source_dir": str(tmp_path),
            "visited_files": 123,
            "indexed_count": 10,
            "photo_count": 100,
            "display_count": 100,
            "folder_count": 2,
            "updated_at": "2000-01-01T00:00:00+00:00",
        }
    }
    writes: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "_index_thread", None)
    monkeypatch.setattr(mod, "_index_meta", lambda _root: {})
    monkeypatch.setattr(mod, "_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "_memory_set", lambda _key, value: writes.append(dict(value)))

    status = mod._index_status(tmp_path)

    assert status["status"] == "interrupted"
    assert writes[-1]["status"] == "interrupted"


def test_refresh_index_does_not_build_preview_surfaces(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    status = {
        "ok": True,
        "status": "running",
        "source_dir": str(tmp_path),
        "photo_count": 32500,
        "display_count": 32500,
        "value": "32 500",
    }
    published: list[str] = []

    monkeypatch.setattr(mod, "_load_state", lambda: {"source_dir": str(tmp_path), "selected_folder": ""})
    monkeypatch.setattr(mod, "_save_state", lambda state: state)
    monkeypatch.setattr(mod, "_start_index_job", lambda _root, webspace_id=None: status)
    monkeypatch.setattr(
        mod,
        "_files_for_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("refresh must not scan files synchronously")),
    )
    monkeypatch.setattr(
        mod,
        "_sync_running_surface",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("refresh must not sync surfaces synchronously")),
    )
    monkeypatch.setattr(
        mod,
        "_preview_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("refresh must not build preview synchronously")),
    )
    monkeypatch.setattr(
        mod,
        "_folders_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("refresh must not build folders synchronously")),
    )
    monkeypatch.setattr(mod, "_publish", lambda receiver, *_args, **_kwargs: published.append(receiver) or {"ok": True})

    result = mod.refresh_slideshow_photo_index(webspace_id="ws-1")

    assert result["status"] == status
    assert result["items"] == []
    assert result["index"]["photo_count"] == 32500
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


def test_endpoint_content_items_skip_unreadable_images(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photos = [tmp_path / "bad.jpg", tmp_path / "good.jpg"]
    for photo in photos:
        photo.write_bytes(b"not really a jpeg")

    failures: list[tuple[str, str]] = []

    def _content_item(path: Path) -> dict[str, object]:
        if path.name == "bad.jpg":
            raise OSError("cannot identify image file")
        return {
            "source_name": path.name,
            "thumbnail_path": str(path),
            "cached": True,
            "thumbnail_bytes": 6,
            "data_uri": "",
        }

    monkeypatch.setattr(mod, "_content_item", _content_item)
    monkeypatch.setattr(mod, "_record_media_failure", lambda path, phase, _exc: failures.append((path.name, phase)))

    items, content_bytes, limited = mod._content_items_for_window(photos)

    assert [item["source_name"] for item in items] == ["good.jpg"]
    assert content_bytes == 6
    assert limited is False
    assert failures == [("bad.jpg", "endpoint_content")]


def test_session_payload_keeps_inline_widget_preview_under_stream_budget(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photo = tmp_path / "large-photo.jpg"
    image = Image.new("RGB", (2400, 1600))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            pixels[x, y] = ((x * 17 + y * 3) % 256, (x * 5 + y * 19) % 256, (x * 11 + y * 7) % 256)
    image.save(photo, "JPEG", quality=94)

    monkeypatch.setattr(mod, "_load_devices", lambda: [{"code": "ABC123", "state": "approved", "display_name": "Tablet"}])
    monkeypatch.setattr(mod, "_favorite_refs", lambda _root: [])
    monkeypatch.setattr(
        mod,
        "_publish_media_file",
        lambda thumb, ref, **_kwargs: {
            "ok": True,
            "url": f"/api/node/media/files/content/{thumb.name}",
            "node_url": f"/api/node/media/files/content/{thumb.name}",
            "browser_path": f"/media/files/content/{thumb.name}",
            "browser_route": "hub_browser_media",
            "filename": thumb.name,
            "content_ref": ref,
        },
    )

    payload = mod._session_payload(
        {
            "source_dir": str(tmp_path),
            "selected_codes": ["ABC123"],
            "sync": True,
            "mode": "sequential",
            "scope": "all",
            "display_mode": "fit",
            "fullscreen": True,
            "running": True,
            "current_index": 0,
        },
        [photo],
    )

    assert len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) < 98_304
    assert "src" not in payload["image"]
    assert payload["image"]["media"]["route"] == "hub_browser_media"
    assert payload["image"]["media"]["path"].startswith("/media/files/content/")
    assert payload["image"]["node_src"].startswith("/api/node/media/files/content/")
    assert payload["image"]["route"] == "hub_browser_media"
    assert payload["label"] == "Tablet"


def test_session_payload_defer_media_does_not_build_thumbnail(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photo = tmp_path / "фото с пробелом.jpg"
    photo.write_bytes(b"jpeg")

    monkeypatch.setattr(mod, "_load_devices", lambda: [{"code": "ABC123", "state": "approved", "display_name": "Tablet"}])
    monkeypatch.setattr(mod, "_favorite_refs", lambda _root: [])
    monkeypatch.setattr(
        mod,
        "_widget_thumbnail",
        lambda _path: (_ for _ in ()).throw(AssertionError("deferred session must not build thumbnails")),
    )

    payload = mod._session_payload(
        {
            "source_dir": str(tmp_path),
            "selected_codes": ["ABC123"],
            "sync": True,
            "mode": "sequential",
            "scope": "all",
            "display_mode": "fit",
            "fullscreen": True,
            "running": True,
            "current_index": 0,
        },
        [photo],
        defer_media=True,
    )

    assert payload["media_deferred"] is True
    assert payload["image"]["reason"] == "index_running"
    assert payload["image"]["content_ref"].startswith("content:sha256:")
    assert payload["title"] == photo.name


def test_session_payload_exposes_ready_fullscreen_and_next_media(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photos = []
    for idx in range(2):
        photo = tmp_path / f"photo-{idx}.jpg"
        Image.new("RGB", (1200, 800), color=(idx * 40, 64, 128)).save(photo, "JPEG")
        photos.append(photo)

    monkeypatch.setattr(mod, "_load_devices", lambda: [{"code": "ABC123", "state": "approved", "display_name": "Tablet"}])
    monkeypatch.setattr(mod, "_favorite_refs", lambda _root: [])
    monkeypatch.setattr(mod, "_widget_thumbnail", lambda path: (path, True))

    published: list[tuple[str, str]] = []

    def _publish(path, ref, **kwargs):
        variant = str(kwargs.get("variant") or "widget")
        published.append((Path(path).name, variant))
        return {
            "ok": True,
            "url": f"/api/node/media/files/content/{Path(path).stem}-{variant}.jpg",
            "node_url": f"/api/node/media/files/content/{Path(path).stem}-{variant}.jpg",
            "browser_path": f"/media/files/content/{Path(path).stem}-{variant}.jpg",
            "browser_route": "hub_browser_media",
            "filename": f"{Path(path).stem}-{variant}.jpg",
            "content_ref": ref,
            "size_bytes": 1234,
        }

    monkeypatch.setattr(mod, "_publish_media_file", _publish)
    monkeypatch.setattr(
        mod,
        "_fullscreen_media_descriptor",
        lambda path, **_kwargs: {
            "route": "hub_browser_media",
            "path": f"/media/files/content/{Path(path).stem}-fullscreen.jpg",
            "filename": f"{Path(path).stem}-fullscreen.jpg",
            "mime": "image/jpeg",
            "content_ref": mod._content_ref(path),
            "size_bytes": 1234,
        },
    )

    payload = mod._session_payload(
        {
            "source_dir": str(tmp_path),
            "selected_codes": ["ABC123"],
            "sync": True,
            "mode": "sequential",
            "scope": "all",
            "display_mode": "fit",
            "fullscreen": True,
            "running": True,
            "current_index": 0,
        },
        photos,
    )

    assert payload["image"]["media"]["path"].endswith("photo-0-widget.jpg")
    assert payload["image"]["fullscreen_media"]["path"].endswith("photo-0-fullscreen.jpg")
    assert payload["image"]["next_media"]["path"].endswith("photo-1-fullscreen.jpg")
    assert [item[1] for item in published] == ["widget"]


def test_session_payload_schedules_fullscreen_prewarm_without_blocking(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photos = []
    for idx in range(2):
        photo = tmp_path / f"photo-{idx}.jpg"
        Image.new("RGB", (1200, 800), color=(idx * 40, 64, 128)).save(photo, "JPEG")
        photos.append(photo)

    monkeypatch.setattr(mod, "_load_devices", lambda: [{"code": "ABC123", "state": "approved", "display_name": "Tablet"}])
    monkeypatch.setattr(mod, "_favorite_refs", lambda _root: [])
    monkeypatch.setattr(mod, "_widget_thumbnail", lambda path: (path, True))
    monkeypatch.setattr(
        mod,
        "_publish_media_file",
        lambda path, ref, **kwargs: {
            "ok": True,
            "browser_path": f"/media/files/content/{Path(path).stem}-{kwargs.get('variant') or 'widget'}.jpg",
            "browser_route": "hub_browser_media",
            "node_url": f"/api/node/media/files/content/{Path(path).stem}.jpg",
            "filename": f"{Path(path).stem}.jpg",
            "content_ref": ref,
        },
    )
    monkeypatch.setattr(mod, "_fullscreen_media_descriptor", lambda path, **_kwargs: {})

    scheduled: list[tuple[str, int]] = []
    monkeypatch.setattr(
        mod,
        "_schedule_fullscreen_prewarm",
        lambda state, files, **kwargs: scheduled.append((str(kwargs.get("webspace_id") or ""), len(files))),
    )

    payload = mod._session_payload(
        {
            "source_dir": str(tmp_path),
            "selected_codes": ["ABC123"],
            "sync": True,
            "mode": "sequential",
            "scope": "all",
            "display_mode": "fit",
            "fullscreen": True,
            "running": True,
            "current_index": 0,
        },
        photos,
        webspace_id="ws-1",
        schedule_prewarm=True,
    )

    assert payload["image"]["media"]["path"].endswith("photo-0-widget.jpg")
    assert payload["image"]["fullscreen_media"] == {}
    assert payload["image"]["next_media"] == {}
    assert scheduled == [("ws-1", 2)]


def test_endpoint_payload_omits_raw_endpoint_details(monkeypatch):
    mod = _load_slideshow_module()

    devices = []
    for idx in range(24):
        devices.append(
            {
                "pair_code": f"CODE{idx:02d}",
                "endpoint_id": f"endpoint-{idx}",
                "state": "approved",
                "display_name": f"Tablet {idx}",
                "last_seen_at": 1,
                "endpoint_policy": {"trust_level": "limited", "large": "x" * 4000},
                "endpoint_manifest": {"services": ["camera"] * 100},
                "service_state": {"logs": ["line"] * 500},
                "diagnostics": {"raw": "y" * 8000},
                "aliases": ["kitchen", "legacy"],
            }
        )

    payload = mod._endpoint_payload(
        devices,
        {
            "selected_codes": ["CODE02"],
            "source_dir": r"C:\photos",
            "sync": True,
            "mode": "sequential",
            "scope": "all",
            "display_mode": "fit",
            "fullscreen": True,
            "running": False,
        },
    )

    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    assert len(raw) < 20_000
    assert payload["items"][0].get("raw") is None
    assert payload["items"][0].get("transport_profile") is None
    assert payload["selected_items"][0]["content"]["code"] == "CODE02"


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


def test_service_tick_reasserts_surface_without_advancing(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photos = [tmp_path / f"photo-{idx}.jpg" for idx in range(8)]
    for photo in photos:
        photo.write_bytes(b"jpeg")

    sent: list[list[str]] = []
    monkeypatch.setattr(mod.time, "time", lambda: 100.0)
    monkeypatch.setattr(mod, "_save_state", lambda state: state)
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
        "last_service_tick_at": 95.0,
        "last_surface_sync_at": 70.0,
        "current_index": 2,
        "mode": "sequential",
        "scope": "all",
    }

    assert mod._apply_service_tick(state, photos, webspace_id="ws-1") is True
    assert state["current_index"] == 2
    assert state["last_surface_sync_reason"] == "periodic_reassert"
    assert sent == [["photo-2.jpg", "photo-3.jpg", "photo-4.jpg", "photo-5.jpg"]]


def test_service_tick_defers_surface_sync_while_index_running(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photos = [tmp_path / f"photo-{idx}.jpg" for idx in range(4)]
    for photo in photos:
        photo.write_bytes(b"jpeg")

    saved: list[dict[str, object]] = []
    monkeypatch.setattr(mod.time, "time", lambda: 100.0)
    monkeypatch.setattr(mod, "_index_busy_for_state", lambda _state: True)
    monkeypatch.setattr(mod, "_save_state", lambda state: saved.append(dict(state)) or state)
    monkeypatch.setattr(
        mod,
        "_send_to_selected",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("service tick must not sync surfaces while indexing")
        ),
    )

    state = {
        "source_dir": str(tmp_path),
        "selected_codes": ["A"],
        "sync": True,
        "running": True,
        "interval_ms": 7000,
        "last_service_tick_at": 90.0,
        "last_surface_sync_at": 70.0,
        "current_index": 2,
        "mode": "sequential",
        "scope": "all",
    }

    assert mod._apply_service_tick(state, photos, webspace_id="ws-1") is False
    assert state["current_index"] == 2
    assert state["last_surface_sync_reason"] == "index_running_deferred"
    assert saved[-1]["last_service_tick_at"] == 100.0


def test_activate_runtime_rehydrates_running_slideshow(monkeypatch):
    mod = _load_slideshow_module()

    started: list[str | None] = []
    polled: list[str | None] = []
    monkeypatch.setattr(
        mod,
        "_load_state",
        lambda: {
            "selected_codes": ["A"],
            "sync": True,
            "running": True,
            "interval_ms": 7000,
            "last_service_tick_at": 0,
            "current_index": 0,
            "mode": "sequential",
            "scope": "all",
        },
    )
    monkeypatch.setattr(mod, "_ensure_polling", lambda webspace_id=None: started.append(webspace_id))
    monkeypatch.setattr(mod, "_poll_once", lambda webspace_id=None: polled.append(webspace_id))

    result = mod.activate_slideshow_runtime(webspace_id="ws-restore")

    assert result["ok"] is True
    assert result["polling"] is True
    assert started == ["ws-restore"]
    assert polled == ["ws-restore"]


def test_select_folder_resends_running_surface(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photos = [tmp_path / f"photo-{idx}.jpg" for idx in range(4)]
    for photo in photos:
        Image.new("RGB", (32, 24), color=(32, 64, 96)).save(photo, "JPEG")

    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        mod,
        "_load_state",
        lambda: {
            "source_dir": str(tmp_path),
            "selected_codes": ["A"],
            "sync": True,
            "running": True,
            "current_index": 5,
            "mode": "sequential",
            "scope": "all",
        },
    )
    monkeypatch.setattr(mod, "_save_state", lambda state: state)
    monkeypatch.setattr(mod, "_files_for_state", lambda *_args, **_kwargs: photos)
    monkeypatch.setattr(
        mod,
        "_send_to_selected",
        lambda state, files, **_kwargs: sent.append((state.get("selected_folder"), [item.name for item in files])) or {"ok": True},
    )
    monkeypatch.setattr(mod, "_preview_payload", lambda state, _limit: {"ok": True, "folder": state.get("selected_folder")})
    monkeypatch.setattr(mod, "_folders_payload", lambda state: {"items": [], "selected_folder": state.get("selected_folder")})
    monkeypatch.setattr(mod, "_index_status", lambda _root: {"state": "ready"})
    monkeypatch.setattr(mod, "_publish", lambda *_args, **_kwargs: {"ok": True})

    result = mod.select_slideshow_folder("Trips", webspace_id="ws-1")

    assert result["folder"] == "Trips"
    assert sent == [("Trips", ["photo-0.jpg", "photo-1.jpg", "photo-2.jpg", "photo-3.jpg"])]


def test_select_endpoint_resends_running_surface(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photos = [tmp_path / f"photo-{idx}.jpg" for idx in range(4)]
    for photo in photos:
        Image.new("RGB", (32, 24), color=(32, 64, 96)).save(photo, "JPEG")

    sent: list[list[str]] = []
    monkeypatch.setattr(
        mod,
        "_load_state",
        lambda: {
            "source_dir": str(tmp_path),
            "selected_codes": ["A"],
            "sync": True,
            "running": True,
            "current_index": 0,
            "mode": "sequential",
            "scope": "all",
        },
    )
    monkeypatch.setattr(mod, "_load_devices", lambda: [{"code": "B", "state": "approved"}])
    monkeypatch.setattr(mod, "_save_state", lambda state: state)
    monkeypatch.setattr(mod, "_files_for_state", lambda *_args, **_kwargs: photos)
    monkeypatch.setattr(
        mod,
        "_send_to_selected",
        lambda state, files, **_kwargs: sent.append(list(state.get("selected_codes") or [])) or {"ok": True},
    )
    monkeypatch.setattr(mod, "_endpoint_payload", lambda _devices, state: {"selected_codes": state.get("selected_codes")})
    monkeypatch.setattr(mod, "_publish", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(mod, "_ensure_polling", lambda *_args, **_kwargs: None)

    result = mod.select_redevice_endpoint("B", webspace_id="ws-1")

    assert result["selected_codes"] == ["B"]
    assert sent == [["B"]]


def test_voice_control_uses_active_app_resolution_when_assignment_missing(monkeypatch):
    mod = _load_slideshow_module()
    calls: list[dict] = []

    def fake_resolve_endpoint_device(**kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("active_app") == "slideshow_skill":
            return {"ok": True, "device_ref": "redevice:endpoint-1", "code": "ABC123"}
        return {"ok": False, "error": "endpoint_not_found"}

    monkeypatch.setattr(mod.device_access, "resolve_endpoint_device", fake_resolve_endpoint_device)
    monkeypatch.setattr(mod.device_access, "assign_endpoint", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(mod, "_load_state", lambda: {"selected_codes": []})
    monkeypatch.setattr(mod, "_save_state", lambda state: dict(state))
    monkeypatch.setattr(
        mod,
        "control_redevice_slideshow",
        lambda action, code=None, webspace_id=None: {"ok": True, "action": action, "code": code},
    )

    result = mod.voice_control_redevice_slideshow(action="next", device_name="Kitchen tablet")

    assert result["ok"] is True
    assert result["code"] == "ABC123"
    assert any(call.get("assignment") == "slideshow" for call in calls)
    assert any(call.get("active_app") == "slideshow_skill" for call in calls)


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
        media_session={
            "schema_version": "endpoint-media-session.v1",
            "primary_transport": "endpoint_media_pull",
            "fallback_transport": "root_relay_inline",
            "inline_fallback": False,
        },
    )
    raw = json.dumps({"command": command}, separators=(",", ":")).encode("utf-8")

    assert len(command["payload"]["items"]) == 1
    assert command["ttl_sec"] == mod._REDEVICE_COMMAND_TTL_S
    assert command["expires_at"] >= int(time.time())
    assert command["payload"]["cache_policy"]["command_ttl_sec"] == mod._REDEVICE_COMMAND_TTL_S
    assert command["payload"]["cache_policy"]["receiver_disk_cache"] is True
    assert command["payload"]["media_session"]["schema_version"] == "endpoint-media-session.v1"
    assert command["payload"]["media_session"]["primary_transport"] == "endpoint_media_pull"
    assert command["payload"]["media_session"]["inline_fallback"] is False
    assert item["cache_key"].startswith("slideshow:v1:")
    assert item["content_hash"]
    assert item["thumbnail_bytes"] < 48_000
    assert len(raw) < 80_000


def test_send_to_selected_skips_offline_endpoint_without_building_payload(monkeypatch, tmp_path):
    mod = _load_slideshow_module()

    photo = tmp_path / "source.jpg"
    photo.write_bytes(b"jpeg")

    sent: list[tuple[str, object]] = []
    monkeypatch.setattr(
        mod,
        "_load_devices",
        lambda: [{"code": "OFFLINE1", "state": "approved", "display_name": "Tablet", "last_seen_at": 1}],
    )
    monkeypatch.setattr(mod, "_save_state", lambda state: state)
    monkeypatch.setattr(
        mod,
        "_content_items_for_window",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("offline endpoint must not build command media")),
    )
    monkeypatch.setattr(mod.device_access, "send_endpoint_command", lambda *args, **kwargs: sent.append((args, kwargs)))
    monkeypatch.setattr(mod, "_session_payload", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(mod, "_publish", lambda *_args, **_kwargs: {"ok": True})

    result = mod._send_to_selected(
        {
            "source_dir": str(tmp_path),
            "selected_codes": ["OFFLINE1"],
            "sync": True,
            "running": True,
            "current_index": 0,
            "mode": "sequential",
            "scope": "all",
        },
        [photo],
    )

    assert result["ok"] is False
    assert result["results"][0]["error"] == "device_offline"
    assert result["results"][0]["state"] == "skipped"
    assert sent == []
