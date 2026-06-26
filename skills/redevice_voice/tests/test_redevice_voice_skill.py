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


def test_payload_includes_compact_audio_readiness(monkeypatch) -> None:
    mod = _load_redevice_voice_module()
    endpoint = {"code": "SNX68P2A", "endpoint_manifest": {"services": {"audio_input_endpoint": {"enabled": True}}}}
    readiness = {
        "schema_version": "endpoint-audio-readiness.v1",
        "ok": True,
        "state": "ready",
        "retention": {"debug_clip_limit": 10, "stored_debug_clips": 1},
        "last_segment": {"state": "ready", "bytes": 1024},
    }

    monkeypatch.setattr(mod, "_compact_endpoint", lambda item, selected_code: {"code": item["code"], "selected": True})
    monkeypatch.setattr(mod, "endpoint_audio_readiness", lambda state, selected_endpoint: dict(readiness))
    monkeypatch.setattr(
        mod,
        "endpoint_audio_session",
        lambda state, selected_endpoint: {"schema_version": "audio-session.v1", "state": "idle"},
    )
    monkeypatch.setattr(
        mod,
        "endpoint_audio_diagnostics",
        lambda state, selected_endpoint: {"schema_version": "endpoint-audio-diagnostics.v1"},
    )

    payload = mod._payload({"selected_code": "SNX68P2A", "events": []}, [endpoint])

    assert payload["readiness"]["schema_version"] == "endpoint-audio-readiness.v1"
    assert payload["readiness"]["state"] == "ready"
    assert payload["session"]["schema_version"] == "audio-session.v1"
    assert "clips" not in payload["readiness"]["retention"]


def test_start_redevice_voice_uses_audio_session_id(monkeypatch) -> None:
    mod = _load_redevice_voice_module()
    endpoint = {
        "code": "SNX68P2A",
        "endpoint_id": "endpoint-1",
        "state": "consumed",
        "endpoint_manifest": {"services": {"audio_input_endpoint": {"enabled": True}}},
    }
    sent: dict[str, object] = {}
    session = {"schema_version": "audio-session.v1", "session_id": "audio:test-session", "state": "active"}

    class FakeBridge:
        def __init__(self, timeout=0):
            self.timeout = timeout

        def send_command(self, code, command):
            sent["code"] = code
            sent["command"] = command
            return {"ok": True, "state": "queued"}

    monkeypatch.setattr(mod, "_load_state", lambda: {"events": []})
    monkeypatch.setattr(mod, "_save_state", lambda state: dict(state))
    monkeypatch.setattr(mod, "_publish", lambda state, endpoints, webspace_id=None: None)
    monkeypatch.setattr(mod, "_load_endpoints", lambda: [endpoint])
    monkeypatch.setattr(mod, "ReDeviceBridge", FakeBridge)

    def fake_create_session(state, selected_endpoint, **kwargs):
        state["session"] = dict(session)
        return dict(session)

    monkeypatch.setattr(mod, "create_endpoint_audio_session", fake_create_session)

    result = mod.start_redevice_voice(code="SNX68P2A", lang="ru", mode="vad")

    assert result["ok"] is True
    assert result["session"]["session_id"] == "audio:test-session"
    assert sent["code"] == "SNX68P2A"
    assert sent["command"]["payload"]["session_id"] == "audio:test-session"
