from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from uuid import uuid4

import yaml


try:
    importlib.import_module("y_py")
except ModuleNotFoundError:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
try:
    importlib.import_module("ypy_websocket.ystore")
except ModuleNotFoundError:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
    if "ypy_websocket" not in sys.modules:
        pkg = types.ModuleType("ypy_websocket")
        pkg.ystore = sys.modules["ypy_websocket.ystore"]
        sys.modules["ypy_websocket"] = pkg


def _load_voice_chat_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    module_name = f"test_voice_chat_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_voice_chat_get_snapshot_uses_projected_state_without_yjs(monkeypatch):
    mod = _load_voice_chat_module()
    assert not hasattr(mod, "get_ydoc")

    projected: list[tuple[str, str | None, object]] = []
    streamed: list[tuple[str, object, dict[str, object]]] = []
    monkeypatch.setattr(
        mod.ctx_subnet,
        "set",
        lambda slot, value, *, webspace_id=None: projected.append((slot, webspace_id, value)),
    )
    monkeypatch.setattr(
        mod._STREAM_RUNTIME,
        "publish_snapshot",
        lambda receiver, data, **kwargs: streamed.append((receiver, data, kwargs)),
    )

    state = mod._state_for("desktop", "member-01")
    state["messages"] = [
        {"id": "m-1", "from": "user", "text": "weather in Berlin"},
    ]
    state["last_refresh_ts"] = 123.0

    snapshot = mod.get_snapshot(webspace_id="desktop", target_node_id="member-01")

    assert snapshot["voice_chat"]["messages"][0]["text"] == "weather in Berlin"
    assert snapshot["messages"][0]["text"] == "weather in Berlin"
    assert snapshot["last_refresh_ts"] == 123.0
    assert projected and projected[0][0] == "voice_chat.state"
    assert streamed and streamed[0][0] == "voice_chat.messages"
    assert streamed[0][2]["webspace_id"] == "desktop"
    assert streamed[0][2]["force"] is True


def test_voice_chat_messages_survive_new_tool_invocation(monkeypatch):
    mod = _load_voice_chat_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(mod, "_project_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "_publish_tail_stream", lambda *_args, **_kwargs: None)

    mod._append_projected_message("desktop", None, from_="user", text="weather in Berlin")
    mod._STATE_BY_KEY.clear()

    snapshot = mod.get_snapshot(webspace_id="desktop")

    assert snapshot["messages"][0]["from"] == "user"
    assert snapshot["messages"][0]["text"] == "weather in Berlin"


def test_voice_chat_local_time_command_replies(monkeypatch):
    mod = _load_voice_chat_module()
    replies: list[str] = []
    spoken: list[str] = []

    monkeypatch.setattr(mod, "_append_reply", lambda text, **_kwargs: replies.append(text))
    monkeypatch.setattr(mod, "_speak_reply", lambda text, _meta: spoken.append(text))

    result = mod._try_handle_local_command(
        "\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u0432\u0440\u0435\u043c\u0435\u043d\u0438",
        webspace_id="desktop",
        target_node_id=None,
        meta={"route_id": "voice_chat"},
    )

    assert result and result["ok"] is True
    assert result["intent"] == "voice.time.now"
    assert replies and replies[0].startswith("\u0421\u0435\u0439\u0447\u0430\u0441 ")
    assert spoken == replies


def test_voice_chat_timer_command_schedules_completion(monkeypatch):
    mod = _load_voice_chat_module()
    replies: list[str] = []
    timers: list[tuple[int, object]] = []

    class _Timer:
        daemon = False

        def __init__(self, seconds, callback):
            timers.append((seconds, callback))
            self.seconds = seconds
            self.callback = callback

        def start(self):
            return None

    monkeypatch.setattr(mod, "_append_reply", lambda text, **_kwargs: replies.append(text))
    monkeypatch.setattr(mod, "_speak_reply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod.threading, "Timer", _Timer)

    result = mod._try_handle_local_command(
        "\u043f\u043e\u0441\u0442\u0430\u0432\u044c \u0442\u0430\u0439\u043c\u0435\u0440 \u043d\u0430 10 \u043c\u0438\u043d\u0443\u0442",
        webspace_id="desktop",
        target_node_id="member-1",
        meta={"route_id": "voice_chat"},
    )

    assert result and result["ok"] is True
    assert result["intent"] == "voice.timer.start"
    assert result["duration_seconds"] == 600
    assert timers and timers[0][0] == 600
    assert "\u0437\u0430\u043f\u0443\u0449\u0435\u043d" in replies[0]


def test_voice_chat_marketplace_command_opens_modal_once(monkeypatch):
    mod = _load_voice_chat_module()
    replies: list[str] = []
    opened: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "_append_reply", lambda text, **_kwargs: replies.append(text))
    monkeypatch.setattr(mod, "_speak_reply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mod,
        "_publish_modal_open",
        lambda **kwargs: opened.append(dict(kwargs)),
    )

    result = mod._try_handle_local_command(
        "\u043e\u0442\u043a\u0440\u043e\u0439 \u043c\u0430\u0440\u043a\u0435\u0442\u043f\u043b\u0435\u0439\u0441",
        webspace_id="desktop",
        target_node_id=None,
        meta={"route_id": "voice_chat"},
    )

    assert result and result["ok"] is True
    assert result["intent"] == "desktop.open_marketplace"
    assert opened[0]["modal_id"] == "marketplace_modal"
    assert opened[0]["suppress_voice_ack"] is True
    assert replies == ["\u041e\u0442\u043a\u0440\u044b\u0432\u0430\u044e Marketplace."]


def test_voice_chat_modal_open_event_acknowledges_voice_marketplace(monkeypatch):
    mod = _load_voice_chat_module()
    replies: list[tuple[str, str, str | None]] = []

    monkeypatch.setattr(
        mod,
        "_append_reply",
        lambda text, *, webspace_id, target_node_id: replies.append((text, webspace_id, target_node_id)),
    )
    monkeypatch.setattr(mod, "_speak_reply", lambda *_args, **_kwargs: None)

    mod.on_desktop_modal_open(
        {
            "modal_id": "marketplace_modal",
            "webspace_id": "desktop",
            "_meta": {"route_id": "voice_chat", "target_node_id": "member-1"},
        }
    )

    assert replies == [("\u041e\u0442\u043a\u0440\u044b\u0432\u0430\u044e Marketplace.", "desktop", "member-1")]


def test_voice_chat_skill_yaml_exports_get_snapshot():
    manifest = (
        Path(__file__).resolve().parents[1]
        / "skill.yaml"
    )
    payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))

    tools = payload.get("tools") or []
    assert any((item or {}).get("name") == "get_snapshot" for item in tools)
