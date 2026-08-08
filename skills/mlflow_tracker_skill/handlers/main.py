"""AdaOS tools for the supervised MLflow tracking provider."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from adaos.sdk.core.decorators import tool


PROVIDER_ID = "mlflow"
CONTRACT_VERSION = "1.0"


def _endpoint() -> str:
    return str(
        os.getenv("ADAOS_MLFLOW_TRACKING_URI")
        or "http://127.0.0.1:18121/api/services/mlflow_tracker_skill/ui"
    ).rstrip("/")


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
            "max_pending_events": 10000,
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
        with urllib.request.urlopen(f"{endpoint}/version", timeout=2.0) as response:
            provider_version = response.read(256).decode("utf-8", errors="replace").strip().strip('"')
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
        "provider_version": provider_version,
        "capability_probe": bool(provider_version),
    }


@tool("get_tracking_ui")
def get_tracking_ui() -> dict[str, Any]:
    return {
        "schema": "adaos.service.ui_surface.v1",
        "provider_id": PROVIDER_ID,
        "url": "/api/services/mlflow_tracker_skill/ui-bootstrap",
        "presentation": "external-tab",
        "embedded": True,
        "embedding_policy": "same-origin",
        "access": "authenticated",
        "origin_policy": "same-origin",
    }


@tool("rehydrate")
def rehydrate() -> dict[str, Any]:
    return provider_health()
