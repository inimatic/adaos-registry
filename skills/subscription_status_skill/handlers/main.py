from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_current_user
from adaos.sdk.subscriptions import project_codex_usage_window

ROOT_GOVERNED_RESOURCES: tuple[str, ...] = (
    "llm.requests",
    "llm.tokens.input",
    "llm.tokens.output",
    "llm.tokens.reasoning",
    "codex.api.tokens",
    "root_mcp.calls",
    "skill.subscription_invocations",
    "background.jobs",
    "storage.bytes",
    "media.indexing",
    "external.integrations",
)
PLAN_CHANGE_REQUEST_SCHEMA = "adaos.subscription.plan_change_request.v1"
USER_SELECTABLE_PLANS: tuple[str, ...] = ("starter", "personal", "builder", "fleet")

_AUTO_ROOT_REFRESH_MIN_INTERVAL_S = 180.0
_LAST_AUTO_ROOT_REFRESH_AT = 0.0


def current_subnet_economic_status() -> dict[str, Any]:
    from adaos.services.economic_policy import current_subnet_economic_status as impl

    return impl()


def refresh_entitlement_snapshot_from_root(*, timeout: float = 8.0) -> dict[str, Any]:
    from adaos.services.economic_policy import refresh_entitlement_snapshot_from_root as impl

    return impl(timeout=timeout)


def lang_res() -> dict[str, str]:
    return {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int_value(value: Any) -> int:
    try:
        parsed = int(float(str(value)))
    except Exception:
        return 0
    return parsed if parsed > 0 else 0


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _webspace_id(value: str | None) -> str:
    token = str(value or "").strip()
    return token if token and not token.startswith("$") else "desktop"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_s(value: Any) -> float | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _skill_state_dir() -> Path:
    try:
        from adaos.services.runtime_paths import current_state_dir

        return (current_state_dir() / "skills" / "subscription_status_skill").resolve()
    except Exception:
        return (Path(__file__).resolve().parents[1] / ".state").resolve()


def _plan_change_request_path() -> Path:
    return _skill_state_dir() / "plan_change_request.json"


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _read_plan_change_request() -> dict[str, Any]:
    payload = _read_json_file(_plan_change_request_path())
    if payload.get("schema") != PLAN_CHANGE_REQUEST_SCHEMA:
        return {}
    return payload


def _disabled_by_resource(status: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    raw = status.get("disabled_resources")
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, Mapping):
            resource = _text(item.get("resource"))
            if resource:
                out[resource] = item
    return out


def _quota_state(
    resource_usage: Mapping[str, Any],
    disabled: Mapping[str, Any] | None,
    *,
    enforcement_active: bool,
) -> str:
    if disabled:
        reason = _text(disabled.get("reason_code"))
        if not enforcement_active:
            return "exhausted_observed" if reason == "quota_exhausted" else "disabled_observed"
        return "disabled"
    if resource_usage.get("quota_exhausted"):
        return "exhausted" if enforcement_active else "exhausted_observed"
    if resource_usage.get("quota_warn"):
        return "warn"
    if resource_usage:
        return "ok"
    return "not_metered"


def _resource_rows(status: Mapping[str, Any]) -> list[dict[str, Any]]:
    usage = _as_mapping(status.get("usage"))
    disabled_map = _disabled_by_resource(status)
    enforcement_active = status.get("enforcement_active") is True
    rows: list[dict[str, Any]] = []
    for resource in ROOT_GOVERNED_RESOURCES:
        item = _as_mapping(usage.get(resource))
        disabled = disabled_map.get(resource)
        quota = _as_mapping(item.get("quota"))
        breakdown = _as_mapping(item.get("usage_breakdown"))
        window_24h = _as_mapping(breakdown.get("window_24h"))
        window_30d = _as_mapping(breakdown.get("window_30d"))
        metering = _text(item.get("metering"))
        if resource == "codex.api.tokens" and metering in {"", "manual_adjustment_pending_codex_stream"}:
            metering = "codex_usage_stream"
        rows.append(
            {
                "resource": resource,
                "state": _quota_state(
                    item,
                    disabled,
                    enforcement_active=enforcement_active,
                ),
                "reason": _text(disabled.get("reason_code") if isinstance(disabled, Mapping) else ""),
                "enforcement_active": enforcement_active,
                "advisory": not enforcement_active,
                "used_24h": _int_value(item.get("used_24h")),
                "used_30d": _int_value(item.get("used_30d")),
                "quota_limit": "" if item.get("quota_limit") is None else _int_value(item.get("quota_limit")),
                "quota_remaining": "" if item.get("quota_remaining") is None else _int_value(item.get("quota_remaining")),
                "quota_period": _text(item.get("quota_period") or quota.get("period")),
                "quota_unit": _text(item.get("quota_unit") or quota.get("unit")),
                "metering": metering,
                "source": _text(item.get("source")),
                "accuracy": _text(item.get("accuracy")),
                "last_model": _text(item.get("last_model")),
                "last_seen_at": _text(item.get("last_seen_at")),
                "quota_metric": _text(item.get("quota_metric")),
                "optimization_metric": _text(item.get("optimization_metric")),
                "fresh_plus_output_24h": _int_value(
                    window_24h.get("fresh_plus_output_tokens")
                ),
                "cached_input_24h": _int_value(window_24h.get("cached_input_tokens")),
                "output_tokens_24h": _int_value(window_24h.get("output_tokens")),
                "runs_24h": _int_value(window_24h.get("runs")),
                "zero_model_tasks_24h": _int_value(window_24h.get("zero_model_tasks")),
                "fresh_plus_output_30d": _int_value(
                    window_30d.get("fresh_plus_output_tokens")
                ),
            }
        )
    return rows


def _current_tile(status: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    enforcement_active = status.get("enforcement_active") is True
    disabled = sum(1 for row in rows if row.get("state") == "disabled")
    observed = sum(
        1
        for row in rows
        if row.get("state") in {"disabled_observed", "exhausted_observed"}
    )
    exhausted = sum(1 for row in rows if row.get("state") == "exhausted")
    warn = sum(1 for row in rows if row.get("state") == "warn")
    subscription_state = _text(status.get("subscription_state")) or "unknown"
    inactive = subscription_state not in {"active", "trial"}
    llm = next((row for row in rows if row.get("resource") == "llm.requests"), {})
    codex = next((row for row in rows if row.get("resource") == "codex.api.tokens"), {})
    llm_limit = llm.get("quota_limit")
    llm_left = llm.get("quota_remaining")
    codex_left = codex.get("quota_remaining")
    codex_used_24h = _int_value(codex.get("used_24h"))
    codex_used = _int_value(codex.get("used_30d"))
    codex_accuracy = _text(codex.get("accuracy"))
    codex_effective_24h = _int_value(codex.get("fresh_plus_output_24h"))
    codex_cached_24h = _int_value(codex.get("cached_input_24h"))
    llm_quota = f"/{_int_value(llm_limit)}" if llm_limit not in {"", None} else ""
    llm_remaining = f", left {_int_value(llm_left)}" if llm_left not in {"", None} else ""
    codex_suffix = ""
    if codex_left not in {"", None}:
        codex_suffix = f"; Codex 30d: {codex_used}, left {codex_left}"
    elif codex_used:
        codex_suffix = f"; Codex 30d: {codex_used}"
    if codex_accuracy:
        codex_suffix = f"{codex_suffix} ({codex_accuracy})" if codex_suffix else f"; Codex: {codex_accuracy}"
    return {
        "value": _text(status.get("plan_id")) or "none",
        "label": "AdaOS subscription",
        "subtitle": f"Plan: {_text(status.get('plan_id')) or 'none'}; status: {subscription_state}",
        "description": (
            f"Usage in the last 24h: LLM {_int_value(llm.get('used_24h'))}{llm_quota}{llm_remaining}; "
            f"Codex quota {codex_used_24h}; "
            f"fresh + output {codex_effective_24h}; cached input {codex_cached_24h}; "
            f"features needing attention: {warn + exhausted + observed}; unavailable: {disabled}; "
            f"advisory observations: {observed}{codex_suffix}"
        ),
        "color": (
            "danger"
            if exhausted or inactive
            else "warning"
            if warn or disabled or observed
            else "success"
        ),
        "enforcement_active": enforcement_active,
        "advisory_observation_count": observed,
        "generated_at": status.get("generated_at"),
    }


def _plan_change_projection(status: Mapping[str, Any]) -> dict[str, Any]:
    request = _read_plan_change_request()
    current_plan = _text(status.get("plan_id")) or "none"
    if not request:
        return {
            "value": "none",
            "label": "Plan request",
            "subtitle": f"current plan: {current_plan}",
            "description": "No pending plan change request. Payments are deferred.",
            "color": "neutral",
            "status": "none",
            "desired_plan_id": "",
            "requested_at": "",
            "note": "",
        }
    desired = _text(request.get("desired_plan_id")) or "unknown"
    return {
        **request,
        "value": desired,
        "label": "Plan request",
        "subtitle": f"{_text(request.get('status')) or 'requested'} / current: {current_plan}",
        "description": _text(request.get("note")) or "Waiting for root operator review. Payments are deferred.",
        "color": "warning",
    }


def _usage_history_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("state") == "not_metered" and not _int_value(row.get("used_24h")) and not _int_value(row.get("used_30d")):
            continue
        out.append(
            {
                "resource": _text(row.get("resource")),
                "state": _text(row.get("state")),
                "used_24h": _int_value(row.get("used_24h")),
                "used_30d": _int_value(row.get("used_30d")),
                "metering": _text(row.get("metering")),
                "source": _text(row.get("source")),
                "accuracy": _text(row.get("accuracy")),
                "last_model": _text(row.get("last_model")),
                "last_seen_at": _text(row.get("last_seen_at")),
                "fresh_plus_output_24h": _int_value(row.get("fresh_plus_output_24h")),
                "cached_input_24h": _int_value(row.get("cached_input_24h")),
                "output_tokens_24h": _int_value(row.get("output_tokens_24h")),
                "runs_24h": _int_value(row.get("runs_24h")),
                "zero_model_tasks_24h": _int_value(row.get("zero_model_tasks_24h")),
                "fresh_plus_output_30d": _int_value(row.get("fresh_plus_output_30d")),
                "reason": _text(row.get("reason")),
            }
        )
    return out


def _projection_payload(status: Mapping[str, Any]) -> dict[str, Any]:
    rows = _resource_rows(status)
    usage = _as_mapping(_as_mapping(status.get("usage")).get("codex.api.tokens"))
    window = _as_mapping(_as_mapping(usage.get("usage_breakdown")).get("window_24h"))
    codex = project_codex_usage_window(window)
    model_rows = []
    for row in codex["by_model"]:
        cost = row["cost"]
        amount = cost.get("estimated_usd")
        model_rows.append({**row, "model": row["model"] or "Unknown",
                           "cost_status": cost["status"],
                           "estimated_usd": f"{amount:.6f}" if amount is not None else "",
                           "unpriced_runs": cost.get("unpriced_runs")})
    return {
        "current": _current_tile(status, rows),
        "buttons": [{"id": "details", "label": "Details", "kind": "primary"}],
        "resources": {"items": rows, "generated_at": status.get("generated_at")},
        "usage_history": {"items": _usage_history_rows(rows), "generated_at": status.get("generated_at")},
        "codex_models": {"items": model_rows, "cost": codex["cost"],
                         "generated_at": status.get("generated_at")},
        "plan_change": _plan_change_projection(status),
        "raw": dict(status),
    }


def _refresh_projection(
    status: Mapping[str, Any],
    refresh: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot = _as_mapping(status.get("entitlement_snapshot"))
    attempted = bool(refresh.get("attempted"))
    ok = refresh.get("ok") is True if attempted else bool(snapshot.get("loaded"))
    updated_at = _text(
        refresh.get("retrieved_at")
        or snapshot.get("updated_at")
        or status.get("generated_at")
    )
    return {
        "status": "refreshed" if attempted and ok else "failed" if attempted else "loaded",
        "updated_at": updated_at,
        "root_base_url": _text(
            refresh.get("root_base_url")
            or _as_mapping(status.get("management_authority")).get("configured_root_base_url")
        ),
        "detail": _text(refresh.get("detail") or refresh.get("error")),
    }


def _write_refreshing_projection(*, webspace_id: str) -> None:
    status = current_subnet_economic_status()
    payload = _projection_payload(status)
    payload["refresh"] = {
        "status": "refreshing",
        "updated_at": _now_iso(),
        "root_base_url": _text(
            _as_mapping(status.get("management_authority")).get("configured_root_base_url")
        ),
        "detail": "Refreshing the authoritative Root entitlement snapshot.",
    }
    ctx_current_user.set("subscription_status.snapshot", payload, webspace_id=webspace_id)


def _needs_root_refresh(status: Mapping[str, Any]) -> bool:
    snapshot = _as_mapping(status.get("entitlement_snapshot"))
    subscription_state = _text(status.get("subscription_state")).lower()
    plan_id = _text(status.get("plan_id")).lower()
    if snapshot and snapshot.get("loaded") is False:
        return True
    updated_at = _timestamp_s(snapshot.get("updated_at"))
    if updated_at is not None and time.time() - updated_at > _AUTO_ROOT_REFRESH_MIN_INTERVAL_S:
        return True
    return subscription_state in {"", "unknown", "unassigned"} or plan_id in {"", "none", "unknown"}


def _root_refresh_allowed() -> bool:
    global _LAST_AUTO_ROOT_REFRESH_AT
    now = time.monotonic()
    if now - _LAST_AUTO_ROOT_REFRESH_AT < _AUTO_ROOT_REFRESH_MIN_INTERVAL_S:
        return False
    _LAST_AUTO_ROOT_REFRESH_AT = now
    return True


def _refresh_root_status(*, attempted: bool = True) -> dict[str, Any]:
    refresh: dict[str, Any] = {"attempted": attempted}
    try:
        refresh.update(refresh_entitlement_snapshot_from_root())
    except Exception as exc:
        refresh.update({"ok": False, "error": type(exc).__name__, "detail": str(exc)})
    return refresh


def _project_status(
    *,
    webspace_id: str = "desktop",
    refresh_root: bool = False,
    refresh_root_if_missing: bool = True,
) -> dict[str, Any]:
    target = _webspace_id(webspace_id)
    refresh: dict[str, Any] = {"attempted": False}
    if refresh_root:
        refresh = _refresh_root_status()
    status = current_subnet_economic_status()
    if (
        not refresh_root
        and refresh_root_if_missing
        and _needs_root_refresh(status)
        and _root_refresh_allowed()
    ):
        refresh = _refresh_root_status()
        if refresh.get("ok") is True:
            status = current_subnet_economic_status()
    payload = _projection_payload(status)
    payload["refresh"] = _refresh_projection(status, refresh)
    ctx_current_user.set("subscription_status.snapshot", payload, webspace_id=target)
    return {**payload, "ok": True, "webspace_id": target, "refresh": refresh}


@tool("get_status")
def get_status(webspace_id: str | None = None, refresh_root_if_missing: bool = True) -> dict[str, Any]:
    return _project_status(
        webspace_id=_webspace_id(webspace_id),
        refresh_root_if_missing=bool(refresh_root_if_missing),
    )


@tool("subscription_status_drain")
def subscription_status_drain(reason: str | None = None) -> dict[str, Any]:
    """Acknowledge that this provider has no durable worker or write queue."""

    return {
        "ok": True,
        "status": "drained",
        "reason": _text(reason) or "application_release_switch",
    }


@tool("subscription_status_rehydrate")
def subscription_status_rehydrate(reason: str | None = None) -> dict[str, Any]:
    """Acknowledge activation; projections are rebuilt lazily for each Webspace."""

    return {
        "ok": True,
        "status": "ready",
        "reason": _text(reason) or "application_release_switch",
    }


@tool("refresh_status")
def refresh_status(webspace_id: str | None = None) -> dict[str, Any]:
    target = _webspace_id(webspace_id)
    _write_refreshing_projection(webspace_id=target)
    return _project_status(webspace_id=target, refresh_root=True)


@tool("list_resources")
def list_resources(webspace_id: str | None = None) -> dict[str, Any]:
    payload = _project_status(webspace_id=_webspace_id(webspace_id))
    resources = _as_mapping(payload.get("resources"))
    return {
        "ok": True,
        "webspace_id": payload.get("webspace_id"),
        "items": list(resources.get("items") or []),
        "generated_at": resources.get("generated_at"),
        "current": payload.get("current"),
    }


@tool("list_usage_history")
def list_usage_history(webspace_id: str | None = None) -> dict[str, Any]:
    payload = _project_status(webspace_id=_webspace_id(webspace_id))
    history = _as_mapping(payload.get("usage_history"))
    return {
        "ok": True,
        "webspace_id": payload.get("webspace_id"),
        "items": list(history.get("items") or []),
        "generated_at": history.get("generated_at"),
        "current": payload.get("current"),
    }


@tool("request_plan_change")
def request_plan_change(
    desired_plan_id: str,
    note: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    desired = _text(desired_plan_id).lower()
    if desired not in USER_SELECTABLE_PLANS:
        raise ValueError("desired_plan_id must be one of: starter, personal, builder, fleet")
    target = _webspace_id(webspace_id)
    status = current_subnet_economic_status()
    request = {
        "schema": PLAN_CHANGE_REQUEST_SCHEMA,
        "status": "requested",
        "requested_at": _now_iso(),
        "subnet_id": _text(status.get("subnet_id")),
        "zone_id": _text(status.get("zone_id")),
        "current_plan_id": _text(status.get("plan_id")) or "none",
        "subscription_state": _text(status.get("subscription_state")) or "unknown",
        "desired_plan_id": desired,
        "note": _text(note),
    }
    _write_json_file(_plan_change_request_path(), request)
    payload = _projection_payload(status)
    ctx_current_user.set("subscription_status.snapshot", payload, webspace_id=target)
    return {"ok": True, "webspace_id": target, "plan_change": request, **payload}


@subscribe("sys.ready")
@subscribe("desktop.webspace.refresh")
@subscribe("desktop.webspace.reload")
@subscribe("root.mgmnt.snapshot.changed")
@subscribe("subnet.member.snapshot.changed")
@subscribe("subnet.member.status.changed")
def on_runtime_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    topic = _text(getattr(evt, "type", None) or (payload.get("type") if isinstance(payload, Mapping) else None))
    webspace_id = payload.get("webspace_id") if isinstance(payload, Mapping) else None
    _project_status(webspace_id=_webspace_id(webspace_id), refresh_root=topic.startswith("root.mgmnt."))
