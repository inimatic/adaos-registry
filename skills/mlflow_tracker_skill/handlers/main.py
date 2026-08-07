"""AdaOS tools for the supervised MLflow tracking provider."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from adaos.sdk.core.decorators import tool


PROVIDER_ID = "mlflow"
CONTRACT_VERSION = "1.0-rc1"


def _endpoint() -> str:
    return str(os.getenv("ADAOS_MLFLOW_TRACKING_URI") or "http://127.0.0.1:18121").rstrip("/")


@tool("provider_descriptor")
def provider_descriptor() -> dict[str, Any]:
    endpoint = _endpoint()
    return {
        "schema": "adaos.research.tracker_descriptor.v1",
        "provider_id": PROVIDER_ID,
        "contract_version": CONTRACT_VERSION,
        "capabilities": [
            "sessions",
            "typed-scalar-observations",
            "metric-history",
            "dataset-input-tags",
            "artifact-reference-tags",
            "idempotent-journal-projection",
            "native-query-ui",
        ],
        "limits": {
            "max_batch_events": 500,
            "metric_step": "integer",
            "parameter_value_bytes": 6000,
        },
        "tracking_uri": endpoint,
        "ui_url": endpoint,
        "authority": "telemetry-projection",
    }


@tool("provider_health")
def provider_health() -> dict[str, Any]:
    endpoint = _endpoint()
    try:
        with urllib.request.urlopen(f"{endpoint}/health", timeout=2.0) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            ok = 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError) as exc:
        return {
            "ok": False,
            "state": "unavailable",
            "provider": provider_descriptor(),
            "error": type(exc).__name__,
        }
    try:
        upstream = json.loads(body) if body.strip().startswith("{") else {"body": body}
    except json.JSONDecodeError:
        upstream = {"body": body}
    return {
        "ok": ok,
        "state": "ready" if ok else "degraded",
        "provider": provider_descriptor(),
        "upstream": upstream,
    }


@tool("get_tracking_ui")
def get_tracking_ui() -> dict[str, Any]:
    return {
        "schema": "adaos.research.tracker_ui_link.v1",
        "provider_id": PROVIDER_ID,
        "url": _endpoint(),
        "presentation": "external-tab",
        "embedded": False,
        "reason": "MLflow security headers and independent navigation make a top-level view the stable integration boundary.",
    }


@tool("rehydrate")
def rehydrate() -> dict[str, Any]:
    return provider_health()
