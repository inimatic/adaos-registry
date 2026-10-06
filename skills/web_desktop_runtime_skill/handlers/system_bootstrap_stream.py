"""Single demand-driven System first-paint read-model.

Dashboard, preferences and AI usage share one receiver and therefore one
admission. Root subscription changes republish the persisted public projection
only while a System consumer is subscribed.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from threading import Event, Lock, Thread, current_thread
from typing import Any

from adaos.sdk.data import ProjectionContext, StreamReceiver, StreamRuntime
from adaos.sdk.io import stream_publish


RECEIVER = "web_desktop.system.bootstrap"
_LOG = logging.getLogger("adaos.skill.web_desktop_runtime.system_bootstrap")
_REFRESH_LOCK = Lock()
_REFRESH_RUNNING = False
_REFRESH_STOP = Event()
_REFRESH_THREAD: Thread | None = None


def _payload(event: Any) -> Mapping[str, Any]:
    value = getattr(event, "payload", event)
    return value if isinstance(value, Mapping) else {}


def _build(context: ProjectionContext) -> dict[str, Any]:
    from .main import get_system_overview

    return get_system_overview(
        webspace_id=context.webspace_id,
        section="system_bootstrap",
    )


_RUNTIME = StreamRuntime(
    "web_desktop_runtime_skill",
    receivers=[StreamReceiver(RECEIVER, build=_build, min_interval_s=1.0)],
    stream_publish=stream_publish,
)


def snapshot_requested(event: Any) -> None:
    if _REFRESH_STOP.is_set():
        return
    _RUNTIME.handle_snapshot_requested(event, receiver_prefix=RECEIVER, force=True)


def subscription_changed(event: Any) -> None:
    if _REFRESH_STOP.is_set():
        return
    _RUNTIME.handle_subscription_changed(event, receiver_prefix=RECEIVER)


def _refresh_active() -> None:
    global _REFRESH_RUNNING, _REFRESH_THREAD
    try:
        # The control plane persists the accepted Root snapshot before it
        # emits the change event. A skill subscriber only republishes the
        # public bounded SDK projection; it must not reach into a private
        # economic service or perform a second remote refresh.
        for active in _RUNTIME.active_receivers_snapshot():
            if _REFRESH_STOP.is_set():
                break
            webspace_id = str(active.get("webspace_id") or "default").strip() or "default"
            _RUNTIME.publish_receiver_snapshot(
                RECEIVER,
                webspace_id=webspace_id,
                force=True,
                context=ProjectionContext(
                    skill_id="web_desktop_runtime_skill",
                    webspace_id=webspace_id,
                    receiver=RECEIVER,
                    event_topic="root.subscription.changed",
                    reason="subscription_snapshot_changed",
                ),
            )
    finally:
        with _REFRESH_LOCK:
            _REFRESH_RUNNING = False
            # Keep the reference until the owner verifies thread termination.


def root_subscription_changed(event: Any) -> None:
    del event
    global _REFRESH_RUNNING, _REFRESH_THREAD
    with _REFRESH_LOCK:
        if (_REFRESH_RUNNING or _REFRESH_STOP.is_set()
                or (_REFRESH_THREAD is not None and _REFRESH_THREAD.is_alive())):
            return
        _REFRESH_RUNNING = True
        thread = Thread(
            target=_refresh_active,
            name="web-desktop-subscription-refresh",
            daemon=True,
        )
        _REFRESH_THREAD = thread
        thread.start()


def stop_refresh() -> None:
    with _REFRESH_LOCK:
        _REFRESH_STOP.set()


def drain_refresh(timeout_s: float = 1.0) -> dict[str, Any]:
    """Stop the owner-scoped refresh worker before runtime replacement."""

    global _REFRESH_RUNNING, _REFRESH_THREAD
    with _REFRESH_LOCK:
        _REFRESH_STOP.set()
        thread = _REFRESH_THREAD
    if thread is not None and thread is not current_thread():
        thread.join(timeout=max(0.0, float(timeout_s)))
    alive = bool(thread is not None and thread.is_alive())
    with _REFRESH_LOCK:
        if not alive and _REFRESH_THREAD is thread:
            _REFRESH_THREAD = None
            _REFRESH_RUNNING = False
    if not alive:
        _RUNTIME.reset()
    return {"worker_present": thread is not None, "worker_alive": alive}


def rehydrate_refresh() -> dict[str, Any]:
    """Re-enable refresh events after the previous runtime has fully drained."""

    global _REFRESH_RUNNING, _REFRESH_THREAD
    with _REFRESH_LOCK:
        thread = _REFRESH_THREAD
        if thread is not None and thread.is_alive():
            return {"ok": False, "worker_alive": True, "reason": "worker_still_draining"}
        _REFRESH_THREAD = None
        _REFRESH_RUNNING = False
        _REFRESH_STOP.clear()
    return {"ok": True, "worker_alive": False}


__all__ = [
    "RECEIVER",
    "drain_refresh",
    "rehydrate_refresh",
    "root_subscription_changed",
    "snapshot_requested",
    "subscription_changed",
]
