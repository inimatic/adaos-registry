from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from adaos.services.skill.activation import load_skill_stream_receiver_patterns

try:
    from skills.subnet_env.handlers import main
except ModuleNotFoundError:
    from handlers import main


def test_activation_policy_allows_desktop_tooling_across_scenarios():
    manifest_path = Path(__file__).resolve().parents[1] / "skill.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    when = manifest["runtime"]["activation"]["when"]

    assert when["client_presence"] is True
    assert when["webspace_scope"] == "active"
    assert "scenarios_active" not in when

    registry_path = next(
        candidate / "registry.json"
        for candidate in Path(__file__).resolve().parents
        if (candidate / "registry.json").is_file()
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    entry = next(item for item in registry["skills"] if item["name"] == "subnet_env")
    assert entry["version"] == str(manifest["version"])
    assert entry["activation"] == manifest["runtime"]["activation"]

    route = manifest["data_routes"][0]
    assert route["route"] == "yjs"
    assert route["projection_slot"] == "subnet_env.snapshot"
    assert route["budget"]["snapshot_policy"] == "on_subscribe"


def test_every_webui_yjs_path_is_a_runtime_receiver_pattern():
    skill_root = Path(__file__).resolve().parents[1]
    webui = json.loads((skill_root / "webui.json").read_text(encoding="utf-8"))

    def _yjs_slots(value):
        slots = set()
        if isinstance(value, dict):
            if str(value.get("kind") or "").lower() == "y":
                path = str(value.get("path") or "").strip("/")
                parts = [part for part in path.split("/") if part]
                if len(parts) >= 2 and parts[0] == "data":
                    slots.add(".".join(parts[1:]))
            for nested in value.values():
                slots.update(_yjs_slots(nested))
        elif isinstance(value, list):
            for nested in value:
                slots.update(_yjs_slots(nested))
        return slots

    expected = _yjs_slots(webui)
    declared = set(load_skill_stream_receiver_patterns(skill_root.parent, "subnet_env"))

    assert expected
    assert expected <= declared


def test_tool_side_effect_contract_separates_reads_from_projection_and_env_writes():
    manifest_path = Path(__file__).resolve().parents[1] / "skill.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    effects = {tool["name"]: tool.get("side_effects") for tool in manifest["tools"]}

    assert effects == {
        "get_snapshot": "read",
        "refresh_snapshot": "runtime_write",
        "set_env_value": "local_write",
        "apply_action": "local_write",
    }


def test_get_snapshot_does_not_write_projection(monkeypatch):
    expected = {"summary": {"value": "dev"}}
    monkeypatch.setattr(main, "_build_snapshot", lambda: expected)
    monkeypatch.setattr(
        main,
        "_project_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected projection write")),
    )

    assert main.get_snapshot(webspace_id="desktop") == expected


def test_snapshot_demand_refreshes_subnet_env_projection(monkeypatch):
    calls: list[str | None] = []
    main._LAST_DEMAND_REFRESH.clear()
    monkeypatch.setattr(
        main,
        "_refresh",
        lambda *, webspace_id=None: calls.append(webspace_id) or {"summary": {"value": "dev"}},
    )
    monkeypatch.setattr(main, "set_current_skill", lambda *_args, **_kwargs: False)

    asyncio.run(
        main.on_webio_yjs_snapshot_requested(
            SimpleNamespace(
                payload={
                    "topic": "webio.yjs.desktop.subnet_env.summary",
                    "webspace_id": "desktop",
                    "slot": "subnet_env.summary",
                }
            )
        )
    )

    assert calls == ["desktop"]


def test_subscription_burst_coalesces_full_snapshot(monkeypatch):
    calls: list[str | None] = []
    main._LAST_DEMAND_REFRESH.clear()
    monkeypatch.setattr(
        main,
        "_refresh",
        lambda *, webspace_id=None: calls.append(webspace_id) or {"summary": {"value": "dev"}},
    )
    monkeypatch.setattr(main, "set_current_skill", lambda *_args, **_kwargs: False)

    async def _run() -> None:
        for slot in ("subnet_env.summary", "subnet_env.overview", "subnet_env.notices"):
            await main.on_webio_yjs_subscription_changed(
                SimpleNamespace(
                    payload={
                        "action": "subscribed",
                        "webspace_id": "desktop",
                        "slot": slot,
                    }
                )
            )

    asyncio.run(_run())

    assert calls == ["desktop"]


def test_unrelated_and_unsubscribe_events_do_not_refresh(monkeypatch):
    calls: list[str | None] = []
    main._LAST_DEMAND_REFRESH.clear()
    monkeypatch.setattr(
        main,
        "_refresh",
        lambda *, webspace_id=None: calls.append(webspace_id) or {},
    )

    async def _run() -> None:
        await main.on_webio_yjs_snapshot_requested(
            SimpleNamespace(payload={"webspace_id": "desktop", "slot": "weather.snapshot"})
        )
        await main.on_webio_yjs_subscription_changed(
            SimpleNamespace(
                payload={
                    "action": "unsubscribed",
                    "webspace_id": "desktop",
                    "slot": "subnet_env.summary",
                }
            )
        )

    asyncio.run(_run())

    assert calls == []


def test_projection_demand_for_another_node_is_ignored(monkeypatch):
    calls: list[str | None] = []
    main._LAST_DEMAND_REFRESH.clear()
    monkeypatch.setattr(main, "_refresh", lambda *, webspace_id=None: calls.append(webspace_id) or {})
    monkeypatch.setattr(
        main,
        "get_self_object",
        lambda: {"id": "hub:hub-local"},
    )

    asyncio.run(
        main.on_webio_yjs_snapshot_requested(
            SimpleNamespace(
                payload={
                    "webspace_id": "desktop",
                    "slot": "subnet_env.snapshot",
                    "target_node_id": "member-remote",
                }
            )
        )
    )

    assert calls == []


def test_projection_demand_accepts_raw_target_for_canonical_self(monkeypatch):
    calls: list[str | None] = []
    main._LAST_DEMAND_REFRESH.clear()
    monkeypatch.setattr(
        main,
        "_refresh",
        lambda *, webspace_id=None: calls.append(webspace_id) or {"summary": {"value": "dev"}},
    )
    monkeypatch.setattr(main, "set_current_skill", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(main, "get_self_object", lambda: {"id": "hub:hub-local"})

    asyncio.run(
        main.on_webio_yjs_snapshot_requested(
            SimpleNamespace(
                payload={
                    "webspace_id": "desktop",
                    "slot": "subnet_env.snapshot",
                    "target_node_id": "hub-local",
                }
            )
        )
    )

    assert calls == ["desktop"]
