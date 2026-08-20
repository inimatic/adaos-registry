from __future__ import annotations

import threading
from typing import Any, Callable, Protocol, TypeVar, cast


class _Worker(Protocol):
    def dispose(self, *, timeout: float = 5.0) -> None: ...


_TWorker = TypeVar("_TWorker", bound=_Worker)


class MediaCenterBackgroundRuntime:
    """Process-owned background resources shared by lifecycle and live handlers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sync_call_lock = threading.Lock()
        self._agent_sync_path = ""
        self._agent_sync_worker: _Worker | None = None
        self._enrichment_path = ""
        self._enrichment_worker: _Worker | None = None

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

    def dispose(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            sync_worker = self._agent_sync_worker
            enrichment_worker = self._enrichment_worker
            self._agent_sync_worker = None
            self._agent_sync_path = ""
            self._enrichment_worker = None
            self._enrichment_path = ""
        if sync_worker is not None:
            sync_worker.dispose(timeout=timeout)
        if enrichment_worker is not None:
            enrichment_worker.dispose(timeout=timeout)


_BACKGROUND_RUNTIME = MediaCenterBackgroundRuntime()


def background_runtime() -> MediaCenterBackgroundRuntime:
    return _BACKGROUND_RUNTIME


__all__ = ["MediaCenterBackgroundRuntime", "background_runtime"]
