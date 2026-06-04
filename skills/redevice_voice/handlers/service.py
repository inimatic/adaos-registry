from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _bootstrap_runtime_env() -> None:
    current_env = os.environ.get("ADAOS_SKILL_ENV_PATH") or ""
    current_memory = os.environ.get("ADAOS_SKILL_MEMORY_PATH") or ""
    if "redevice_voice" in current_env and "redevice_voice" in current_memory:
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
    os.environ["ADAOS_SKILL_NAME"] = "redevice_voice"
    os.environ["ADAOS_SKILL_INTERNAL_DATA_ROOT"] = str(runtime_root / "data" / "internal" / "redevice_voice")


_bootstrap_runtime_env()

from handlers import main

_log = logging.getLogger("skills.redevice_voice.service")
_stop = threading.Event()
_last_api_call: dict[str, Any] = {}


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        state = main._load_state()
        body = {
            "ok": True,
            "skill": "redevice_voice",
            "selected_code": state.get("selected_code") or "",
            "ptt": state.get("ptt") or {},
            "service": state.get("service") or {},
            "last_api_call": _last_api_call,
            "memory_path": os.environ.get("ADAOS_SKILL_ENV_PATH") or "",
            "updated_at": main._now(),
        }
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _serve_health() -> None:
    host = os.environ.get("ADAOS_SERVICE_HOST") or "127.0.0.1"
    port = int(os.environ.get("ADAOS_SERVICE_PORT") or "18107")
    server = ThreadingHTTPServer((host, port), _HealthHandler)
    server.timeout = 0.5
    try:
        while not _stop.is_set():
            server.handle_request()
    finally:
        server.server_close()


def _handle_stop(_signum: int, _frame: Any) -> None:
    _stop.set()


def _api_base() -> str:
    return (os.environ.get("ADAOS_API_BASE") or os.environ.get("ADAOS_CONTROL_BASE_URL") or "http://127.0.0.1:8777").rstrip("/")


def _api_token() -> str:
    return (os.environ.get("ADAOS_TOKEN") or os.environ.get("ADAOS_HUB_TOKEN") or "dev-local-token").strip()


def _refresh_via_api() -> None:
    global _last_api_call
    payload = {
        "tool": "redevice_voice:refresh_redevice_voice_state",
        "args": {
            "webspace_id": "desktop",
            "service_poll": True,
        },
    }
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _api_base() + "/api/tools/call",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-AdaOS-Token": _api_token(),
        },
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            status = int(getattr(resp, "status", 0) or resp.getcode())
            body = resp.read(4096).decode("utf-8", errors="replace")
        _last_api_call = {
            "ok": 200 <= status < 300,
            "status": status,
            "duration_ms": int((time.time() - started) * 1000),
            "updated_at": main._now(),
        }
        if status >= 300:
            _last_api_call["body"] = body[:500]
    except Exception as exc:
        _last_api_call = {
            "ok": False,
            "error": str(exc),
            "duration_ms": int((time.time() - started) * 1000),
            "updated_at": main._now(),
        }
        _log.debug("ReDevice voice service API refresh failed", exc_info=True)


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    health_thread = threading.Thread(target=_serve_health, name="redevice-voice-health", daemon=True)
    health_thread.start()
    _log.info("ReDevice voice service started")
    _refresh_via_api()
    while not _stop.wait(main._POLL_INTERVAL_S):
        _refresh_via_api()
    _log.info("ReDevice voice service stopped")


if __name__ == "__main__":
    run()
