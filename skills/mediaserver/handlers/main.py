from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from adaos.sdk.core.decorators import tool
from adaos.services.media_library import (
    MEDIA_LIBRARY_DEFAULT_PAGE_SIZE,
    MEDIA_LIBRARY_MAX_PAGE_SIZE,
    list_media_files,
    list_media_files_page,
    media_capabilities,
    media_library_summary,
    media_runtime_snapshot,
)

REQUIRES_DATA_PROJECTIONS = ["mediaserver.library_summary"]
PROJECTION_SLOT = "mediaserver.library_summary"
PROJECTION_PATH = "data/media/library_summary"
LIBRARY_PAGE_TOOL = "mediaserver.list_library_page"
PROJECTION_BUDGET_HINT = {
    "max_payload_bytes": 65536,
    "max_items": 0,
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
    count = _safe_int(snapshot.get("count"))
    if not count:
        items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
        count = len(items)
    capabilities = snapshot.get("capabilities") if isinstance(snapshot.get("capabilities"), dict) else {}
    return {
        "title": "Media Server",
        "value": count,
        "subtitle": f"{count} media files",
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
    status = "ready"
    return {
        "id": "mediaserver.full_list_projection_contract",
        "title": "Current mediaserver projection shape",
        "status": status,
        "icon": _status_icon(status),
        "subtitle": f"{item_count} rows indexed outside Yjs; {total_bytes} bytes on disk",
        "details": {
            "owner": "skill:mediaserver",
            "slot": PROJECTION_SLOT,
            "path": PROJECTION_PATH,
            "current_shape": "constant_size_summary",
            "budget_hint": PROJECTION_BUDGET_HINT,
            "page_route": LIBRARY_PAGE_TOOL,
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
    summary = _library_scan_summary()
    item_count = _safe_int(summary.get("count"))
    total_bytes = _safe_int(summary.get("total_bytes"))
    runtime = _diagnostic_runtime(summary)
    reliability, reliability_error = _safe_reliability_payload(webspace_id)
    reliability_runtime = (
        reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    )
    rows = [
        _projection_contract_row(item_count=item_count, total_bytes=total_bytes),
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
            "value": item_count,
            "subtitle": f"{item_count} media rows behind bounded page route",
            "details": f"{total_bytes} bytes on disk; Yjs slot {PROJECTION_SLOT}",
            "status": "ready",
        },
        "items": rows,
        "media": {
            "projection": {
                "owner": "skill:mediaserver",
                "slot": PROJECTION_SLOT,
                "path": PROJECTION_PATH,
                "item_total": item_count,
                "total_bytes": total_bytes,
                "budget_hint": PROJECTION_BUDGET_HINT,
                "shape": "constant_size_summary",
                "page_route": LIBRARY_PAGE_TOOL,
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
            "Keep Yjs limited to count, bytes, freshness, capability, and route contract fields.",
            "Use mediaserver.list_library_page for browser rows and searches.",
            "Watch reliability guard cards after large-library stress runs.",
        ],
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _library_scan_summary() -> dict[str, Any]:
    try:
        summary = media_library_summary()
        if isinstance(summary, dict):
            return {
                "count": _safe_int(summary.get("count")),
                "total_bytes": _safe_int(summary.get("total_bytes")),
                "latest_modified_at": str(summary.get("latest_modified_at") or ""),
            }
    except Exception:
        pass
    items = list_media_files()
    latest_modified_at = ""
    if items:
        latest_modified_at = max(str(item.get("modified_at") or "") for item in items)
    return {
        "count": len(items),
        "total_bytes": sum(_safe_int(item.get("size_bytes")) for item in items),
        "latest_modified_at": latest_modified_at,
    }


def _summary_runtime(summary: dict[str, Any]) -> dict[str, Any]:
    runtime = media_runtime_snapshot([])
    counts = runtime.get("counts") if isinstance(runtime.get("counts"), dict) else {}
    assessment = runtime.get("assessment") if isinstance(runtime.get("assessment"), dict) else {}
    paths = runtime.get("paths") if isinstance(runtime.get("paths"), dict) else {}
    compact_paths: dict[str, Any] = {}
    for name in ("direct_local_http", "hub_http_proxy", "member_browser_direct", "hub_webrtc_loopback"):
        route = paths.get(name) if isinstance(paths.get(name), dict) else {}
        if route:
            compact_paths[name] = {
                "ready": bool(route.get("ready")),
                "reason": route.get("reason"),
                "url": route.get("url"),
            }
    return {
        "available": bool(runtime.get("available")),
        "recommended_path": runtime.get("recommended_path"),
        "selection_reason": runtime.get("selection_reason"),
        "assessment": {
            "state": assessment.get("state"),
            "reason": assessment.get("reason"),
        },
        "counts": {
            **counts,
            "file_total": _safe_int(summary.get("count")),
            "total_bytes": _safe_int(summary.get("total_bytes")),
        },
        "paths": compact_paths,
    }


def _diagnostic_runtime(summary: dict[str, Any]) -> dict[str, Any]:
    runtime = media_runtime_snapshot([])
    counts = runtime.get("counts") if isinstance(runtime.get("counts"), dict) else {}
    runtime["counts"] = {
        **counts,
        "file_total": _safe_int(summary.get("count")),
        "total_bytes": _safe_int(summary.get("total_bytes")),
    }
    return runtime


def _compact_capabilities() -> dict[str, Any]:
    capabilities = media_capabilities()
    upload = capabilities.get("upload") if isinstance(capabilities.get("upload"), dict) else {}
    playback = capabilities.get("playback") if isinstance(capabilities.get("playback"), dict) else {}
    broadcast = capabilities.get("broadcast") if isinstance(capabilities.get("broadcast"), dict) else {}
    return {
        "status": "ready",
        "state": "ready",
        "upload": {
            "ready": bool(upload.get("ready", True)),
            "max_bytes": upload.get("max_bytes"),
        },
        "playback": {
            "ready": bool(playback.get("ready", True)),
            "mode": playback.get("mode"),
        },
        "broadcast": {
            "ready": bool(broadcast.get("ready")),
            "mode": broadcast.get("mode"),
            "reason": broadcast.get("reason"),
            "peer_total": broadcast.get("peer_total"),
            "connected_peers": broadcast.get("connected_peers"),
        },
    }


def _library_summary_snapshot() -> dict[str, Any]:
    summary = _library_scan_summary()
    capabilities = _compact_capabilities()
    runtime = _summary_runtime(summary)
    count = _safe_int(summary.get("count"))
    total_bytes = _safe_int(summary.get("total_bytes"))
    payload = {
        "ok": True,
        "schema": "mediaserver.library_summary.v1",
        "items": [],
        "count": count,
        "total_bytes": total_bytes,
        "latest_modified_at": str(summary.get("latest_modified_at") or ""),
        "updated_at": _utc_now(),
        "summary": {
            "title": "Media Server",
            "value": count,
            "subtitle": f"{count} media files",
            "details": f"{total_bytes} bytes; rows load via {LIBRARY_PAGE_TOOL}",
            "status": "ready",
        },
        "capabilities": capabilities,
        "runtime": runtime,
        "library": {
            "route": {
                "kind": "skill",
                "name": LIBRARY_PAGE_TOOL,
                "default_limit": MEDIA_LIBRARY_DEFAULT_PAGE_SIZE,
                "max_limit": MEDIA_LIBRARY_MAX_PAGE_SIZE,
                "supports_cursor": True,
                "filters": ["query", "mime_type"],
            },
            "projection": {
                "owner": "skill:mediaserver",
                "slot": PROJECTION_SLOT,
                "path": PROJECTION_PATH,
                "shape": "constant_size_summary",
                "budget_hint": PROJECTION_BUDGET_HINT,
            },
        },
    }
    return payload


def _publish_snapshot(snapshot: dict[str, Any], *, webspace_id: str) -> None:
    from adaos.sdk.data import ctx_subnet

    payload = {**snapshot, "summary": _summary(snapshot)}
    ctx_subnet.set(PROJECTION_SLOT, payload, webspace_id=webspace_id)


def _payload_value(payload: dict[str, Any] | None, key: str, fallback: Any = None) -> Any:
    if isinstance(payload, dict) and key in payload:
        return payload.get(key)
    return fallback


def _safe_limit(value: Any) -> int:
    parsed = _safe_int(value)
    if parsed <= 0:
        parsed = MEDIA_LIBRARY_DEFAULT_PAGE_SIZE
    return min(max(1, parsed), MEDIA_LIBRARY_MAX_PAGE_SIZE)


def _library_page(
    *,
    payload: dict[str, Any] | None = None,
    limit: Any = None,
    offset: Any = None,
    cursor: Any = None,
    query: Any = None,
    mime_type: Any = None,
) -> dict[str, Any]:
    page = list_media_files_page(
        limit=_safe_limit(_payload_value(payload, "limit", _payload_value(payload, "page_size", limit))),
        offset=_safe_int(_payload_value(payload, "offset", offset)),
        cursor=str(_payload_value(payload, "cursor", cursor) or ""),
        query=str(_payload_value(payload, "query", query) or ""),
        mime_type=str(_payload_value(payload, "mime_type", mime_type) or ""),
    )
    items = page.get("items") if isinstance(page.get("items"), list) else []
    pagination = page.get("pagination") if isinstance(page.get("pagination"), dict) else {}
    summary = page.get("summary") if isinstance(page.get("summary"), dict) else {}
    return {
        "ok": True,
        "schema": "mediaserver.library_page.v1",
        "items": items,
        "count": _safe_int(summary.get("count")),
        "total_bytes": _safe_int(summary.get("total_bytes")),
        "pagination": {
            "limit": _safe_int(pagination.get("limit")),
            "offset": _safe_int(pagination.get("offset")),
            "cursor": str(pagination.get("cursor") or ""),
            "next_cursor": str(pagination.get("next_cursor") or ""),
            "has_more": bool(pagination.get("has_more")),
            "total_count": _safe_int(pagination.get("total_count")),
            "scanned_count": _safe_int(pagination.get("scanned_count")),
        },
        "summary": {
            "title": "Media Library",
            "value": _safe_int(summary.get("count")),
            "subtitle": f"{len(items)} rows loaded",
            "details": "bounded page route",
            "query": str(summary.get("query") or ""),
            "mime_type": str(summary.get("mime_type") or ""),
        },
        "capabilities": _compact_capabilities(),
        "runtime": _summary_runtime(
            {
                "count": _safe_int(summary.get("count")),
                "total_bytes": _safe_int(summary.get("total_bytes")),
            }
        ),
    }


@tool(
    "get_snapshot",
    summary="return mediaserver library snapshot and channel capability diagnostics",
    stability="experimental",
    side_effects="runtime_write",
)
def get_snapshot(
    _payload: dict[str, Any] | None = None,
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    snapshot = _library_summary_snapshot()
    _publish_snapshot(snapshot, webspace_id=_webspace_id(webspace_id, _payload))
    return snapshot


@tool(
    "list_library_page",
    summary="return a bounded page of mediaserver library rows",
    stability="experimental",
    side_effects="none",
)
def list_library_page(
    _payload: dict[str, Any] | None = None,
    limit: int | None = None,
    page_size: int | None = None,
    offset: int | None = None,
    cursor: str | None = None,
    query: str | None = None,
    mime_type: str | None = None,
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return _library_page(
        payload=_payload,
        limit=limit if limit is not None else page_size,
        offset=offset,
        cursor=cursor,
        query=query,
        mime_type=mime_type,
    )


@tool(
    "refresh_snapshot",
    summary="publish mediaserver library snapshot and return lightweight ack",
    stability="experimental",
    side_effects="runtime_write",
)
def refresh_snapshot(
    _payload: dict[str, Any] | None = None,
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    snapshot = _library_summary_snapshot()
    _publish_snapshot(snapshot, webspace_id=_webspace_id(webspace_id, _payload))
    return {"ok": True, "summary": _summary(snapshot), "delivery": "yjs_projection"}


@tool(
    "get_diagnostics",
    summary="return compact operator diagnostics for mediaserver Yjs projection pressure",
    stability="experimental",
    side_effects="none",
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
