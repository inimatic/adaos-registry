from __future__ import annotations

import contextvars
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MediaAgentSyncWorker:
    """Own bounded, cursor-based background catch-up for distributed agents."""

    def __init__(
        self,
        sync: Callable[[], Mapping[str, Any]],
        *,
        publish: Callable[[], None] | None = None,
        poll_seconds: float = 30.0,
        retry_seconds: float = 5.0,
        catchup_seconds: float = 0.25,
    ) -> None:
        self.sync = sync
        self.publish = publish
        self.poll_seconds = max(1.0, min(float(poll_seconds), 300.0))
        self.retry_seconds = max(0.5, min(float(retry_seconds), 60.0))
        self.catchup_seconds = max(0.05, min(float(catchup_seconds), 5.0))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._context = contextvars.copy_context()
        self._revision = 0
        self._state = "idle"
        self._last_result: dict[str, Any] = {}
        self._updated_at = _now()

    def ensure_started(self, *, wake: bool = False) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if wake:
                    self._wake.set()
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="media-center-agent-sync",
                daemon=True,
            )
            self._thread.start()
        return True

    def dispose(self, *, timeout: float = 30.0) -> dict[str, Any]:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return {"stopped": stopped, "worker": "agent_sync"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "revision": self._revision,
                "updated_at": self._updated_at,
                "last_result": dict(self._last_result),
            }

    def run_once(self) -> dict[str, Any]:
        with self._lock:
            self._state = "running"
            self._revision += 1
            self._updated_at = _now()
        try:
            raw = self._context.run(self.sync)
            result = dict(raw) if isinstance(raw, Mapping) else {
                "ok": False,
                "error": "media_agent_sync_invalid_result",
            }
        except Exception:
            result = {"ok": False, "error": "media_agent_sync_failed"}
        summary = {
            key: result[key]
            for key in (
                "ok",
                "mode",
                "agent_count",
                "applied_count",
                "has_more",
                "error",
            )
            if key in result
        }
        with self._lock:
            previous_summary = dict(self._last_result)
            self._state = (
                "catching_up"
                if result.get("ok") and result.get("has_more")
                else "idle"
                if result.get("ok")
                else "retry_wait"
            )
            self._last_result = summary
            self._revision += 1
            self._updated_at = _now()
        publish_required = bool(
            not previous_summary
            or summary != previous_summary
            or int(result.get("applied_count") or 0) > 0
            or result.get("has_more")
        )
        if publish_required and self.publish is not None:
            try:
                self._context.run(self.publish)
            except Exception:
                pass
        return result

    def _loop(self) -> None:
        while not self._stop.is_set():
            result = self.run_once()
            delay = (
                self.catchup_seconds
                if result.get("ok") and result.get("has_more")
                else self.poll_seconds
                if result.get("ok")
                else self.retry_seconds
            )
            self._wake.wait(delay)
            self._wake.clear()


__all__ = ["MediaAgentSyncWorker"]
