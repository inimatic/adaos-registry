from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from uuid import uuid4


if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg


def _load_weather_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    module_name = f"test_weather_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _patch_memory(monkeypatch, mod, memory: dict[str, object]) -> None:
    def _get(key, default=None):
        return memory.get(key, default)

    def _set(key, value):
        memory[key] = value

    async def _async_get(key, default=None):
        return _get(key, default)

    async def _async_set(key, value):
        _set(key, value)

    monkeypatch.setattr(mod, "memory_get", _get)
    monkeypatch.setattr(mod, "memory_set", _set)
    monkeypatch.setattr(mod, "memory_async_get", _async_get)
    monkeypatch.setattr(mod, "memory_async_set", _async_set)


def test_weather_webui_uses_targeted_projection_observers():
    webui_path = Path(__file__).resolve().parents[1] / "webui.json"
    contract = json.loads(webui_path.read_text(encoding="utf-8"))
    data_sources: list[dict] = []

    def _collect(value):
        if isinstance(value, dict):
            source = value.get("dataSource")
            if isinstance(source, dict) and str(source.get("path", "")).startswith("data/weather/"):
                data_sources.append(source)
            for child in value.values():
                _collect(child)
        elif isinstance(value, list):
            for child in value:
                _collect(child)

    _collect(contract)

    assert data_sources
    assert all(source.get("observeRoot") is not True for source in data_sources)
    modal_widgets = contract["registry"]["modals"]["weather_modal"]["schema"]["widgets"]
    selector = next(widget for widget in modal_widgets if widget["id"] == "weather-city-selector")
    action = selector["actions"][0]
    assert action["on"] == "change"
    assert action["params"]["payload"]["city"] == "$event.value"
    assert action["feedback"]["observe"]["match"]["request_id"] == "$client.requestId"
    assert action["feedback"]["observe"]["timeout_ms"] == 20000


def test_weather_city_changed_projects_without_blocking_sync_ctx_set(monkeypatch):
    mod = _load_weather_module()
    projected: list[tuple[str, dict, str | None]] = []
    memory: dict[str, object] = {}

    class _CtxSubnet:
        def set(self, *_args, **_kwargs):
            raise AssertionError("async weather handler must not call sync ctx_subnet.set")

        async def set_async(self, slot, payload, webspace_id=None):
            projected.append((slot, payload, webspace_id))

    async def _fetch_weather_async(*_args, **_kwargs):
        return True, {"temp": 10, "description": "clear", "wind_ms": 1}

    monkeypatch.setattr(mod, "get_self_object", lambda: {"id": "member:member-local"})
    monkeypatch.setattr(mod, "_load_config", lambda: ("https://example.test", None))
    monkeypatch.setattr(mod, "_fetch_weather_async", _fetch_weather_async)
    monkeypatch.setattr(mod, "ctx_subnet", _CtxSubnet())
    _patch_memory(monkeypatch, mod, memory)

    import asyncio

    async def _run():
        await mod.on_weather_city_changed(
            {
                "city": "Berlin",
                "webspace_id": "desktop",
                "target_node_id": "member-local",
                "_meta": {"target_node_id": "member-local"},
            }
        )
        assert mod._WEATHER_UPDATE_TASKS == {}

    asyncio.run(_run())

    assert [entry[0] for entry in projected] == ["weather.snapshot"]
    assert [entry[1].get("status") for entry in projected] == ["ok"]
    assert {entry[2] for entry in projected} == {"desktop"}
    assert projected[0][1]["current"]["city"] == "Berlin"
    assert projected[0][1]["current"]["source"] == "api"
    assert projected[0][1]["current"]["pending"] is False


def test_weather_location_requested_projects_browser_coordinates(monkeypatch):
    mod = _load_weather_module()
    projected: list[tuple[str, dict, str | None]] = []
    memory: dict[str, object] = {}

    class _CtxSubnet:
        async def set_async(self, slot, payload, webspace_id=None):
            projected.append((slot, payload, webspace_id))

    async def _fetch_weather_async(_api, _city=None, location=None):
        return True, {
            "city": "Current location",
            "temp": 21,
            "description": "Clear",
            "wind_ms": 2,
            "current": {
                "city": "Current location",
                "temp_c": 21,
                "condition": "Clear",
                "wind_ms": 2,
                "location": location,
                "updated_at": "after",
                "source": "api",
            },
            "hourly_chart": {"points": [{"x": "10:00", "y": 21}]},
            "daily": [{"day": "2026-05-17"}],
        }

    monkeypatch.setattr(mod, "get_self_object", lambda: {"id": "member:member-local"})
    monkeypatch.setattr(mod, "_load_config", lambda: ("https://example.test", "Moscow"))
    monkeypatch.setattr(mod, "_fetch_weather_async", _fetch_weather_async)
    monkeypatch.setattr(mod, "ctx_subnet", _CtxSubnet())
    _patch_memory(monkeypatch, mod, memory)

    import asyncio

    async def _run():
        await mod.on_weather_location_requested(
            {
                "location": {"latitude": 52.52, "longitude": 13.405, "accuracy": 10},
                "request_id": "req-geo",
                "webspace_id": "desktop",
                "target_node_id": "member-local",
            }
        )
        assert mod._WEATHER_UPDATE_TASKS == {}

    asyncio.run(_run())

    assert [entry[1].get("status") for entry in projected] == ["ok"]
    assert projected[0][1]["current"]["request_id"] == "req-geo"
    assert projected[0][1]["current"]["pending"] is False
    assert projected[0][1]["current"]["location"]["latitude"] == 52.52


def test_weather_rapid_city_changes_project_only_latest_terminal_snapshot(monkeypatch):
    mod = _load_weather_module()
    projected: list[tuple[str, dict, str | None]] = []
    berlin_started = None
    memory: dict[str, object] = {}

    class _CtxSubnet:
        async def set_async(self, slot, payload, webspace_id=None):
            projected.append((slot, payload, webspace_id))

    monkeypatch.setattr(mod, "get_self_object", lambda: {"id": "member:member-local"})
    monkeypatch.setattr(mod, "_load_config", lambda: ("https://example.test", None))
    monkeypatch.setattr(mod, "ctx_subnet", _CtxSubnet())
    _patch_memory(monkeypatch, mod, memory)

    import asyncio

    async def _run():
        nonlocal berlin_started
        berlin_started = asyncio.Event()

        async def _fetch_weather_async(_api, city=None, _location=None):
            if city == "Berlin":
                berlin_started.set()
                await asyncio.sleep(10)
            return True, {"city": city, "temp": 12, "description": "clear", "wind_ms": 1}

        monkeypatch.setattr(mod, "_fetch_weather_async", _fetch_weather_async)
        first = asyncio.create_task(
            mod.on_weather_city_changed(
                {"city": "Berlin", "request_id": "req-berlin", "webspace_id": "desktop"}
            )
        )
        await asyncio.wait_for(berlin_started.wait(), timeout=1)
        second = asyncio.create_task(
            mod.on_weather_city_changed(
                {"city": "Moscow", "request_id": "req-moscow", "webspace_id": "desktop"}
            )
        )
        await asyncio.gather(first, second, return_exceptions=True)
        assert mod._WEATHER_UPDATE_TASKS == {}

    asyncio.run(_run())

    assert len(projected) == 1
    assert projected[0][1]["status"] == "ok"
    assert projected[0][1]["current"]["city"] == "Moscow"
    assert projected[0][1]["current"]["request_id"] == "req-moscow"
    runtime = mod.get_runtime_status()
    assert runtime["completed_total"] == 1
    assert runtime["superseded_total"] == 1
    assert runtime["active_total"] == 0
    assert runtime["status_source"] == "skill_memory"


def test_weather_runtime_status_does_not_report_stale_active_state_as_current(monkeypatch):
    mod = _load_weather_module()
    stale_state = {
        "accepted_total": 2,
        "completed_total": 0,
        "superseded_total": 1,
        "failed_total": 0,
        "projection_rejected_total": 0,
        "active_total": 1,
        "max_active_total": 2,
        "state_updated_at": "2020-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(
        mod,
        "memory_get",
        lambda key, default=None: stale_state if key == mod._REQUEST_DIAGNOSTICS_MEMORY_KEY else default,
    )

    runtime = mod.get_runtime_status()

    assert runtime["status_source"] == "skill_memory"
    assert runtime["active_state_stale"] is True
    assert runtime["reported_active_total"] == 1
    assert runtime["active_total"] is None
    assert runtime["state_updated_at"] == "2020-01-01T00:00:00+00:00"
    assert runtime["observed_at"] != runtime["state_updated_at"]


def test_weather_runtime_status_exposes_diagnostic_persistence_failure(monkeypatch):
    mod = _load_weather_module()

    async def _fail_set(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(mod, "memory_async_set", _fail_set)
    monkeypatch.setattr(mod, "memory_get", lambda _key, default=None: default)

    import asyncio

    asyncio.run(
        mod._persist_weather_request_diagnostics(
            {"active_total": 0, "state_updated_at": mod._now_iso()}
        )
    )
    runtime = mod.get_runtime_status()

    assert runtime["status_source"] == "process_memory"
    assert runtime["diagnostic_persist_failed_total"] == 1
    assert runtime["last_persist_error"]["type"] == "OSError"


def test_weather_snapshot_returns_last_projected_webspace_state_without_refetch(monkeypatch):
    mod = _load_weather_module()
    persisted = {
        "status": "ok",
        "current": {
            "city": "Vienna",
            "temp_c": 22.3,
            "request_id": "req-vienna",
            "pending": False,
            "source": "api",
            "updated_at": mod._now_iso(),
        },
        "hourly_chart": {"points": []},
        "daily": [],
    }

    monkeypatch.setattr(
        mod,
        "memory_get",
        lambda key, default=None: persisted if key == "webspace_snapshot.desktop" else default,
    )
    monkeypatch.setattr(
        mod,
        "get_weather",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not refetch")),
    )

    result = mod.get_snapshot(webspace_id="desktop")

    assert result["ok"] is True
    assert result["snapshot_source"] == "skill_memory"
    assert result["current"]["city"] == "Vienna"
    assert result["current"]["request_id"] == "req-vienna"


def test_weather_snapshot_refreshes_stale_selected_city_instead_of_default(monkeypatch):
    mod = _load_weather_module()
    persisted = {
        "status": "ok",
        "current": {
            "city": "Vienna",
            "temp_c": 18.0,
            "updated_at": "2020-01-01T00:00:00+00:00",
            "source": "api",
        },
    }
    requests: list[dict] = []

    monkeypatch.setattr(
        mod,
        "memory_get",
        lambda key, default=None: persisted if key == "webspace_snapshot.desktop" else default,
    )
    monkeypatch.setattr(mod, "memory_set", lambda *_args, **_kwargs: None)

    def _get_weather(payload):
        requests.append(dict(payload))
        return {
            "ok": True,
            "current": {"city": "Vienna", "temp_c": 22.3, "updated_at": mod._now_iso(), "source": "api"},
            "hourly_chart": {"points": []},
            "daily": [],
        }

    monkeypatch.setattr(mod, "get_weather", _get_weather)

    result = mod.get_snapshot(webspace_id="desktop")

    assert requests[0]["city"] == "Vienna"
    assert result["current"]["city"] == "Vienna"
    assert result.get("snapshot_source") is None


def test_weather_snapshot_does_not_overwrite_newer_city_request(monkeypatch):
    mod = _load_weather_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "memory_set", lambda key, value: memory.__setitem__(key, value))

    def _get_weather(_payload):
        generation = mod._advance_webspace_request_generation("desktop")
        newer = {
            "status": "pending",
            "request_generation": generation,
            "current": {
                "city": "Prague",
                "request_id": "req-prague",
                "pending": True,
                "source": "pending",
                "updated_at": mod._now_iso(),
            },
        }
        assert mod._store_webspace_snapshot("desktop", newer, expected_generation=generation) is True
        with mod._SNAPSHOT_STATE_LOCK:
            mod._WEBSPACE_REQUEST_GENERATIONS.clear()
        return {
            "ok": True,
            "current": {"city": "Moscow", "temp_c": 10, "updated_at": mod._now_iso(), "source": "api"},
        }

    monkeypatch.setattr(mod, "get_weather", _get_weather)

    result = mod.get_snapshot(webspace_id="desktop")

    assert result["snapshot_source"] == "superseded_by_newer_request"
    assert result["current"]["city"] == "Prague"
    assert memory["webspace_snapshot.desktop"]["current"]["city"] == "Prague"


def test_weather_projection_persists_snapshot_before_yjs_write(monkeypatch):
    mod = _load_weather_module()
    projected: list[tuple[str, dict, str | None]] = []
    memory: dict[str, object] = {}

    class _CtxSubnet:
        async def set_async(self, slot, payload, webspace_id=None):
            assert memory["webspace_snapshot.desktop"] == snapshot
            projected.append((slot, payload, webspace_id))

    snapshot = {
        "status": "ok",
        "current": {"city": "Vienna", "temp_c": 22.3, "request_id": "req-vienna", "pending": False},
    }
    monkeypatch.setattr(mod, "ctx_subnet", _CtxSubnet())
    monkeypatch.setattr(mod, "memory_set", lambda key, value: memory.__setitem__(key, value))

    import asyncio

    async def _run():
        assert await mod._project_weather_snapshot_async(snapshot, webspace_id="desktop") is True
        await mod.memory_async_set("projection_context_probe", {"ready": True})

    asyncio.run(_run())

    assert projected == [("weather.snapshot", snapshot, "desktop")]
    assert memory["webspace_snapshot.desktop"] == snapshot
    assert memory["last_city"] == "Vienna"
    assert mod.memory_get("projection_context_probe") == {"ready": True}


def test_weather_targeted_request_is_only_processed_by_target_node(monkeypatch):
    mod = _load_weather_module()
    fetches: list[str] = []

    async def _fetch_weather_async(*_args, **_kwargs):
        fetches.append("called")
        return True, {"temp": 10, "description": "clear", "wind_ms": 1}

    monkeypatch.setattr(mod, "get_self_object", lambda: {"id": "hub:hub-local"})
    monkeypatch.setattr(mod, "_fetch_weather_async", _fetch_weather_async)

    import asyncio

    asyncio.run(
        mod.on_weather_city_changed(
            {
                "city": "Berlin",
                "webspace_id": "desktop",
                "target_node_id": "member-remote",
                "_meta": {"target_node_id": "member-remote"},
            }
        )
    )

    assert fetches == []
    assert mod._WEATHER_UPDATE_TASKS == {}


def test_weather_legacy_openweathermap_endpoint_uses_open_meteo(monkeypatch):
    mod = _load_weather_module()
    request: dict[str, object] = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"current": {"temperature_2m": 11.25, "wind_speed_10m": 2.5}}

    def _get(url, *, params=None, service=None, timeout=None):
        request["url"] = url
        request["params"] = params
        request["service"] = service
        request["timeout"] = timeout
        return mod.external_api.ExternalApiResult(ok=True, response=_Response(), mode="local", url=url)

    monkeypatch.setattr(mod.external_api, "get", _get)

    ok, data = mod._fetch_weather("https://api.openweathermap.org/data/2.5/weather", "Moscow")

    assert ok is True
    assert request["url"] == mod.DEFAULT_API_ENDPOINT
    assert request["service"] == mod.WEATHER_API_CHANNEL
    assert request["timeout"] == mod.WEATHER_HTTP_TIMEOUT
    assert request["params"]["latitude"] == 55.75
    assert request["params"]["longitude"] == 37.62
    assert "temperature_2m" in request["params"]["current"]
    assert "wind_speed_10m" in request["params"]["current"]
    assert "relative_humidity_2m" in request["params"]["current"]
    assert request["params"]["wind_speed_unit"] == "ms"
    assert data["temp_c"] == 11.25
    assert data["wind_ms"] == 2.5


def test_weather_geocoding_uses_resilient_external_api_route(monkeypatch):
    mod = _load_weather_module()
    request: dict[str, object] = {}

    class _Response:
        status_code = 200

        def json(self):
            return {
                "results": [
                    {
                        "name": "Reykjavik",
                        "country_code": "IS",
                        "latitude": 64.1466,
                        "longitude": -21.9426,
                        "timezone": "Atlantic/Reykjavik",
                    }
                ]
            }

    def _get(url, *, params=None, service=None, timeout=None):
        request.update(url=url, params=params, service=service, timeout=timeout)
        return mod.external_api.ExternalApiResult(ok=True, response=_Response(), mode="global_proxy", url=url)

    monkeypatch.setattr(mod.external_api, "get", _get)
    mod._GEOCODE_CACHE.clear()

    location = mod._geocode_city("Reykjavik")

    assert request["url"] == mod.DEFAULT_GEOCODING_ENDPOINT
    assert request["service"] == mod.WEATHER_GEOCODING_CHANNEL
    assert request["timeout"] == mod.WEATHER_HTTP_TIMEOUT
    assert location["city"] == "Reykjavik"
    assert location["source"] == "geocoding"


def test_weather_fetch_uses_last_success_as_status_error_fallback(monkeypatch):
    mod = _load_weather_module()
    mod._CITY_CACHE.clear()
    memory: dict[str, object] = {}
    calls = {"count": 0}

    def _fetch(_api, location):
        calls["count"] += 1
        if calls["count"] == 1:
            return True, {
                "city": "Berlin",
                "temp": 12.5,
                "temp_c": 12.5,
                "description": "Clear",
                "condition": "Clear",
                "wind_ms": 2,
                "current": {
                    "city": "Berlin",
                    "temp_c": 12.5,
                    "condition": "Clear",
                    "description": "Clear",
                    "wind_ms": 2,
                    "updated_at": "cached",
                    "source": "api",
                },
                "hourly_chart": {"points": []},
                "daily": [],
                "updated_at": "cached",
                "source": "api",
            }
        return False, {"error": "Weather API returned status 502", "location": location}

    monkeypatch.setattr(mod, "memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(mod, "_fetch_weather_for_location", _fetch)

    ok, data = mod._fetch_weather("https://example.test", "Berlin")
    assert ok is True
    assert data["source"] == "api"

    mod._CITY_CACHE.clear()
    ok, data = mod._fetch_weather("https://example.test", "Berlin")

    assert ok is True
    assert data["source"] == "cache"
    assert data["fallback"] is True
    assert data["current"]["source"] == "cache"
    assert data["current"]["stale"] is True
    assert data["error"] == "Weather API returned status 502"


def test_weather_config_migrates_legacy_openweathermap_endpoint(monkeypatch):
    mod = _load_weather_module()
    memory = {
        "api_entry_point": "https://api.openweathermap.org/data/2.5/weather",
        "default_city": "Berlin",
    }

    monkeypatch.setattr(mod, "memory_get", lambda key: memory.get(key))
    monkeypatch.setattr(mod, "memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(mod, "get_current_skill", lambda: None)

    api_entry_point, default_city = mod._load_config()

    assert api_entry_point == mod.DEFAULT_API_ENDPOINT
    assert memory["api_entry_point"] == mod.DEFAULT_API_ENDPOINT
    assert default_city == "Berlin"


def test_weather_async_fetch_preserves_skill_i18n_in_worker_thread(monkeypatch):
    mod = _load_weather_module()
    from adaos.services.agent_context import get_ctx

    ctx = get_ctx()
    skill_dir = ctx.paths.skills_workspace_dir() / "weather_skill"
    (skill_dir / "i18n").mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text("name: weather_skill\nversion: 1.0.0\n", encoding="utf-8")
    (skill_dir / "i18n" / "en.json").write_text(
        json.dumps({"runtime.weather.errors.status": "Weather API returned status {status}"}, ensure_ascii=False),
        encoding="utf-8",
    )
    previous = ctx.skill_ctx.get()
    assert ctx.skill_ctx.set("weather_skill", skill_dir) is True

    class _Response:
        status_code = 503

        def json(self):
            return {}

    monkeypatch.setattr(
        mod.external_api,
        "get",
        lambda *_args, **_kwargs: mod.external_api.ExternalApiResult(
            ok=True,
            response=_Response(),
            mode="local",
            url="https://example.test",
        ),
    )

    import asyncio

    try:
        ok, data = asyncio.run(mod._fetch_weather_async("https://example.test", "Berlin"))
    finally:
        if previous is None:
            ctx.skill_ctx.clear()
        else:
            ctx.skill_ctx.set(previous.name, previous.path)

    assert ok is False
    assert data["error"] == "Weather API returned status 503"


def test_weather_intent_api_error_appends_chat_when_route_missing(monkeypatch):
    mod = _load_weather_module()
    emitted: list[tuple[str, dict, dict]] = []

    async def _emit(topic, payload, **kwargs):
        emitted.append((topic, payload, kwargs))

    async def _fetch_weather_async(*_args, **_kwargs):
        return False, {"error": "Weather API error"}

    monkeypatch.setattr(mod, "emit", _emit)
    monkeypatch.setattr(mod, "_load_config", lambda: ("https://example.test", "Berlin"))
    monkeypatch.setattr(mod, "_resolve_city", lambda city: city)
    monkeypatch.setattr(mod, "_fetch_weather_async", _fetch_weather_async)

    import asyncio

    asyncio.run(mod.on_weather_intent({"city": "Berlin", "_meta": {"webspace_id": "desktop"}}))

    assert [entry[0] for entry in emitted] == ["ui.notify", "io.out.chat.append"]
    assert emitted[0][1]["text"] == mod._WEATHER_UNAVAILABLE_TEXT
    assert emitted[1][1]["text"] == mod._WEATHER_UNAVAILABLE_TEXT
    assert emitted[1][1]["_meta"] == {"webspace_id": "desktop"}


def test_weather_intent_success_appends_chat_when_route_missing(monkeypatch):
    mod = _load_weather_module()
    emitted: list[tuple[str, dict, dict]] = []

    async def _emit(topic, payload, **kwargs):
        emitted.append((topic, payload, kwargs))

    async def _fetch_weather_async(*_args, **_kwargs):
        return True, {"city": "Moscow", "temp": 19.5, "description": ""}

    monkeypatch.setattr(mod, "emit", _emit)
    monkeypatch.setattr(mod, "_load_config", lambda: ("https://example.test", "Moscow"))
    monkeypatch.setattr(mod, "_resolve_city", lambda city: city)
    monkeypatch.setattr(mod, "_fetch_weather_async", _fetch_weather_async)
    monkeypatch.setattr(mod, "_", lambda key, **kw: f"In {kw['city']} the temperature is {kw['temp']}. {kw['description']}")

    import asyncio

    asyncio.run(mod.on_weather_intent({"city": "Moscow", "_meta": {"webspace_id": "desktop"}}))

    assert [entry[0] for entry in emitted] == ["ui.notify", "io.out.chat.append"]
    assert emitted[0][1]["text"] == "In Moscow the temperature is 19.5. "
    assert emitted[1][1]["text"] == "In Moscow the temperature is 19.5. "
    assert emitted[1][1]["_meta"] == {"webspace_id": "desktop"}


def test_weather_intent_api_error_keeps_existing_route_single_delivery(monkeypatch):
    mod = _load_weather_module()
    emitted: list[tuple[str, dict, dict]] = []

    async def _emit(topic, payload, **kwargs):
        emitted.append((topic, payload, kwargs))

    async def _fetch_weather_async(*_args, **_kwargs):
        return False, {"error": "Weather API error"}

    monkeypatch.setattr(mod, "emit", _emit)
    monkeypatch.setattr(mod, "_load_config", lambda: ("https://example.test", "Berlin"))
    monkeypatch.setattr(mod, "_resolve_city", lambda city: city)
    monkeypatch.setattr(mod, "_fetch_weather_async", _fetch_weather_async)

    import asyncio

    asyncio.run(mod.on_weather_intent({"city": "Berlin", "_meta": {"webspace_id": "desktop", "route_id": "voice_chat"}}))

    assert [entry[0] for entry in emitted] == ["ui.notify"]
    assert emitted[0][1]["text"] == mod._WEATHER_UNAVAILABLE_TEXT


def test_weather_intent_success_keeps_existing_route_single_delivery(monkeypatch):
    mod = _load_weather_module()
    emitted: list[tuple[str, dict, dict]] = []

    async def _emit(topic, payload, **kwargs):
        emitted.append((topic, payload, kwargs))

    async def _fetch_weather_async(*_args, **_kwargs):
        return True, {"city": "Moscow", "temp": 19.5, "description": ""}

    monkeypatch.setattr(mod, "emit", _emit)
    monkeypatch.setattr(mod, "_load_config", lambda: ("https://example.test", "Moscow"))
    monkeypatch.setattr(mod, "_resolve_city", lambda city: city)
    monkeypatch.setattr(mod, "_fetch_weather_async", _fetch_weather_async)
    monkeypatch.setattr(mod, "_", lambda key, **kw: f"In {kw['city']} the temperature is {kw['temp']}. {kw['description']}")

    import asyncio

    asyncio.run(mod.on_weather_intent({"city": "Moscow", "_meta": {"webspace_id": "desktop", "route_id": "voice_chat"}}))

    assert [entry[0] for entry in emitted] == ["ui.notify"]
    assert emitted[0][1]["text"] == "In Moscow the temperature is 19.5. "
