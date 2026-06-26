from __future__ import annotations

from typing import Any

from adaos.sdk.core.decorators import tool
from adaos.services.media_library import list_media_files, media_runtime_snapshot, media_snapshot

REQUIRES_DATA_PROJECTIONS = ["mediaserver.library"]
PROJECTION_SLOT = "mediaserver.library"
PROJECTION_PATH = "data/media/library"
PROJECTION_BUDGET_HINT = {
    "max_payload_bytes": 65536,
    "max_items": 1,
    "target_shape": "constant_size_summary",
}


def _webspace_id(webspace_id: str | None = None, payload: dict[str, Any] | None = None) -> str:
    token = str(webspace_id or "").strip()
    if token:
        return token
    if isinstance(payload, dict):
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        token = str(payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or "").strip()
        if token:
            return token
    try:
        from adaos.services.yjs.webspace import default_webspace_id

        return default_webspace_id()
    except Exception:
        return "default"


def _summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
    capabilities = snapshot.get("capabilities") if isinstance(snapshot.get("capabilities"), dict) else {}
    return {
        "title": "Media Server",
        "value": len(items),
        "subtitle": f"{len(items)} media files",
        "details": str(capabilities.get("state") or capabilities.get("status") or "ready"),
    }


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _status_icon(status: str) -> str:
    token = str(status or "").strip().lower()
    if token in {"ready", "ok", "online"}:
        return "checkmark-circle-outline"
    if token in {"degraded", "warning", "warn"}:
        return "warning-outline"
    if token in {"down", "offline", "critical", "error"}:
        return "alert-circle-outline"
    return "information-circle-outline"


def _safe_reliability_payload(webspace_id: str) -> tuple[dict[str, Any], str | None]:
    try:
        from adaos.services.system_model.service import current_reliability_payload

        payload = current_reliability_payload(webspace_id=webspace_id)
        return (payload if isinstance(payload, dict) else {}, None)
    except Exception as exc:
        return (
            {
                "ok": False,
                "runtime": {},
                "diagnostic": {
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
            },
            f"{type(exc).__name__}: {exc}",
        )


def _guard_cards(runtime: dict[str, Any], webspace_id: str) -> list[dict[str, Any]]:
    try:
        from adaos.services.status.guard_cards import guard_status_cards_from_runtime

        return [
            card.to_dict()
            for card in guard_status_cards_from_runtime(runtime, webspace_id=webspace_id)
        ]
    except Exception:
        return []


def _projection_guard_row(runtime: dict[str, Any]) -> dict[str, Any]:
    guard = runtime.get("yjs_projection_guard") if isinstance(runtime.get("yjs_projection_guard"), dict) else {}
    totals = guard.get("totals") if isinstance(guard.get("totals"), dict) else {}
    items = guard.get("items") if isinstance(guard.get("items"), list) else []
    top = items[0] if items and isinstance(items[0], dict) else {}
    guarded = _safe_int(totals.get("guarded"))
    payload_bytes = _safe_int(top.get("payload_bytes"))
    max_payload_bytes = _safe_int(top.get("max_payload_bytes"))
    media_related = (
        str(top.get("owner") or "") == "skill:mediaserver"
        or str(top.get("slot") or "") == PROJECTION_SLOT
        or PROJECTION_PATH in str(top.get("path") or "")
    )
    status = "degraded" if guarded or media_related else "ready"
    subtitle = "no projection guard pressure"
    if guarded or payload_bytes:
        subtitle = (
            f"guarded={guarded} bytes={payload_bytes}"
            + (f"/{max_payload_bytes}" if max_payload_bytes else "")
        )
    return {
        "id": "mediaserver.yjs_projection_guard",
        "title": "Yjs projection guard",
        "status": status,
        "icon": _status_icon(status),
        "subtitle": subtitle,
        "details": {
            "field": "runtime.yjs_projection_guard",
            "owner": top.get("owner"),
            "slot": top.get("slot"),
            "path": top.get("path"),
            "reason": top.get("reason") or guard.get("reason"),
            "totals": totals,
            "top": top,
        },
    }


def _yjs_pressure_row(runtime: dict[str, Any]) -> dict[str, Any]:
    pressure = runtime.get("yjs_pressure") if isinstance(runtime.get("yjs_pressure"), dict) else {}
    observed = str(pressure.get("observed_state") or pressure.get("status") or "unknown").strip().lower()
    reason = str(pressure.get("reason") or pressure.get("last_reason") or "no yjs pressure reason").strip()
    status = "ready" if observed in {"ready", "ok", "healthy"} else "warning" if observed == "unknown" else "degraded"
    return {
        "id": "mediaserver.yjs_pressure",
        "title": "Yjs pressure",
        "status": status,
        "icon": _status_icon(status),
        "subtitle": reason[:160],
        "details": {
            "field": "runtime.yjs_pressure",
            "observed_state": observed,
            "payload": pressure,
        },
    }


def _stream_guard_row(runtime: dict[str, Any]) -> dict[str, Any]:
    guard = runtime.get("webio_stream_guard") if isinstance(runtime.get("webio_stream_guard"), dict) else {}
    totals = guard.get("totals") if isinstance(guard.get("totals"), dict) else {}
    suppressed = _safe_int(totals.get("suppressed"))
    throttled = _safe_int(totals.get("throttled"))
    status = "degraded" if suppressed else "warning" if throttled else "ready"
    return {
        "id": "mediaserver.webio_stream_guard",
        "title": "WebIO stream guard",
        "status": status,
        "icon": _status_icon(status),
        "subtitle": f"suppressed={suppressed} throttled={throttled}",
        "details": {
            "field": "runtime.webio_stream_guard",
            "totals": totals,
            "payload": guard,
        },
    }


def _projection_contract_row(*, item_count: int, total_bytes: int) -> dict[str, Any]:
    status = "warning" if item_count else "ready"
    return {
        "id": "mediaserver.full_list_projection_contract",
        "title": "Current mediaserver projection shape",
        "status": status,
        "icon": _status_icon(status),
        "subtitle": f"{item_count} rows, {total_bytes} bytes; Yjs path {PROJECTION_PATH}",
        "details": {
            "owner": "skill:mediaserver",
            "slot": PROJECTION_SLOT,
            "path": PROJECTION_PATH,
            "current_shape": "full_items_projection",
            "budget_hint": PROJECTION_BUDGET_HINT,
            "repair_route": "Publish only summary/counts to Yjs and move rows behind page/search/detail routes.",
        },
    }


def _media_runtime_row(runtime: dict[str, Any]) -> dict[str, Any]:
    assessment = runtime.get("assessment") if isinstance(runtime.get("assessment"), dict) else {}
    counts = runtime.get("counts") if isinstance(runtime.get("counts"), dict) else {}
    status = "ready" if bool(runtime.get("available")) else "warning"
    return {
        "id": "mediaserver.media_runtime",
        "title": "Media runtime",
        "status": status,
        "icon": _status_icon(status),
        "subtitle": str(assessment.get("reason") or assessment.get("state") or "media runtime snapshot"),
        "details": {
            "recommended_path": runtime.get("recommended_path"),
            "counts": counts,
            "paths": runtime.get("paths"),
        },
    }


def _build_diagnostics(webspace_id: str) -> dict[str, Any]:
    items = list_media_files()
    total_bytes = sum(_safe_int(item.get("size_bytes")) for item in items)
    runtime = media_runtime_snapshot(items)
    reliability, reliability_error = _safe_reliability_payload(webspace_id)
    reliability_runtime = (
        reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    )
    rows = [
        _projection_contract_row(item_count=len(items), total_bytes=total_bytes),
        _projection_guard_row(reliability_runtime),
        _yjs_pressure_row(reliability_runtime),
        _stream_guard_row(reliability_runtime),
        _media_runtime_row(runtime),
    ]
    if reliability_error:
        rows.append(
            {
                "id": "mediaserver.reliability_unavailable",
                "title": "Reliability snapshot unavailable",
                "status": "warning",
                "icon": _status_icon("warning"),
                "subtitle": reliability_error[:160],
                "details": {"error": reliability_error},
            }
        )
    return {
        "ok": True,
        "schema": "mediaserver.diagnostics.v1",
        "webspace_id": webspace_id,
        "summary": {
            "title": "Media diagnostics",
            "value": len(items),
            "subtitle": f"{len(items)} media rows in current full-list projection",
            "details": f"{total_bytes} bytes on disk; Yjs slot {PROJECTION_SLOT}",
            "status": "warning" if len(items) else "ready",
        },
        "items": rows,
        "media": {
            "projection": {
                "owner": "skill:mediaserver",
                "slot": PROJECTION_SLOT,
                "path": PROJECTION_PATH,
                "item_total": len(items),
                "total_bytes": total_bytes,
                "budget_hint": PROJECTION_BUDGET_HINT,
            },
            "runtime": runtime,
        },
        "reliability": {
            "available": reliability_error is None,
            "node": reliability.get("node") if isinstance(reliability.get("node"), dict) else {},
            "guard_cards": _guard_cards(reliability_runtime, webspace_id),
            "yjs_projection_guard": reliability_runtime.get("yjs_projection_guard"),
            "yjs_pressure": reliability_runtime.get("yjs_pressure"),
            "webio_stream_guard": reliability_runtime.get("webio_stream_guard"),
            "state_sync": reliability_runtime.get("state_sync"),
        },
        "next_actions": [
            "Keep the diagnostic UI as the evidence surface while core Yjs projection guards are hardened.",
            "Migrate mediaserver.library to a constant-size summary after guard visibility is proven.",
            "Move full media rows behind bounded page/search/detail routes.",
        ],
    }


def _publish_snapshot(snapshot: dict[str, Any], *, webspace_id: str) -> None:
    from adaos.sdk.data import ctx_subnet

    payload = {**snapshot, "summary": _summary(snapshot)}
    ctx_subnet.set(PROJECTION_SLOT, payload, webspace_id=webspace_id)


@tool(
    "get_snapshot",
    summary="return mediaserver library snapshot and channel capability diagnostics",
    stability="experimental",
)
def get_snapshot(
    _payload: dict[str, Any] | None = None,
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    snapshot = media_snapshot()
    _publish_snapshot(snapshot, webspace_id=_webspace_id(webspace_id, _payload))
    return snapshot


@tool(
    "refresh_snapshot",
    summary="publish mediaserver library snapshot and return lightweight ack",
    stability="experimental",
)
def refresh_snapshot(
    _payload: dict[str, Any] | None = None,
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    snapshot = media_snapshot()
    _publish_snapshot(snapshot, webspace_id=_webspace_id(webspace_id, _payload))
    return {"ok": True, "summary": _summary(snapshot), "delivery": "yjs_projection"}


@tool(
    "get_diagnostics",
    summary="return compact operator diagnostics for mediaserver Yjs projection pressure",
    stability="experimental",
)
def get_diagnostics(
    _payload: dict[str, Any] | None = None,
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return _build_diagnostics(_webspace_id(webspace_id, _payload))


def handle(_topic: str, _payload: dict[str, Any]) -> None:
    return None
