from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet
from adaos.sdk.data.events import publish as publish_event
from adaos.services.release_validation import (
    OBSERVE_CHECKS,
    TestNode,
    TestSuite,
    ValidationCampaign,
    get_release_validation_service,
)


_log = logging.getLogger("skills.release_validation_skill")
DEFAULT_NODE_ID = "linux-exp-01"
DEFAULT_SUITE_ID = "adaos-observe-smoke"
DEFAULT_NODE = {
    "node_id": DEFAULT_NODE_ID,
    "display_name": "AdaOS Linux experimental node",
    "host": "192.168.0.30",
    "identity_file": "d:/git/inimatic/adaos/.ssh/adaos_linux_exp",
    "ssh_user": "root",
    "ssh_port": 22,
    "runtime_port": 8778,
    "supervisor_port": 8776,
    "base_dir": "/root/.adaos",
    "capabilities": ("adaos.runtime.observe",),
    "allowed_profiles": ("observe",),
}


def _service():
    return get_release_validation_service()


def _webspace_id(value: str | None) -> str:
    token = str(value or "").strip()
    return token if token and not token.startswith("$") else "desktop"


def _campaign_description(campaign: Mapping[str, Any]) -> str:
    result = campaign.get("result") if isinstance(campaign.get("result"), Mapping) else {}
    return (
        f"target={campaign.get('target_build') or '-'}; "
        f"passed={result.get('passed', 0)}; failed={result.get('failed', 0)}; "
        f"inconclusive={result.get('inconclusive', 0)}; timed_out={result.get('timed_out', 0)}"
    )


def _event_description(item: Mapping[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    preferred = ["campaign_id", "assignment_id", "node_id", "state", "reason"]
    parts = [f"{key}={payload[key]}" for key in preferred if payload.get(key) not in (None, "")]
    if parts:
        return "; ".join(parts)
    return json.dumps(payload, ensure_ascii=False, default=str)[:500]


def _ui_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    campaigns = [dict(item) for item in snapshot.get("campaigns") or [] if isinstance(item, Mapping)]
    assignments = [dict(item) for item in snapshot.get("assignments") or [] if isinstance(item, Mapping)]
    nodes = [dict(item) for item in snapshot.get("nodes") or [] if isinstance(item, Mapping)]
    latest = campaigns[0] if campaigns else None
    latest_detail: dict[str, Any] = {}
    if latest:
        try:
            latest_detail = _service().campaign(str(latest.get("campaign_id") or ""))
        except Exception:
            latest_detail = dict(latest)

    state = str((latest or {}).get("state") or "idle")
    colors = {
        "passed": "success",
        "failed": "danger",
        "inconclusive": "warning",
        "running": "info",
        "pending": "neutral",
    }
    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), Mapping) else {}
    return {
        "summary": {
            "value": state.upper(),
            "label": "Release validation",
            "subtitle": (
                f"{summary.get('nodes_enabled', 0)} nodes / "
                f"{summary.get('assignments_running', 0)} active assignments"
            ),
            "description": _campaign_description(latest) if latest else "No validation campaign has been created.",
            "color": colors.get(state, "neutral"),
        },
        "nodes": {
            "items": [
                {
                    **item,
                    "profile": ", ".join(item.get("allowed_profiles") or []),
                    "capabilities_text": ", ".join(item.get("capabilities") or []),
                    "enabled_label": "yes" if item.get("enabled") else "no",
                }
                for item in nodes
            ]
        },
        "campaigns": {
            "items": [
                {
                    **item,
                    "nodes": ", ".join(item.get("node_ids") or []),
                    "result_text": _campaign_description(item),
                }
                for item in campaigns
            ]
        },
        "assignments": {
            "items": [
                {
                    **item,
                    "checks_text": (
                        f"{(item.get('result') or {}).get('checks_passed', 0)}/"
                        f"{(item.get('result') or {}).get('checks_total', 0)}"
                    ),
                    "reason": str((item.get("result") or {}).get("reason") or ""),
                }
                for item in assignments[:100]
            ]
        },
        "events": {
            "items": [
                {
                    "id": item.get("event_id"),
                    "title": str(item.get("type") or "validation.event"),
                    "description": _event_description(item),
                    "at": item.get("at"),
                }
                for item in snapshot.get("events") or []
                if isinstance(item, Mapping)
            ]
        },
        "detail": latest_detail,
        "mode": "observe-only",
        "updated_at": snapshot.get("updated_at"),
    }


def _project(snapshot: dict[str, Any], *, webspace_id: str) -> None:
    try:
        ctx_subnet.set("release_validation.snapshot", snapshot, webspace_id=webspace_id)
    except Exception:
        _log.warning("release validation projection failed", exc_info=True)


def _refresh(*, webspace_id: str = "desktop") -> dict[str, Any]:
    snapshot = _ui_snapshot(_service().snapshot())
    _project(snapshot, webspace_id=_webspace_id(webspace_id))
    return snapshot


def _notify(campaign: Mapping[str, Any]) -> None:
    state = str(campaign.get("state") or "unknown").upper()
    try:
        publish_event(
            "ui.notify",
            {
                "text": (
                    f"AdaOS validation {state}: {campaign.get('campaign_id')}\n"
                    f"{_campaign_description(campaign)}"
                ),
                "_meta": {
                    "source": "release_validation_skill",
                    "campaign_id": campaign.get("campaign_id"),
                    "severity": "info" if campaign.get("state") == "passed" else "critical",
                },
            },
            source="release_validation_skill",
        )
    except Exception:
        _log.warning("release validation notification failed", exc_info=True)


def _register_defaults() -> None:
    _service().register_node(TestNode(**DEFAULT_NODE))
    _service().register_suite(
        TestSuite(
            suite_id=DEFAULT_SUITE_ID,
            version="1.0.0",
            display_name="AdaOS observe-only runtime smoke",
            checks=OBSERVE_CHECKS,
        )
    )


def _manual_campaign_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"manual-{stamp}-{int(time.time_ns() % 1000):03d}"


@tool("get_snapshot")
def get_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    return {"ok": True, "snapshot": _ui_snapshot(_service().snapshot()), "webspace_id": _webspace_id(webspace_id)}


@tool("refresh_snapshot")
def refresh_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    return {"ok": True, "snapshot": _refresh(webspace_id=_webspace_id(webspace_id))}


@tool("register_default_observe_contracts")
def register_default_observe_contracts(webspace_id: str | None = None) -> dict[str, Any]:
    _register_defaults()
    return {"ok": True, "snapshot": _refresh(webspace_id=_webspace_id(webspace_id))}


@tool("prepare_campaign")
def prepare_campaign(
    target_build: str,
    campaign_id: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    _register_defaults()
    campaign = _service().create_campaign(
        ValidationCampaign(
            campaign_id=str(campaign_id or "").strip() or _manual_campaign_id(),
            suite_id=DEFAULT_SUITE_ID,
            target_build=target_build,
            node_ids=(DEFAULT_NODE_ID,),
            quorum=1,
        )
    )
    _refresh(webspace_id=_webspace_id(webspace_id))
    return {"ok": True, "campaign": campaign}


@tool("run_campaign")
def run_campaign(campaign_id: str, webspace_id: str | None = None) -> dict[str, Any]:
    campaign = _service().run_campaign(campaign_id)
    _notify(campaign)
    snapshot = _refresh(webspace_id=_webspace_id(webspace_id))
    return {"ok": True, "campaign": campaign, "snapshot": snapshot}


@tool("run_latest_campaign")
def run_latest_campaign(webspace_id: str | None = None) -> dict[str, Any]:
    snapshot = _service().snapshot()
    pending = [item for item in snapshot.get("campaigns") or [] if item.get("state") == "pending"]
    if not pending:
        return {"ok": False, "error": "no_pending_campaign", "snapshot": _ui_snapshot(snapshot)}
    return run_campaign(str(pending[0]["campaign_id"]), webspace_id=webspace_id)


@tool("run_default_observe")
def run_default_observe(
    target_build: str,
    campaign_id: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    prepared = prepare_campaign(target_build, campaign_id=campaign_id, webspace_id=webspace_id)
    return run_campaign(str(prepared["campaign"]["campaign_id"]), webspace_id=webspace_id)


@subscribe("sys.ready")
@subscribe("desktop.webspace.refresh")
@subscribe("desktop.webspace.reload")
def on_runtime_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    webspace_id = payload.get("webspace_id") if isinstance(payload, Mapping) else None
    _refresh(webspace_id=_webspace_id(webspace_id))


@tool("rehydrate")
def rehydrate(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    webspace_id = payload.get("webspace_id") if isinstance(payload, Mapping) else None
    return {"ok": True, "snapshot": _refresh(webspace_id=_webspace_id(webspace_id))}

