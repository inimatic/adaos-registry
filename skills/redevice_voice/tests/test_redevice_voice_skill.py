from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import uuid4


def _load_redevice_voice_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    module_name = f"test_redevice_voice_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_stream_subscription_publishes_cached_voice_state_without_refresh(monkeypatch) -> None:
    mod = _load_redevice_voice_module()
    published: list[dict] = []
    cached = {
        "desktop": {
            "ok": True,
            "selected_code": "SNX68P2A",
            "count": 1,
            "items": [],
            "vad": {"state": "idle"},
            "updated_at": "2026-06-23T10:00:00+00:00",
        }
    }

    def fail_load_endpoints():
        raise AssertionError("subscription snapshots must not call ReDeviceBridge")

    monkeypatch.setattr(mod, "_last_snapshots", lambda: dict(cached))
    monkeypatch.setattr(mod, "_load_endpoints", fail_load_endpoints)
    monkeypatch.setattr(mod, "stream_publish", lambda receiver, payload, _meta=None: published.append(dict(payload)))

    mod.on_webio_stream_snapshot_requested(
        {"receiver": "redevice_voice.state", "webspace_id": "desktop"}
    )

    assert published == [cached["desktop"]]
