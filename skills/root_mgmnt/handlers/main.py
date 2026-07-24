from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Mapping, Optional
from urllib.parse import quote

import requests

from adaos.domain.types import Event
from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet
from adaos.services.agent_context import get_ctx
from adaos.services.root.client import RootHttpClient

_log = logging.getLogger("skills.root_mgmnt")
_CACHE_TTL_S = float(str(os.getenv("ADAOS_ROOT_MGMNT_CACHE_TTL") or "5").strip() or "5")
_STALE_MAX_AGE_S = float(str(os.getenv("ADAOS_ROOT_MGMNT_STALE_MAX_AGE") or "120").strip() or "120")
_REQUEST_TIMEOUT_S = float(str(os.getenv("ADAOS_ROOT_MGMNT_TIMEOUT") or "4.5").strip() or "4.5")
_SNAPSHOT_CACHE: dict[str, Any] = {"ts": 0.0, "value": None}
_SNAPSHOT_FETCH_LOCK = threading.Lock()
_PROJECTION_LOCK = threading.Lock()
_PROJECTION_DIGESTS: dict[str, str] = {}
_ACTIVE_WEBSPACES: set[str] = set()
_EVENT_STREAM_STOP = threading.Event()
_EVENT_STREAM_THREAD: threading.Thread | None = None
_EVENT_STREAM_LOCK = threading.Lock()


def lang_res() -> Dict[str, str]:
    return {}


def _root_base_url() -> str:
    for env_name in (
        "ADAOS_ROOT_MGMNT_BASE_URL",
        "ROOT_MGMNT_BASE_URL",
        "ADAOS_ROOT_API_BASE",
        "ROOT_API_BASE",
    ):
        raw = str(os.getenv(env_name) or "").strip().rstrip("/")
        if raw:
            return raw
    try:
        ctx = get_ctx()
        api_base = str(getattr(getattr(ctx, "settings", None), "api_base", "") or "").strip().rstrip("/")
        if api_base:
            return api_base
    except Exception:
        pass
    proto = str(os.getenv("ROOT_SERVER_PROTO") or os.getenv("SERVER_PROTO") or "http").strip().lower() or "http"
    host = str(os.getenv("ROOT_MGMNT_LOCAL_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = str(os.getenv("PORT") or "3030").strip() or "3030"
    return f"{proto}://{host}:{port}"


def _root_verify(base_url: str) -> str | bool:
    raw = str(os.getenv("ROOT_MGMNT_VERIFY") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    try:
        ctx = get_ctx()
        pki = getattr(getattr(ctx, "settings", None), "pki", None)
        ca_path = str(getattr(pki, "ca", "") or "").strip()
        if ca_path and os.path.exists(ca_path):
            return ca_path
    except Exception:
        pass
    if base_url.startswith("https://127.0.0.1") or base_url.startswith("https://localhost"):
        return False
    return True


def _root_token() -> str:
    return str(os.getenv("ROOT_MGMNT_TOKEN") or os.getenv("ROOT_TOKEN") or "dev-root-token").strip() or "dev-root-token"


def _client() -> RootHttpClient:
    base_url = _root_base_url()
    return RootHttpClient(
        base_url=base_url,
        verify=_root_verify(base_url),
        timeout=_REQUEST_TIMEOUT_S,
        default_headers={
            "X-Root-Mgmnt-Token": _root_token(),
            "X-Root-Mgmnt-Actor": "root_mgmnt.skill",
        },
    )


def _invalidate_cache() -> None:
    _SNAPSHOT_CACHE["ts"] = 0.0
    _SNAPSHOT_CACHE["value"] = None


def _copy_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return dict(snapshot) if isinstance(snapshot, Mapping) else {}


def _read_cached_snapshot(*, max_age_s: float | None = None) -> dict[str, Any] | None:
    cached = _SNAPSHOT_CACHE.get("value")
    cached_ts = float(_SNAPSHOT_CACHE.get("ts") or 0.0)
    if not isinstance(cached, Mapping):
        return None
    if max_age_s is not None and cached_ts > 0 and (time.time() - cached_ts) > max_age_s:
        return None
    return _copy_snapshot(cached)


def _snapshot_meta(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": snapshot.get("generated_at"),
        "stale": bool(snapshot.get("stale")),
        "warning": str(snapshot.get("warning") or "").strip() or None,
        "error": str(snapshot.get("error") or "").strip() or None,
    }


def _snapshot(force: bool = False) -> dict[str, Any]:
    if not force:
        cached = _read_cached_snapshot(max_age_s=_CACHE_TTL_S)
        if cached is not None:
            return cached
    with _SNAPSHOT_FETCH_LOCK:
        if not force:
            cached = _read_cached_snapshot(max_age_s=_CACHE_TTL_S)
            if cached is not None:
                return cached
        payload = _client().request("GET", "/v1/root_mgmnt/snapshot", timeout=_REQUEST_TIMEOUT_S)
        snapshot = dict(payload) if isinstance(payload, Mapping) else {"ok": False, "error": "invalid_snapshot"}
        snapshot.pop("stale", None)
        snapshot.pop("warning", None)
        _SNAPSHOT_CACHE["ts"] = time.time()
        _SNAPSHOT_CACHE["value"] = snapshot
        return dict(snapshot)


def _snapshot_or_fallback(force: bool = False) -> dict[str, Any]:
    try:
        return _snapshot(force=force)
    except Exception as exc:
        _log.warning("root_mgmnt snapshot failed", exc_info=True)
        stale = _read_cached_snapshot(max_age_s=_STALE_MAX_AGE_S)
        if stale is not None:
            stale["stale"] = True
            stale["warning"] = "showing cached root snapshot"
            stale["error"] = f"{type(exc).__name__}: {exc}"
            stale["stale_age_s"] = round(max(0.0, time.time() - float(_SNAPSHOT_CACHE.get("ts") or 0.0)), 3)
            return stale
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "warning": "root snapshot unavailable",
            "overview": {},
            "policy": {},
            "fleet": [],
            "lifecycle_candidates": [],
            "audit": [],
        }


def _fleet(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = snapshot.get("fleet")
    return [dict(item) for item in items] if isinstance(items, list) else []


def _lifecycle_candidates(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = snapshot.get("lifecycle_candidates")
    return [dict(item) for item in items] if isinstance(items, list) else []


def _audit(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = snapshot.get("audit")
    return [dict(item) for item in items] if isinstance(items, list) else []


def _policy(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    policy = snapshot.get("policy")
    return dict(policy) if isinstance(policy, Mapping) else {}


def _overview(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    overview = snapshot.get("overview")
    return dict(overview) if isinstance(overview, Mapping) else {}


def _find_subnet(snapshot: Mapping[str, Any], subnet_id: str | None) -> dict[str, Any] | None:
    target = str(subnet_id or "").strip()
    if not target:
        return None
    for item in _fleet(snapshot):
        if str(item.get("subnet_id") or "").strip() == target:
            return item
    return None


def _metric_value(snapshot: Mapping[str, Any], metric_id: str) -> dict[str, Any]:
    overview = _overview(snapshot)
    policy = _policy(snapshot)
    total = int(overview.get("total_subnets") or 0)
    live = int(overview.get("live_subnets") or 0)
    dormant = int(overview.get("dormant_subnets") or 0)
    retirees = int(overview.get("retire_candidates") or 0)
    archive = int(overview.get("archive_candidates") or 0)
    requests_24h = int(overview.get("llm_requests_24h") or 0)
    denied_30d = int(overview.get("llm_denied_30d") or 0)
    if metric_id == "fleet_total":
        return {
            "value": total,
            "label": "Registered subnets",
            "subtitle": f"{live} live / {dormant} dormant",
            "description": f"{archive} still keep forge artifacts.",
        }
    if metric_id == "retire_candidates":
        return {
            "value": retirees,
            "label": "Retire candidates",
            "subtitle": "Lifecycle queue",
            "description": "Subnets with long inactivity and no recent LLM traffic.",
        }
    if metric_id == "llm_requests_24h":
        return {
            "value": requests_24h,
            "label": "LLM requests / 24h",
            "subtitle": f"mode={policy.get('access_mode') or 'open'}",
            "description": f"default model: {policy.get('default_model') or 'gpt-4o-mini'}",
        }
    if metric_id == "llm_policy":
        enabled = bool(policy.get("llm_enabled", True))
        return {
            "value": "ON" if enabled else "OFF",
            "label": str(policy.get("access_mode") or "open"),
            "subtitle": "Root LLM policy",
            "description": f"{denied_30d} denied requests over the last 30 days.",
        }
    return {
        "value": "n/a",
        "label": metric_id,
        "subtitle": "unknown metric",
        "description": "Metric is not configured.",
    }


def _webspace_id(value: str | None) -> str:
    token = str(value or "").strip()
    return token if token and not token.startswith("$") else "desktop"


def _sorted_fleet(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        _fleet(snapshot),
        key=lambda item: (
            0 if str(item.get("live_now") or "") == "yes" else 1,
            -(int(item.get("activity_score") or 0)),
            str(item.get("subnet_id") or ""),
        ),
    )


def _projection_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    meta = _snapshot_meta(snapshot)
    return {
        "metrics": {
            metric_id: {**_metric_value(snapshot, metric_id), **meta}
            for metric_id in ("fleet_total", "retire_candidates", "llm_requests_24h", "llm_policy")
        },
        "fleet": {"items": _sorted_fleet(snapshot), **meta},
        "policy": {**_policy_summary(snapshot), **meta},
        "lifecycle_candidates": {"items": _lifecycle_candidates(snapshot), **meta},
        "audit": {"items": _audit(snapshot), **meta},
    }


def _semantic_projection_digest(payload: Mapping[str, Any]) -> str:
    def stable(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): stable(item)
                for key, item in value.items()
                if str(key) not in {"generated_at", "stale_age_s"}
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    encoded = json.dumps(stable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _project_snapshot(snapshot: Mapping[str, Any], *, webspace_id: str, force_emit: bool = False) -> bool:
    target = _webspace_id(webspace_id)
    payload = _projection_payload(snapshot)
    digest = _semantic_projection_digest(payload)
    with _PROJECTION_LOCK:
        if not force_emit and _PROJECTION_DIGESTS.get(target) == digest:
            return False
        ctx_subnet.set("root_mgmnt.snapshot", payload, webspace_id=target)
        _PROJECTION_DIGESTS[target] = digest
        _ACTIVE_WEBSPACES.add(target)
    return True


def _refresh_projection(*, webspace_id: str = "desktop", force: bool = False, force_emit: bool = False) -> dict[str, Any]:
    target = _webspace_id(webspace_id)
    with _PROJECTION_LOCK:
        _ACTIVE_WEBSPACES.add(target)
    snapshot = _snapshot_or_fallback(force=force)
    changed = _project_snapshot(snapshot, webspace_id=target, force_emit=force_emit)
    return {"snapshot": snapshot, "projection_changed": changed, "webspace_id": target}


def _refresh_active_projections(*, force: bool = True) -> None:
    with _PROJECTION_LOCK:
        webspaces = tuple(_ACTIVE_WEBSPACES) or ("desktop",)
    for webspace_id in webspaces:
        try:
            _refresh_projection(webspace_id=webspace_id, force=force)
        except Exception:
            _log.warning("root_mgmnt event projection failed webspace_id=%s", webspace_id, exc_info=True)


def _publish_snapshot_changed(payload: Mapping[str, Any]) -> None:
    try:
        get_ctx().bus.publish(
            Event(
                type="root.mgmnt.snapshot.changed",
                payload=dict(payload),
                source="root_mgmnt.sse",
                ts=time.time(),
            )
        )
    except Exception:
        _log.warning("root_mgmnt could not publish snapshot change event", exc_info=True)


def _event_stream_loop() -> None:
    backoff_s = 1.0
    while not _EVENT_STREAM_STOP.is_set():
        try:
            base_url = _root_base_url()
            response = requests.get(
                f"{base_url}/v1/root_mgmnt/events",
                headers={
                    "X-Root-Mgmnt-Token": _root_token(),
                    "X-Root-Mgmnt-Actor": "root_mgmnt.skill",
                    "Accept": "text/event-stream",
                },
                verify=_root_verify(base_url),
                timeout=(_REQUEST_TIMEOUT_S, 45.0),
                stream=True,
            )
            response.raise_for_status()
            backoff_s = 1.0
            event_name = ""
            event_data = ""
            with response:
                for raw_line in response.iter_lines(decode_unicode=True):
                    if _EVENT_STREAM_STOP.is_set():
                        break
                    line = str(raw_line or "")
                    if line.startswith("event:"):
                        event_name = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        event_data = line.split(":", 1)[1].strip()
                    elif not line:
                        if event_name in {"snapshot.changed", "subscribed"}:
                            try:
                                payload = json.loads(event_data) if event_data else {}
                            except json.JSONDecodeError:
                                payload = {"reason": "invalid_event_payload"}
                            if event_name == "subscribed" and isinstance(payload, dict):
                                payload.setdefault("reason", "stream.subscribed")
                            _publish_snapshot_changed(payload if isinstance(payload, Mapping) else {})
                        event_name = ""
                        event_data = ""
        except Exception as exc:
            if not _EVENT_STREAM_STOP.is_set():
                _log.info("root_mgmnt event stream reconnecting error=%s", type(exc).__name__)
        if _EVENT_STREAM_STOP.wait(backoff_s):
            break
        backoff_s = min(30.0, backoff_s * 2.0)


def _start_event_subscription() -> bool:
    global _EVENT_STREAM_THREAD
    with _EVENT_STREAM_LOCK:
        if _EVENT_STREAM_THREAD is not None and _EVENT_STREAM_THREAD.is_alive():
            return False
        _EVENT_STREAM_STOP.clear()
        _EVENT_STREAM_THREAD = threading.Thread(
            target=_event_stream_loop,
            name="root-mgmnt-events",
            daemon=True,
        )
        _EVENT_STREAM_THREAD.start()
        return True


def _policy_summary(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    policy = _policy(snapshot)
    overview = _overview(snapshot)
    return {
        "generated_at": snapshot.get("generated_at"),
        "llm_enabled": bool(policy.get("llm_enabled", True)),
        "access_mode": str(policy.get("access_mode") or "open"),
        "default_model": str(policy.get("default_model") or "gpt-4o-mini"),
        "allowed_models": list(policy.get("allowed_models") or []),
        "dev_model_profiles": list(policy.get("dev_model_profiles") or []),
        "allowed_subnets": list(policy.get("allowed_subnets") or []),
        "fleet_overview": overview,
        "top_retire_candidates": _lifecycle_candidates(snapshot)[:5],
    }


def _subnet_details(snapshot: Mapping[str, Any], subnet_id: str | None) -> dict[str, Any]:
    item = _find_subnet(snapshot, subnet_id)
    if not item:
        return {
            "hint": "Select a subnet from Fleet or Lifecycle.",
            "generated_at": snapshot.get("generated_at"),
            "policy": _policy(snapshot),
        }
    return {
        "subnet_id": item.get("subnet_id"),
        "owner_id": item.get("owner_id"),
        "owner_revoked": item.get("owner_revoked"),
        "lifecycle_state": item.get("lifecycle_state"),
        "auto_state": item.get("auto_state"),
        "llm_access": item.get("llm_access"),
        "activity_score": item.get("activity_score"),
        "live_now": item.get("live_now"),
        "last_seen": item.get("last_seen"),
        "last_seen_at": item.get("last_seen_at"),
        "idle_days": item.get("idle_days"),
        "llm": {
            "requests_24h": item.get("llm_requests_24h"),
            "requests_7d": item.get("llm_requests_7d"),
            "requests_30d": item.get("llm_requests_30d"),
            "denied_30d": item.get("llm_denied_30d"),
            "last_model": item.get("llm_last_model"),
            "last_seen_at": item.get("llm_last_seen_at"),
        },
        "forge": {
            "dev_nodes": item.get("dev_nodes"),
            "draft_artifacts": item.get("draft_artifacts"),
            "registry_artifacts": item.get("registry_artifacts"),
            "uploads": item.get("uploads"),
        },
        "candidate_reason": item.get("candidate_reason"),
        "note": item.get("note"),
        "policy_mode": _policy(snapshot).get("access_mode"),
        "generated_at": snapshot.get("generated_at"),
    }


def _subnet_action(subnet_id: str, action: str, note: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": action}
    if note:
        payload["note"] = note
    result = _client().request("POST", f"/v1/root_mgmnt/subnets/{quote(subnet_id.strip(), safe='')}/action", json=payload)
    _invalidate_cache()
    _refresh_active_projections(force=True)
    return dict(result) if isinstance(result, Mapping) else {"ok": True, "action": action, "subnet_id": subnet_id}


def _policy_update(**payload: Any) -> dict[str, Any]:
    result = _client().request("POST", "/v1/root_mgmnt/policy", json=payload)
    _invalidate_cache()
    _refresh_active_projections(force=True)
    return dict(result) if isinstance(result, Mapping) else {"ok": True, "policy": payload}


@tool("get_snapshot")
def get_snapshot(force: bool = False) -> dict[str, Any]:
    return _snapshot_or_fallback(force=bool(force))


@tool("refresh_snapshot")
def refresh_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    _start_event_subscription()
    result = _refresh_projection(webspace_id=_webspace_id(webspace_id), force=True)
    return {
        **result["snapshot"],
        "projection_changed": result["projection_changed"],
        "webspace_id": result["webspace_id"],
    }


@tool("get_metric_tile")
def get_metric_tile(metric_id: str, refresh_nonce: Any | None = None) -> dict[str, Any]:
    _ = refresh_nonce
    snapshot = _snapshot_or_fallback(force=False)
    return {
        **_metric_value(snapshot, str(metric_id or "").strip()),
        **_snapshot_meta(snapshot),
    }


@tool("get_policy_summary")
def get_policy_summary(refresh_nonce: Any | None = None) -> dict[str, Any]:
    _ = refresh_nonce
    snapshot = _snapshot_or_fallback(force=False)
    return {
        **_policy_summary(snapshot),
        **_snapshot_meta(snapshot),
    }


@tool("get_fleet")
def get_fleet(refresh_nonce: Any | None = None) -> dict[str, Any]:
    _ = refresh_nonce
    snapshot = _snapshot_or_fallback(force=False)
    items = _sorted_fleet(snapshot)
    return {
        "items": items,
        **_snapshot_meta(snapshot),
    }


@tool("get_lifecycle_candidates")
def get_lifecycle_candidates(refresh_nonce: Any | None = None) -> dict[str, Any]:
    _ = refresh_nonce
    snapshot = _snapshot_or_fallback(force=False)
    items = sorted(
        _lifecycle_candidates(snapshot),
        key=lambda item: (-(int(item.get("idle_days") or 0)), str(item.get("subnet_id") or "")),
    )
    return {
        "items": items,
        **_snapshot_meta(snapshot),
    }


@tool("get_audit_events")
def get_audit_events(refresh_nonce: Any | None = None) -> dict[str, Any]:
    _ = refresh_nonce
    snapshot = _snapshot_or_fallback(force=False)
    return {
        "items": _audit(snapshot),
        **_snapshot_meta(snapshot),
    }


@tool("get_subnet_details")
def get_subnet_details(subnet_id: Optional[str] = None, refresh_nonce: Any | None = None) -> dict[str, Any]:
    _ = refresh_nonce
    snapshot = _snapshot_or_fallback(force=False)
    return {
        **_subnet_details(snapshot, subnet_id),
        **_snapshot_meta(snapshot),
    }


@tool("freeze_subnet_llm")
def freeze_subnet_llm(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "freeze_llm", note=note)


@tool("unfreeze_subnet_llm")
def unfreeze_subnet_llm(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "unfreeze_llm", note=note)


@tool("mark_dormant")
def mark_dormant(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "mark_dormant", note=note)


@tool("reactivate_subnet")
def reactivate_subnet(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "reactivate", note=note)


@tool("archive_dev_space")
def archive_dev_space(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "archive_dev_space", note=note)


@tool("retire_subnet")
def retire_subnet(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "retire_subnet", note=note)


@tool("set_policy_mode")
def set_policy_mode(mode: str) -> dict[str, Any]:
    normalized = str(mode or "").strip().lower()
    if normalized not in {"open", "allowlist", "denyall"}:
        raise ValueError("mode must be one of: open, allowlist, denyall")
    return _policy_update(access_mode=normalized)


@tool("set_llm_enabled")
def set_llm_enabled(enabled: bool) -> dict[str, Any]:
    return _policy_update(llm_enabled=bool(enabled))


@tool("allow_subnet")
def allow_subnet(subnet_id: str) -> dict[str, Any]:
    snapshot = _snapshot_or_fallback(force=True)
    policy = _policy(snapshot)
    allowed = {str(item).strip() for item in policy.get("allowed_subnets") or [] if str(item).strip()}
    allowed.add(str(subnet_id or "").strip())
    return _policy_update(allowed_subnets=sorted(allowed))


@tool("remove_allowed_subnet")
def remove_allowed_subnet(subnet_id: str) -> dict[str, Any]:
    snapshot = _snapshot_or_fallback(force=True)
    policy = _policy(snapshot)
    allowed = [str(item).strip() for item in policy.get("allowed_subnets") or [] if str(item).strip()]
    filtered = [item for item in allowed if item != str(subnet_id or "").strip()]
    return _policy_update(allowed_subnets=filtered)


@subscribe("root.mgmnt.snapshot.changed")
@subscribe("subnet.member.snapshot.changed")
@subscribe("subnet.member.status.changed")
@subscribe("subnet.member.meta.changed")
@subscribe("subnet.member.link.up")
@subscribe("subnet.member.link.down")
def on_root_context_changed(evt: Any) -> None:
    _ = evt
    _refresh_active_projections(force=True)


@subscribe("sys.ready")
@subscribe("desktop.webspace.refresh")
@subscribe("desktop.webspace.reload")
def on_runtime_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    webspace_id = payload.get("webspace_id") if isinstance(payload, Mapping) else None
    _start_event_subscription()
    _refresh_projection(webspace_id=_webspace_id(webspace_id), force=True)


@tool("rehydrate")
def rehydrate(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    webspace_id = payload.get("webspace_id") if isinstance(payload, Mapping) else None
    started = _start_event_subscription()
    result = _refresh_projection(webspace_id=_webspace_id(webspace_id), force=True)
    return {
        "ok": True,
        "subscription_started": started,
        "projection_changed": result["projection_changed"],
        "webspace_id": result["webspace_id"],
    }


@tool("dispose")
def dispose() -> dict[str, Any]:
    _EVENT_STREAM_STOP.set()
    return {"ok": True}
