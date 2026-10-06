"""Demand-driven system telemetry stream for the selected node only."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from threading import Event, Lock, Thread
from typing import Any

from adaos.sdk import system as sdk_system
from adaos.sdk.data import ProjectionContext, StreamReceiver, StreamRuntime
from adaos.sdk.io import stream_publish

from .operational import project


RECEIVER = "web_desktop.system.hardware"
_LOG = logging.getLogger("adaos.skill.web_desktop_runtime.system_stream")
_WORKERS: dict[tuple[str, str], tuple[Event, Thread]] = {}
_WORKERS_LOCK = Lock()
_RETIRED: list[tuple[tuple[str, str], tuple[Event, Thread]]] = []
_DRAINING = False


def _payload(event: Any) -> Mapping[str, Any]:
    value = getattr(event, "payload", event)
    return value if isinstance(value, Mapping) else {}


def _webspace(value: Mapping[str, Any]) -> str:
    return str(value.get("webspace_id") or value.get("workspace_id") or "default").strip() or "default"


def _node(value: Mapping[str, Any]) -> str:
    meta = value.get("_meta") if isinstance(value.get("_meta"), Mapping) else {}
    return str(
        value.get("target_node_id")
        or value.get("node_target_id")
        or value.get("node_id")
        or meta.get("target_node_id")
        or meta.get("node_target_id")
        or meta.get("node_id")
        or ""
    ).strip()


def _build(context: ProjectionContext) -> dict[str, Any]:
    try:
        snapshot = sdk_system.get_operational_snapshot(
            sections={"summary", "resources"},
            webspace_id=context.webspace_id,
            limit=1,
        )
        result = project(snapshot if isinstance(snapshot, dict) else {}, "metrics")
        result["node_id"] = str(context.node_id or "").strip()
        return result
    except Exception as exc:
        return {
            "hardware_metrics": [],
            "center": {"value": "—", "label": "Unavailable"},
            "subtitle": "",
            "description": "Hardware telemetry is temporarily unavailable.",
            "freshness": "unavailable",
            "node_id": str(context.node_id or "").strip(),
            "reason": type(exc).__name__,
        }


_RUNTIME = StreamRuntime(
    "web_desktop_runtime_skill",
    receivers=[StreamReceiver(RECEIVER, build=_build, min_interval_s=4.0)],
    stream_publish=stream_publish,
)


def _worker(key: tuple[str, str], stop: Event) -> None:
    webspace_id, node_id = key
    context = ProjectionContext(
        skill_id="web_desktop_runtime_skill",
        webspace_id=webspace_id,
        receiver=RECEIVER,
        node_id=node_id or None,
        reason="active_hardware_subscription",
    )
    while not stop.is_set():
        try:
            _RUNTIME.publish_receiver_snapshot(
                RECEIVER,
                webspace_id=webspace_id,
                context=context,
            )
        except Exception:
            _LOG.debug("hardware stream refresh failed", exc_info=True)
        stop.wait(5.0)
    # The owner retains the thread until is_alive() is false or join completes.


def _start_worker(webspace_id: str, node_id: str) -> None:
    key = (webspace_id, node_id)
    with _WORKERS_LOCK:
        if _DRAINING:
            return
        if key in _WORKERS and not _WORKERS[key][0].is_set() and _WORKERS[key][1].is_alive():
            return
        if key in _WORKERS:
            _RETIRED.append((key, _WORKERS.pop(key)))
        _RETIRED[:] = [entry for entry in _RETIRED if entry[1][1].is_alive()]
        # A System surface has one selected node. Stop any stale selection for
        # the same webspace before starting the new demand stream.
        for old_key, (old_stop, _thread) in list(_WORKERS.items()):
            if old_key[0] == webspace_id and old_key != key:
                old_stop.set()
                _RETIRED.append((old_key, _WORKERS.pop(old_key)))
        stop = Event()
        thread = Thread(
            target=_worker,
            args=(key, stop),
            name=f"web-desktop-hardware-{node_id or 'local'}",
            daemon=True,
        )
        _WORKERS[key] = (stop, thread)
        thread.start()


def _stop_worker(webspace_id: str, node_id: str) -> None:
    with _WORKERS_LOCK:
        keys = [
            key for key in _WORKERS
            if key[0] == webspace_id and (not node_id or key[1] == node_id)
        ]
        for key in keys:
            stop, _thread = _WORKERS[key]
            stop.set()


def snapshot_requested(event: Any) -> None:
    if _DRAINING:
        return
    _RUNTIME.handle_snapshot_requested(event, receiver_prefix=RECEIVER, force=True)


def subscription_changed(event: Any) -> None:
    if _DRAINING:
        return
    value = _payload(event)
    if str(value.get("receiver") or "").strip() != RECEIVER:
        return
    webspace_id = _webspace(value)
    node_id = _node(value)
    action = str(value.get("action") or "").strip().lower()
    _RUNTIME.handle_subscription_changed(event, receiver_prefix=RECEIVER)
    if action == "unsubscribed":
        _stop_worker(webspace_id, node_id)
    else:
        _start_worker(webspace_id, node_id)


def stop_workers() -> None:
    global _DRAINING
    with _WORKERS_LOCK:
        _DRAINING = True
        for _key, (stop, _thread) in [*list(_WORKERS.items()), *_RETIRED]:
            stop.set()


def drain_workers(timeout_s: float = 1.0) -> dict[str, Any]:
    """Stop and join every owner-scoped hardware publisher."""

    global _DRAINING
    with _WORKERS_LOCK:
        _DRAINING = True
        workers = list(_WORKERS.items()) + list(_RETIRED)
        for _key, (stop, _thread) in workers:
            stop.set()
    alive: list[str] = []
    for key, (_stop, thread) in workers:
        thread.join(timeout=max(0.0, float(timeout_s)))
        if thread.is_alive():
            alive.append(f"{key[0]}:{key[1]}")
    with _WORKERS_LOCK:
        for key, pair in workers:
            if not pair[1].is_alive() and _WORKERS.get(key) is pair:
                _WORKERS.pop(key, None)
        _RETIRED[:] = [entry for entry in _RETIRED if entry[1][1].is_alive()]
    if not alive:
        _RUNTIME.reset()
    return {
        "worker_total": len(workers),
        "stopped_total": len(workers) - len(alive),
        "alive": alive,
    }


def rehydrate_workers() -> dict[str, Any]:
    """Re-enable subscriptions only after every previous worker has exited."""

    global _DRAINING
    with _WORKERS_LOCK:
        alive = [
            f"{key[0]}:{key[1]}"
            for key, (_stop, thread) in [*list(_WORKERS.items()), *_RETIRED]
            if thread.is_alive()
        ]
        if alive:
            return {"ok": False, "alive": alive, "reason": "workers_still_draining"}
        _WORKERS.clear()
        _RETIRED.clear()
        _DRAINING = False
    return {"ok": True, "alive": []}


__all__ = [
    "RECEIVER",
    "drain_workers",
    "rehydrate_workers",
    "snapshot_requested",
    "subscription_changed",
]
