"""Small supervised readiness process; domain calls remain AdaOS tools."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/health", "/status"}:
            self.send_error(404)
            return
        payload = json.dumps(
            {
                "ok": True,
                "service": "research_manager_skill",
                "protocol_version": "1.0",
                "state_authority": "adaos-tool-sdk",
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    host = str(os.getenv("ADAOS_SERVICE_HOST") or "127.0.0.1")
    port = int(os.getenv("ADAOS_SERVICE_PORT") or "18120")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
