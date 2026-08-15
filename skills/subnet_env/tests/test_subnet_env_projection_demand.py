from __future__ import annotations

import asyncio
from types import SimpleNamespace

try:
    from skills.subnet_env.handlers import main
except ModuleNotFoundError:
    from handlers import main


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
