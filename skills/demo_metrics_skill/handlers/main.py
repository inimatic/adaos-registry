from __future__ import annotations

import time
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.io.out import stream_publish
from adaos.services.resources import ResourceAccessDenied, ResourceConflict, ResourceWorkbenchService

_RECEIVER_ID = "demo_metrics.events"
_WORKBENCH_SCHEMA = "adaos.demo_metrics.resource_workbench.v1"
_WORKBENCH_RESOURCE_TYPES = (
    "adaos.dev.ticket",
    "demo.metric",
    "demo.metric_note",
    "demo.metric_event",
)
_BUILDER_E2E_NOTE = {
    "id": "builder-e2e-validation",
    "metric_id": "cpu",
    "title": "Builder E2E validation",
    "body": "Visible validation note supplied by demo_metrics_skill; it is not persisted.",
    "actor": "builder",
    "revision": 0,
    "updated_at": "2026-08-31T11:07:00Z",
    "non_persistent": True,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _command_body(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    body = _mapping(payload)
    params = _mapping(body.get("params"))
    if not params:
        return body
    command = dict(params)
    for key in ("webspace_id", "workspace_id", "scenario_id", "node_id", "target_node_id", "_meta"):
        if key in body and key not in command:
            command[key] = body[key]
    if "context" in body and "context" not in command:
        command["context"] = body["context"]
    return command


def _workbench_actor(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    body = _mapping(payload)
    actor = _mapping(body.get("actor"))
    role = _text(body.get("role") or actor.get("role") or "owner").lower() or "owner"
    actor_id = _text(actor.get("id") or body.get("actor_id")) or f"demo_metrics:{role}"
    return {"id": actor_id, "role": role}


def _definition_rows(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for definition in definitions:
        query = _mapping(definition.get("query"))
        authority = _mapping(definition.get("authority"))
        i18n = _mapping(definition.get("i18n"))
        operations = definition.get("operations") if isinstance(definition.get("operations"), list) else []
        views = definition.get("views") if isinstance(definition.get("views"), list) else []
        locales = i18n.get("locales") if isinstance(i18n.get("locales"), list) else []
        readiness = _mapping(definition.get("readiness"))
        rows.append(
            {
                "resource_type": _text(definition.get("resource_type")),
                "title": _text(definition.get("title")),
                "provider": _text(authority.get("provider")),
                "writes": _text(authority.get("writes")),
                "default_query": _text(query.get("default")),
                "filters": len(query.get("filters") or []),
                "operations": len(operations),
                "views": len(views),
                "locales": ", ".join(_text(item) for item in locales if _text(item)),
                "readiness": ", ".join(_text(item) for item in (readiness.get("states") or readiness.get("fixtures") or []) if _text(item)),
            }
        )
    return rows


def _role_rows(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for definition in definitions:
        resource_type = _text(definition.get("resource_type"))
        role_fixtures = _mapping(_mapping(definition.get("access")).get("role_fixtures"))
        if not role_fixtures:
            continue
        for role, policy in sorted(role_fixtures.items()):
            if isinstance(policy, Mapping):
                rows.append(
                    {
                        "resource_type": resource_type,
                        "role": _text(role),
                        "create": _text(policy.get("create") or "allowed"),
                        "update": _text(policy.get("update") or "allowed"),
                        "delete": _text(policy.get("delete") or "allowed"),
                    }
                )
            else:
                decision = _text(policy) or "allowed"
                rows.append(
                    {
                        "resource_type": resource_type,
                        "role": _text(role),
                        "create": decision,
                        "update": decision,
                        "delete": decision,
                    }
                )
    return rows


def _compact_trace_rows(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trace in reversed(traces):
        actor = _mapping(trace.get("actor"))
        readiness = _mapping(trace.get("readiness"))
        result = _mapping(trace.get("result"))
        result_label = result.get("count")
        if result_label is None:
            result_label = result.get("record_id") or result.get("error") or ""
        rows.append(
            {
                "trace_id": _text(trace.get("trace_id")),
                "resource_type": _text(trace.get("resource_type")),
                "operation": _text(trace.get("operation_id") or trace.get("query_id")),
                "status": _text(trace.get("status")),
                "readiness": _text(readiness.get("state")),
                "actor": _text(actor.get("id") or actor.get("actor")),
                "result": str(result_label),
                "completed_at": _text(trace.get("completed_at")),
            }
        )
    return rows


def _event_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in reversed(events):
        rows.append(
            {
                "event_id": _text(event.get("event_id")),
                "resource_type": _text(event.get("resource_type")),
                "semantic_type": _text(event.get("semantic_type")),
                "record_ref": _text(event.get("record_ref")),
                "occurred_at": _text(event.get("occurred_at")),
            }
        )
    return rows


def _query_resource(
    service: ResourceWorkbenchService,
    resource_type: str,
    *,
    filters: Mapping[str, Any] | None = None,
    search: str = "",
    limit: int = 50,
    actor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return service.query(
        {
            "schema": "adaos.resource.query.v1",
            "resource_type": resource_type,
            "filters": _mapping(filters),
            "search": search,
            "limit": limit,
            "actor": _mapping(actor) or {"id": "demo_metrics:owner", "role": "owner"},
            "relevance_context": {"surface": "demo_metrics_resource_workbench"},
        }
    )


def _with_builder_e2e_note(result: Mapping[str, Any], *, search: str = "") -> dict[str, Any]:
    visible = dict(result)
    items = [dict(item) for item in (result.get("items") or []) if isinstance(item, Mapping)]
    needle = search.strip().casefold()
    note_text = " ".join(str(value) for value in _BUILDER_E2E_NOTE.values()).casefold()
    if not needle or needle in note_text:
        items.append(dict(_BUILDER_E2E_NOTE))
    visible["items"] = items
    visible["count"] = len(items)
    return visible


def _with_open_dev_tickets_metric(
    service: ResourceWorkbenchService,
    result: Mapping[str, Any],
    *,
    actor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    visible = dict(result)
    items = [dict(item) for item in (result.get("items") or []) if isinstance(item, Mapping)]
    source_state = "ready"
    try:
        tickets = _query_resource(
            service,
            "adaos.dev.ticket",
            filters={"status_group": "open"},
            limit=1,
            actor=actor,
        )
        count = int(tickets.get("count", 0))
    except Exception:
        count = 0
        source_state = "degraded"
    items.append(
        {
            "id": "open-dev-tickets",
            "title": "Open change requests",
            "status": source_state,
            "value": count,
            "unit": "tickets",
            "group": "subnet",
            "source": "adaos.dev.ticket",
            "source_state": source_state,
            "non_persistent": True,
        }
    )
    visible["items"] = items
    visible["count"] = len(items)
    visible["source_state"] = source_state
    return visible


def _resource_workbench_snapshot(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    body = _command_body(payload)
    service = ResourceWorkbenchService()
    actor = _workbench_actor(body)
    metric_filters = _mapping(body.get("metric_filters"))
    note_filters = _mapping(body.get("note_filters"))
    fixture = _text(body.get("fixture"))
    if fixture:
        metric_filters.setdefault("fixture", fixture)
        note_filters.setdefault("fixture", fixture)
    definitions = service.definitions()
    metrics = _with_open_dev_tickets_metric(
        service,
        _query_resource(service, "demo.metric", filters=metric_filters, limit=20, actor=actor),
        actor=actor,
    )
    notes = _with_builder_e2e_note(
        _query_resource(service, "demo.metric_note", filters=note_filters, limit=50, actor=actor)
    )
    traces = service.traces(limit=30)
    events = service.events(limit=30)
    return {
        "schema": _WORKBENCH_SCHEMA,
        "title": "Declarative Resource Workbench",
        "summary": {
            "value": str(len(definitions)),
            "label": "Resource definitions",
            "description": "Typed definition/query/operation/traces over Dev Tickets and demo resources.",
        },
        "definitions": {"items": _definition_rows(definitions)},
        "metrics": {
            "items": metrics.get("items") or [],
            "count": metrics.get("count", 0),
            "source_state": metrics.get("source_state", "ready"),
        },
        "notes": {"items": notes.get("items") or [], "count": notes.get("count", 0)},
        "roles": {"items": _role_rows(definitions)},
        "traces": {"items": _compact_trace_rows(traces), "count": len(traces)},
        "events": {"items": _event_rows(events), "count": len(events)},
        "fixtures": {
            "items": [
                {"id": "normal", "label": "Normal", "purpose": "baseline typed query"},
                {"id": "empty", "label": "Empty", "purpose": "empty-state rendering"},
                {"id": "long_text", "label": "Long text", "purpose": "layout resilience"},
                {"id": "unavailable_provider", "label": "Unavailable", "purpose": "provider failure/readiness"},
            ]
        },
    }


def _webspace_id_from_payload(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return "desktop"
    raw = payload.get("webspace_id") or payload.get("workspace_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    meta = payload.get("_meta")
    if isinstance(meta, Mapping):
        nested = meta.get("webspace_id") or meta.get("workspace_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return "desktop"


def _publish_demo_event(
    *,
    webspace_id: str,
    title: str,
    description: str,
    source: str,
    severity: str = "info",
) -> dict[str, Any]:
    item = {
        "id": f"demo:{source}:{int(time.time() * 1000)}",
        "title": title,
        "description": description,
        "source": source,
        "severity": severity,
        "ts": time.time(),
    }
    stream_publish(_RECEIVER_ID, item, _meta={"webspace_id": webspace_id})
    return item


def _snapshot() -> dict[str, Any]:
    rows = [
        {
            "id": "cpu",
            "title": "CPU Load",
            "status": "healthy",
            "value": 42,
            "unit": "%",
            "updated_at": "2026-05-07T10:00:00Z",
            "group": "compute",
        },
        {
            "id": "memory",
            "title": "Memory Pressure",
            "status": "warning",
            "value": 76,
            "unit": "%",
            "updated_at": "2026-05-07T10:00:00Z",
            "group": "compute",
        },
        {
            "id": "queue",
            "title": "Queue Depth",
            "status": "healthy",
            "value": 7,
            "unit": "jobs",
            "updated_at": "2026-05-07T10:00:00Z",
            "group": "runtime",
        },
    ]
    tree = {
        "root": {
            "id": "demo-metrics-root",
            "title": "Demo metrics",
            "subtitle": "Taiga UI Tree hierarchy",
            "children": [
                {
                    "id": "compute",
                    "title": "Compute",
                    "subtitle": "2 metrics",
                    "children": [
                        {
                            "id": "cpu",
                            "title": "CPU Load",
                            "subtitle": "Updated 10:00",
                            "status": "healthy",
                            "value": 42,
                            "unit": "%",
                        },
                        {
                            "id": "memory",
                            "title": "Memory Pressure",
                            "subtitle": "Updated 10:00",
                            "status": "warning",
                            "value": 76,
                            "unit": "%",
                        },
                    ],
                },
                {
                    "id": "runtime",
                    "title": "Runtime",
                    "subtitle": "1 metric",
                    "children": [
                        {
                            "id": "queue",
                            "title": "Queue Depth",
                            "subtitle": "Updated 10:00",
                            "status": "healthy",
                            "value": 7,
                            "unit": "jobs",
                        }
                    ],
                },
            ],
        }
    }
    series = {
        "metric_id": "cpu",
        "title": "CPU Load",
        "x_key": "ts",
        "y_key": "value",
        "series_by_metric": {
            "cpu": {
                "metric_id": "cpu",
                "title": "CPU Load",
                "points": [
                    {"ts": "10:00", "value": 31},
                    {"ts": "10:05", "value": 34},
                    {"ts": "10:10", "value": 39},
                    {"ts": "10:15", "value": 42},
                ],
            },
            "memory": {
                "metric_id": "memory",
                "title": "Memory Pressure",
                "points": [
                    {"ts": "10:00", "value": 62},
                    {"ts": "10:05", "value": 68},
                    {"ts": "10:10", "value": 74},
                    {"ts": "10:15", "value": 76},
                ],
            },
            "queue": {
                "metric_id": "queue",
                "title": "Queue Depth",
                "points": [
                    {"ts": "10:00", "value": 4},
                    {"ts": "10:05", "value": 6},
                    {"ts": "10:10", "value": 5},
                    {"ts": "10:15", "value": 7},
                ],
            },
        },
        "points": [
            {"ts": "10:00", "value": 31},
            {"ts": "10:05", "value": 34},
            {"ts": "10:10", "value": 39},
            {"ts": "10:15", "value": 42},
        ],
    }
    events = {
        "items": [
            {
                "id": "evt-1",
                "title": "Initial demo snapshot",
                "description": "Shared demo metrics payload seeded for the current webspace.",
            },
            {
                "id": "evt-2",
                "title": "Chart selection linked",
                "description": "The selected table row drives the chart series payload.",
            },
        ]
    }
    chat = {
        "messages": [
            {
                "id": "chat-1",
                "from": "hub",
                "text": "Semantic chat_panel is now part of the demo surface.",
                "ts": "2026-05-07T10:00:00Z",
            },
            {
                "id": "chat-2",
                "from": "operator",
                "text": "The first rollout keeps chat read-only and shared-state backed.",
                "ts": "2026-05-07T10:01:00Z",
            },
        ]
    }
    return {
        "summary": {
            "value": "3",
            "label": "Demo metrics",
            "description": "Neutral semantic Web UI control task",
            "buttons": [
                {"id": "open-demo", "label": "Open modal"},
                {"id": "open-workbench", "label": "Resource Workbench"},
                {"id": "open-workspace", "label": "Data workspace"},
                {"id": "open-operations", "label": "Runtime operations"},
                {"id": "emit-skill", "label": "Skill event"},
                {"id": "emit-host", "label": "Host event"},
            ],
        },
        "table": {"items": rows},
        "tree": tree,
        "chart": series,
        "selection": {
            "metric_id": "cpu",
            "status_filter": "all",
            "group_filter": "all",
        },
        "events": events,
        "chat": chat,
        "resource_workbench": _resource_workbench_snapshot(),
    }


@tool(
    "get_demo_snapshot",
    summary="Return the current static snapshot for the demo metrics browser surfaces.",
    stability="experimental",
)
def get_demo_snapshot(request: Mapping[str, Any]) -> dict[str, Any]:
    _ = request
    return {"ok": True, "snapshot": _snapshot()}


@tool(
    "get_resource_workbench_snapshot",
    summary="Return the Declarative Resource Workbench demo snapshot.",
    stability="experimental",
)
def get_resource_workbench_snapshot(request: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _resource_workbench_snapshot(_command_body(request))
    return {"ok": True, "snapshot": snapshot, "items": snapshot["definitions"]["items"]}


@tool(
    "list_resource_role_matrix",
    summary="Return resource role-policy rows for the Demo Metrics Resource Workbench.",
    stability="experimental",
)
def list_resource_role_matrix(request: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _resource_workbench_snapshot(_command_body(request))
    items = snapshot["roles"]["items"]
    return {"ok": True, "resource_type": "resource.role_policy", "items": items, "count": len(items)}


@tool(
    "query_resource_workbench",
    summary="Run a typed resource query for the demo workbench resource set.",
    stability="experimental",
)
def query_resource_workbench(request: Mapping[str, Any]) -> dict[str, Any]:
    body = _command_body(request)
    resource_type = _text(body.get("resource_type")) or "demo.metric"
    if resource_type not in _WORKBENCH_RESOURCE_TYPES:
        return {
            "ok": False,
            "resource_type": resource_type,
            "items": [],
            "count": 0,
            "error_type": "unsupported_resource_type",
            "error": f"unsupported demo resource_type: {resource_type}",
        }
    try:
        result = _query_resource(
            ResourceWorkbenchService(),
            resource_type,
            filters=_mapping(body.get("filters")),
            search=_text(body.get("search")),
            limit=int(body.get("limit") or 50),
            actor=_workbench_actor(body),
        )
        if resource_type == "demo.metric_note":
            return _with_builder_e2e_note(result, search=_text(body.get("search")))
        if resource_type == "demo.metric":
            return _with_open_dev_tickets_metric(
                ResourceWorkbenchService(), result, actor=_workbench_actor(body)
            )
        return result
    except Exception as exc:
        return {
            "ok": False,
            "resource_type": resource_type,
            "items": [],
            "count": 0,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


@tool(
    "operate_metric_note",
    summary="Execute a typed CRUD operation against the demo.metric_note prototype store.",
    stability="experimental",
)
def operate_metric_note(request: Mapping[str, Any]) -> dict[str, Any]:
    body = _command_body(request)
    operation_id = _text(body.get("operation_id")) or "create"
    try:
        result = ResourceWorkbenchService().operate(
            {
                "schema": "adaos.resource.operation.v1",
                "resource_type": "demo.metric_note",
                "operation_id": operation_id,
                "record_id": _text(body.get("record_id")),
                "payload": _mapping(body.get("payload")),
                "actor": _workbench_actor(body),
                "subject": _mapping(body.get("subject")),
                "expected_revision": body.get("expected_revision"),
                "context": {
                    "surface": "demo_metrics_resource_workbench",
                    **_mapping(body.get("context")),
                },
            }
        )
        item = _publish_demo_event(
            webspace_id=_webspace_id_from_payload(body),
            title=f"Workbench operation: {operation_id}",
            description=f"demo.metric_note {operation_id} completed through the Resource Workbench contract.",
            source="resource_workbench",
            severity="success",
        )
        return {"ok": True, **result, "event": item, "snapshot": _resource_workbench_snapshot(body)}
    except ResourceAccessDenied as exc:
        return _failed_resource_operation(body, operation_id, "permission_denied", str(exc))
    except ResourceConflict as exc:
        return _failed_resource_operation(body, operation_id, "conflict", str(exc))
    except Exception as exc:
        return _failed_resource_operation(body, operation_id, type(exc).__name__, str(exc))


def _failed_resource_operation(
    payload: Mapping[str, Any],
    operation_id: str,
    error_type: str,
    error: str,
) -> dict[str, Any]:
    _publish_demo_event(
        webspace_id=_webspace_id_from_payload(payload),
        title=f"Workbench operation failed: {operation_id}",
        description=error,
        source="resource_workbench",
        severity="warning",
    )
    return {
        "ok": False,
        "resource_type": "demo.metric_note",
        "operation_id": operation_id,
        "error_type": error_type,
        "error": error,
        "snapshot": _resource_workbench_snapshot(payload),
    }


@tool(
    "list_demo_series",
    summary="Return one chart payload for the demo metrics skill.",
    stability="experimental",
)
def list_demo_series(request: Mapping[str, Any]) -> dict[str, Any]:
    metric_id = ""
    if isinstance(request, Mapping):
        metric_id = str(request.get("metric_id") or "").strip()
    snap = _snapshot()
    if metric_id:
        series = snap["chart"].get("series_by_metric", {}).get(metric_id)
        if isinstance(series, Mapping):
            snap["chart"] = {
                **snap["chart"],
                **series,
                "metric_id": metric_id,
            }
    return {"ok": True, "series": snap["chart"]}


@tool(
    "emit_demo_event",
    summary="Publish one live demo event into the browser event stream.",
    stability="experimental",
)
def emit_demo_event(request: Mapping[str, Any]) -> dict[str, Any]:
    body = request if isinstance(request, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    action_id = str(body.get("action_id") or "skill_action").strip() or "skill_action"
    metric_id = str(body.get("metric_id") or "").strip() or "current"
    item = _publish_demo_event(
        webspace_id=webspace_id,
        title=f"Skill action: {action_id}",
        description=f"demo_metrics_skill emitted a live event for metric `{metric_id}`.",
        source="skill",
        severity="success",
    )
    return {"ok": True, "event": item}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, Mapping):
        return
    receiver = str(payload.get("receiver") or "").strip()
    if receiver != _RECEIVER_ID:
        return
    webspace_id = _webspace_id_from_payload(payload)
    _publish_demo_event(
        webspace_id=webspace_id,
        title="Stream attached",
        description="The demo event stream is now subscribed for this browser session.",
        source="stream.snapshot",
    )


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, Mapping):
        return
    receiver = str(payload.get("receiver") or "").strip()
    if receiver != _RECEIVER_ID:
        return
    action = str(payload.get("action") or "").strip().lower() or "subscribed"
    if action == "unsubscribed":
        return
    webspace_id = _webspace_id_from_payload(payload)
    _publish_demo_event(
        webspace_id=webspace_id,
        title="Subscription changed",
        description="A browser consumer subscribed to the demo metrics event feed.",
        source="stream.subscription",
    )


@subscribe("demo_metrics.host_action")
def on_demo_metrics_host_action(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, Mapping):
        return
    webspace_id = _webspace_id_from_payload(payload)
    metric_id = str(payload.get("metric_id") or "").strip() or "current"
    action_id = str(payload.get("action_id") or "host_action").strip() or "host_action"
    _publish_demo_event(
        webspace_id=webspace_id,
        title=f"Host action: {action_id}",
        description=f"Host event accepted for metric `{metric_id}` and mirrored into the live stream.",
        source="host",
        severity="warning",
    )
