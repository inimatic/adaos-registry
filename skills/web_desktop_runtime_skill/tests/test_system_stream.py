"""Demand lifecycle checks for selected-node hardware telemetry."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import Mock
from threading import Event

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
system_stream = importlib.import_module("handlers.system_stream")


def test_native_lifecycle_tools_have_distinct_exported_entries():
    manifest = yaml.safe_load((ROOT / "skill.yaml").read_text(encoding="utf-8"))
    declarations = {tool["name"]: tool for tool in manifest["tools"]}
    main = importlib.import_module("handlers.main")
    assert manifest["data_lifecycle"]["execution"] == "native_tools"
    for phase in ("drain", "dispose", "rehydrate"):
        name = "web_desktop_runtime_" + phase
        assert manifest["lifecycle"][phase] == name
        assert name in manifest["exports"]["tools"]
        assert declarations[name]["entry"] == "handlers.main:" + name
        assert declarations[name]["side_effects"] == "runtime_write"
        assert callable(getattr(main, name))


def test_rehydrate_resets_both_streams_before_reopening(monkeypatch):
    main = importlib.import_module("handlers.main")
    bootstrap = importlib.import_module("handlers.system_bootstrap_stream")
    monkeypatch.setattr(system_stream, "_WORKERS", {})
    monkeypatch.setattr(system_stream, "_RETIRED", [])
    monkeypatch.setattr(system_stream, "_DRAINING", True)
    monkeypatch.setattr(bootstrap, "_REFRESH_THREAD", None)
    monkeypatch.setattr(bootstrap, "_REFRESH_RUNNING", False)
    stop = Event()
    stop.set()
    monkeypatch.setattr(bootstrap, "_REFRESH_STOP", stop)
    hardware, subscription = Mock(), Mock()

    def reset():
        assert system_stream._DRAINING
        assert stop.is_set()

    hardware.reset.side_effect = subscription.reset.side_effect = reset
    monkeypatch.setattr(system_stream, "_RUNTIME", hardware)
    monkeypatch.setattr(bootstrap, "_RUNTIME", subscription)
    assert main.web_desktop_runtime_rehydrate()["ok"]
    hardware.reset.assert_called_once()
    subscription.reset.assert_called_once()
    assert not system_stream._DRAINING
    assert not stop.is_set()


@pytest.mark.parametrize("blocked_owner", ["hardware", "subscription"])
def test_rehydrate_does_not_partially_reopen_publishers(monkeypatch, blocked_owner):
    main = importlib.import_module("handlers.main")
    bootstrap = importlib.import_module("handlers.system_bootstrap_stream")
    order = []
    monkeypatch.setattr(system_stream, "stop_workers", lambda: order.append("hardware_stop"))
    monkeypatch.setattr(bootstrap, "stop_refresh", lambda: order.append("subscription_stop"))

    def hardware_drain():
        assert order == ["hardware_stop", "subscription_stop"]
        return {"alive": ["desktop:node"] if blocked_owner == "hardware" else []}

    monkeypatch.setattr(system_stream, "drain_workers", hardware_drain)
    monkeypatch.setattr(bootstrap, "drain_refresh", lambda: {
        "worker_alive": blocked_owner == "subscription",
    })
    hardware_resume, subscription_resume = Mock(), Mock()
    monkeypatch.setattr(system_stream, "rehydrate_workers", hardware_resume)
    monkeypatch.setattr(bootstrap, "rehydrate_refresh", subscription_resume)
    assert not main.web_desktop_runtime_rehydrate()["ok"]
    hardware_resume.assert_not_called()
    subscription_resume.assert_not_called()


def test_completed_refresh_retains_thread_until_owner_joins(monkeypatch):
    stream = importlib.import_module("handlers.system_bootstrap_stream")
    runtime = Mock()
    runtime.active_receivers_snapshot.return_value = []
    monkeypatch.setattr(stream, "_RUNTIME", runtime)
    monkeypatch.setattr(stream, "_REFRESH_STOP", Event())
    monkeypatch.setattr(stream, "_REFRESH_RUNNING", False)
    monkeypatch.setattr(stream, "_REFRESH_THREAD", None)
    stream.root_subscription_changed({})
    thread = stream._REFRESH_THREAD
    assert thread is not None
    thread.join(1)
    assert not thread.is_alive()
    assert stream._REFRESH_THREAD is thread
    assert not stream.drain_refresh()["worker_alive"]
    assert stream._REFRESH_THREAD is None


@pytest.mark.parametrize("replace", [False, True])
def test_drain_retains_inflight_hardware_and_blocks_restart(monkeypatch, replace):
    entered, release = Event(), Event()
    runtime = Mock()

    def publish(*args, **kwargs):
        entered.set()
        assert release.wait(3)

    runtime.publish_receiver_snapshot.side_effect = publish
    monkeypatch.setattr(system_stream, "_RUNTIME", runtime)
    monkeypatch.setattr(system_stream, "_WORKERS", {})
    monkeypatch.setattr(system_stream, "_RETIRED", [])
    monkeypatch.setattr(system_stream, "_DRAINING", False)
    system_stream._start_worker("desktop", "one")
    try:
        assert entered.wait(1)
        system_stream._stop_worker("desktop", "one")
        if replace:
            system_stream._start_worker("desktop", "two")
        result = system_stream.drain_workers(timeout_s=0)
        assert result["alive"]
        runtime.reset.assert_not_called()
        system_stream._start_worker("other", "three")
        assert ("other", "three") not in system_stream._WORKERS
    finally:
        release.set()
        result = system_stream.drain_workers(timeout_s=1)
    assert not result["alive"]
    assert not system_stream._WORKERS
    assert not system_stream._RETIRED
    runtime.reset.assert_called_once()
    assert system_stream.rehydrate_workers() == {"ok": True, "alive": []}
    assert not system_stream._DRAINING


def test_subscription_drain_rehydrate_joins_before_accepting_events(monkeypatch):
    stream = importlib.import_module("handlers.system_bootstrap_stream")
    entered, release = Event(), Event()
    runtime = Mock()
    runtime.active_receivers_snapshot.return_value = [{"webspace_id": "desktop"}]

    def publish(*args, **kwargs):
        entered.set()
        assert release.wait(3)

    runtime.publish_receiver_snapshot.side_effect = publish
    monkeypatch.setattr(stream, "_RUNTIME", runtime)
    monkeypatch.setattr(stream, "_REFRESH_STOP", Event())
    monkeypatch.setattr(stream, "_REFRESH_RUNNING", False)
    monkeypatch.setattr(stream, "_REFRESH_THREAD", None)
    stream.root_subscription_changed({})
    try:
        assert entered.wait(1)
        assert stream.drain_refresh(timeout_s=0)["worker_alive"]
        assert not stream.rehydrate_refresh()["ok"]
        runtime.reset.assert_not_called()
    finally:
        release.set()
        assert not stream.drain_refresh(timeout_s=1)["worker_alive"]
    runtime.reset.assert_called_once()
    stream.root_subscription_changed({})
    stream.subscription_changed({})
    stream.snapshot_requested({})
    assert stream._REFRESH_THREAD is None
    runtime.handle_subscription_changed.assert_not_called()
    runtime.handle_snapshot_requested.assert_not_called()
    assert stream.rehydrate_refresh() == {"ok": True, "worker_alive": False}
    stream.subscription_changed({})
    stream.snapshot_requested({})
    runtime.handle_subscription_changed.assert_called_once()
    runtime.handle_snapshot_requested.assert_called_once()


def test_subscription_uses_meta_node_and_stops_on_release(monkeypatch):
    runtime = Mock()
    start = Mock()
    stop = Mock()
    monkeypatch.setattr(system_stream, "_RUNTIME", runtime)
    monkeypatch.setattr(system_stream, "_start_worker", start)
    monkeypatch.setattr(system_stream, "_stop_worker", stop)
    event = {
        "receiver": system_stream.RECEIVER,
        "webspace_id": "desktop",
        "_meta": {"target_node_id": "hub-1"},
        "action": "subscribed",
    }

    system_stream.subscription_changed(event)
    start.assert_called_once_with("desktop", "hub-1")
    runtime.handle_subscription_changed.assert_called_once()

    event["action"] = "unsubscribed"
    system_stream.subscription_changed(event)
    stop.assert_called_once_with("desktop", "hub-1")


def test_foreign_receiver_does_not_start_telemetry(monkeypatch):
    runtime = Mock()
    start = Mock()
    monkeypatch.setattr(system_stream, "_RUNTIME", runtime)
    monkeypatch.setattr(system_stream, "_start_worker", start)

    system_stream.subscription_changed({
        "receiver": "another.receiver",
        "action": "subscribed",
        "target_node_id": "hub-1",
    })

    runtime.handle_subscription_changed.assert_not_called()
    start.assert_not_called()


def test_worker_publishes_selected_node_before_first_refresh_wait(monkeypatch):
    calls = []
    stop = Mock()
    stop.is_set.side_effect = [False, True]
    stop.wait.side_effect = lambda seconds: calls.append(("wait", seconds))
    runtime = Mock()
    runtime.publish_receiver_snapshot.side_effect = lambda *args, **kwargs: calls.append(
        ("publish", args, kwargs))
    monkeypatch.setattr(system_stream, "_RUNTIME", runtime)
    monkeypatch.setattr(system_stream, "_WORKERS", {})
    event = {"webspace_id": "desktop", "_meta": {"target_node_id": "selected-node"}}

    system_stream._worker((system_stream._webspace(event), system_stream._node(event)), stop)

    assert [call[0] for call in calls] == ["publish", "wait"]
    assert calls[1] == ("wait", 5.0)
    runtime.publish_receiver_snapshot.assert_called_once()
    _, args, kwargs = calls[0]
    assert args == ("web_desktop.system.hardware",)
    assert kwargs["webspace_id"] == "desktop"
    context = kwargs["context"]
    assert context.node_id == "selected-node"
    assert context.webspace_id == "desktop"
    assert context.receiver == "web_desktop.system.hardware"
