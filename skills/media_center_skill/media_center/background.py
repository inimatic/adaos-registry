from __future__ import annotations

import contextvars
import logging
import threading
from collections import OrderedDict
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
        self._projection_condition = threading.Condition()
        self._projections: OrderedDict[str, tuple[Any, Any]] = OrderedDict()
        self._projection_thread: threading.Thread | None = None
        self._projection_stopping = False
        self._projection_error = ""

    def queue_projection(self, key: str, callback: Callable[[], Any]) -> bool:
        """Coalesce routes into one bounded, process-owned projection lane."""
        context = contextvars.copy_context()
        with self._projection_condition:
            if self._projection_stopping:
                return False
            self._projections.pop(key, None)
            self._projections[key] = (context, callback)
            while len(self._projections) > 32:
                self._projections.popitem(last=False)
            if self._projection_thread is None or not self._projection_thread.is_alive():
                self._projection_thread = threading.Thread(
                    target=self._projection_loop, name="media-center-projection", daemon=True)
                self._projection_thread.start()
            self._projection_condition.notify_all()
        return True

    def _projection_loop(self) -> None:
        while True:
            with self._projection_condition:
                while not self._projections and not self._projection_stopping:
                    self._projection_condition.wait(1.0)
                if self._projection_stopping:
                    return
                _key, (context, callback) = self._projections.popitem(last=False)
            try:
                context.run(callback)
            except Exception as exc:
                self._projection_error = type(exc).__name__
                _log.warning("media projection failed: %s", self._projection_error)
            else:
                self._projection_error = ""

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
        key: str,
        factory: Callable[[], _TWorker],
    ) -> _TWorker:
        stale: _Worker | None = None
        restart = False
        with self._lock:
            if self._enrichment_worker is None or self._enrichment_path != key:
                stale = self._enrichment_worker
                status = getattr(stale, "status", None)
                if callable(status):
                    try:
                        restart = str(dict(status()).get("state") or "") == "running"
                    except Exception:
                        restart = False
                self._enrichment_worker = factory()
                self._enrichment_path = key
            worker = self._enrichment_worker
        if stale is not None:
            stale.dispose(timeout=0.2)
        assert worker is not None
        if restart:
            ensure_started = getattr(worker, "ensure_started", None)
            if callable(ensure_started):
                ensure_started()
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

    def enrichment_status(self) -> dict[str, Any]:
        with self._lock:
            worker = self._enrichment_worker
        status = getattr(worker, "status", None)
        return dict(status()) if callable(status) else {"state": "stopped"}

    @staticmethod
    def _dispose_worker(worker: _Worker | None, *, timeout: float) -> dict[str, Any]:
        if worker is None:
            return {"stopped": True, "skipped": True}
        result = worker.dispose(timeout=timeout)
        if isinstance(result, Mapping):
            return dict(result)
        return {"stopped": True, "skipped": False}

    def dispose(self, *, timeout: float = 30.0) -> dict[str, Any]:
        with self._projection_condition:
            self._projection_stopping = True
            self._projections.clear()
            projection_thread = self._projection_thread
            self._projection_condition.notify_all()
        if projection_thread and projection_thread is not threading.current_thread():
            projection_thread.join(timeout=max(0.0, timeout))
        projection_stopped = projection_thread is None or not projection_thread.is_alive()
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
        stopped = bool(sync.get("stopped")) and bool(enrichment.get("stopped")) and projection_stopped
        if projection_stopped:
            with self._projection_condition:
                self._projection_thread = None
                self._projection_stopping = False
        return {
            "ok": stopped,
            "stopped": stopped,
            "bootstrap": {"stopped": True},
            "agent_sync": sync,
            "enrichment": enrichment,
            "projection": {"stopped": projection_stopped},
        }


_BACKGROUND_RUNTIME = MediaCenterBackgroundRuntime()


def background_runtime() -> MediaCenterBackgroundRuntime:
    return _BACKGROUND_RUNTIME


__all__ = ["MediaCenterBackgroundRuntime", "background_runtime"]
