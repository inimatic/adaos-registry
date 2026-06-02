from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import uuid4


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
