from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Mapping
from typing import Any, Callable, Protocol, TypeVar, cast


class _Worker(Protocol):
    def dispose(self, *, timeout: float = 5.0) -> Mapping[str, Any] | None: ...


_TWorker = TypeVar("_TWorker", bound=_Worker)
_log = logging.getLogger("adaos.skill.media_center.background")


class MediaCenterBackgroundRuntime:
    """Process-owned background resources shared by lifecycle and live handlers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sync_call_lock = threading.Lock()
        self._bootstrap_key = ""
        self._bootstrap_thread: threading.Thread | None = None
        self._bootstrap_stop = threading.Event()
        self._bootstrap_state = "stopped"
        self._bootstrap_error = ""
        self._agent_sync_path = ""
        self._agent_sync_worker: _Worker | None = None
        self._enrichment_path = ""
        self._enrichment_worker: _Worker | None = None

    def ensure_bootstrap_started(
        self,
        key: str,
        callback: Callable[[], Any],
        *,
        attempts: int = 60,
        retry_seconds: float = 5.0,
    ) -> bool:
        token = str(key or "default")
        retry_limit = max(1, min(int(attempts), 60))
        retry_delay = max(0.1, min(float(retry_seconds), 30.0))
        context = contextvars.copy_context()
        with self._lock:
            thread = self._bootstrap_thread
            if thread is not None and thread.is_alive():
                return False
            if self._bootstrap_key == token and self._bootstrap_state == "ready":
                return False
            self._bootstrap_key = token
            self._bootstrap_stop.clear()
            self._bootstrap_state = "starting"
            self._bootstrap_error = ""

            def run() -> None:
                for attempt in range(1, retry_limit + 1):
                    if self._bootstrap_stop.is_set():
                        with self._lock:
                            self._bootstrap_state = "stopped"
                        return
                    with self._lock:
                        self._bootstrap_state = "running"
                    try:
                        context.run(callback)
                    except Exception as exc:
                        with self._lock:
                            self._bootstrap_error = (
                                f"{type(exc).__name__}: {str(exc)[:300]}"
                            )
                            self._bootstrap_state = (
                                "failed" if attempt >= retry_limit else "retry_wait"
                            )
                        if attempt == 1 or attempt % 6 == 0 or attempt >= retry_limit:
                            _log.warning(
                                "media center runtime bootstrap failed attempt=%s/%s error=%s",
                                attempt,
                                retry_limit,
                                self._bootstrap_error,
                            )
                        if attempt >= retry_limit or self._bootstrap_stop.wait(
                            retry_delay
                        ):
                            return
                        continue
                    with self._lock:
                        self._bootstrap_state = "ready"
                        self._bootstrap_error = ""
                    return

            self._bootstrap_thread = threading.Thread(
                target=run,
                name="media-center-runtime-bootstrap",
                daemon=True,
            )
            self._bootstrap_thread.start()
        return True

    def bootstrap_status(self) -> dict[str, Any]:
        with self._lock:
            thread = self._bootstrap_thread
            return {
                "state": self._bootstrap_state,
                "key": self._bootstrap_key,
                "running": bool(thread is not None and thread.is_alive()),
                "last_error": self._bootstrap_error,
            }

    def run_agent_sync(self, callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._sync_call_lock:
            return callback()

    def agent_sync_worker(
        self,
        path: str,
        factory: Callable[[], _TWorker],
    ) -> _TWorker:
        stale: _Worker | None = None
        with self._lock:
            if self._agent_sync_worker is None or self._agent_sync_path != path:
                stale = self._agent_sync_worker
                self._agent_sync_worker = factory()
                self._agent_sync_path = path
            worker = self._agent_sync_worker
        if stale is not None:
            stale.dispose(timeout=0.2)
        assert worker is not None
        return cast(_TWorker, worker)

    def enrichment_worker(
        self,
        path: str,
        factory: Callable[[], _TWorker],
    ) -> _TWorker:
        stale: _Worker | None = None
        with self._lock:
            if self._enrichment_worker is None or self._enrichment_path != path:
                stale = self._enrichment_worker
                self._enrichment_worker = factory()
                self._enrichment_path = path
            worker = self._enrichment_worker
        if stale is not None:
            stale.dispose(timeout=0.2)
        assert worker is not None
        return cast(_TWorker, worker)

    def agent_sync_status(self) -> dict[str, Any]:
        with self._lock:
            worker = self._agent_sync_worker
        if worker is None:
            return {"state": "stopped", "revision": 0}
        status = getattr(worker, "status", None)
        return dict(status()) if callable(status) else {"state": "unknown", "revision": 0}

    def reset_enrichment(self, *, timeout: float = 30.0) -> dict[str, Any]:
        with self._lock:
            worker = self._enrichment_worker
            self._enrichment_worker = None
            self._enrichment_path = ""
        return self._dispose_worker(worker, timeout=timeout)

    @staticmethod
    def _dispose_worker(worker: _Worker | None, *, timeout: float) -> dict[str, Any]:
        if worker is None:
            return {"stopped": True, "skipped": True}
        result = worker.dispose(timeout=timeout)
        if isinstance(result, Mapping):
            return dict(result)
        return {"stopped": True, "skipped": False}

    def dispose(self, *, timeout: float = 30.0) -> dict[str, Any]:
        self._bootstrap_stop.set()
        with self._lock:
            bootstrap_thread = self._bootstrap_thread
        if (
            bootstrap_thread
            and bootstrap_thread.is_alive()
            and bootstrap_thread is not threading.current_thread()
        ):
            bootstrap_thread.join(timeout=max(0.0, timeout))
        bootstrap_stopped = bootstrap_thread is None or not bootstrap_thread.is_alive()
        if not bootstrap_stopped:
            return {
                "ok": False,
                "stopped": False,
                "bootstrap": {**self.bootstrap_status(), "stopped": False},
                "agent_sync": {"stopped": False, "deferred": True},
                "enrichment": {"stopped": False, "deferred": True},
            }
        with self._lock:
            sync_worker = self._agent_sync_worker
            enrichment_worker = self._enrichment_worker
            self._bootstrap_thread = None
            self._bootstrap_key = ""
            self._bootstrap_state = "stopped"
            self._bootstrap_error = ""
            self._agent_sync_worker = None
            self._agent_sync_path = ""
            self._enrichment_worker = None
            self._enrichment_path = ""
        sync = self._dispose_worker(sync_worker, timeout=timeout)
        enrichment = self._dispose_worker(enrichment_worker, timeout=timeout)
        stopped = bool(sync.get("stopped")) and bool(enrichment.get("stopped"))
        return {
            "ok": stopped,
            "stopped": stopped,
            "bootstrap": {"stopped": True},
            "agent_sync": sync,
            "enrichment": enrichment,
        }


_BACKGROUND_RUNTIME = MediaCenterBackgroundRuntime()


def background_runtime() -> MediaCenterBackgroundRuntime:
    return _BACKGROUND_RUNTIME


__all__ = ["MediaCenterBackgroundRuntime", "background_runtime"]
