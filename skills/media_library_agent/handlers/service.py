from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _bootstrap_core_path() -> None:
    candidates: list[Path] = []
    for name in ("ADAOS_PACKAGE_DIR", "ADAOS_REPO_ROOT"):
        raw = os.environ.get(name)
        if raw:
            path = Path(raw).expanduser()
            candidates.append(path.parent if path.name == "adaos" else path / "src")
    path = Path(__file__).resolve()
    parts = path.parts
    if ".adaos" in parts:
        candidates.append(Path(*parts[: parts.index(".adaos")]) / "src")
    for candidate in candidates:
        if (candidate / "adaos").exists() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
            break


_bootstrap_core_path()

from handlers import main  # noqa: E402


_stop = threading.Event()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        payload = main.status()
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200 if payload.get("ok") else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(raw)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _handle_stop(_signum: int, _frame: Any) -> None:
    _stop.set()


def run() -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    main.recover_interrupted_runtime()
    host = os.environ.get("ADAOS_SERVICE_HOST") or "127.0.0.1"
    port = int(os.environ.get("ADAOS_SERVICE_PORT") or "18106")
    server = ThreadingHTTPServer((host, port), HealthHandler)
    server.timeout = 0.5
    try:
        while not _stop.is_set():
            server.handle_request()
    finally:
        server.server_close()
        main.dispose()


if __name__ == "__main__":
    run()
