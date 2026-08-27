from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet

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


def _quota_state(resource_usage: Mapping[str, Any], disabled: Mapping[str, Any] | None) -> str:
    if disabled:
        return "disabled"
    if resource_usage.get("quota_exhausted"):
        return "exhausted"
    if resource_usage.get("quota_warn"):
        return "warn"
    if resource_usage:
        return "ok"
    return "not_metered"


def _resource_rows(status: Mapping[str, Any]) -> list[dict[str, Any]]:
    usage = _as_mapping(status.get("usage"))
    disabled_map = _disabled_by_resource(status)
    rows: list[dict[str, Any]] = []
    for resource in ROOT_GOVERNED_RESOURCES:
        item = _as_mapping(usage.get(resource))
        disabled = disabled_map.get(resource)
        quota = _as_mapping(item.get("quota"))
        rows.append(
            {
                "resource": resource,
                "state": _quota_state(item, disabled),
                "reason": _text(disabled.get("reason_code") if isinstance(disabled, Mapping) else ""),
                "used_24h": _int_value(item.get("used_24h")),
                "used_30d": _int_value(item.get("used_30d")),
                "quota_limit": "" if item.get("quota_limit") is None else _int_value(item.get("quota_limit")),
                "quota_remaining": "" if item.get("quota_remaining") is None else _int_value(item.get("quota_remaining")),
                "quota_period": _text(item.get("quota_period") or quota.get("period")),
                "quota_unit": _text(item.get("quota_unit") or quota.get("unit")),
                "last_seen_at": _text(item.get("last_seen_at")),
            }
        )
    return rows


def _current_tile(status: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    disabled = _int_value(status.get("disabled_resource_count"))
    exhausted = sum(1 for row in rows if row.get("state") == "exhausted")
    warn = sum(1 for row in rows if row.get("state") == "warn")
    subscription_state = _text(status.get("subscription_state")) or "unknown"
    entitlement_state = _text(status.get("entitlement_state")) or "unknown"
    inactive = subscription_state not in {"active", "trial"}
    llm = _as_mapping(_as_mapping(status.get("usage")).get("llm.requests"))
    codex = next((row for row in rows if row.get("resource") == "codex.api.tokens"), {})
    codex_left = codex.get("quota_remaining")
    suffix = f", Codex left {codex_left}" if codex_left not in {"", None} else ""
    return {
        "value": _text(status.get("plan_id")) or "none",
        "label": "AdaOS subscription",
        "subtitle": f"{subscription_state} / {entitlement_state}",
        "description": f"LLM 24h: {_int_value(llm.get('used_24h'))}; disabled: {disabled}; warn: {warn}; exhausted: {exhausted}{suffix}",
        "color": "danger" if exhausted or inactive else "warning" if warn or disabled else "success",
        "generated_at": status.get("generated_at"),
    }


def _projection_payload(status: Mapping[str, Any]) -> dict[str, Any]:
    rows = _resource_rows(status)
    return {
        "current": _current_tile(status, rows),
        "resources": {"items": rows, "generated_at": status.get("generated_at")},
        "raw": dict(status),
    }


def _project_status(*, webspace_id: str = "desktop", refresh_root: bool = False) -> dict[str, Any]:
    target = _webspace_id(webspace_id)
    refresh: dict[str, Any] = {"attempted": False}
    if refresh_root:
        refresh = {"attempted": True}
        try:
            refresh.update(refresh_entitlement_snapshot_from_root())
        except Exception as exc:
            refresh.update({"ok": False, "error": type(exc).__name__, "detail": str(exc)})
    status = current_subnet_economic_status()
    payload = _projection_payload(status)
    ctx_subnet.set("subscription_status.snapshot", payload, webspace_id=target)
    return {"ok": True, "webspace_id": target, "refresh": refresh, **payload}


@tool("get_status")
def get_status(webspace_id: str | None = None) -> dict[str, Any]:
    return _project_status(webspace_id=_webspace_id(webspace_id))


@tool("refresh_status")
def refresh_status(webspace_id: str | None = None) -> dict[str, Any]:
    return _project_status(webspace_id=_webspace_id(webspace_id), refresh_root=True)


@subscribe("sys.ready")
@subscribe("desktop.webspace.refresh")
@subscribe("desktop.webspace.reload")
def on_runtime_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    webspace_id = payload.get("webspace_id") if isinstance(payload, Mapping) else None
    _project_status(webspace_id=_webspace_id(webspace_id))
