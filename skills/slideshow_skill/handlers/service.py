from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _bootstrap_runtime_env() -> None:
    current_env = os.environ.get("ADAOS_SKILL_ENV_PATH") or ""
    current_memory = os.environ.get("ADAOS_SKILL_MEMORY_PATH") or ""
    if "slideshow_skill" in current_env and "slideshow_skill" in current_memory:
        return
    path = Path(__file__).resolve()
    parts = path.parts
    try:
        idx = parts.index(".runtime")
    except ValueError:
        return
    if len(parts) <= idx + 2:
        return
    runtime_root = Path(*parts[: idx + 3])
    skill_env = runtime_root / "data" / "db" / "skill_env.json"
    os.environ["ADAOS_SKILL_ENV_PATH"] = str(skill_env)
    os.environ["ADAOS_SKILL_MEMORY_PATH"] = str(skill_env)
    os.environ["ADAOS_SKILL_NAME"] = "slideshow_skill"
    os.environ["ADAOS_SKILL_INTERNAL_DATA_ROOT"] = str(runtime_root / "data" / "internal" / "slideshow_skill")


def _bootstrap_core_path() -> None:
    candidates: list[Path] = []
    for env_name in ("ADAOS_PACKAGE_DIR", "ADAOS_REPO_ROOT"):
        raw = os.environ.get(env_name)
        if raw:
            path = Path(raw).expanduser()
            candidates.append(path.parent if path.name == "adaos" else path / "src")

    path = Path(__file__).resolve()
    parts = path.parts
    try:
        idx = parts.index(".adaos")
    except ValueError:
        idx = -1
    if idx > 0:
        candidates.append(Path(*parts[:idx]) / "src")

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if not (resolved / "adaos").exists():
            continue
        token = str(resolved)
        if token not in sys.path:
            sys.path.insert(0, token)
        return


_bootstrap_runtime_env()
_bootstrap_core_path()

from handlers import main

_log = logging.getLogger("skills.slideshow_skill.service")
_stop = threading.Event()


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        state = main._load_state()
        body = {
            "ok": True,
            "skill": "slideshow_skill",
            "running": bool(state.get("running")),
            "selected_codes": state.get("selected_codes") or [],
            "current_index": state.get("current_index") or 0,
            "last_surface_sync_reason": state.get("last_surface_sync_reason") or "",
            "memory_path": os.environ.get("ADAOS_SKILL_ENV_PATH") or "",
            "updated_at": main._now(),
        }
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _serve_health() -> None:
    host = os.environ.get("ADAOS_SERVICE_HOST") or "127.0.0.1"
    port = int(os.environ.get("ADAOS_SERVICE_PORT") or "18104")
    server = ThreadingHTTPServer((host, port), _HealthHandler)
    server.timeout = 0.5
    try:
        while not _stop.is_set():
            server.handle_request()
    finally:
        server.server_close()


def _handle_stop(_signum: int, _frame: Any) -> None:
    _stop.set()


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    health_thread = threading.Thread(target=_serve_health, name="slideshow-health", daemon=True)
    health_thread.start()
    _log.info("slideshow service started")
    webspace_id = main.default_webspace_id()
    main._ensure_polling(webspace_id, force=True)
    main._poll_once(webspace_id)
    _stop.wait()
    main.dispose(reason="service_stopped")
    _log.info("slideshow service stopped")


if __name__ == "__main__":
    run()
