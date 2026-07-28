from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from adaos.sdk import conversation
from adaos.sdk.builder import automation, preview, workflow
from adaos.sdk.core.decorators import tool
from adaos.sdk.developer import projects, prompt_context
from adaos.sdk.llm.llm_client import list_llm_models

SKILL_ID = "builder_sdk_control_skill"
DEFAULT_PROJECT_KIND = "scenario"
DEFAULT_PROJECT_ID = "builder"
MAX_LIFECYCLE_CHILDREN = 5


def _webspace_id(value: str | None, meta: Mapping[str, Any] | None) -> str:
    metadata = meta if isinstance(meta, Mapping) else {}
    return str(value or metadata.get("webspace_id") or metadata.get("source_webspace_id") or "desktop").strip() or "desktop"


def _preview_source_webspace_id(value: str | None, meta: Mapping[str, Any] | None) -> str:
    candidate = _webspace_id(value, meta)
    try:
        return preview.canonical_source_webspace_id(candidate)
    except RuntimeError:
        # Read-only collection tools also run in isolated validation without an AgentContext.
        return candidate


def _preview_dev_webspace_id(source: str) -> str:
    try:
        return preview.dev_webspace_id(source)
    except RuntimeError:
        token = str(source or "desktop").strip() or "desktop"
        return token if token.endswith("-dev") else f"{token}-dev"


def _identity(object_type: str | None, object_id: str | None) -> tuple[str, str]:
    kind = str(object_type or DEFAULT_PROJECT_KIND).strip().lower().rstrip("s")
    project_id = str(object_id or DEFAULT_PROJECT_ID).strip()
    if kind not in {"scenario", "skill"}:
        raise ValueError("object_type must be scenario or skill")
    if not project_id:
        raise ValueError("object_id is required")
    return kind, project_id


def _context(kind: str, project_id: str) -> dict[str, Any]:
    try:
        return prompt_context.get(kind, project_id)
    except Exception:
        return {
            "ok": False,
            "object_type": kind,
            "object_id": project_id,
            "workflow_state": "tz",
            "archived": False,
        }


def _workflow_projection(kind: str, project_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    try:
        return workflow.get_state(kind, project_id)
    except Exception:
        legacy = state if isinstance(state, Mapping) else _context(kind, project_id)
        token = str(legacy.get("workflow_state") or "prototype").strip().lower()
        active = "automation" if token in {"automation", "publication"} else "prototype"
        automation_status = "completed" if token == "publication" else ("working" if active == "automation" else "not_started")
        return {
            "active_phase": active,
            "prototype": {"status": "working" if active == "prototype" else "frozen"},
            "automation": {"status": automation_status},
            "delivery": {"status": "published" if token == "publication" else "idle"},
            "publication": {"status": "published" if token == "publication" else "not_started"},
            "change_set": None,
            "capabilities": {
                "can_edit_prototype": active == "prototype",
                "can_stabilize_prototype": active == "prototype",
                "can_handoff_to_automation": active == "prototype",
                "can_edit_automation": active == "automation",
                "can_return_to_prototype": active == "automation" and automation_status == "completed",
                "can_prepare_candidate": active == "automation" and automation_status == "completed",
                "can_decide_candidate": False,
                "can_publish": False,
                "can_preview_prototype": kind == "scenario",
                "can_preview_automation": kind == "scenario" and automation_status == "completed",
                "can_preview_publication": kind == "scenario" and token == "publication",
                "can_plan_change_set": True,
                "can_update_change_set": False,
            },
        }


def _probe(operation: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = operation()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if isinstance(value, Mapping):
        return {"ok": bool(value.get("ok", True)), "value": dict(value)}
    if isinstance(value, list):
        return {"ok": True, "value": list(value)}
    return {"ok": True, "value": value}


def _list_text(values: Any, *, empty: str = "None") -> str:
    if not isinstance(values, (list, tuple)):
        return empty
    items = [str(item).strip() for item in values if str(item).strip()]
    return ", ".join(items) if items else empty


def _change_text(value: Any) -> str:
    changes = value if isinstance(value, Mapping) else {}
    parts: list[str] = []
    for key in ("added", "changed", "removed", "retained"):
        items = changes.get(key)
        if isinstance(items, (list, tuple)) and items:
            parts.append(f"{key}: {', '.join(str(item) for item in items)}")
    return "; ".join(parts) if parts else "No component changes"


def _dependency_text(value: Any) -> str:
    dependencies = value if isinstance(value, list) else []
    items: list[str] = []
    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            continue
        kind = str(dependency.get("kind") or "artifact").strip()
        artifact_id = str(dependency.get("artifact_id") or "").strip()
        version = str(dependency.get("version") or "").strip()
        if artifact_id:
            items.append(f"{kind}:{artifact_id}" + (f" @ {version}" if version else ""))
    return "; ".join(items) if items else "No resolved dependencies"


def _subscription_update_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    subscription = value.get("subscription") if isinstance(value.get("subscription"), Mapping) else {}
    pointer = value.get("pointer") if isinstance(value.get("pointer"), Mapping) else {}
    plan = value.get("update_plan") if isinstance(value.get("update_plan"), Mapping) else {}
    activation = plan.get("activation") if isinstance(plan.get("activation"), Mapping) else {}
    permissions = activation.get("permissions") if isinstance(activation.get("permissions"), Mapping) else {}
    schemas = activation.get("schemas") if isinstance(activation.get("schemas"), Mapping) else {}
    migrations = activation.get("migrations") if isinstance(activation.get("migrations"), Mapping) else {}
    rollback = activation.get("rollback") if isinstance(activation.get("rollback"), Mapping) else {}
    warnings = activation.get("warnings") if isinstance(activation.get("warnings"), list) else []
    available = bool(value.get("available"))
    allowed = bool(value.get("activation_allowed"))
    return {
        "ok": bool(value.get("ok", True)),
        "subscribed": True,
        "status": "update_available" if available else "up_to_date",
        "available": available,
        "activation_allowed": allowed,
        "policy": subscription.get("policy"),
        "current_release": subscription.get("installed_release"),
        "target_release": activation.get("target_release") or pointer.get("release"),
        "plan_digest": plan.get("plan_digest"),
        "observed_lock_digest": activation.get("observed_lock_digest"),
        "components": _change_text(activation.get("component_changes")),
        "dependencies": _dependency_text(activation.get("resolved_dependencies")),
        "permissions": _list_text(permissions.get("introduced"), empty="No new permissions"),
        "permission_removals": _list_text(permissions.get("removed"), empty="None"),
        "schemas": (
            f"added {len(schemas.get('added') or [])}, "
            f"changed {len(schemas.get('changed') or [])}, "
            f"removed {len(schemas.get('removed') or [])}"
        ),
        "migrations": (
            f"{int(migrations.get('count') or 0)}; "
            f"rollback {'ready' if migrations.get('rollback_ready') else 'not required or unavailable'}"
        ),
        "rollback_available": bool(rollback.get("available")),
        "rollback_reason": rollback.get("reason"),
        "runtime_checks": "reload + health verification required",
        "warnings": _list_text(warnings, empty="None"),
        "raw": dict(value),
    }


def _record_project_change(
    *,
    kind: str,
    project_id: str,
    action: str,
    summary: str,
    webspace_id: str,
    path: str | None = None,
    commit: str | None = None,
    meta: Mapping[str, Any] | None = None,
    change_id: str | None = None,
    status: str | None = None,
    source_message_ids: list[str] | None = None,
) -> dict[str, Any]:
    topic = conversation.ensure_builder_topic(
        webspace_id=webspace_id,
        scenario_id=project_id if kind == "scenario" else None,
        dev_webspace_id=_preview_dev_webspace_id(webspace_id),
        project_id=project_id,
        title=f"Builder: {project_id}",
        meta={"artifact_kind": kind, "artifact_id": project_id},
    )
    conversation_id = str(topic.get("conversation_id") or "conv.skill.builder_skill.default")
    thread_id = str(topic.get("thread_id") or topic.get("topic_id") or "").strip() or None
    topic_id = str(topic.get("topic_id") or thread_id or "").strip() or None
    change_id = str(change_id or "").strip() or f"builder-{action}-{uuid4().hex}"
    artifact_ref = {"kind": kind, "id": project_id}
    if path:
        artifact_ref["path"] = path
    evidence = conversation.upsert_development_change(
        change_id=change_id,
        conversation_id=conversation_id,
        thread_id=thread_id,
        topic_id=topic_id,
        status=str(status or ("pushed" if action == "checkpoint" else "accepted")),
        source_message_ids=source_message_ids,
        source_refs={"kind": "builder_ui", "action": action, "webspace_id": webspace_id},
        artifact_refs=[artifact_ref],
        commit_refs=[{"commit": commit}] if commit else [],
        summary=summary,
        meta={"skill_id": SKILL_ID, **dict(meta or {})},
    )
    return dict(evidence or {"change_id": change_id, "status": "recorded"})


def _sync_change_set_record(
    *,
    kind: str,
    project_id: str,
    webspace_id: str,
    change_set: Mapping[str, Any],
) -> dict[str, Any]:
    change_set_id = str(change_set.get("change_set_id") or "").strip()
    if not change_set_id:
        raise ValueError("change_set_id is required")
    topic = conversation.ensure_builder_topic(
        webspace_id=webspace_id,
        scenario_id=project_id if kind == "scenario" else None,
        dev_webspace_id=_preview_dev_webspace_id(webspace_id),
        project_id=project_id,
        title=f"Builder: {project_id}",
        meta={"artifact_kind": kind, "artifact_id": project_id},
    )
    conversation_id = str(topic.get("conversation_id") or "conv.skill.builder_skill.default")
    topic_id = str(topic.get("topic_id") or "").strip() or None
    thread_id = str(topic.get("thread_id") or topic_id or "").strip() or None
    evidence = conversation.upsert_development_change(
        change_id=change_set_id,
        conversation_id=conversation_id,
        thread_id=thread_id,
        topic_id=topic_id,
        status=str(change_set.get("status") or "planned"),
        source_message_ids=list(change_set.get("source_message_ids") or []),
        source_refs={
            "kind": "builder_change_set",
            "action": "plan",
            "webspace_id": webspace_id,
        },
        artifact_refs=[{"kind": kind, "id": project_id}],
        summary=str(change_set.get("request") or "Builder change set"),
        meta={"skill_id": SKILL_ID, "change_set": dict(change_set)},
    )
    return dict(evidence or {"change_id": change_set_id, "status": change_set.get("status")})


def _version_title(value: Any) -> str:
    token = str(value or "DEV").strip() or "DEV"
    return token if token.lower().startswith("v") else f"v {token}"


def _datetime_value(value: Any) -> str | None:
    """Return one UTC ISO representation for every Lifecycle timestamp."""

    if value in (None, ""):
        return None
    parsed: datetime | None = None
    if isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        token = str(value).strip()
        if not token:
            return None
        try:
            parsed = datetime.fromtimestamp(float(token), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(token.replace("Z", "+00:00"))
            except ValueError:
                return token
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _automation_children(
    projection: Mapping[str, Any],
    *,
    project_version: str | None = None,
) -> list[dict[str, Any]]:
    """Build one bounded, browser-ready node for the current automation task."""
    status = str(projection.get("status") or "idle")
    task = projection.get("task") if isinstance(projection.get("task"), Mapping) else {}
    result = projection.get("result") if isinstance(projection.get("result"), Mapping) else {}
    if not result and isinstance(task.get("result"), Mapping):
        result = task["result"]
    progress = projection.get("progress") if isinstance(projection.get("progress"), Mapping) else {}
    raw_evidence = projection.get("evidence") or task.get("evidence") or result.get("evidence") or {}
    task_id = projection.get("task_id") or task.get("task_id") or task.get("id")
    phase = projection.get("phase") or task.get("phase")
    error = projection.get("error") or task.get("error") or result.get("error")
    summary = (
        projection.get("summary")
        or projection.get("result_summary")
        or task.get("summary")
        or result.get("summary")
        or progress.get("message")
    )
    result_branch = (
        projection.get("result_branch")
        or projection.get("branch")
        or task.get("result_branch")
        or task.get("branch")
        or result.get("result_branch")
        or result.get("branch")
    )
    version = (
        result.get("version")
        or task.get("version")
        or projection.get("version")
        or project_version
        or "DEV"
    )
    source_prototype_version = projection.get("source_prototype_version") or project_version
    updated_at = projection.get("updated_at") or task.get("updated_at") or result.get("updated_at")
    if isinstance(raw_evidence, Mapping):
        evidence_items = [
            {"kind": str(key).removesuffix("_path"), "path": value}
            for key, value in raw_evidence.items()
            if value not in (None, "", [], {})
        ][:MAX_LIFECYCLE_CHILDREN]
    elif isinstance(raw_evidence, list):
        evidence_items = list(raw_evidence[:MAX_LIFECYCLE_CHILDREN])
    else:
        evidence_items = []
    has_result = any((task_id, phase, summary, error, result_branch, evidence_items))
    if status == "idle" and not has_result:
        return []
    return [
        {
            "id": f"automation-task-{task_id or 'current'}",
            "kind": "automation_result",
            "lifecycleState": "failed" if error else ("current" if status not in {"completed", "succeeded"} else "complete"),
            "title": _version_title(version),
            "version": str(version),
            "updated_at": _datetime_value(updated_at),
            "source_prototype_version": source_prototype_version,
            "lifecycleStage": "automation",
            "conversationLabel": "Automation conversation",
            "status": status,
            "phase": phase,
            "summary": summary,
            "error": error,
            "task_id": task_id,
            "revision": str(task_id or version),
            "result_branch": result_branch,
            "evidence": evidence_items,
            "canPreview": status in {"completed", "succeeded", "failed"},
            "children": [],
        }
    ]


def _publication_children(kind: str, project_id: str) -> list[dict[str, Any]]:
    """Project only durable, successful publication Builder Changes."""
    children: list[dict[str, Any]] = []
    try:
        changes = conversation.list_development_changes(
            artifact_kind=kind,
            artifact_id=project_id,
            limit=MAX_LIFECYCLE_CHILDREN * 4,
        )
    except Exception:
        changes = []
    for change in changes:
        source_refs = change.get("source_refs") if isinstance(change.get("source_refs"), Mapping) else {}
        meta = change.get("meta") if isinstance(change.get("meta"), Mapping) else {}
        if (
            source_refs.get("action") != "publication"
            or meta.get("dry_run") is True
            or str(change.get("status") or "accepted") not in {"accepted", "published", "recorded", "succeeded"}
        ):
            continue
        change_id = str(change.get("change_id") or change.get("id") or "publication")
        version = meta.get("version") or meta.get("release") or "DEV"
        changed_at = change.get("updated_at") or change.get("created_at")
        children.append(
            {
                "id": f"publication-{change_id}",
                "kind": "publication_release",
                "lifecycleState": "complete",
                "title": _version_title(version),
                "status": str(change.get("status") or "accepted"),
                "change_id": change_id,
                "version": version,
                "revision": str(version),
                "release": meta.get("release"),
                "created_at": _datetime_value(changed_at),
                "updated_at": _datetime_value(changed_at),
                "lifecycleStage": "publication",
                "conversationLabel": "Publication",
                "source_automation_task": meta.get("source_automation_task"),
                "source_automation_version": meta.get("source_automation_version"),
                "source_prototype_revision": meta.get("source_prototype_revision"),
                "change_set_id": meta.get("change_set_id"),
                "canPreview": False,
                "children": [],
                "evidence": dict(change),
            }
        )
        if len(children) >= MAX_LIFECYCLE_CHILDREN:
            break
    return children


@tool("list_projects", summary="List bounded DEV projects for Builder.", side_effects="none")
def list_projects(
    kind: str | None = None,
    query: str | None = None,
    limit: int = 200,
    selected_object_type: str | None = None,
    selected_object_id: str | None = None,
    webspace_id: str | None = None,
    include_archived: bool = False,
    _meta: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    needle = str(query or "").strip().casefold()
    selected_kind = str(selected_object_type or "").strip().lower().rstrip("s")
    selected_id = str(selected_object_id or "").strip()
    source = _preview_source_webspace_id(webspace_id, _meta)
    items: list[dict[str, Any]] = []
    for item in projects.list_projects(kind=kind, limit=limit):
        raw_object_id = str(item.get("id") or item.get("name") or "").strip()
        if not raw_object_id or raw_object_id.startswith((".", "_")):
            continue
        object_type, object_id = _identity(str(item.get("kind") or kind), raw_object_id)
        title = str(item.get("title") or item.get("name") or object_id)
        description = str(item.get("description") or "")
        if needle and needle not in f"{object_id} {title} {description}".casefold():
            continue
        state = _context(object_type, object_id)
        if state.get("archived") and not include_archived:
            continue
        current = object_type == selected_kind and object_id == selected_id
        items.append(
            {
                **dict(item),
                "id": f"{object_type}:{object_id}",
                "object_type": object_type,
                "object_id": object_id,
                "title": title,
                "subtitle": description or f"{object_type} · {item.get('version') or 'DEV'}",
                "type": "Сценарий" if object_type == "scenario" else "Навык",
                "type_i18n": {
                    "key": "builder.project_type.scenario"
                    if object_type == "scenario"
                    else "builder.project_type.skill"
                },
                "stage": "Архив" if state.get("archived") else "Прототип",
                "stage_i18n": {
                    "key": "builder.project_stage.archive"
                    if state.get("archived")
                    else "builder.project_stage.prototype"
                },
                "version": str(item.get("version") or "DEV"),
                "stable": str(item.get("version") or "—"),
                "space": _preview_dev_webspace_id(source),
                "sync": "Текущий" if current else "Доступен в DEV",
                "sync_i18n": {
                    "key": "builder.project_sync.current"
                    if current
                    else "builder.project_sync.available_dev"
                },
                "updated": str(state.get("updated_at") or "DEV"),
                "current": current,
                "archived": bool(state.get("archived")),
                "builder_llm_model": state.get("builder_llm_model"),
            }
        )
    return items


@tool("get_project", summary="Describe the selected DEV project.", side_effects="none")
def get_project(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    item = projects.describe(kind, project_id)
    state = _context(kind, project_id)
    workflow_projection = _workflow_projection(kind, project_id, state)
    capabilities = (
        workflow_projection.get("capabilities")
        if isinstance(workflow_projection.get("capabilities"), Mapping)
        else {}
    )
    source = _preview_source_webspace_id(webspace_id, _meta)
    prototype_projection = (
        workflow_projection.get("prototype")
        if isinstance(workflow_projection.get("prototype"), Mapping)
        else {}
    )
    automation_projection = (
        workflow_projection.get("automation")
        if isinstance(workflow_projection.get("automation"), Mapping)
        else {}
    )
    change_set_projection = (
        workflow_projection.get("change_set")
        if isinstance(workflow_projection.get("change_set"), Mapping)
        else {}
    )
    active_phase = str(workflow_projection.get("active_phase") or "prototype")
    working_ref = (
        prototype_projection.get("head_revision")
        if active_phase == "prototype"
        else automation_projection.get("result_version") or automation_projection.get("head_task_id")
    )
    working_label = f"WORKING: {active_phase.title()}"
    if working_ref:
        working_label += f" · {working_ref}"
    try:
        preview_binding = preview.get_binding(source)
    except Exception:
        preview_binding = {}
    preview_target = (
        preview_binding.get("preview_target")
        if isinstance(preview_binding.get("preview_target"), Mapping)
        else {}
    )
    viewing_label = str(preview_target.get("label") or "Preview: not selected")
    viewing_stage = str(preview_target.get("stage") or "")
    viewing_revision = str(preview_target.get("revision") or "")
    active_ref = str(working_ref or "")
    viewing_read_only = bool(
        viewing_stage
        and (viewing_stage != active_phase or (viewing_revision and active_ref and viewing_revision != active_ref))
    )
    if viewing_read_only:
        viewing_label += " · READ ONLY"
    return {
        **item,
        "object_type": kind,
        "object_id": project_id,
        "project_ref": f"{kind}:{project_id}",
        "project_type": str(item.get("project_type") or kind),
        "dev_webspace_id": _preview_dev_webspace_id(source),
        "source_webspace_id": source,
        "stage": "DEV prototype",
        "archived": bool(state.get("archived")),
        "workflow_state": str(workflow_projection.get("active_phase") or "prototype"),
        "workflow": workflow_projection,
        "workflow_active_phase": str(workflow_projection.get("active_phase") or "prototype"),
        "workflow_generation": workflow_projection.get("generation"),
        "working_label": working_label,
        "viewing_label": viewing_label,
        "viewing_read_only": viewing_read_only,
        "can_edit_prototype": bool(capabilities.get("can_edit_prototype")),
        "can_edit_automation": bool(capabilities.get("can_edit_automation")),
        "can_return_to_prototype": bool(capabilities.get("can_return_to_prototype")),
        "can_prepare_candidate": bool(capabilities.get("can_prepare_candidate")),
        "can_decide_candidate": bool(capabilities.get("can_decide_candidate")),
        "can_publish": bool(capabilities.get("can_publish")),
        "change_set_id": change_set_projection.get("change_set_id"),
        "change_set_status": change_set_projection.get("status") or "not_planned",
        "change_set_gate": change_set_projection.get("gate"),
        "change_set_route": change_set_projection.get("route"),
        "change_set_request": change_set_projection.get("request"),
        "can_plan_change_set": bool(capabilities.get("can_plan_change_set")),
        "can_update_change_set": bool(capabilities.get("can_update_change_set")),
        "builder_llm_model": state.get("builder_llm_model"),
        "llm_provider": state.get("llm_provider"),
        "updated_at": state.get("updated_at"),
    }


@tool("list_project_objects", summary="List a project and its declared skill dependencies.", side_effects="none")
def list_project_objects(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> list[dict[str, Any]]:
    kind, project_id = _identity(object_type, object_id)
    root = projects.describe(kind, project_id)
    identities = [(kind, project_id)]
    if kind == "scenario":
        identities.extend(("skill", str(item)) for item in root.get("depends") or [] if str(item).strip())
    items: list[dict[str, Any]] = []
    for current_kind, current_id in identities:
        try:
            described = projects.describe(current_kind, current_id)
        except projects.DeveloperProjectError:
            described = {"title": current_id, "version": "", "description": "Dependency is not present in DEV"}
        state = _context(current_kind, current_id) if described.get("version") != "" else {}
        items.append(
            {
                "id": f"{current_kind}:{current_id}",
                "object_type": current_kind,
                "object_id": current_id,
                "label": str(described.get("title") or current_id),
                "title": str(described.get("title") or current_id),
                "subtitle": f"{current_kind} · {described.get('version') or 'DEV'}",
                "workflow_state": str(state.get("workflow_state") or "unavailable"),
            }
        )
    return items


@tool("list_project_files", summary="List bounded files of the selected DEV project.", side_effects="none")
def list_project_files(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    limit: int = 500,
) -> list[dict[str, Any]]:
    kind, project_id = _identity(object_type, object_id)
    items: list[dict[str, Any]] = []
    ignored_parts = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
    for item in projects.list_files(kind, project_id, limit=limit):
        relative = str(item.get("path") or "")
        path = PurePosixPath(relative)
        if ignored_parts.intersection(path.parts) or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        items.append(
            {
                **item,
                "id": relative,
                "title": path.name,
                "subtitle": relative,
                "protected": not bool(item.get("editable")),
            }
        )
    return items


@tool("list_project_file_tree", summary="List bounded project files as a nested tree.", side_effects="none")
def list_project_file_tree(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    limit: int = 500,
) -> list[dict[str, Any]]:
    kind, project_id = _identity(object_type, object_id)
    roots: list[dict[str, Any]] = []
    directories: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in list_project_files(kind, project_id, limit=limit):
        relative = str(item.get("path") or "")
        parts = PurePosixPath(relative).parts
        parent = roots
        for index, part in enumerate(parts[:-1]):
            key = tuple(parts[: index + 1])
            node = directories.get(key)
            if node is None:
                node = {"id": "/".join(key), "title": part, "kind": "directory", "children": []}
                directories[key] = node
                parent.append(node)
            parent = node["children"]
        parent.append(
            {
                **item,
                "id": relative,
                "kind": "file",
                "object_type": kind,
                "object_id": project_id,
            }
        )
    return roots


@tool("list_templates", summary="List DEV project templates by project kind.", side_effects="none")
def list_templates(object_type: str = DEFAULT_PROJECT_KIND) -> list[dict[str, Any]]:
    kind, _project_id = _identity(object_type, "template")
    return projects.list_templates(kind)


@tool("read_project_file", summary="Read one bounded DEV project text file.", side_effects="none")
def read_project_file(
    path: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    max_bytes: int = 131_072,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return projects.read_file(kind, project_id, path, max_bytes=max_bytes)


@tool("save_project_file", summary="Atomically save one allowlisted DEV project text file.", side_effects="local_write")
def save_project_file(
    path: str,
    text: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    max_bytes: int = 131_072,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    result = projects.write_file(kind, project_id, path, text, max_bytes=max_bytes)
    evidence = _record_project_change(
        kind=kind,
        project_id=project_id,
        action="file_save",
        summary=f"Saved {path}",
        webspace_id=_webspace_id(webspace_id, _meta),
        path=str(result.get("path") or path),
    )
    return {**result, "change_id": evidence.get("change_id"), "evidence": evidence}


@tool("get_prompt_context", summary="Read technical specification and Builder project preferences.", side_effects="none")
def get_prompt_context(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return _context(kind, project_id)


@tool("save_prompt_context", summary="Save the base technical specification.", side_effects="local_write")
def save_prompt_context(
    text: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    result = prompt_context.save_base(kind, project_id, text)
    evidence = _record_project_change(
        kind=kind,
        project_id=project_id,
        action="technical_specification",
        summary="Updated the base technical specification",
        webspace_id=_webspace_id(webspace_id, _meta),
        path="tz/base_tz.md",
    )
    return {**result, "change_id": evidence.get("change_id"), "evidence": evidence}


@tool("append_prompt_addendum", summary="Append one technical-specification addendum.", side_effects="local_write")
def append_prompt_addendum(
    text: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    iteration_ref: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    result = prompt_context.append_addendum(kind, project_id, text, iteration_ref=iteration_ref)
    evidence = _record_project_change(
        kind=kind,
        project_id=project_id,
        action="technical_specification_addendum",
        summary="Appended a technical-specification addendum",
        webspace_id=_webspace_id(webspace_id, _meta),
    )
    return {**result, "change_id": evidence.get("change_id"), "evidence": evidence}


def _llm_options(payload: Any) -> list[dict[str, Any]]:
    source = payload if isinstance(payload, Mapping) else {}
    raw: Any = source.get("model_profiles") or source.get("dev_model_profiles") or source.get("data") or []
    if isinstance(raw, Mapping):
        raw = raw.get("model_profiles") or raw.get("dev_model_profiles") or raw.get("data") or []
    items: list[dict[str, Any]] = []
    for value in raw if isinstance(raw, list) else []:
        item = dict(value) if isinstance(value, Mapping) else {"id": str(value), "label": str(value)}
        model = str(item.get("id") or item.get("model") or "").strip()
        if not model:
            continue
        items.append(
            {
                **item,
                "id": model,
                "model": model,
                "label": str(item.get("label") or model),
                "provider": str(
                    item.get("provider")
                    or ("openai" if model.startswith(("gpt-", "o1", "o3", "o4")) else "")
                ),
                "scope": str(item.get("scope") or "development"),
            }
        )
    if items:
        return items
    return [
        {"id": "gpt-5", "model": "gpt-5", "label": "GPT-5", "provider": "openai", "scope": "development"},
        {"id": "gpt-4.1", "model": "gpt-4.1", "label": "GPT-4.1", "provider": "openai", "scope": "development"},
        {"id": "gpt-4o-mini", "model": "gpt-4o-mini", "label": "GPT-4o mini", "provider": "openai", "scope": "development"},
    ]


@tool("get_llm_options", summary="List development LLM options and current project selection.", side_effects="none")
def get_llm_options(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    try:
        payload = list_llm_models(timeout=5, scope="development")
        source = "root"
    except Exception as exc:
        payload = {}
        source = f"fallback:{type(exc).__name__}"
    options = _llm_options(payload)
    state = _context(kind, project_id)
    selected = str(state.get("builder_llm_model") or options[0]["id"])
    for item in options:
        item["selected"] = item["id"] == selected
    return {"ok": True, "value": selected, "options": options, "source": source}


@tool("set_llm_profile", summary="Persist the selected Builder development LLM.", side_effects="local_write")
def set_llm_profile(
    model: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    selection = get_llm_options(kind, project_id)
    option = next((item for item in selection["options"] if item["id"] == str(model).strip()), None)
    if option is None:
        raise ValueError("model is not available for Builder development")
    return prompt_context.set_preferences(
        kind,
        project_id,
        llm_model=option["id"],
        llm_provider=option.get("provider"),
        llm_profile=option,
    )


@tool("update_project_metadata", summary="Update bounded project title and description.", side_effects="local_write")
def update_project_metadata(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    title: str | None = None,
    description: str | None = None,
    project_type: str | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return projects.update_metadata(
        kind,
        project_id,
        title=title,
        description=description,
        # Accepted for compatibility, but the SDK verifies that type is immutable.
        project_type=project_type,
    )


@tool("set_workflow_state", summary="Persist the selected Builder project workflow state.", side_effects="local_write")
def set_workflow_state(
    state: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    token = str(state or "").strip().lower()
    if token == "prototype_stable":
        return workflow.transition(kind, project_id, "stabilize_prototype", actor="builder.ui.compat")
    if token == "automation":
        return workflow.transition(kind, project_id, "handoff_to_automation", actor="builder.ui.compat")
    if token == "publication":
        raise ValueError("Publication is an immutable snapshot, not an active workflow phase")
    raise ValueError("use an explicit Builder workflow transition")


@tool("get_workflow", summary="Read the authoritative Builder workflow state.", side_effects="none")
def get_workflow(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return workflow.get_state(kind, project_id)


@tool("transition_workflow", summary="Apply one validated Builder workflow transition.", side_effects="local_write")
def transition_workflow(
    action: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return workflow.transition(kind, project_id, action, actor="builder.ui")


@tool("plan_change_set", summary="Project one user request into an executable Builder change set.", side_effects="local_write")
def plan_change_set(
    request: str,
    issues: list[Mapping[str, Any]],
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    change_set_id: str | None = None,
    supersedes_change_set_id: str | None = None,
    source_message_ids: list[str] | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an LLM- or user-structured request without pretending it is a global AdaOS Issue."""

    kind, project_id = _identity(object_type, object_id)
    selected_change_set_id = str(change_set_id or "").strip() or f"builder-change-set-{uuid4().hex}"
    source = _webspace_id(webspace_id, _meta)
    result = workflow.transition(
        kind,
        project_id,
        "plan_change_set",
        actor="builder.change_planner",
        metadata={
            "change_set_id": selected_change_set_id,
            "request": request,
            "issues": [dict(item) for item in issues if isinstance(item, Mapping)],
            "source_message_ids": list(source_message_ids or []),
            "supersedes_change_set_id": str(supersedes_change_set_id or "").strip() or None,
        },
    )
    projection = result.get("workflow") if isinstance(result.get("workflow"), Mapping) else {}
    change_set = projection.get("change_set") if isinstance(projection.get("change_set"), Mapping) else {}
    evidence: dict[str, Any] = {}
    evidence_error: str | None = None
    try:
        evidence = _sync_change_set_record(
            kind=kind,
            project_id=project_id,
            webspace_id=source,
            change_set=change_set,
        )
    except Exception as exc:
        evidence_error = f"{type(exc).__name__}: {exc}"
    return {
        **result,
        "change_set": dict(change_set),
        "evidence": evidence or None,
        "evidence_synced": evidence_error is None,
        "evidence_error": evidence_error,
    }


@tool("add_change_issues", summary="Add follow-up issues to the active Builder change set.", side_effects="local_write")
def add_change_issues(
    request: str,
    issues: list[Mapping[str, Any]],
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    change_set_id: str | None = None,
    change_id: str | None = None,
    source_message_ids: list[str] | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extend one active change set while preserving its prior evidence and identity."""

    kind, project_id = _identity(object_type, object_id)
    source = _webspace_id(webspace_id, _meta)
    current = workflow.get_state(kind, project_id)
    active = current.get("change_set") if isinstance(current.get("change_set"), Mapping) else {}
    active_id = str(active.get("change_set_id") or "").strip()
    selected_id = str(change_set_id or active_id).strip()
    if not selected_id:
        raise ValueError("an active change set is required")
    result = workflow.transition(
        kind,
        project_id,
        "change_issues_added",
        actor="builder.change_planner",
        metadata={
            "change_set_id": selected_id,
            "change_id": str(change_id or "").strip() or f"builder_change_{uuid4().hex[:12]}",
            "request": request,
            "issues": [dict(item) for item in issues if isinstance(item, Mapping)],
            "source_message_ids": list(source_message_ids or []),
        },
    )
    projection = result.get("workflow") if isinstance(result.get("workflow"), Mapping) else {}
    updated = projection.get("change_set") if isinstance(projection.get("change_set"), Mapping) else {}
    evidence: dict[str, Any] = {}
    evidence_error: str | None = None
    try:
        evidence = _sync_change_set_record(
            kind=kind,
            project_id=project_id,
            webspace_id=source,
            change_set=updated,
        )
    except Exception as exc:
        evidence_error = f"{type(exc).__name__}: {exc}"
    return {
        **result,
        "change_set": dict(updated),
        "evidence": evidence or None,
        "evidence_synced": evidence_error is None,
        "evidence_error": evidence_error,
    }


@tool("get_change_set", summary="Read the active Builder change set and its durable evidence.", side_effects="none")
def get_change_set(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    projection = workflow.get_state(kind, project_id)
    change_set = projection.get("change_set") if isinstance(projection.get("change_set"), Mapping) else {}
    change_set_id = str(change_set.get("change_set_id") or "").strip()
    evidence = conversation.get_development_change(change_set_id) if change_set_id else None
    issues = [
        {
            **dict(item),
            "id": str(item.get("issue_id") or ""),
            "subtitle": f"{item.get('lane') or 'unrouted'} · {item.get('status') or 'open'}",
            "preview": "; ".join(str(value) for value in item.get("acceptance_criteria") or []),
            "canResolve": str(item.get("status") or "open") not in {"resolved", "deferred"},
            "canReopen": str(item.get("status") or "open") in {"resolved", "deferred"},
        }
        for item in change_set.get("issues") or []
        if isinstance(item, Mapping)
    ]
    return {
        "ok": True,
        "object_type": kind,
        "object_id": project_id,
        "change_set": dict(change_set),
        "change_set_id": change_set_id or None,
        "status": change_set.get("status") or "not_planned",
        "gate": change_set.get("gate"),
        "route": change_set.get("route"),
        "request": change_set.get("request"),
        "issues": issues,
        "evidence": dict(evidence) if isinstance(evidence, Mapping) else None,
        "evidence_synced": bool(evidence),
    }


@tool("update_change_issue", summary="Update one issue item in the active Builder change set.", side_effects="local_write")
def update_change_issue(
    issue_id: str,
    status: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    change_set_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    source = _webspace_id(webspace_id, _meta)
    result = workflow.transition(
        kind,
        project_id,
        "change_issue_updated",
        actor="builder.change_planner",
        metadata={
            "change_set_id": str(change_set_id or "").strip() or None,
            "issue_id": issue_id,
            "status": status,
        },
    )
    projection = result.get("workflow") if isinstance(result.get("workflow"), Mapping) else {}
    change_set = projection.get("change_set") if isinstance(projection.get("change_set"), Mapping) else {}
    evidence = _sync_change_set_record(
        kind=kind,
        project_id=project_id,
        webspace_id=source,
        change_set=change_set,
    )
    return {**result, "change_set": dict(change_set), "evidence": evidence}


@tool("archive_project", summary="Archive or restore a DEV project in Builder.", side_effects="local_write")
def archive_project(
    archived: bool = True,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return prompt_context.set_preferences(kind, project_id, archived=archived)


@tool("select_preview", summary="Select a DEV scenario in its paired preview.", side_effects="ui_navigation")
def select_preview(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    if kind == "scenario":
        return preview.select_target(
            kind,
            project_id,
            stage="prototype",
            source_webspace_id=_preview_source_webspace_id(webspace_id, _meta),
            follow_active=True,
        )
    return preview.select_project(
        kind,
        project_id,
        source_webspace_id=_preview_source_webspace_id(webspace_id, _meta),
        ensure_ready=True,
        wait_for_rebuild=True,
        publish_event=True,
    )


@tool("select_preview_target", summary="Show one explicit Lifecycle snapshot in Preview.", side_effects="ui_navigation")
def select_preview_target(
    stage: str,
    revision: str | None = None,
    follow_active: bool = False,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return preview.select_target(
        kind,
        project_id,
        stage=stage,
        revision=revision,
        source_webspace_id=_preview_source_webspace_id(webspace_id, _meta),
        follow_active=follow_active,
    )


@tool("get_preview", summary="Read the selected Builder preview binding.", side_effects="none")
def get_preview(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = _preview_source_webspace_id(webspace_id, _meta)
    binding = preview.get_binding(source)
    opened = preview.open_workspace(source)
    target = binding.get("preview_target") if isinstance(binding.get("preview_target"), Mapping) else {}
    return {
        **binding,
        "ok": bool(binding.get("ok", True)),
        "source_webspace_id": source,
        "dev_webspace_id": str(binding.get("dev_webspace_id") or _preview_dev_webspace_id(source)),
        "preview_url": str(opened.get("url") or f"/?webspace={_preview_dev_webspace_id(source)}"),
        "qr_text": str(opened.get("url") or f"/?webspace={_preview_dev_webspace_id(source)}"),
        "status": "ready" if binding.get("runtime_scenario_id") else "not_selected",
        "preview_target": dict(target),
        "viewing": target.get("label"),
        "viewing_stage": target.get("stage"),
        "viewing_revision": target.get("revision"),
        "preview_follows_active": bool(target.get("follow_active")),
    }


@tool("list_changes", summary="List Builder Change evidence for the selected project.", side_effects="none")
def list_changes(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    kind, project_id = _identity(object_type, object_id)
    changes = conversation.list_development_changes(artifact_kind=kind, artifact_id=project_id, limit=limit)
    return [
        {
            **dict(item),
            "id": str(item.get("change_id") or item.get("id") or ""),
            "title": str(item.get("title") or item.get("summary") or item.get("change_id") or "Builder change"),
            "subtitle": str(item.get("status") or "recorded"),
            "created_at": item.get("updated_at") or item.get("created_at"),
        }
        for item in changes
    ]


@tool("get_automation", summary="Read Builder Automation state for a DEV project.", side_effects="none")
def get_automation(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    result = automation.get_state(
        object_type=kind,
        object_id=project_id,
        webspace_id=_webspace_id(webspace_id, _meta),
    )
    if result.get("error") == "automation_session_not_found" and isinstance(
        result.get("automation"), Mapping
    ):
        result = {**result, "ok": True, "session_present": False}
        result.pop("error", None)
    projection = result.get("automation") if isinstance(result.get("automation"), Mapping) else {}
    progress = projection.get("progress") if isinstance(projection.get("progress"), Mapping) else {}
    evidence = projection.get("evidence") if isinstance(projection.get("evidence"), Mapping) else {}
    return {
        **result,
        "status": projection.get("status"),
        "phase": projection.get("phase"),
        "task_id": projection.get("task_id"),
        "progress_message": progress.get("message"),
        "failure_message": projection.get("error"),
        "failure_id": projection.get("failure_id"),
        "failure_stage": projection.get("failure_stage"),
        "version": projection.get("version"),
        "updated_at": _datetime_value(projection.get("updated_at")),
        "source_prototype_version": projection.get("source_prototype_version"),
        "retryable": projection.get("retryable"),
        "diagnostic_hint": projection.get("diagnostic_hint"),
        "events_path": evidence.get("events_path"),
        "stderr_path": evidence.get("stderr_path"),
        "result_path": evidence.get("result_path"),
    }


@tool("get_lifecycle", summary="Project the prototype, automation, and publication lifecycle tree.", side_effects="none")
def get_lifecycle(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    kind, project_id = _identity(object_type, object_id)
    project = projects.describe(kind, project_id)
    state = _context(kind, project_id)
    workflow_projection = _workflow_projection(kind, project_id, state)
    workflow_capabilities = (
        workflow_projection.get("capabilities")
        if isinstance(workflow_projection.get("capabilities"), Mapping)
        else {}
    )
    active_phase = str(workflow_projection.get("active_phase") or "prototype")
    current_revision = ""
    revisions: list[str] = []
    file_updated_at: dict[str, Any] = {}
    for item in projects.list_files(kind, project_id, limit=1000):
        path = PurePosixPath(str(item.get("path") or ""))
        file_updated_at[path.as_posix()] = item.get("updated_at")
        if kind == "scenario" and len(path.parts) == 2 and path.parts[0] == "ui_revisions" and path.suffix == ".json":
            revisions.append(path.stem)
    if kind == "scenario":
        try:
            current_revision = str(projects.read_file(kind, project_id, "ui_revisions/current.txt", max_bytes=64)["content"]).strip()
        except projects.DeveloperProjectError:
            current_revision = ""
    revision_nodes: list[dict[str, Any]] = []
    for revision in sorted(set(revisions), reverse=True)[:5]:
        current = revision == current_revision
        revision_nodes.append(
            {
                "id": f"ui-revision-{revision}",
                "kind": "revision",
                "revision": revision,
                "lifecycleState": "current" if current else "past",
                "status": "текущая" if current else "предыдущая",
                "status_i18n": {
                    "key": "builder.lifecycle.status.current"
                    if current
                    else "builder.lifecycle.status.previous"
                },
                "title": f"UI {revision}" + (f" · v {project.get('version')}" if current and project.get("version") else ""),
                "version": f"UI {revision}",
                "updated_at": _datetime_value(file_updated_at.get(f"ui_revisions/{revision}.json")),
                "lifecycleStage": "prototype",
                "conversationLabel": "Prototype conversation",
                "badges": ["текущая"] if current else [],
                "canMakeCurrent": not current and active_phase == "prototype",
                "canStabilize": current and bool(workflow_capabilities.get("can_stabilize_prototype")),
                "canOpenAutomation": current and bool(workflow_capabilities.get("can_handoff_to_automation")),
                "canPreview": kind == "scenario",
            }
        )
    if not revision_nodes:
        revision_nodes.append(
            {
                "id": "project-current-version",
                "kind": "revision",
                "lifecycleState": "current",
                "status": "текущая",
                "status_i18n": {"key": "builder.lifecycle.status.current"},
                "title": f"v {project.get('version') or 'DEV'}",
                "version": str(project.get("version") or "DEV"),
                "updated_at": _datetime_value(file_updated_at.get(str(project.get("manifest") or "scenario.yaml"))),
                "lifecycleStage": "prototype",
                "conversationLabel": "Prototype conversation",
                "badges": ["текущая"],
                "canStabilize": bool(workflow_capabilities.get("can_stabilize_prototype")),
                "canOpenAutomation": bool(workflow_capabilities.get("can_handoff_to_automation")),
                "canPreview": kind == "scenario",
            }
        )
    automation_state = get_automation(kind, project_id, webspace_id, _meta)
    automation_projection = automation_state.get("automation") if isinstance(automation_state.get("automation"), Mapping) else {}
    workflow_automation = (
        workflow_projection.get("automation")
        if isinstance(workflow_projection.get("automation"), Mapping)
        else {}
    )
    automation_status = str(workflow_automation.get("status") or "not_started")
    project_version = str(project.get("version") or "DEV")
    automation_children = _automation_children(automation_projection, project_version=project_version)
    workflow_result_version = str(workflow_automation.get("result_version") or "").strip()
    if automation_children and workflow_result_version:
        automation_children[0]["version"] = workflow_result_version
        automation_children[0]["title"] = _version_title(workflow_result_version)
    publication_children = _publication_children(kind, project_id)
    publication_projection = (
        workflow_projection.get("publication")
        if isinstance(workflow_projection.get("publication"), Mapping)
        else {}
    )
    publication_active = str(publication_projection.get("status") or "") == "published" or bool(publication_children)
    prototype_updated_at = _datetime_value(next(
        (item.get("updated_at") for item in revision_nodes if item.get("updated_at")),
        file_updated_at.get(str(project.get("manifest") or ("scenario.yaml" if kind == "scenario" else "skill.yaml"))),
    ))
    automation_updated_at = _datetime_value(
        workflow_automation.get("completed_at")
        or workflow_automation.get("started_at")
        or automation_projection.get("updated_at")
        or state.get("updated_at")
    )
    publication_version = publication_children[0].get("version") if publication_children else project_version
    publication_updated_at = _datetime_value(
        publication_projection.get("published_at")
        or (publication_children[0].get("created_at") if publication_children else state.get("updated_at"))
    )
    source_prototype_revision = str(
        workflow_automation.get("source_prototype_revision")
        or automation_projection.get("source_prototype_version")
        or current_revision
        or project_version
    ).strip()
    if source_prototype_revision.lower().startswith("ui "):
        source_prototype_revision = source_prototype_revision[3:].strip()

    for node in revision_nodes:
        node["children"] = []

    def attach_to_prototype(node: dict[str, Any], source_revision: Any) -> None:
        token = str(source_revision or "").strip()
        if token.lower().startswith("ui "):
            token = token[3:].strip()
        target = next(
            (item for item in revision_nodes if str(item.get("revision") or "") == token),
            None,
        )
        if target is None:
            target = next(
                (item for item in revision_nodes if item.get("lifecycleState") == "current"),
                revision_nodes[0],
            )
            if token and token != str(target.get("revision") or target.get("version") or ""):
                node["lineageWarning"] = "source_prototype_revision_not_retained"
        target.setdefault("children", []).append(node)

    current_automation_task = str(
        workflow_automation.get("snapshot_task_id")
        or workflow_automation.get("head_task_id")
        or (automation_children[0].get("task_id") if automation_children else "")
        or ""
    ).strip()
    automation_nodes: list[dict[str, Any]] = []
    if automation_children:
        current_automation = dict(automation_children[0])
        current_automation["task_id"] = current_automation.get("task_id") or current_automation_task or None
        current_automation["canPreview"] = bool(workflow_capabilities.get("can_preview_automation"))
        current_automation["canOpenAutomation"] = True
        current_automation["canReturnToPrototype"] = bool(
            workflow_capabilities.get("can_return_to_prototype")
        )
        current_automation["source_prototype_version"] = source_prototype_revision
        current_automation["children"] = []
        automation_nodes.append(current_automation)

    current_publication_version = str(
        publication_projection.get("current_version") or publication_version or ""
    ).strip()
    for release_node in publication_children:
        release = dict(release_node)
        source_task = str(release.get("source_automation_task") or "").strip()
        source_version = str(
            release.get("source_automation_version") or release.get("version") or project_version
        ).strip()
        source_revision = release.get("source_prototype_revision") or source_prototype_revision
        automation_node = next(
            (
                item
                for item in automation_nodes
                if source_task and source_task == str(item.get("task_id") or "").strip()
            ),
            None,
        )
        if automation_node is None and not source_task and len(automation_nodes) == 1:
            only_automation = automation_nodes[0]
            if source_version == str(only_automation.get("version") or "").strip():
                automation_node = only_automation
                release["lineageInferred"] = True
        if automation_node is None:
            lineage_id = source_task or f"release-{release.get('change_id') or source_version}"
            automation_node = {
                "id": f"automation-lineage-{lineage_id}",
                "kind": "automation_result",
                "lifecycleState": "past",
                "title": _version_title(source_version),
                "version": source_version,
                "revision": source_task or source_version,
                "updated_at": release.get("updated_at"),
                "source_prototype_version": source_revision,
                "lifecycleStage": "automation",
                "conversationLabel": "Automation conversation",
                "status": "historical",
                "task_id": source_task or None,
                "canPreview": False,
                "children": [],
                "lineageInferred": True,
                "lineageWarning": "publication_source_metadata_missing",
            }
            release["lineageInferred"] = True
            automation_nodes.append(automation_node)
        release["canPreview"] = bool(
            workflow_capabilities.get("can_preview_publication")
            and str(release.get("version") or "").strip() == current_publication_version
        )
        release["canOpenPublication"] = True
        automation_node.setdefault("children", []).append(release)

    for automation_node in automation_nodes:
        attach_to_prototype(
            automation_node,
            automation_node.get("source_prototype_version") or source_prototype_revision,
        )

    return [
        {
            "id": "stage-proto",
            "kind": "stage",
            "lifecycleState": "working" if active_phase == "prototype" else "frozen",
            "title": "Прототип",
            "title_i18n": {"key": "builder.lifecycle.stage.prototype"},
            "status": "WORKING" if active_phase == "prototype" else "FROZEN",
            "status_i18n": None,
            "badges": ["текущая"],
            "canStabilize": False,
            "canPreview": False,
            "version": project_version,
            "updated_at": prototype_updated_at,
            "lifecycleStage": "prototype",
            "conversationLabel": "Prototype conversation",
            "children": revision_nodes,
            "dependentStages": ["prototype", "automation", "publication"],
            "automationStatus": automation_status,
            "publicationStatus": "published" if publication_active else "not_started",
            "automationUpdatedAt": automation_updated_at,
            "publicationUpdatedAt": publication_updated_at,
        }
    ]


@tool("start_automation", summary="Start Builder Automation from an approved brief.", side_effects="local_write")
def start_automation(
    implementation_brief: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    conversation_id: str | None = None,
    brief_path: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_state = workflow.get_state(kind, project_id)
    change_set = (
        workflow_state.get("change_set")
        if isinstance(workflow_state.get("change_set"), Mapping)
        else {}
    )
    if not change_set or str(change_set.get("status") or "") in {
        "published",
        "rejected",
        "superseded",
    }:
        planned = plan_change_set(
            request=implementation_brief,
            issues=[
                {
                    "issue_id": f"automation-{uuid4().hex[:12]}",
                    "title": " ".join(str(implementation_brief).split())[:240],
                    "lane": "automation",
                    "acceptance_criteria": [
                        f"The implementation and its tests satisfy: {' '.join(str(implementation_brief).split())}"[:500]
                    ],
                }
            ],
            object_type=kind,
            object_id=project_id,
            webspace_id=_webspace_id(webspace_id, _meta),
            _meta=_meta,
        )
        workflow_state = (
            planned.get("workflow")
            if isinstance(planned.get("workflow"), Mapping)
            else workflow.get_state(kind, project_id)
        )
        change_set = (
            workflow_state.get("change_set")
            if isinstance(workflow_state.get("change_set"), Mapping)
            else {}
        )
    return automation.start(
        object_type=kind,
        object_id=project_id,
        implementation_brief=implementation_brief,
        webspace_id=_webspace_id(webspace_id, _meta),
        conversation_id=conversation_id,
        brief_path=brief_path,
        change_set_id=str(change_set.get("change_set_id") or "").strip() or None,
    )


@tool("submit_automation", summary="Submit one follow-up Builder Automation turn.", side_effects="local_write")
def submit_automation(
    text: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return automation.submit(
        text,
        object_type=kind,
        object_id=project_id,
        webspace_id=_webspace_id(webspace_id, _meta),
    )


@tool("return_to_prototype", summary="Use the built-in LLM to derive a safe Prototype from Automation.", side_effects="local_write")
def return_to_prototype(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return automation.return_to_prototype(
        object_type=kind,
        object_id=project_id,
        webspace_id=_webspace_id(webspace_id, _meta),
    )


@tool(
    "recover_validated_automation",
    summary="Activate a preserved validated Automation result without rerunning Codex.",
    side_effects="local_write",
)
def recover_validated_automation(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return automation.recover_validated_result(
        object_type=kind,
        object_id=project_id,
    )


@tool(
    "reconcile_automation_checkpoint",
    summary="Reconcile failed Automation Forge checkpoints without rerunning Codex.",
    side_effects="external_write",
)
def reconcile_automation_checkpoint(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return automation.reconcile_checkpoint(
        object_type=kind,
        object_id=project_id,
    )


@tool("get_subscription_update", summary="Inspect one stable subscription and its reviewed update plan.")
def get_subscription_update(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    _kind, project_id = _identity(object_type, object_id)
    try:
        inspected = projects.inspect_subscription_update(project_id)
    except Exception as exc:
        message = str(exc)
        no_subscription = "no stable subscription" in message.lower()
        return {
            "ok": no_subscription,
            "subscribed": False if no_subscription else None,
            "status": "not_subscribed" if no_subscription else "unavailable",
            "available": False,
            "activation_allowed": False,
            "error": None if no_subscription else f"{type(exc).__name__}: {exc}",
        }
    return _subscription_update_projection(inspected)


@tool(
    "apply_subscription_update",
    summary="Apply one explicitly reviewed stable package update.",
    side_effects="external_write",
)
async def apply_subscription_update(
    expected_plan_digest: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    approve_permissions: bool = False,
    webspace_id: str | None = None,
    idempotency_key: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    expected = str(expected_plan_digest or "").strip()
    if not expected:
        raise ValueError("expected_plan_digest is required")
    attempt_id = (
        str(idempotency_key or "").strip()
        or f"builder-update:{kind}:{project_id}:{expected.lower()}"
    )
    permission_decision = (
        {
            "approved": True,
            "actor": "builder.user",
            "plan_digest": expected,
        }
        if approve_permissions
        else None
    )
    result = await projects.apply_subscription_update(
        kind,
        project_id,
        expected_plan_digest=expected,
        idempotency_key=attempt_id,
        permission_decision=permission_decision,
        webspace_id=_webspace_id(webspace_id, _meta),
    )
    return {**result, "idempotency_key": attempt_id}


@tool("create_project", summary="Create a DEV skill or scenario project.", side_effects="local_write")
def create_project(object_type: str, object_id: str, template: str | None = None) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    template_id = str(template or "").strip()
    if not template_id or template_id.lower() == "default":
        template_id = "scenario_default" if kind == "scenario" else "skill_default"
    return projects.create(kind, project_id, template=template_id)


@tool("delete_project", summary="Delete a project through the governed developer lifecycle.", side_effects="external_write")
def delete_project(
    confirm: bool = False,
    remove_local: bool = False,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    if not confirm:
        raise ValueError("confirm=true is required to delete a project")
    return projects.delete(kind, project_id, remove_local=remove_local)


@tool("push_project", summary="Checkpoint a DEV project in Forge.", side_effects="local_write")
def push_project(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    message: str | None = None,
    checkpoint_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    checkpoint_change_id = str(checkpoint_id or "").strip()
    if not checkpoint_change_id:
        raise ValueError("checkpoint_id is required")
    checkpoint_message = message or f"chore(builder): checkpoint {kind} {project_id}"
    checkpoint_metadata: dict[str, Any] = {"change_id": checkpoint_change_id}
    checkpoint_results: list[dict[str, Any]] = []
    if kind == "scenario":
        automation_state = get_automation(kind, project_id, webspace_id, _meta)
        automation_projection = (
            automation_state.get("automation")
            if isinstance(automation_state.get("automation"), Mapping)
            else {}
        )
        automation_project = (
            automation_projection.get("project")
            if isinstance(automation_projection.get("project"), Mapping)
            else {}
        )
        task_id = str(automation_projection.get("task_id") or "").strip()
        if task_id:
            checkpoint_metadata["request_id"] = task_id
        companion_skill_id = str(automation_project.get("companion_skill_id") or "").strip()
        if (
            str(automation_projection.get("status") or "").strip() == "completed"
            and companion_skill_id
        ):
            companion_result = projects.push(
                "skill",
                companion_skill_id,
                message=checkpoint_message,
                metadata=checkpoint_metadata,
            )
            checkpoint_results.append(dict(companion_result))
    result = projects.push(
        kind,
        project_id,
        message=checkpoint_message,
        metadata=checkpoint_metadata,
    )
    checkpoint_results.append(dict(result))
    commit = str(result.get("commit") or result.get("commit_sha") or "").strip() or None
    evidence = _record_project_change(
        kind=kind,
        project_id=project_id,
        action="checkpoint",
        summary=checkpoint_message,
        webspace_id=_webspace_id(webspace_id, _meta),
        commit=commit,
        change_id=checkpoint_change_id,
        meta={
            "checkpoint_artifacts": [
                {
                    "kind": item.get("kind"),
                    "name": item.get("name"),
                    "commit": item.get("commit"),
                    "package_digest": item.get("package_digest"),
                }
                for item in checkpoint_results
            ]
        },
    )
    change_id = str(evidence.get("change_id") or "").strip()
    package_digest = str(result.get("package_digest") or "").strip()
    source_revision = str(result.get("source_revision") or commit or "").strip()
    if not package_digest or not source_revision:
        raise ValueError(
            "Forge checkpoint did not return immutable package/source identities; "
            "the updated artifact pipeline must be available"
        )
    workflow_result = workflow.transition(
        kind,
        project_id,
        "checkpoint_recorded",
        actor="builder.checkpoint",
        metadata={
            "change_id": change_id,
            "package_digest": package_digest,
            "source_revision": source_revision,
        },
    )
    workflow_projection = (
        workflow_result.get("workflow")
        if isinstance(workflow_result.get("workflow"), Mapping)
        else {}
    )
    change_set_projection = (
        workflow_projection.get("change_set")
        if isinstance(workflow_projection.get("change_set"), Mapping)
        else {}
    )
    if change_set_projection:
        _sync_change_set_record(
            kind=kind,
            project_id=project_id,
            webspace_id=_webspace_id(webspace_id, _meta),
            change_set=change_set_projection,
        )
    return {
        **result,
        "change_id": change_id,
        "checkpoint_artifacts": checkpoint_results,
        "evidence": evidence,
        "workflow": workflow_projection,
    }


@tool("publish_project", summary="Validate or publish a DEV project release.", side_effects="external_write")
def publish_project(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    bump: str = "patch",
    dry_run: bool = True,
    force: bool = False,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    if bump not in {"major", "minor", "patch"}:
        raise ValueError("bump must be major, minor, or patch")
    workflow_before = workflow.get_state(kind, project_id)
    capabilities = (
        workflow_before.get("capabilities")
        if isinstance(workflow_before.get("capabilities"), Mapping)
        else {}
    )
    delivery = (
        workflow_before.get("delivery")
        if isinstance(workflow_before.get("delivery"), Mapping)
        else {}
    )
    automation_workflow = (
        workflow_before.get("automation")
        if isinstance(workflow_before.get("automation"), Mapping)
        else {}
    )
    change_set_workflow = (
        workflow_before.get("change_set")
        if isinstance(workflow_before.get("change_set"), Mapping)
        else {}
    )
    if dry_run:
        if not bool(capabilities.get("can_prepare_candidate")):
            raise ValueError(
                "Candidate preparation requires the current completed Automation result "
                "and no active trial"
            )
        checkpoint_change_id = str(delivery.get("checkpoint_change_id") or "").strip()
        if not checkpoint_change_id:
            raise ValueError("Checkpoint the completed Automation result before preparing a trial")
        validation_evidence = {
            "status": "passed",
            "validator": "builder.release.preflight",
            "checkpoint_package_digest": delivery.get("package_digest"),
            "checkpoint_source_revision": delivery.get("source_revision"),
            "automation_task_id": automation_workflow.get("head_task_id"),
            "change_set_id": change_set_workflow.get("change_set_id"),
        }
        candidate_change_ids = list(
            dict.fromkeys(
                [
                    *(
                        str(item).strip()
                        for item in change_set_workflow.get("member_change_ids") or []
                        if str(item).strip()
                    ),
                    checkpoint_change_id,
                ]
            )
        )
        stale_candidate_id = str(delivery.get("replaces_candidate_id") or "").strip()
        if stale_candidate_id:
            result = projects.prepare_rebased_candidate(
                stale_candidate_id,
                kind,
                project_id,
                validation_evidence=validation_evidence,
            )
        else:
            result = projects.prepare_candidate(
                kind,
                project_id,
                change_ids=candidate_change_ids,
                validation_evidence=validation_evidence,
            )
        candidate = result.get("candidate") if isinstance(result.get("candidate"), Mapping) else {}
        release_data = result.get("release") if isinstance(result.get("release"), Mapping) else {}
        release_digest = str(candidate.get("release_digest") or release_data.get("release_digest") or "").strip()
        package_digest = str(candidate.get("package_digest") or "").strip()
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        if not candidate_id or not release_digest or not package_digest:
            raise ValueError("Candidate preparation returned incomplete immutable identity")
        workflow_result = workflow.transition(
            kind,
            project_id,
            "candidate_prepared",
            actor="builder.candidate",
            metadata={
                "candidate_id": candidate_id,
                "release": f"{release_data.get('project_id')}@{release_data.get('version')}",
                "release_digest": release_digest,
                "package_digest": package_digest,
                "base_release": candidate.get("base_release"),
                "base_release_digest": candidate.get("base_release_digest"),
                "trial_workspace": result.get("trial_workspace"),
            },
        )
        trial_workflow = (
            workflow_result.get("workflow")
            if isinstance(workflow_result.get("workflow"), Mapping)
            else {}
        )
        trial_change_set = (
            trial_workflow.get("change_set")
            if isinstance(trial_workflow.get("change_set"), Mapping)
            else {}
        )
        if trial_change_set:
            _sync_change_set_record(
                kind=kind,
                project_id=project_id,
                webspace_id=_webspace_id(webspace_id, _meta),
                change_set=trial_change_set,
            )
        return {
            **result,
            "dry_run": True,
            "trial_ready": True,
            "workflow": trial_workflow,
        }

    candidate_id = str(delivery.get("candidate_id") or "").strip()
    delivery_status = str(delivery.get("status") or "").strip()
    if delivery_status == "trial":
        decided = projects.decide_candidate(
            candidate_id,
            accepted=True,
            observations=[
                {
                    "actor": "builder.user",
                    "decision": "accepted_for_stable",
                    "source": "publication_confirmation",
                }
            ],
        )
        accepted_result = workflow.transition(
            kind,
            project_id,
            "candidate_accepted",
            actor="builder.user",
            metadata={
                "candidate_id": candidate_id,
                "observations": decided.get("candidate", {}).get("trials", []),
            },
        )
        accepted_workflow = (
            accepted_result.get("workflow")
            if isinstance(accepted_result.get("workflow"), Mapping)
            else {}
        )
        accepted_change_set = (
            accepted_workflow.get("change_set")
            if isinstance(accepted_workflow.get("change_set"), Mapping)
            else {}
        )
        if accepted_change_set:
            _sync_change_set_record(
                kind=kind,
                project_id=project_id,
                webspace_id=_webspace_id(webspace_id, _meta),
                change_set=accepted_change_set,
            )
    elif not bool(capabilities.get("can_publish")):
        raise ValueError("Publication requires an accepted candidate trial")

    result = projects.promote_candidate(candidate_id)
    promotion_status = str(result.get("status") or "").strip().lower()
    if promotion_status == "stale":
        workflow_result = workflow.transition(
            kind,
            project_id,
            "candidate_stale",
            actor="builder.publication",
            metadata={
                "candidate_id": candidate_id,
                "rebase_plan": result.get("rebase_plan"),
            },
        )
        stale_workflow = (
            workflow_result.get("workflow")
            if isinstance(workflow_result.get("workflow"), Mapping)
            else {}
        )
        stale_change_set = (
            stale_workflow.get("change_set")
            if isinstance(stale_workflow.get("change_set"), Mapping)
            else {}
        )
        if stale_change_set:
            _sync_change_set_record(
                kind=kind,
                project_id=project_id,
                webspace_id=_webspace_id(webspace_id, _meta),
                change_set=stale_change_set,
            )
        return {
            **result,
            "requires_reapply": True,
            "workflow": stale_workflow,
        }
    if not bool(result.get("ok", True)) or result.get("error"):
        return result
    successful_promotion_statuses = {
        "completed",
        "promoted",
        "published",
        "stable",
        "succeeded",
        "success",
    }
    if promotion_status and promotion_status not in successful_promotion_statuses:
        return {
            **result,
            "ok": False,
            "error": f"Candidate is not promotable (status: {promotion_status})",
        }
    version = str(result.get("version") or result.get("published_version") or "").strip() or None
    release = str(result.get("release") or result.get("release_id") or result.get("url") or "").strip() or None
    summary = f"Published {kind} {project_id}" + (f" v{version}" if version else "")
    evidence = _record_project_change(
        kind=kind,
        project_id=project_id,
        action="publication",
        summary=summary,
        webspace_id=_webspace_id(webspace_id, _meta),
        commit=str(result.get("commit") or result.get("commit_sha") or "").strip() or None,
        meta={
            key: value
            for key, value in {
                "dry_run": False,
                "version": version,
                "release": release,
                "bump": bump,
                "source_automation_task": automation_workflow.get("head_task_id"),
                "source_automation_version": automation_workflow.get("result_version"),
                "source_prototype_revision": automation_workflow.get("source_prototype_revision"),
                "change_set_id": change_set_workflow.get("change_set_id"),
            }.items()
            if value is not None
        },
    )
    workflow_result = workflow.transition(
        kind,
        project_id,
        "publish",
        actor="builder.publication",
        metadata={
            "version": version,
            "release": release,
            "candidate_id": candidate_id,
            "task_id": automation_workflow.get("head_task_id"),
        },
    )
    published_workflow = (
        workflow_result.get("workflow")
        if isinstance(workflow_result.get("workflow"), Mapping)
        else {}
    )
    published_change_set = (
        published_workflow.get("change_set")
        if isinstance(published_workflow.get("change_set"), Mapping)
        else {}
    )
    if published_change_set:
        _sync_change_set_record(
            kind=kind,
            project_id=project_id,
            webspace_id=_webspace_id(webspace_id, _meta),
            change_set=published_change_set,
        )
    return {
        **result,
        "change_id": evidence.get("change_id"),
        "evidence": evidence,
        "workflow": published_workflow,
    }


@tool("get_state", summary="Verify the complete Builder SDK capability set.", side_effects="none")
def get_state(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    source = _webspace_id(webspace_id, _meta)
    checks = {
        "project": _probe(lambda: get_project(kind, project_id, source)),
        "project_objects": _probe(lambda: list_project_objects(kind, project_id)),
        "files": _probe(lambda: list_project_files(kind, project_id, limit=300)),
        "prompt_context": _probe(lambda: get_prompt_context(kind, project_id)),
        "workflow": _probe(lambda: get_workflow(kind, project_id)),
        "lifecycle": _probe(lambda: get_lifecycle(kind, project_id, source)),
        "preview_binding": _probe(lambda: get_preview(source)),
        "automation": _probe(lambda: get_automation(kind, project_id, source)),
        "changes": _probe(lambda: list_changes(kind, project_id, limit=20)),
        "change_set": _probe(lambda: get_change_set(kind, project_id)),
    }
    return {
        "ok": all(checks[name]["ok"] for name in ("project", "files")),
        "schema": "adaos.builder.sdk_control.v2",
        "skill_id": SKILL_ID,
        "project": {"kind": kind, "id": project_id},
        "webspace_id": source,
        "checks": checks,
    }


__all__ = [
    "add_change_issues",
    "append_prompt_addendum",
    "archive_project",
    "create_project",
    "delete_project",
    "get_automation",
    "get_lifecycle",
    "get_llm_options",
    "get_workflow",
    "get_preview",
    "get_prompt_context",
    "get_project",
    "get_state",
    "list_changes",
    "list_project_file_tree",
    "list_project_files",
    "list_project_objects",
    "list_projects",
    "list_templates",
    "publish_project",
    "push_project",
    "read_project_file",
    "save_prompt_context",
    "save_project_file",
    "select_preview",
    "select_preview_target",
    "set_llm_profile",
    "set_workflow_state",
    "start_automation",
    "submit_automation",
    "return_to_prototype",
    "transition_workflow",
    "update_project_metadata",
]
