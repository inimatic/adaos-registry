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
            "publication": {"status": "published" if token == "publication" else "not_started"},
            "capabilities": {
                "can_edit_prototype": active == "prototype",
                "can_stabilize_prototype": active == "prototype",
                "can_handoff_to_automation": active == "prototype",
                "can_edit_automation": active == "automation",
                "can_return_to_prototype": active == "automation" and automation_status == "completed",
                "can_publish": active == "automation" and automation_status == "completed",
                "can_preview_prototype": kind == "scenario",
                "can_preview_automation": kind == "scenario" and automation_status == "completed",
                "can_preview_publication": kind == "scenario" and token == "publication",
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
    change_id = f"builder-{action}-{uuid4().hex}"
    artifact_ref = {"kind": kind, "id": project_id}
    if path:
        artifact_ref["path"] = path
    evidence = conversation.upsert_development_change(
        change_id=change_id,
        conversation_id=conversation_id,
        thread_id=thread_id,
        topic_id=topic_id,
        status="pushed" if action == "checkpoint" else "accepted",
        source_refs={"kind": "builder_ui", "action": action, "webspace_id": webspace_id},
        artifact_refs=[artifact_ref],
        commit_refs=[{"commit": commit}] if commit else [],
        summary=summary,
        meta={"skill_id": SKILL_ID, **dict(meta or {})},
    )
    return dict(evidence or {"change_id": change_id, "status": "recorded"})


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
            "result_branch": result_branch,
            "evidence": evidence_items,
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
                "release": meta.get("release"),
                "created_at": _datetime_value(changed_at),
                "updated_at": _datetime_value(changed_at),
                "lifecycleStage": "publication",
                "conversationLabel": "Publication",
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
        "can_publish": bool(capabilities.get("can_publish")),
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
            "canStabilize": bool(workflow_capabilities.get("can_stabilize_prototype")),
            "canPreview": kind == "scenario",
            "version": project_version,
            "updated_at": prototype_updated_at,
            "lifecycleStage": "prototype",
            "conversationLabel": "Prototype conversation",
            "children": revision_nodes,
        },
        {
            "id": "stage-auto",
            "kind": "stage",
            "lifecycleState": "working" if active_phase == "automation" else ("frozen" if automation_status != "not_started" else "not_started"),
            "title": "Автоматизация",
            "title_i18n": {"key": "builder.lifecycle.stage.automation"},
            "status": "WORKING" if active_phase == "automation" else ("FROZEN" if automation_status != "not_started" else "NOT STARTED"),
            "status_i18n": None,
            "canOpenAutomation": True,
            "canPreview": bool(workflow_capabilities.get("can_preview_automation")),
            "canReturnToPrototype": bool(workflow_capabilities.get("can_return_to_prototype")),
            "version": project_version,
            "updated_at": automation_updated_at,
            "source_prototype_version": (
                workflow_automation.get("source_prototype_revision")
                or automation_projection.get("source_prototype_version")
                or project_version
            ),
            "lifecycleStage": "automation",
            "conversationLabel": "Automation conversation",
            "children": automation_children,
        },
        {
            "id": "stage-pub",
            "kind": "stage",
            "lifecycleState": "published" if publication_active else "not_started",
            "title": "Публикация",
            "title_i18n": {"key": "builder.lifecycle.stage.publication"},
            "status": "PUBLISHED" if publication_active else "NOT STARTED",
            "status_i18n": None,
            "canOpenPublication": True,
            "canPreview": bool(workflow_capabilities.get("can_preview_publication")),
            "canPublish": bool(workflow_capabilities.get("can_publish")),
            "version": publication_projection.get("current_version") or publication_version,
            "updated_at": publication_updated_at,
            "lifecycleStage": "publication",
            "conversationLabel": "Publication",
            "children": publication_children,
        },
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
    return automation.start(
        object_type=kind,
        object_id=project_id,
        implementation_brief=implementation_brief,
        webspace_id=_webspace_id(webspace_id, _meta),
        conversation_id=conversation_id,
        brief_path=brief_path,
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
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    checkpoint_message = message or f"chore(builder): checkpoint {kind} {project_id}"
    result = projects.push(kind, project_id, message=checkpoint_message)
    commit = str(result.get("commit") or result.get("commit_sha") or "").strip() or None
    evidence = _record_project_change(
        kind=kind,
        project_id=project_id,
        action="checkpoint",
        summary=checkpoint_message,
        webspace_id=_webspace_id(webspace_id, _meta),
        commit=commit,
    )
    return {**result, "change_id": evidence.get("change_id"), "evidence": evidence}


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
    if not dry_run:
        workflow_before = workflow.get_state(kind, project_id)
        capabilities = (
            workflow_before.get("capabilities")
            if isinstance(workflow_before.get("capabilities"), Mapping)
            else {}
        )
        if not bool(capabilities.get("can_publish")):
            raise ValueError("Publication requires the current completed Automation result")
    result = projects.publish(kind, project_id, bump=bump, force=force, dry_run=dry_run)  # type: ignore[arg-type]
    if dry_run or result.get("dry_run") is True or not bool(result.get("ok", True)) or result.get("error"):
        return result
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
        meta={"dry_run": False, "version": version, "release": release, "bump": bump},
    )
    current_workflow = workflow.get_state(kind, project_id)
    automation_workflow = (
        current_workflow.get("automation")
        if isinstance(current_workflow.get("automation"), Mapping)
        else {}
    )
    workflow_result = workflow.transition(
        kind,
        project_id,
        "publish",
        actor="builder.publication",
        metadata={
            "version": version,
            "release": release,
            "task_id": automation_workflow.get("head_task_id"),
        },
    )
    return {
        **result,
        "change_id": evidence.get("change_id"),
        "evidence": evidence,
        "workflow": workflow_result.get("workflow"),
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
