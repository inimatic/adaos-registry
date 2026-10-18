"""Interactive read scope: no migrations, no writes and a shared SQL deadline."""
from __future__ import annotations

import contextvars
import functools
import sqlite3
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError, wait

_deadline = contextvars.ContextVar("media_center_read_deadline", default=0.0)


class ReadModelUnavailable(RuntimeError):
    pass


class BoundedReadExecutor:
    """Bound caller latency even when the kernel cannot interrupt disk I/O."""
    def __init__(self, workers=2):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="media-center-read")
        self._slots = threading.BoundedSemaphore(workers)
        self._lock = threading.Lock()
        self._pending = set()
        self._stopping = False

    def run(self, callback, *, timeout=0.25):
        with self._lock:
            if self._stopping or not self._slots.acquire(blocking=False):
                raise ReadModelUnavailable("read_lane_busy")
            context = contextvars.copy_context()
            try:
                future = self._pool.submit(context.run, callback)
            except Exception:
                self._slots.release()
                raise
            self._pending.add(future)
        def finished(completed):
            with self._lock:
                self._pending.discard(completed)
            self._slots.release()
        future.add_done_callback(finished)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            # No replacement worker is admitted until this exact read exits.
            raise ReadModelUnavailable("read_deadline") from exc

    def dispose(self, *, timeout=5.0):
        with self._lock:
            self._stopping = True
            pending = set(self._pending)
        _done, remaining = wait(pending, timeout=max(0.0, timeout))
        self._pool.shutdown(wait=False, cancel_futures=True)
        return {"stopped": not remaining, "inflight": len(remaining)}


_executor = None
_executor_lock = threading.Lock()


def _read_executor():
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = BoundedReadExecutor()
        return _executor


def dispose_read_models(*, timeout=5.0):
    global _executor
    with _executor_lock:
        executor = _executor
    if executor is None:
        return {"stopped": True, "inflight": 0}
    result = executor.dispose(timeout=timeout)
    if result["stopped"]:
        with _executor_lock:
            if _executor is executor:
                _executor = None
    return result


def read_deadline() -> float:
    return _deadline.get()


def interactive_read(function):
    @functools.wraps(function)
    def bounded(*args, **kwargs):
        # Nested read helpers share the original budget, not a fresh one.
        previous = _deadline.get()
        if previous:
            return function(*args, **kwargs)
        deadline = time.monotonic() + 0.25
        def execute():
            token = _deadline.set(deadline)
            try:
                return function(*args, **kwargs)
            finally:
                _deadline.reset(token)
        try:
            return _read_executor().run(execute)
        except (ReadModelUnavailable, sqlite3.OperationalError) as exc:
            if isinstance(exc, sqlite3.OperationalError) and not any(
                value in str(exc).lower() for value in ("locked", "busy", "interrupted")
            ):
                raise
            return {"ok": True, "state": "loading", "ready": False,
                    "reason": "catalog_read_temporarily_unavailable",
                    "retry_after_ms": 1000, "items": [], "count": 0}
    return bounded


__all__ = ["ReadModelUnavailable", "BoundedReadExecutor", "dispose_read_models", "interactive_read", "read_deadline"]
