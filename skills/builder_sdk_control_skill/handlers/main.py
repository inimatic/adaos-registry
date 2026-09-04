from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import yaml

from adaos.sdk import conversation, navigation
from adaos.sdk.builder import automation, development_sessions, issues as builder_issues, preview, review, semantic_ui, workflow
from adaos.sdk.core.decorators import tool
from adaos.sdk.developer import compositions, projects, prompt_context
from adaos.sdk.llm.llm_client import list_llm_models

SKILL_ID = "builder_sdk_control_skill"
DEFAULT_PROJECT_KIND = "scenario"
DEFAULT_PROJECT_ID = "builder"
MAX_LIFECYCLE_CHILDREN = 5
MAX_CATALOG_STATE_BYTES = 512 * 1024


def _webspace_id(value: str | None, meta: Mapping[str, Any] | None) -> str:
    metadata = meta if isinstance(meta, Mapping) else {}
    return str(value or metadata.get("webspace_id") or metadata.get("source_webspace_id") or "desktop").strip() or "desktop"


def _preview_source_webspace_id(value: str | None, meta: Mapping[str, Any] | None) -> str:
    candidate = _webspace_id(value, meta)
    metadata = meta if isinstance(meta, Mapping) else {}
    current_scenario = str(
        metadata.get("scenario_id") or metadata.get("current_scenario") or ""
    ).strip()
    try:
        if current_scenario:
            return preview.action_source_webspace_id(
                candidate,
                current_scenario_id=current_scenario,
            )
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


def _project_topic(
    kind: str,
    project_id: str,
    *,
    webspace_id: str | None = None,
    meta: Mapping[str, Any] | None = None,
    execution_identity: tuple[str, str] | None = None,
) -> dict[str, Any]:
    source = _preview_source_webspace_id(webspace_id, meta)
    execution_kind, execution_id = execution_identity or _execution_identity(kind, project_id)
    return conversation.ensure_builder_topic(
        webspace_id=source,
        scenario_id=execution_id if execution_kind == "scenario" else None,
        dev_webspace_id=_preview_dev_webspace_id(source),
        project_id=execution_id,
        title=f"Builder: {project_id}",
        meta={
            "artifact_kind": execution_kind,
            "artifact_id": execution_id,
            "context_ref": f"{kind}:{project_id}",
        },
    )


def _identity(object_type: str | None, object_id: str | None) -> tuple[str, str]:
    kind = str(object_type or DEFAULT_PROJECT_KIND).strip().lower().rstrip("s")
    project_id = str(object_id or DEFAULT_PROJECT_ID).strip()
    if kind not in {"project", "scenario", "skill"}:
        raise ValueError("object_type must be project, scenario or skill")
    if not project_id:
        raise ValueError("object_id is required")
    return kind, project_id


_PROJECT_TEXT_SUFFIXES = {".json", ".md", ".txt", ".yaml", ".yml"}
_PROJECT_READONLY_NAMES = {"prompt_state.json"}


def _composition_manifest(project_id: str) -> dict[str, Any]:
    return dict(compositions.get(project_id))


def _composition_catalog(project: Mapping[str, Any]) -> dict[str, Any]:
    return dict(project.get("catalog")) if isinstance(project.get("catalog"), Mapping) else {}


def _composition_refs(project: Mapping[str, Any], group: str) -> list[str]:
    components = dict(project.get("components")) if isinstance(project.get("components"), Mapping) else {}
    refs: list[str] = []
    for item in components.get(group) or []:
        if not isinstance(item, Mapping):
            continue
        ref = str(item.get("ref") or "").strip()
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _composition_owned_refs(project_id: str) -> list[str]:
    project = _composition_manifest(project_id)
    return _composition_refs(project, "owned")


def _composition_dependency_refs(project_id: str) -> list[str]:
    project = _composition_manifest(project_id)
    return _composition_refs(project, "dependencies")


def _split_component_ref(ref: str) -> tuple[str, str] | None:
    kind, separator, component_id = str(ref or "").strip().partition(":")
    if separator != ":" or kind not in {"scenario", "skill"} or not component_id:
        return None
    return kind, component_id


def _execution_identity(kind: str, project_id: str) -> tuple[str, str]:
    if kind != "project":
        return kind, project_id
    project = _composition_manifest(project_id)
    owned = (
        project.get("components", {}).get("owned", [])
        if isinstance(project.get("components"), Mapping)
        else []
    )
    primary = next(
        (
            item
            for item in owned
            if isinstance(item, Mapping) and str(item.get("role") or "").strip() == "primary"
        ),
        owned[0] if owned else None,
    )
    resolved = _split_component_ref(str((primary or {}).get("ref") or ""))
    if resolved is None:
        raise ValueError(f"project:{project_id} has no usable primary component")
    return resolved


def _execution_scope(kind: str, project_id: str) -> dict[str, Any]:
    execution_kind, execution_id = _execution_identity(kind, project_id)
    return {
        "context_ref": f"{kind}:{project_id}",
        "execution_ref": f"{execution_kind}:{execution_id}",
        "object_type": execution_kind,
        "object_id": execution_id,
    }


def _project_presentation_scenario_id(project_id: str) -> str:
    project = _composition_manifest(project_id)
    entrypoints = [
        dict(item)
        for item in project.get("entrypoints") or []
        if isinstance(item, Mapping)
    ]
    selected = next(
        (item for item in entrypoints if item.get("default") is True),
        entrypoints[0] if entrypoints else {},
    )
    presentation = str(selected.get("presentation") or "").strip()
    kind, separator, component_id = presentation.partition(":")
    if separator == ":" and kind == "scenario" and component_id:
        return component_id
    return ""


def _component_path_from_project_file(project_id: str, path: str) -> tuple[str, str, str] | None:
    raw = str(path or "").strip().replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if not parts or parts[0] != "components":
        return None
    if len(parts) < 4:
        raise ValueError("component file path must be components/<kind>/<id>/<path>")
    component_kind = str(parts[1]).strip().lower().rstrip("s")
    component_id = str(parts[2]).strip()
    relative = "/".join(parts[3:]).strip("/")
    if component_kind not in {"scenario", "skill"} or not component_id or not relative:
        raise ValueError("component file path must be components/<kind>/<id>/<path>")
    if f"{component_kind}:{component_id}" not in set(_composition_owned_refs(project_id)):
        raise ValueError(f"component is not owned by project:{project_id}: {component_kind}:{component_id}")
    return component_kind, component_id, relative


def _project_root_file(project_id: str, relative_path: str) -> tuple[str, Path]:
    raw = str(relative_path or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("path is required")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path is outside project root")
    root = compositions.resolve_root(project_id)
    full = (root / relative).resolve()
    try:
        full.relative_to(root)
    except ValueError as exc:
        raise ValueError("path is outside project root") from exc
    return relative.as_posix(), full


def _project_file_editable(relative_path: str, full: Path | None = None) -> tuple[bool, str]:
    path = PurePosixPath(relative_path)
    if path.name in _PROJECT_READONLY_NAMES:
        return False, "managed_state_file"
    if path.suffix.lower() not in _PROJECT_TEXT_SUFFIXES:
        return False, "unsupported_file_type"
    if full is not None and full.is_symlink():
        return False, "symlink_not_editable"
    return True, ""


def _project_root_file_descriptor(project_id: str, full: Path) -> dict[str, Any]:
    root = compositions.resolve_root(project_id)
    relative = full.relative_to(root).as_posix()
    editable, reason = _project_file_editable(relative, full)
    stat = full.stat()
    return {
        "kind": "project",
        "project_id": project_id,
        "path": relative,
        "size_bytes": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "editable": editable,
        "readonly_reason": reason,
    }


def _read_project_composition_file(project_id: str, path: str, *, max_bytes: int) -> dict[str, Any]:
    relative, full = _project_root_file(project_id, path)
    if not full.is_file():
        raise FileNotFoundError(f"project file '{relative}' was not found")
    maximum = max(1, min(int(max_bytes), 1_048_576))
    raw = full.read_bytes()
    truncated = len(raw) > maximum
    editable, reason = _project_file_editable(relative, full)
    return {
        "ok": True,
        "kind": "project",
        "project_id": project_id,
        "path": relative,
        "content": raw[:maximum].decode("utf-8", errors="replace"),
        "size_bytes": len(raw),
        "truncated": truncated,
        "editable": editable and not truncated,
        "readonly_reason": reason or ("file_too_large" if truncated else ""),
    }


def _write_project_composition_file(
    project_id: str,
    path: str,
    text: str,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    relative, full = _project_root_file(project_id, path)
    editable, reason = _project_file_editable(relative, full)
    if not editable:
        raise ValueError(f"project file is not editable: {reason}")
    raw = str(text).encode("utf-8")
    maximum = max(1, min(int(max_bytes), 1_048_576))
    if len(raw) > maximum:
        raise ValueError(f"project file exceeds {maximum} bytes")
    full.parent.mkdir(parents=True, exist_ok=True)
    temporary = full.with_name(f".{full.name}.tmp")
    temporary.write_bytes(raw)
    temporary.replace(full)
    return {
        "ok": True,
        "kind": "project",
        "project_id": project_id,
        "path": relative,
        "size_bytes": len(raw),
    }


@tool("get_development_session", summary="Read the scoped Development Session bound to this Builder host.", side_effects="none")
def get_development_session(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = _preview_source_webspace_id(webspace_id, _meta)
    binding = development_sessions.binding_for(source)
    if not binding:
        return {
            "ok": True,
            "bound": False,
            "builder_webspace_id": source,
            "content": "No external Development Session is bound. Select a normal Builder project or open an accepted handoff from its owning Workbench.",
        }
    session = development_sessions.get(str(binding["session_id"]))
    targets = [
        item
        for group in session["targets"].values()
        for item in group
    ]
    target_labels = ", ".join(f"`{item['ref']}`" for item in targets)
    context_labels = ", ".join(f"`{item['ref']}`" for item in session["context_members"])
    artifact_labels = ", ".join(f"`{item['ref']}`" for item in session["artifact_inputs"])
    context_refs = {str(item.get("ref") or "") for item in session["context_members"]}
    return_url = None
    if "scenario:research_workbench" in context_refs:
        scope = navigation.runtime_scope()
        destination = navigation.webspace_destination(
            zone=str(scope["zone"]),
            subnet_id=str(scope["subnet_id"]),
            webspace_id=_preview_dev_webspace_id(source),
            space_kind="development",
            expected_scenario_id="research_workbench",
        )
        return_url = navigation.build_url(destination, base_url=preview.public_app_base())
    return_link = f"\n\n[Return to Research Workbench]({return_url})" if return_url else ""
    content = (
        f"## Scoped Development Session `{session['session_id']}`\n\n"
        f"**Project:** `{session['project_ref']}`  \n"
        f"**Focus:** `{session['focus']['ref']}`  \n"
        f"**Read-write targets:** {target_labels}  \n"
        f"**Read-only context:** {context_labels}  \n"
        f"**Artifact inputs:** {artifact_labels}\n\n"
        "Changing UI focus does not enlarge write authority. Codex has not been started."
        f"{return_link}"
    )
    return {
        "ok": True,
        "bound": True,
        "builder_webspace_id": source,
        "binding": binding,
        "session": session,
        "targets": targets,
        "return_url": return_url,
        "content": content,
    }


@tool("review_development_changes", summary="Reject changed paths outside the bound Development Session write scope.", side_effects="none")
def review_development_changes(
    paths: list[str],
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = _preview_source_webspace_id(webspace_id, _meta)
    binding = development_sessions.binding_for(source)
    if not binding:
        raise ValueError("no Development Session is bound to this Builder host")
    return development_sessions.review_changes(str(binding["session_id"]), paths)


@tool("request_development_scope", summary="Request, but never auto-approve, one additional Development Session target.", side_effects="local_write")
def request_development_scope(
    target_ref: str,
    reason: str,
    webspace_id: str | None = None,
    actor: str = "codex",
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = _preview_source_webspace_id(webspace_id, _meta)
    binding = development_sessions.binding_for(source)
    if not binding:
        raise ValueError("no Development Session is bound to this Builder host")
    return development_sessions.request_scope_expansion(
        str(binding["session_id"]),
        target_ref,
        reason,
        actor=actor,
    )


@tool("get_skill_preview", summary="Describe the skill selected by the paired Builder host.", side_effects="none")
def get_skill_preview(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = _preview_source_webspace_id(webspace_id, _meta)
    binding = preview.get_binding(source)
    selection = binding.get("selection") if isinstance(binding.get("selection"), Mapping) else {}
    kind = str(selection.get("object_type") or selection.get("project_kind") or "").strip().lower().rstrip("s")
    skill_id = str(selection.get("object_id") or selection.get("project_id") or "").strip()
    if kind != "skill" or not skill_id:
        return {
            "ok": True,
            "selected": False,
            "state": "empty",
            "next_steps": [{"id": "select_skill", "label": "Select a skill in Builder"}],
            "content": "## Skill preview\n\nSelect a skill in Builder to inspect its metadata, README, capabilities, and declared presentation.",
        }
    description = projects.describe("skill", skill_id)
    try:
        readme = str(projects.read_file("skill", skill_id, "README.md").get("content") or "").strip()
    except Exception:
        readme = "README.md is not declared for this skill."
    presentation = compositions.resolve_presentation(f"skill:{skill_id}")
    capabilities = description.get("capabilities") or []
    capability_text = ", ".join(f"`{item}`" for item in capabilities) or "not declared"
    content = (
        f"## {description.get('title') or description.get('name') or skill_id}\n\n"
        f"**Skill:** `skill:{skill_id}`  \n"
        f"**Version:** `{description.get('version') or 'development'}`  \n"
        f"**Capabilities:** {capability_text}  \n"
        f"**Presentation:** `{presentation.get('presentation') or 'scenario:skill_preview'}`\n\n"
        f"{readme}"
    )
    return {
        "ok": True,
        "selected": True,
        "state": "ready",
        "next_steps": [{"id": "return_to_builder", "label": "Return to Builder to edit the skill"}],
        "skill_id": skill_id,
        "skill": description,
        "presentation": presentation,
        "source_webspace_id": source,
        "content": content,
    }


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


def _catalog_state(kind: str, project_id: str) -> dict[str, Any]:
    normalized = str(kind or "").strip().lower().rstrip("s")
    state: dict[str, Any] = {
        "object_type": normalized,
        "object_id": str(project_id),
        "archived": False,
    }
    try:
        root = (
            compositions.resolve_root(project_id)
            if normalized == "project"
            else projects.resolve_root(normalized, project_id)
        )
        path = root / "prompt_state.json"
        if not path.is_file() or path.stat().st_size > MAX_CATALOG_STATE_BYTES:
            return state
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return state
    if not isinstance(raw, Mapping):
        return state
    for key in ("archived", "updated_at", "builder_llm_model"):
        if key in raw:
            state[key] = raw[key]
    return state


def _workflow_projection(kind: str, project_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    execution_kind, execution_id = _execution_identity(kind, project_id)
    projection = dict(workflow.get_state(execution_kind, execution_id))
    projection["execution_scope"] = _execution_scope(kind, project_id)
    return projection


def _workflow_execution_identity(projection: Mapping[str, Any] | None) -> tuple[str, str]:
    value = projection if isinstance(projection, Mapping) else {}
    change = value.get("change") if isinstance(value.get("change"), Mapping) else {}
    change_set = value.get("change_set") if isinstance(value.get("change_set"), Mapping) else {}
    packet = value.get("context_packet") if isinstance(value.get("context_packet"), Mapping) else {}
    change_id = str(change.get("change_id") or change_set.get("change_set_id") or "").strip()
    digest = str(packet.get("digest") or change.get("context_packet_digest") or "").strip()
    return change_id, digest


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
    topic = _project_topic(kind, project_id, webspace_id=webspace_id)
    conversation_id = str(topic.get("conversation_id") or "conv.skill.builder_skill.default")
    thread_id = str(topic.get("thread_id") or topic.get("topic_id") or "").strip() or None
    topic_id = str(topic.get("topic_id") or thread_id or "").strip() or None
    change_id = str(change_id or "").strip() or f"builder-{action}-{uuid4().hex}"
    selected_meta = {"skill_id": SKILL_ID, **dict(meta or {})}
    canonical_change_id = ""
    canonical_change_set: dict[str, Any] = {}
    context_packet_digest = ""
    try:
        workflow_kind, workflow_id = _execution_identity(kind, project_id)
        projection = workflow.get_state(workflow_kind, workflow_id)
        change = projection.get("change") if isinstance(projection.get("change"), Mapping) else {}
        canonical_change_set = (
            dict(projection.get("change_set"))
            if isinstance(projection.get("change_set"), Mapping)
            else {}
        )
        context_packet = (
            projection.get("context_packet")
            if isinstance(projection.get("context_packet"), Mapping)
            else {}
        )
        canonical_change_id = str(
            change.get("change_id")
            or canonical_change_set.get("change_set_id")
            or ""
        ).strip()
        context_packet_digest = str(
            context_packet.get("digest") or change.get("context_packet_digest") or ""
        ).strip()
    except Exception:
        canonical_change_id = ""
        canonical_change_set = {}
        context_packet_digest = ""
    if canonical_change_id:
        selected_meta.update(
            {
                "canonical_change_id": canonical_change_id,
                "run_id": change_id,
                "evidence_role": "builder_run_compatibility",
            }
        )
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
        meta=selected_meta,
    )
    result = dict(evidence or {"change_id": change_id, "status": "recorded"})
    if not canonical_change_id:
        return result

    run: dict[str, Any] = {}
    run_error: str | None = None
    try:
        if canonical_change_set and not conversation.get_development_change(canonical_change_id):
            _sync_change_set_record(
                kind=kind,
                project_id=project_id,
                webspace_id=webspace_id,
                change_set=canonical_change_set,
            )
        completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        output_refs = [
            value
            for value in (
                f"artifact:{kind}:{project_id}:{path}" if path else None,
                f"commit:{commit}" if commit else None,
                f"evidence:{change_id}",
            )
            if value
        ]
        run = dict(
            conversation.upsert_development_run(
                run_id=change_id,
                change_id=canonical_change_id,
                conversation_id=conversation_id,
                thread_id=thread_id,
                topic_id=topic_id,
                activity=action,
                executor=f"skill:{SKILL_ID}",
                status="succeeded",
                context_packet_digest=context_packet_digest or None,
                environment_ref=f"webspace:{webspace_id}",
                input_refs=[f"message:{item}" for item in source_message_ids or [] if str(item).strip()],
                output_refs=output_refs,
                evidence_refs=[f"development-change:{change_id}"],
                started_at=completed_at,
                completed_at=completed_at,
            )
            or {}
        )
    except Exception as exc:
        # Compatibility evidence remains authoritative until all release
        # consumers read Runs. Surface mirroring failures without rolling back
        # an already completed artifact operation.
        run_error = f"{type(exc).__name__}: {exc}"
    return {
        **result,
        "canonical_change_id": canonical_change_id,
        "run_id": change_id,
        "run": run or None,
        "run_synced": run_error is None,
        "run_error": run_error,
    }


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
    topic = _project_topic(kind, project_id, webspace_id=webspace_id)
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


def _project_descriptor(
    kind: str,
    project_id: str,
    described: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe a project, taking scenario metadata only from scenario.yaml."""

    if kind == "project":
        project = (
            dict(described)
            if isinstance(described, Mapping) and described.get("components")
            else _composition_manifest(project_id)
        )
        catalog = _composition_catalog(project)
        owned = [
            dict(item)
            for item in dict(project.get("components") or {}).get("owned") or []
            if isinstance(item, Mapping)
        ]
        dependencies = [
            dict(item)
            for item in dict(project.get("components") or {}).get("dependencies") or []
            if isinstance(item, Mapping)
        ]
        primary = next(
            (item for item in owned if str(item.get("role") or "") == "primary"),
            owned[0] if owned else {},
        )
        component_refs = [
            str(item.get("ref") or "").strip()
            for item in owned
            if str(item.get("ref") or "").strip()
        ]
        dependency_refs = [
            str(item.get("ref") or "").strip()
            for item in dependencies
            if str(item.get("ref") or "").strip()
        ]
        return {
            **project,
            "ok": True,
            "kind": "project",
            "id": str(project.get("id") or project_id),
            "name": str(project.get("name") or project.get("id") or project_id),
            "title": str(catalog.get("title") or project.get("title") or project_id),
            "description": str(catalog.get("description") or project.get("description") or ""),
            "project_type": "project",
            "version": str(project.get("version") or ""),
            "depends": dependency_refs,
            "manifest": "project.yaml",
            "profiles": list(project.get("profiles") or []),
            "catalog": catalog,
            "primary_ref": str(primary.get("ref") or "").strip() or None,
            "component_refs": component_refs,
            "dependency_refs": dependency_refs,
            "entrypoints": [
                dict(item)
                for item in project.get("entrypoints") or []
                if isinstance(item, Mapping)
            ],
        }
    described = dict(described) if isinstance(described, Mapping) else dict(projects.describe(kind, project_id))
    if kind != "scenario":
        return described
    semantic_fields = {
        "name", "title", "description", "version", "depends", "metadata", "ui", "actions", "data_routes"
    }
    # describe() may include legacy scenario.json values; strip them before any fallback.
    for field in semantic_fields:
        described.pop(field, None)
    try:
        payload = projects.read_file(kind, project_id, "scenario.yaml", max_bytes=256_000)
        manifest = yaml.safe_load(str(payload.get("content") or "")) or {}
    except Exception:
        # Missing/unreadable canonical metadata remains unknown rather than using scenario.json.
        return described
    if not isinstance(manifest, Mapping):
        return described
    described.update(dict(manifest))
    described.setdefault("id", project_id)
    described.setdefault("kind", kind)
    described["manifest"] = "scenario.yaml"
    return described


def _declared_skill_dependencies(kind: str, project_id: str) -> set[str]:
    """Read dependency declarations only from the canonical project manifest."""

    if kind == "project":
        refs = [*_composition_owned_refs(project_id), *_composition_dependency_refs(project_id)]
        return {
            component_id
            for ref in refs
            for component_kind, component_id in [_split_component_ref(ref) or ("", "")]
            if component_kind == "skill" and component_id
        }
    manifest_name = "scenario.yaml" if kind == "scenario" else "skill.yaml"
    try:
        payload = projects.read_file(kind, project_id, manifest_name, max_bytes=256_000)
        manifest = yaml.safe_load(str(payload.get("content") or "")) or {}
    except Exception as exc:
        raise ValueError(f"canonical project manifest is unavailable: {manifest_name}") from exc
    if not isinstance(manifest, Mapping):
        raise ValueError(f"canonical project manifest must be an object: {manifest_name}")
    values: list[Any] = []
    depends = manifest.get("depends") or []
    values.extend([depends] if isinstance(depends, str) else depends if isinstance(depends, (list, tuple)) else [])
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), Mapping) else {}
    skills = runtime.get("skills") if isinstance(runtime.get("skills"), Mapping) else {}
    required = skills.get("required") or []
    values.extend([required] if isinstance(required, str) else required if isinstance(required, (list, tuple)) else [])
    return {str(value).strip() for value in values if str(value).strip()}


def _preview_label(stage: Any, revision: Any = None) -> str:
    token = str(stage or "prototype").strip().lower()
    prefix = {"prototype": "proto", "automation": "active", "publication": "public"}.get(token, token)
    ref = str(revision or "current").strip() or "current"
    return f"{prefix}: {ref}"


def _require_transport_integrity(*values: Any) -> None:
    """Reject likely lossy text transport before a durable or launching mutation."""

    def strings(value: Any):
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            for key, nested in value.items():
                yield from strings(key)
                yield from strings(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                yield from strings(nested)

    for value in values:
        for text in strings(value):
            if "\ufffd" in text:
                raise ValueError("text transport integrity check failed: replacement character U+FFFD")
            if re.search(r"\?{3,}", text):
                raise ValueError("text transport integrity check failed: suspicious question-mark run")


def _automation_children(
    projection: Mapping[str, Any],
    *,
    project_version: str | None = None,
) -> list[dict[str, Any]]:
    """Build one bounded, browser-ready node for the current automation task."""
    status = str(projection.get("status") or "idle").strip().lower()
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
    default_states = {"", "idle", "not_started", "not-started", "default"}
    meaningful_phase = str(phase or "").strip().lower() not in default_states
    retained_result = bool(result) or bool(projection.get("result_version")) or bool(projection.get("snapshot_task_id"))
    has_lineage = any((task_id, meaningful_phase, summary, error, result_branch, evidence_items, retained_result))
    if status in default_states and not has_lineage:
        return []
    return [
        {
            "id": f"automation-task-{task_id or 'current'}",
            "kind": "automation_result",
            "lifecycleState": "failed" if error else ("current" if status not in {"completed", "succeeded"} else "complete"),
            "title": _version_title(version),
            "previewLabel": _preview_label("automation", version),
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
    requested_kind = str(kind or "").strip().lower().rstrip("s")
    if requested_kind and requested_kind != "project":
        raise ValueError("Builder project catalog only accepts kind=project")
    bounded_limit = max(1, min(int(limit), 5000))
    selected_kind = str(selected_object_type or "").strip().lower().rstrip("s")
    selected_id = str(selected_object_id or "").strip()
    source = _preview_source_webspace_id(webspace_id, _meta)
    dev_space = _preview_dev_webspace_id(source)
    items: list[dict[str, Any]] = []
    if requested_kind in {"", "project"}:
        try:
            project_items = compositions.list_projects(limit=bounded_limit)
        except Exception:
            project_items = []
        for project_item in project_items:
            object_id = str(project_item.get("id") or project_item.get("name") or "").strip()
            if not object_id or object_id.startswith((".", "_")):
                continue
            item = _project_descriptor("project", object_id, project_item)
            title = str(item.get("title") or item.get("name") or object_id)
            description = str(item.get("description") or "")
            if needle and needle not in f"{object_id} {title} {description}".casefold():
                continue
            state = _catalog_state("project", object_id)
            if state.get("archived") and not include_archived:
                continue
            current = selected_kind == "project" and object_id == selected_id
            primary_ref = str(item.get("primary_ref") or "").strip()
            primary_identity = _split_component_ref(primary_ref)
            conversation_identity = primary_identity or ("project", object_id)
            conversation_topic_id = (
                f"prompt-project:{conversation_identity[0]}:{conversation_identity[1]}"
            )
            if (
                primary_identity
                and selected_kind == primary_identity[0]
                and selected_id == primary_identity[1]
            ):
                current = True
            items.append(
                {
                    "kind": "project",
                    "name": str(item.get("name") or object_id),
                    "project_type": "project",
                    "depends": list(item.get("depends") or []),
                    "manifest": str(item.get("manifest") or "project.yaml"),
                    "profiles": list(item.get("profiles") or []),
                    "primary_ref": item.get("primary_ref"),
                    "target_object_type": primary_identity[0] if primary_identity else "project",
                    "target_object_id": primary_identity[1] if primary_identity else object_id,
                    "component_refs": list(item.get("component_refs") or []),
                    "dependency_refs": list(item.get("dependency_refs") or []),
                    "id": f"project:{object_id}",
                    "object_type": "project",
                    "object_id": object_id,
                    "context_topic_id": f"prompt-project:project:{object_id}",
                    "context_thread_id": f"prompt-project:project:{object_id}",
                    "conversation_topic_id": conversation_topic_id,
                    "conversation_thread_id": conversation_topic_id,
                    "title": title,
                    "subtitle": description or f"project · {item.get('version') or 'DEV'}",
                    "type": "Project",
                    "type_i18n": {"key": "builder.project_type.project"},
                    "stage": "Архив" if state.get("archived") else "Прототип",
                    "stage_i18n": {
                        "key": "builder.project_stage.archive"
                        if state.get("archived")
                        else "builder.project_stage.prototype"
                    },
                    "version": str(item.get("version") or "DEV"),
                    "stable": str(item.get("version") or "—"),
                    "space": dev_space,
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
            if len(items) >= bounded_limit:
                return items
        return items


@tool("get_project", summary="Describe the selected DEV project.", side_effects="none")
def get_project(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    item = _project_descriptor(kind, project_id)
    state = _context(kind, project_id)
    workflow_projection = _workflow_projection(kind, project_id, state)
    capabilities = (
        workflow_projection.get("capabilities")
        if isinstance(workflow_projection.get("capabilities"), Mapping)
        else {}
    )
    source = _preview_source_webspace_id(webspace_id, _meta)
    topic = _project_topic(kind, project_id, webspace_id=source)
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
    change_projection = (
        workflow_projection.get("change")
        if isinstance(workflow_projection.get("change"), Mapping)
        else change_set_projection
    )
    change_id = str(change_projection.get("change_id") or change_set_projection.get("change_set_id") or "").strip()
    change_status = str(change_projection.get("status") or change_set_projection.get("status") or "not_planned")
    change_gate = str(change_projection.get("gate") or change_set_projection.get("gate") or "").strip()
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
    viewing_stage = str(preview_target.get("stage") or "")
    viewing_revision = str(preview_target.get("revision") or "")
    viewing_label = str(preview_target.get("label") or "").strip()
    if not viewing_label:
        viewing_label = _preview_label(viewing_stage, viewing_revision) if viewing_stage else "Preview: not selected"
    active_ref = str(working_ref or "")
    viewing_read_only = bool(
        viewing_stage
        and (viewing_stage != active_phase or (viewing_revision and active_ref and viewing_revision != active_ref))
    )
    return {
        **item,
        "object_type": kind,
        "object_id": project_id,
        "project_ref": f"{kind}:{project_id}",
        "project_type": str(item.get("project_type") or kind),
        "dev_webspace_id": _preview_dev_webspace_id(source),
        "source_webspace_id": source,
        "conversation_id": str(topic.get("conversation_id") or "conv.skill.builder_skill.default"),
        "topic_id": str(topic.get("topic_id") or f"prompt-project:{kind}:{project_id}"),
        "thread_id": str(topic.get("thread_id") or topic.get("topic_id") or f"prompt-project:{kind}:{project_id}"),
        "stage": "DEV prototype",
        "archived": bool(state.get("archived")),
        "workflow_state": str(workflow_projection.get("active_phase") or "prototype"),
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
        "can_start_implementation": bool(
            active_phase == "prototype" and change_gate == "automation" and change_id
        ),
        "execution_scope": _execution_scope(kind, project_id),
        "change_set_id": change_set_projection.get("change_set_id"),
        "change_id": change_id or None,
        "change_label": f"{change_id} · {change_status}" if change_id else "No active change",
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
    identities = [(kind, project_id)]
    if kind == "project":
        refs = [*_composition_owned_refs(project_id), *_composition_dependency_refs(project_id)]
        for ref in refs:
            component = _split_component_ref(ref)
            if component and component not in identities:
                identities.append(component)
    elif kind == "scenario":
        root = projects.describe(kind, project_id)
        identities.extend(("skill", str(item)) for item in root.get("depends") or [] if str(item).strip())
    items: list[dict[str, Any]] = []
    for current_kind, current_id in identities:
        try:
            described = (
                _project_descriptor("project", current_id)
                if current_kind == "project"
                else projects.describe(current_kind, current_id)
            )
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

    if kind == "project":
        root = compositions.resolve_root(project_id)
        bounded_limit = max(1, min(int(limit), 5000))
        for full in sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: path.as_posix().lower()):
            relative = full.relative_to(root).as_posix()
            path = PurePosixPath(relative)
            if ignored_parts.intersection(path.parts) or path.suffix.lower() in {".pyc", ".pyo"}:
                continue
            item = _project_root_file_descriptor(project_id, full)
            items.append(
                {
                    **item,
                    "id": relative,
                    "title": path.name,
                    "subtitle": relative,
                    "object_type": "project",
                    "object_id": project_id,
                    "protected": not bool(item.get("editable")),
                }
            )
            if len(items) >= bounded_limit:
                return items
        for ref in _composition_owned_refs(project_id):
            component = _split_component_ref(ref)
            if component is None:
                continue
            component_kind, component_id = component
            remaining = bounded_limit - len(items)
            if remaining <= 0:
                return items
            try:
                component_files = projects.list_files(component_kind, component_id, limit=remaining)
            except projects.DeveloperProjectError:
                continue
            prefix = f"components/{component_kind}/{component_id}"
            for item in component_files:
                relative = str(item.get("path") or "")
                path = PurePosixPath(relative)
                if ignored_parts.intersection(path.parts) or path.suffix.lower() in {".pyc", ".pyo"}:
                    continue
                project_path = f"{prefix}/{relative}"
                items.append(
                    {
                        **item,
                        "id": project_path,
                        "kind": "project",
                        "project_id": project_id,
                        "path": project_path,
                        "title": path.name,
                        "subtitle": project_path,
                        "object_type": "project",
                        "object_id": project_id,
                        "component_ref": ref,
                        "component_object_type": component_kind,
                        "component_object_id": component_id,
                        "protected": not bool(item.get("editable")),
                    }
                )
                if len(items) >= bounded_limit:
                    return items
        return items

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
    if kind == "project":
        return []
    return projects.list_templates(kind)


@tool("read_project_file", summary="Read one bounded DEV project text file.", side_effects="none")
def read_project_file(
    path: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    max_bytes: int = 131_072,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    if kind == "project":
        component = _component_path_from_project_file(project_id, path)
        if component is not None:
            component_kind, component_id, relative = component
            result = dict(projects.read_file(component_kind, component_id, relative, max_bytes=max_bytes))
            project_path = f"components/{component_kind}/{component_id}/{result.get('path') or relative}"
            return {
                **result,
                "kind": "project",
                "project_id": project_id,
                "path": project_path,
                "object_type": "project",
                "object_id": project_id,
                "component_ref": f"{component_kind}:{component_id}",
                "component_object_type": component_kind,
                "component_object_id": component_id,
            }
        return _read_project_composition_file(project_id, path, max_bytes=max_bytes)
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
    _require_transport_integrity(path, text)
    kind, project_id = _identity(object_type, object_id)
    if kind == "project":
        component = _component_path_from_project_file(project_id, path)
        if component is not None:
            component_kind, component_id, relative = component
            raw_result = dict(projects.write_file(component_kind, component_id, relative, text, max_bytes=max_bytes))
            project_path = f"components/{component_kind}/{component_id}/{raw_result.get('path') or relative}"
            result = {
                **raw_result,
                "kind": "project",
                "project_id": project_id,
                "path": project_path,
                "object_type": "project",
                "object_id": project_id,
                "component_ref": f"{component_kind}:{component_id}",
                "component_object_type": component_kind,
                "component_object_id": component_id,
            }
        else:
            result = _write_project_composition_file(project_id, path, text, max_bytes=max_bytes)
    else:
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


def _development_feedback_rows(
    *,
    webspace_id: str | None,
    meta: Mapping[str, Any] | None,
    status: str | None = None,
    category: str | None = None,
    source_filter: str | None = None,
    rejection_class: str | None = None,
    contract_ref: str | None = None,
    operation_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    source = _preview_source_webspace_id(webspace_id, meta)
    inspector = preview.context_inspector(source, limit=max(1, min(int(limit), 100)))
    projection = (
        dict(inspector.get("development_feedback"))
        if isinstance(inspector.get("development_feedback"), Mapping)
        else {}
    )
    status_filter = str(status or "").strip()
    category_filter = str(category or "").strip()
    producer_filter = str(source_filter or "").strip()
    rejection_filter = str(rejection_class or "").strip()
    contract_filter = str(contract_ref or "").strip()
    operation_filter = str(operation_id or "").strip()
    rows: list[dict[str, Any]] = []
    for value in projection.get("items") or []:
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        if status_filter and item.get("status") != status_filter:
            continue
        if category_filter and item.get("category") != category_filter:
            continue
        if producer_filter and item.get("source") != producer_filter:
            continue
        classification = (
            dict(item.get("classification"))
            if isinstance(item.get("classification"), Mapping)
            else {}
        )
        rejection = str(classification.get("rejection_class") or "").strip()
        if rejection_filter and rejection != rejection_filter:
            continue
        application_trace = (
            dict(classification.get("application_trace"))
            if isinstance(classification.get("application_trace"), Mapping)
            else {}
        )
        contract = str(
            application_trace.get("contract_ref")
            or classification.get("public_contract_ref")
            or ""
        ).strip()
        operations = list(
            dict.fromkeys(
                str(value).strip()
                for value in (
                    application_trace.get("operation_id"),
                    *(classification.get("operation_ids") or []),
                )
                if str(value or "").strip()
            )
        )
        if contract_filter and contract != contract_filter:
            continue
        if operation_filter and operation_filter not in operations:
            continue
        feedback_id = str(item.get("feedback_id") or "").strip()
        if not feedback_id:
            continue
        details = str(item.get("details") or item.get("recommendation") or "").strip()
        item.update(
            {
                "id": feedback_id,
                "title": str(item.get("summary") or feedback_id),
                "subtitle": " · ".join(
                    token
                    for token in (
                        str(item.get("category") or "").strip(),
                        str(item.get("status") or "").strip(),
                    )
                    if token
                ),
                "preview": details,
                "targets": ", ".join(str(ref) for ref in item.get("target_refs") or []),
                "blocking_label": "blocking" if item.get("blocking") else "non-blocking",
                "rejection_class": rejection,
                "contract_ref": contract,
                "operation_id": ", ".join(operations),
                "input_summary": str(application_trace.get("input_summary") or "").strip(),
                "expected_behavior": str(
                    application_trace.get("expected_behavior")
                    or classification.get("expected_behavior")
                    or ""
                ).strip(),
                "observed_behavior": str(
                    application_trace.get("observed_behavior")
                    or classification.get("observed_behavior")
                    or ""
                ).strip(),
                "validation_result": str(
                    application_trace.get("validation_result") or ""
                ).strip(),
                "user_response": str(
                    application_trace.get("user_response") or ""
                ).strip(),
            }
        )
        rows.append(item)
    return rows


@tool(
    "list_development_feedback",
    summary="List project-scoped model, Codex, validator, and review feedback.",
    side_effects="none",
)
def list_development_feedback(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    status: str | None = None,
    category: str | None = None,
    source: str | None = None,
    rejection_class: str | None = None,
    contract_ref: str | None = None,
    operation_id: str | None = None,
    limit: int = 50,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    _identity(object_type, object_id)
    return _development_feedback_rows(
        webspace_id=webspace_id,
        meta=_meta,
        status=status,
        category=category,
        source_filter=source,
        rejection_class=rejection_class,
        contract_ref=contract_ref,
        operation_id=operation_id,
        limit=limit,
    )


@tool(
    "get_development_feedback",
    summary="Read one project-scoped development feedback observation.",
    side_effects="none",
)
def get_development_feedback(
    feedback_id: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _identity(object_type, object_id)
    token = str(feedback_id or "").strip()
    if not token:
        raise ValueError("feedback_id is required")
    item = next(
        (
            row
            for row in _development_feedback_rows(
                webspace_id=webspace_id,
                meta=_meta,
                limit=100,
            )
            if row.get("feedback_id") == token
        ),
        None,
    )
    if item is None:
        raise ValueError("development feedback is not available in the selected project context")
    return item


@tool("save_prompt_context", summary="Save the base technical specification.", side_effects="local_write")
def save_prompt_context(
    text: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _require_transport_integrity(text)
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
    _require_transport_integrity(text, iteration_ref)
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
    if kind == "project":
        current = _composition_manifest(project_id)
        if project_type is not None and str(project_type).strip() not in {"", "project"}:
            raise ValueError("project_type is immutable after creation (current: project)")
        if title is not None and not str(title).strip():
            raise ValueError("title must not be empty")
        payload = {
            key: value
            for key, value in current.items()
            if key not in {"ref", "manifest_digest", "source_path"}
        }
        catalog = _composition_catalog(payload)
        if title is not None:
            catalog["title"] = str(title).strip()
        if description is not None:
            catalog["description"] = str(description).strip()
        payload["catalog"] = catalog
        updated = compositions.replace(
            project_id,
            payload,
            expected_manifest_digest=str(current.get("manifest_digest") or ""),
        )
        return _project_descriptor("project", project_id, updated)
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
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    token = str(state or "").strip().lower()
    if token == "prototype_stable":
        return workflow.transition(workflow_kind, workflow_id, "stabilize_prototype", actor="builder.ui.compat")
    if token == "automation":
        return workflow.transition(workflow_kind, workflow_id, "handoff_to_automation", actor="builder.ui.compat")
    if token == "publication":
        raise ValueError("Publication is an immutable snapshot, not an active workflow phase")
    raise ValueError("use an explicit Builder workflow transition")


@tool("get_workflow", summary="Read the authoritative Builder workflow state.", side_effects="none")
def get_workflow(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    result = dict(workflow.get_state(workflow_kind, workflow_id))
    result["execution_scope"] = _execution_scope(kind, project_id)
    return result


@tool("transition_workflow", summary="Apply one validated Builder workflow transition.", side_effects="local_write")
def transition_workflow(
    action: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    expected_generation: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    try:
        result = workflow.transition(
            workflow_kind,
            workflow_id,
            action,
            actor="builder.ui",
            metadata=dict(metadata or {}),
            expected_generation=expected_generation,
        )
        result["execution_scope"] = _execution_scope(kind, project_id)
        return result
    except Exception as exc:
        if "stale Builder action generation" not in str(exc):
            raise
        current = workflow.get_state(workflow_kind, workflow_id)
        return {
            "ok": False,
            "stale": True,
            "error": str(exc),
            "workflow": current,
            "interaction_frame": workflow.get_interaction_frame(workflow_kind, workflow_id),
            "execution_scope": _execution_scope(kind, project_id),
        }


@tool(
    "accept_prototype",
    summary="Accept one exact executable Prototype revision with review evidence.",
    side_effects="local_write",
)
def accept_prototype(
    reviewer_id: str,
    reviewer_kind: str,
    behavior_checks: list[Mapping[str, Any]],
    visual_checks: list[Mapping[str, Any]],
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    delegated_by: str | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    result = workflow.accept_prototype(
        workflow_kind,
        workflow_id,
        reviewer={
            "id": str(reviewer_id).strip(),
            "kind": str(reviewer_kind).strip().lower(),
            "delegated_by": str(delegated_by or "").strip() or None,
        },
        behavior_checks=[dict(item) for item in behavior_checks if isinstance(item, Mapping)],
        visual_checks=[dict(item) for item in visual_checks if isinstance(item, Mapping)],
        actor="builder.prototype.review",
        expected_generation=expected_generation,
    )
    result["execution_scope"] = _execution_scope(kind, project_id)
    return result


@tool("get_interaction_frame", summary="Read deterministic Builder actions for the current context.", side_effects="none")
def get_interaction_frame(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    frame = workflow.get_interaction_frame(workflow_kind, workflow_id)
    frame["execution_scope"] = _execution_scope(kind, project_id)
    try:
        binding = preview.get_binding(_preview_source_webspace_id(webspace_id, _meta))
    except Exception:
        binding = {}
    target = binding.get("preview_target") if isinstance(binding.get("preview_target"), Mapping) else {}
    if target:
        stage = str(target.get("stage") or "prototype")
        revision = str(target.get("revision") or "current")
        frame["context"]["preview_target"] = f"{stage}:{project_id}:{revision}"
    return frame


@tool("inspect_process_ref", summary="Inspect one Process item without changing Preview.", side_effects="local_write")
def inspect_process_ref(
    inspected_ref: str | None,
    expected_generation: int | None = None,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    requested_generation = expected_generation
    for attempt in range(2):
        current = workflow.get_state(workflow_kind, workflow_id)
        current_generation = int(current.get("generation") or 0)
        try:
            result = workflow.update_interaction_context(
                workflow_kind,
                workflow_id,
                {"inspected_ref": inspected_ref},
                expected_generation=current_generation,
            )
        except ValueError as exc:
            if attempt == 0 and "stale Builder action generation" in str(exc):
                continue
            raise
        payload = dict(result)
        payload["requested_generation"] = requested_generation
        payload["applied_generation"] = current_generation
        payload["stale_reconciled"] = (
            requested_generation is not None
            and int(requested_generation) != current_generation
        )
        return payload
    raise RuntimeError("Builder Process selection could not reconcile its generation")


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
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """Persist an LLM- or user-structured request without pretending it is a global AdaOS Issue."""

    _require_transport_integrity(request, issues, source_message_ids)
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    selected_change_set_id = str(change_set_id or "").strip() or f"builder-change-{uuid4().hex}"
    source = _webspace_id(webspace_id, _meta)
    result = workflow.transition(
        workflow_kind,
        workflow_id,
        "plan_change_set",
        actor="builder.change_planner",
        metadata={
            "change_set_id": selected_change_set_id,
            "request": request,
            "issues": [dict(item) for item in issues if isinstance(item, Mapping)],
            "source_message_ids": list(source_message_ids or []),
            "supersedes_change_set_id": str(supersedes_change_set_id or "").strip() or None,
            "run_id": f"builder-plan-{uuid4().hex}",
            "prototype_acceptance_required": any(
                str(item.get("lane") or "").strip().lower() == "prototype"
                for item in issues
                if isinstance(item, Mapping)
            ),
        },
        expected_generation=expected_generation,
    )
    projection = result.get("workflow") if isinstance(result.get("workflow"), Mapping) else {}
    change_set = projection.get("change_set") if isinstance(projection.get("change_set"), Mapping) else {}
    change = projection.get("change") if isinstance(projection.get("change"), Mapping) else {}
    context_packet = workflow.build_context_packet(workflow_kind, workflow_id, persist=True)
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
        "change": dict(change),
        "change_id": change.get("change_id") or change_set.get("change_set_id"),
        "context_packet": context_packet,
        "evidence": evidence or None,
        "evidence_synced": evidence_error is None,
        "evidence_error": evidence_error,
        "execution_scope": _execution_scope(kind, project_id),
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
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """Extend one active change set while preserving its prior evidence and identity."""

    _require_transport_integrity(request, issues, source_message_ids)
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    source = _webspace_id(webspace_id, _meta)
    current = workflow.get_state(workflow_kind, workflow_id)
    active = current.get("change_set") if isinstance(current.get("change_set"), Mapping) else {}
    active_id = str(active.get("change_set_id") or "").strip()
    selected_id = str(change_set_id or active_id).strip()
    if not selected_id:
        raise ValueError("an active change set is required")
    result = workflow.transition(
        workflow_kind,
        workflow_id,
        "change_issues_added",
        actor="builder.change_planner",
        metadata={
            "change_set_id": selected_id,
            "change_id": str(change_id or "").strip() or f"builder_change_{uuid4().hex[:12]}",
            "request": request,
            "issues": [dict(item) for item in issues if isinstance(item, Mapping)],
            "source_message_ids": list(source_message_ids or []),
            "run_id": f"builder-plan-{uuid4().hex}",
        },
        expected_generation=expected_generation,
    )
    projection = result.get("workflow") if isinstance(result.get("workflow"), Mapping) else {}
    updated = projection.get("change_set") if isinstance(projection.get("change_set"), Mapping) else {}
    change = projection.get("change") if isinstance(projection.get("change"), Mapping) else {}
    context_packet = workflow.build_context_packet(workflow_kind, workflow_id, persist=True)
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
        "change": dict(change),
        "change_id": change.get("change_id") or updated.get("change_set_id"),
        "context_packet": context_packet,
        "evidence": evidence or None,
        "evidence_synced": evidence_error is None,
        "evidence_error": evidence_error,
        "execution_scope": _execution_scope(kind, project_id),
    }


@tool("split_change_issue", summary="Split one ambiguous active Issue into explicit replacement Issues.", side_effects="local_write")
def split_change_issue(
    issue_id: str,
    replacement_issues: list[Mapping[str, Any]],
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    change_id: str | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    return builder_issues.split(
        workflow_kind,
        workflow_id,
        issue_id,
        replacement_issues,
        change_id=change_id,
        expected_generation=expected_generation,
    )


@tool("merge_change_issues", summary="Merge active Issues into one explicit replacement Issue.", side_effects="local_write")
def merge_change_issues(
    issue_ids: list[str],
    replacement_issue: Mapping[str, Any],
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    change_id: str | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    return builder_issues.merge(
        workflow_kind,
        workflow_id,
        issue_ids,
        replacement_issue,
        change_id=change_id,
        expected_generation=expected_generation,
    )


@tool("get_change_set", summary="Read the active Builder change set and its durable evidence.", side_effects="none")
def get_change_set(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    projection = workflow.get_state(workflow_kind, workflow_id)
    change_set = projection.get("change_set") if isinstance(projection.get("change_set"), Mapping) else {}
    change = projection.get("change") if isinstance(projection.get("change"), Mapping) else {}
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
            "canSplit": str(item.get("structural_status") or "active") == "active",
            "structuralStatus": str(item.get("structural_status") or "active"),
        }
        for item in change_set.get("issues") or []
        if isinstance(item, Mapping)
    ]
    return {
        "ok": True,
        "object_type": kind,
        "object_id": project_id,
        "change_set": dict(change_set),
        "change": dict(change),
        "change_id": change.get("change_id") or change_set_id or None,
        "change_set_id": change_set_id or None,
        "status": change_set.get("status") or "not_planned",
        "gate": change_set.get("gate"),
        "route": change_set.get("route"),
        "request": change_set.get("request"),
        "issues": issues,
        "evidence": dict(evidence) if isinstance(evidence, Mapping) else None,
        "evidence_synced": bool(evidence),
        "execution_scope": _execution_scope(kind, project_id),
    }


@tool("get_change_context", summary="Inspect the bounded execution context for the active Change.", side_effects="none")
def get_change_context(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    packet = workflow.build_context_packet(workflow_kind, workflow_id, persist=False)
    return {
        "ok": True,
        "schema": packet.get("schema"),
        "digest": packet.get("digest"),
        "built_at": packet.get("built_at"),
        "project": packet.get("project"),
        "change": packet.get("change"),
        "base": packet.get("base"),
        "artifacts": packet.get("artifacts"),
        "dependencies": packet.get("dependencies"),
        "allowed_paths": packet.get("allowed_paths"),
        "instruction_refs": packet.get("instruction_refs"),
        "previous_run": packet.get("previous_run"),
        "conversation": packet.get("conversation"),
        "pending_actions": packet.get("pending_actions"),
        "budget": packet.get("budget"),
        "omitted_categories": ["raw_transcript", "secrets", "unselected_files"],
        "execution_scope": _execution_scope(kind, project_id),
    }


@tool("apply_semantic_ui_change", summary="Apply one reversible semantic UI operation.", side_effects="local_write")
def apply_semantic_ui_change(
    operation_id: str,
    change_id: str,
    target_ref: str,
    source_revision: str,
    value: Any,
    operation: str = "rename",
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    review_id: str | None = None,
    acceptance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    return semantic_ui.apply(
        {
            "schema": "adaos.builder.semantic_ui_change.v1",
            "operation_id": operation_id,
            "change_id": change_id,
            "project_ref": f"{kind}:{project_id}",
            "review_id": str(review_id or "").strip() or None,
            "operation": operation,
            "target_ref": target_ref,
            "source_revision": source_revision,
            "value": value,
            "risk": "local_reversible",
            "acceptance": dict(acceptance) if isinstance(acceptance, Mapping) else None,
        }
    )


@tool("register_review_constraint", summary="Compile one structured Review note into an acceptance constraint.", side_effects="local_write")
def register_review_constraint(
    review_id: str,
    change_id: str,
    target_ref: str,
    comment: str,
    kind: str,
    expected: Any,
    source_revision: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    author_ref: str = "user:owner",
    expected_generation: int | None = None,
) -> dict[str, Any]:
    project_kind, project_id = _identity(object_type, object_id)
    execution_kind, execution_id = _execution_identity(project_kind, project_id)
    if execution_kind != "scenario":
        raise ValueError("typed Review constraints currently support scenarios only")
    anchor = {
        "schema": "adaos.builder.review_anchor.v1",
        "review_id": str(review_id or "").strip(),
        "change_id": str(change_id or "").strip(),
        "artifact_ref": f"scenario:{execution_id}@ui_revision:{str(source_revision or '').strip()}",
        "target_ref": str(target_ref or "").strip(),
        "comment": str(comment or "").strip(),
        "status": "accepted",
        "author_ref": str(author_ref or "user:owner").strip(),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    return review.register_constraint(
        anchor,
        kind=kind,
        expected=expected,
        source_revision=source_revision,
        expected_generation=expected_generation,
    )


@tool("evaluate_review_constraints", summary="Verify active Review constraints against the current UI revision.", side_effects="local_write")
def evaluate_review_constraints(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    revision: str | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    project_kind, project_id = _identity(object_type, object_id)
    execution_kind, execution_id = _execution_identity(project_kind, project_id)
    return review.evaluate_current(
        execution_kind,
        execution_id,
        revision=revision,
        expected_generation=expected_generation,
    )


@tool(
    "link_dependency_checkpoint",
    summary="Link one declared dependency checkpoint into the active Builder change set.",
    side_effects="local_write",
)
def link_dependency_checkpoint(
    dependency_type: str,
    dependency_id: str,
    checkpoint_change_id: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge immutable dependency evidence without rebuilding or delivering either project."""

    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    dependency_kind, selected_dependency_id = _identity(dependency_type, dependency_id)
    checkpoint_id = str(checkpoint_change_id or "").strip()
    if not checkpoint_id:
        raise ValueError("checkpoint_change_id is required")
    if (kind, project_id) == (dependency_kind, selected_dependency_id):
        raise ValueError("a project cannot link its own dependency checkpoint")
    if dependency_kind != "skill" or selected_dependency_id not in _declared_skill_dependencies(kind, project_id):
        raise ValueError(f"dependency is not declared by the canonical project manifest: {dependency_kind}:{selected_dependency_id}")
    if bool(_context(kind, project_id).get("archived")):
        raise ValueError("archived projects cannot link dependency checkpoints")

    project_workflow = workflow.get_state(workflow_kind, workflow_id)
    change_set = project_workflow.get("change_set") if isinstance(project_workflow.get("change_set"), Mapping) else {}
    change_set_id = str(change_set.get("change_set_id") or "").strip()
    if not change_set_id:
        raise ValueError("an active change set is required")
    if str(change_set.get("status") or "").strip().lower() in {"published", "rejected", "superseded"}:
        raise ValueError("the active change set is already terminal")

    dependency_workflow = workflow.get_state(dependency_kind, selected_dependency_id)
    delivery = dependency_workflow.get("delivery") if isinstance(dependency_workflow.get("delivery"), Mapping) else {}
    receipt_change_id = str(delivery.get("checkpoint_change_id") or "").strip()
    if str(delivery.get("status") or "").strip().lower() != "checkpoint":
        raise ValueError("dependency workflow delivery is not checkpointed")
    if receipt_change_id != checkpoint_id:
        raise ValueError("dependency checkpoint_change_id does not match the requested checkpoint")
    if not str(delivery.get("package_digest") or "").strip() or not str(delivery.get("source_revision") or "").strip():
        raise ValueError("dependency checkpoint receipt is incomplete")

    already_linked = checkpoint_id in {
        str(item).strip() for item in change_set.get("member_change_ids") or [] if str(item).strip()
    }
    transitioned = workflow.transition(
        workflow_kind,
        workflow_id,
        "change_evidence_recorded",
        actor="builder.dependency_checkpoint_linker",
        metadata={"change_set_id": change_set_id, "change_id": checkpoint_id},
    )
    updated_workflow = transitioned.get("workflow") if isinstance(transitioned.get("workflow"), Mapping) else {}
    updated_change_set = updated_workflow.get("change_set") if isinstance(updated_workflow.get("change_set"), Mapping) else {}
    evidence = _sync_change_set_record(
        kind=kind,
        project_id=project_id,
        webspace_id=_webspace_id(webspace_id, _meta),
        change_set=updated_change_set,
    )
    receipt = {
        "object_type": dependency_kind,
        "object_id": selected_dependency_id,
        "status": "checkpoint",
        "checkpoint_change_id": receipt_change_id,
        "package_digest": str(delivery["package_digest"]),
        "source_revision": str(delivery["source_revision"]),
    }
    return {
        **transitioned,
        "linked": not already_linked,
        "idempotent": already_linked,
        "dependency_receipt": receipt,
        "change_set": dict(updated_change_set),
        "evidence": evidence,
        "workflow": dict(updated_workflow),
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
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    source = _webspace_id(webspace_id, _meta)
    result = workflow.transition(
        workflow_kind,
        workflow_id,
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


def _select_scenario_preview_target(
    kind: str,
    project_id: str,
    *,
    stage: str,
    revision: str | None = None,
    follow_active: bool = False,
    source_webspace_id: str,
) -> dict[str, Any]:
    """Materialize one target through the SDK's atomic selection boundary.

    ``preview.select_target`` publishes the single ``builder.context.selected``
    projection event after selection succeeds.  It intentionally does not
    publish ``builder.preview.desired`` because target materialization is
    synchronous.  Keeping both public tools on this boundary prevents a second
    control-layer event from duplicating either operation.
    """

    options: dict[str, Any] = {
        "stage": stage,
        "source_webspace_id": source_webspace_id,
        "follow_active": follow_active,
    }
    if revision is not None:
        options["revision"] = revision
    result = preview.select_target(kind, project_id, **options)
    selected = result.get("target") if isinstance(result.get("target"), Mapping) else {}
    selected_stage = str(selected.get("stage") or stage or "prototype").strip()
    selected_revision = str(selected.get("revision") or revision or "current").strip()
    try:
        current = workflow.get_state(kind, project_id)
        interaction = workflow.update_interaction_context(
            kind,
            project_id,
            {"preview_target": f"{selected_stage}:{project_id}:{selected_revision}"},
            expected_generation=int(current.get("generation") or 0),
        )
    except Exception as exc:
        # Preview was already materialized. Report, but never retry, the
        # independent context projection update.
        result["interaction_updated"] = False
        result["interaction_error"] = str(exc)
        return result
    result["interaction_updated"] = True
    result["interaction"] = interaction.get("workflow", {}).get("interaction")
    return result


@tool("select_preview", summary="Select a DEV scenario in its paired preview.", side_effects="ui_navigation")
def select_preview(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    if kind == "scenario":
        return _select_scenario_preview_target(
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
    if kind == "project":
        if str(stage or "").strip().lower() not in {"", "prototype"} and not follow_active:
            raise ValueError("only the project prototype entrypoint can be shown in Preview")
        result = preview.select_project(
            kind,
            project_id,
            source_webspace_id=_preview_source_webspace_id(webspace_id, _meta),
            ensure_ready=True,
            wait_for_rebuild=True,
            publish_event=True,
        )
        try:
            execution_kind, execution_id = _execution_identity(kind, project_id)
            current = workflow.get_state(execution_kind, execution_id)
            interaction = workflow.update_interaction_context(
                execution_kind,
                execution_id,
                {"preview_target": f"prototype:{project_id}:current"},
                expected_generation=int(current.get("generation") or 0),
            )
            result["interaction_updated"] = True
            result["interaction"] = interaction.get("workflow", {}).get("interaction")
        except Exception as exc:
            result["interaction_updated"] = False
            result["interaction_error"] = str(exc)
        return result
    return _select_scenario_preview_target(
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
    try:
        navigation = preview.navigation_link(source)
    except (RuntimeError, ValueError):
        # Isolated validator/tests may not have subnet navigation identity.
        opened = preview.open_workspace(source)
        navigation = {
            "url": str(opened.get("url") or f"/?webspace={_preview_dev_webspace_id(source)}"),
            "destination": {},
        }
    target = binding.get("preview_target") if isinstance(binding.get("preview_target"), Mapping) else {}
    normalized_target = dict(target)
    if target and not str(normalized_target.get("label") or "").strip():
        normalized_target["label"] = _preview_label(target.get("stage"), target.get("revision"))
    return {
        **binding,
        "ok": bool(binding.get("ok", True)),
        "source_webspace_id": source,
        "dev_webspace_id": str(binding.get("dev_webspace_id") or _preview_dev_webspace_id(source)),
        "preview_url": str(navigation["url"]),
        "qr_text": str(navigation["url"]),
        "navigation": dict(navigation),
        "status": "ready" if binding.get("runtime_scenario_id") else "not_selected",
        "preview_target": normalized_target,
        "viewing": normalized_target.get("label"),
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
    conversation_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    source = _webspace_id(webspace_id, _meta)
    topic = _project_topic(kind, project_id, webspace_id=source)
    result = automation.get_state(
        object_type=kind,
        object_id=project_id,
        webspace_id=source,
        conversation_id=str(conversation_id or topic.get("conversation_id") or "").strip() or None,
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
        "ok": bool(result.get("ok")),
        "session_present": bool(result.get("session_present", result.get("ok"))),
        "automation": dict(projection),
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
        "execution_scope": _execution_scope(kind, project_id),
    }


@tool("get_process", summary="Project dependent Change, Prototype, Implementation, Trial, and Release provenance.", side_effects="none")
def get_process(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    """Return an on-demand lineage tree; inspecting its nodes never selects Preview."""

    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    projection = workflow.get_state(workflow_kind, workflow_id)
    change = projection.get("change") if isinstance(projection.get("change"), Mapping) else {}
    prototype = projection.get("prototype") if isinstance(projection.get("prototype"), Mapping) else {}
    implementation = projection.get("automation") if isinstance(projection.get("automation"), Mapping) else {}
    delivery = projection.get("delivery") if isinstance(projection.get("delivery"), Mapping) else {}
    publication = projection.get("publication") if isinstance(projection.get("publication"), Mapping) else {}
    project = projection.get("project") if isinstance(projection.get("project"), Mapping) else {}
    change_id = str(change.get("change_id") or change.get("change_set_id") or "").strip()

    issues = [
        {
            "id": f"issue:{item.get('issue_id')}",
            "ref": f"issue:{item.get('issue_id')}",
            "kind": "issue",
            "title": str(item.get("title") or item.get("issue_id") or "Issue"),
            "status": str(item.get("status") or "open"),
            "lane": str(item.get("lane") or "prototype"),
            "acceptance": list(item.get("acceptance_criteria") or []),
            "children": [],
        }
        for item in change.get("issues") or []
        if isinstance(item, Mapping)
    ]
    runs = [dict(item) for item in change.get("runs") or [] if isinstance(item, Mapping)]
    revision_ids: list[str] = []
    if workflow_kind == "scenario":
        for item in projects.list_files(workflow_kind, workflow_id, limit=1000):
            path = PurePosixPath(str(item.get("path") or ""))
            if len(path.parts) == 2 and path.parts[0] == "ui_revisions" and path.suffix == ".json" and path.stem.isdigit():
                revision_ids.append(path.stem)
    source_revision = str(
        implementation.get("source_prototype_revision") or prototype.get("head_revision") or ""
    ).strip()
    implementation_children: list[dict[str, Any]] = []
    implementation_status = str(implementation.get("status") or "not_started")
    if implementation_status != "not_started" or runs:
        trial_children: list[dict[str, Any]] = []
        delivery_status = str(delivery.get("status") or "idle")
        if delivery_status != "idle":
            publication_children: list[dict[str, Any]] = []
            publication_status = str(publication.get("status") or "not_started")
            if publication_status != "not_started":
                publication_children.append(
                    {
                        "id": "publication:current",
                        "ref": f"publication:{project_id}:{publication.get('current_version') or 'current'}",
                        "kind": "publication",
                        "title": f"Publication {publication.get('current_version') or ''}".strip(),
                        "status": publication_status,
                        "previewStage": "publication",
                        "revision": str(publication.get("current_version") or "current"),
                        "canPreview": publication_status == "published" and kind == "scenario",
                        "updated_at": publication.get("published_at"),
                        "children": [],
                    }
                )
            trial_placement = next(
                (
                    dict(item)
                    for item in reversed(project.get("placements") or [])
                    if isinstance(item, Mapping)
                    and str(item.get("kind") or "") == "trial"
                    and str(item.get("status") or "") == "active"
                ),
                None,
            )
            trial_children_nodes: list[dict[str, Any]] = []
            if trial_placement:
                target = (
                    trial_placement.get("target")
                    if isinstance(trial_placement.get("target"), Mapping)
                    else {}
                )
                result_ref = (
                    trial_placement.get("result_ref")
                    if isinstance(trial_placement.get("result_ref"), Mapping)
                    else {}
                )
                trial_children_nodes.append(
                    {
                        "id": f"placement:{trial_placement.get('placement_id')}",
                        "ref": f"placement:{trial_placement.get('placement_id')}",
                        "kind": "placement",
                        "title": f"Beta in {target.get('webspace_id') or 'trial workspace'}",
                        "status": "active",
                        "placementKind": "trial",
                        "canOpenPlacement": True,
                        "revision": str(result_ref.get("version") or "current"),
                        "children": [],
                    }
                )
            trial_children.append(
                {
                    "id": f"trial:{delivery.get('candidate_id') or 'current'}",
                    "ref": f"trial:{delivery.get('candidate_id') or 'current'}",
                    "kind": "trial",
                    "title": "Trial",
                    "status": delivery_status,
                    "updated_at": delivery.get("prepared_at") or delivery.get("decided_at"),
                    "children": [*trial_children_nodes, *publication_children],
                }
            )
        implementation_children.append(
            {
                "id": f"implementation:{implementation.get('head_task_id') or 'current'}",
                "ref": f"implementation:{implementation.get('head_task_id') or 'current'}",
                "kind": "implementation",
                "title": "Implementation",
                "status": implementation_status,
                "source_prototype_revision": source_revision or None,
                "previewStage": "automation",
                "revision": str(
                    implementation.get("result_version")
                    or implementation.get("snapshot_task_id")
                    or implementation.get("head_task_id")
                    or "current"
                ),
                "canPreview": implementation_status in {"completed", "failed", "frozen"} and kind == "scenario",
                "runs": runs,
                "children": trial_children,
            }
        )

    revision_nodes: list[dict[str, Any]] = []
    for revision in sorted(set(revision_ids), reverse=True)[:20]:
        revision_nodes.append(
            {
                "id": f"prototype:{project_id}:{revision}",
                "ref": f"prototype:{project_id}:{revision}",
                "kind": "prototype_revision",
                "title": f"Prototype UI {revision}",
                "status": "current" if revision == str(prototype.get("head_revision") or "") else "previous",
                "previewStage": "prototype",
                "revision": revision,
                "canPreview": kind == "scenario",
                "children": implementation_children if revision == source_revision else [],
            }
        )
    if implementation_children and not any(item["children"] for item in revision_nodes):
        revision_nodes.insert(
            0,
            {
                "id": f"prototype:{project_id}:{source_revision or 'current'}",
                "ref": f"prototype:{project_id}:{source_revision or 'current'}",
                "kind": "prototype_revision",
                "title": f"Prototype {source_revision or 'current'}",
                "status": "source",
                "previewStage": "prototype",
                "revision": source_revision or "current",
                "canPreview": kind == "scenario",
                "children": implementation_children,
            },
        )
    root_ref = f"change:{change_id}" if change_id else f"{kind}:{project_id}"
    return {
        "ok": True,
        "schema": "adaos.builder.process.v1",
        "project_ref": f"{kind}:{project_id}",
        "execution_ref": f"{workflow_kind}:{workflow_id}",
        "generation": projection.get("generation"),
        "interaction": dict(projection.get("interaction") or {}),
        "change": dict(change),
        "tree": [
            {
                "id": root_ref,
                "ref": root_ref,
                "kind": "change" if change_id else "project",
                "title": str(change.get("request") or project_id),
                "status": str(change.get("status") or "ready"),
                "children": [
                    {
                        "id": "issues",
                        "ref": root_ref,
                        "kind": "issue_group",
                        "title": "Issues",
                        "status": f"{len(issues)} items",
                        "children": issues,
                    },
                    {
                        "id": "prototypes",
                        "ref": f"prototype:{project_id}",
                        "kind": "prototype_group",
                        "title": "Prototypes",
                        "status": str(prototype.get("status") or "working"),
                        "children": revision_nodes,
                    },
                ],
            }
        ],
    }


@tool("get_process_tree", summary="List the dependent Builder Process tree for rich renderers.", side_effects="none")
def get_process_tree(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> list[dict[str, Any]]:
    return list(get_process(object_type, object_id).get("tree") or [])


@tool(
    "get_project_placement_navigation",
    summary="Open one active Project placement through the topology-aware navigation contract.",
    side_effects="ui_navigation",
)
def get_project_placement_navigation(
    placement_kind: str = "stable",
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    base_url: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    if kind != "project":
        raise ValueError("Project placement navigation requires object_type=project")
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    placement_token = str(placement_kind or "stable").strip().lower()
    if placement_token not in {"trial", "stable"}:
        raise ValueError("placement_kind must be trial or stable")
    materialization: dict[str, Any] | None = None
    try:
        result = workflow.get_project_placement_navigation(
            workflow_kind,
            workflow_id,
            kind=placement_token,
            base_url=str(base_url or "").strip() or None,
        )
    except ValueError as exc:
        if placement_token != "stable" or "stable ProjectPlacement is unavailable" not in str(exc):
            raise
        published_workflow = workflow.get_state(workflow_kind, workflow_id)
        publication = (
            published_workflow.get("publication")
            if isinstance(published_workflow.get("publication"), Mapping)
            else {}
        )
        if str(publication.get("status") or "").strip() != "published":
            raise
        published_workflow, materialization = _ensure_stable_placement(
            workflow_kind,
            workflow_id,
            owner_kind=kind,
            owner_id=project_id,
            result={
                "release": publication.get("release"),
                "version": publication.get("current_version"),
            },
            published_workflow=published_workflow,
            webspace_id=webspace_id,
            meta=_meta,
        )
        result = workflow.get_project_placement_navigation(
            workflow_kind,
            workflow_id,
            kind=placement_token,
            base_url=str(base_url or "").strip() or None,
        )
    if placement_token == "trial":
        placement = (
            result.get("placement")
            if isinstance(result.get("placement"), Mapping)
            else {}
        )
        target = (
            placement.get("target")
            if isinstance(placement.get("target"), Mapping)
            else {}
        )
        result_ref = (
            placement.get("result_ref")
            if isinstance(placement.get("result_ref"), Mapping)
            else {}
        )
        scenario_id = str(placement.get("scenario_id") or "").strip()
        revision = str(result_ref.get("version") or "").strip()
        target_webspace_id = str(target.get("webspace_id") or "").strip()
        if not scenario_id or not revision or not target_webspace_id:
            raise ValueError("Active Project trial placement is incomplete")
        source_webspace_id = _preview_source_webspace_id(webspace_id, _meta)
        materialization = preview.materialize_revision(
            webspace_id=target_webspace_id,
            scenario_id=scenario_id,
            revision=revision,
            preview_stage="trial",
            preview_label=f"trial: {project_id} · {revision}",
            source_fingerprint=str(result_ref.get("digest") or "").strip() or None,
            event_payload={
                "source": "builder.project.placement_navigation",
                "source_webspace_id": source_webspace_id,
                "preview_stage": "trial",
                "preview_revision": revision,
            },
        )
        if materialization.get("ok") is False:
            raise ValueError(
                str(materialization.get("error") or "Project trial materialization failed")
            )
    return {
        **dict(result),
        "ok": True,
        "preview_url": str(result.get("url") or ""),
        "qr_text": str(result.get("url") or ""),
        "materialization": materialization,
        "execution_scope": _execution_scope(kind, project_id),
    }


@tool("get_lifecycle", summary="Project the prototype, automation, and publication lifecycle tree.", side_effects="none")
def get_lifecycle(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    kind, project_id = _identity(object_type, object_id)
    project = _project_descriptor(kind, project_id)
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
    presentation_scenario_id = _project_presentation_scenario_id(project_id) if kind == "project" else project_id
    file_items = (
        list_project_files(kind, project_id, limit=1000)
        if kind == "project"
        else projects.list_files(kind, project_id, limit=1000)
    )
    for item in file_items:
        path = PurePosixPath(str(item.get("path") or ""))
        component_path = path
        if kind == "project":
            parts = path.parts
            if (
                len(parts) >= 5
                and parts[0] == "components"
                and parts[1] == "scenario"
                and parts[2] == presentation_scenario_id
            ):
                component_path = PurePosixPath(*parts[3:])
            else:
                file_updated_at[path.as_posix()] = item.get("updated_at")
                continue
        file_updated_at[component_path.as_posix()] = item.get("updated_at")
        if len(component_path.parts) == 2 and component_path.parts[0] == "ui_revisions" and component_path.suffix == ".json":
            revisions.append(component_path.stem)
    if kind in {"project", "scenario"} and presentation_scenario_id:
        try:
            current_revision = str(
                projects.read_file(
                    "scenario",
                    presentation_scenario_id,
                    "ui_revisions/current.txt",
                    max_bytes=64,
                )["content"]
            ).strip()
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
                "previewLabel": _preview_label("prototype", f"UI {revision}"),
                "updated_at": _datetime_value(file_updated_at.get(f"ui_revisions/{revision}.json")),
                "lifecycleStage": "prototype",
                "conversationLabel": "Prototype conversation",
                "badges": ["текущая"] if current else [],
                "canMakeCurrent": not current and active_phase == "prototype",
                "canStabilize": current and bool(workflow_capabilities.get("can_stabilize_prototype")),
                "canOpenAutomation": current and bool(workflow_capabilities.get("can_handoff_to_automation")),
                "canPreview": kind in {"project", "scenario"},
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
                "previewLabel": _preview_label("prototype", project.get("version") or "DEV"),
                "updated_at": _datetime_value(file_updated_at.get(str(project.get("manifest") or "scenario.yaml"))),
                "lifecycleStage": "prototype",
                "conversationLabel": "Prototype conversation",
                "badges": ["текущая"],
                "canStabilize": bool(workflow_capabilities.get("can_stabilize_prototype")),
                "canOpenAutomation": bool(workflow_capabilities.get("can_handoff_to_automation")),
                "canPreview": kind in {"project", "scenario"},
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
    # Prototype adaptation is transition evidence, not a second Automation
    # release. Keep the durable workflow result authoritative for that job.
    pending_transition = str(
        automation_projection.get("pending_workflow_transition")
        or automation_projection.get("workflow_transition")
        or ""
    ).strip().lower()
    runtime_task_id = str(
        automation_projection.get("task_id")
        or automation_projection.get("current_task_id")
        or ""
    ).strip()
    durable_task_id = str(
        workflow_automation.get("snapshot_task_id")
        or workflow_automation.get("head_task_id")
        or ""
    ).strip()
    adaptation_task = pending_transition == "return_to_prototype" or bool(
        durable_task_id
        and runtime_task_id
        and durable_task_id != runtime_task_id
        and str(workflow_automation.get("status") or "").strip().lower() in {"completed", "frozen"}
        and active_phase == "prototype"
    )
    combined_automation = (
        dict(workflow_automation)
        if adaptation_task
        else {**workflow_automation, **automation_projection}
    )
    runtime_status = str(automation_projection.get("status") or "").strip().lower()
    if runtime_status in {"", "idle", "not_started", "not-started", "default"}:
        combined_automation["status"] = workflow_automation.get("status") or runtime_status
    automation_children = _automation_children(combined_automation, project_version=project_version)
    workflow_result_version = str(workflow_automation.get("result_version") or "").strip()
    if automation_children and workflow_result_version:
        automation_children[0]["version"] = workflow_result_version
        automation_children[0]["title"] = _version_title(workflow_result_version)
        automation_children[0]["previewLabel"] = _preview_label(
            "automation", workflow_result_version
        )
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
    ) if automation_children else None
    publication_version = publication_children[0].get("version") if publication_children else None
    publication_updated_at = _datetime_value(
        publication_projection.get("published_at")
        or (publication_children[0].get("created_at") if publication_children else None)
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
        if automation_node is None:
            lineage_id = source_task or f"release-{release.get('change_id') or source_version}"
            automation_node = {
                "id": f"automation-lineage-{lineage_id}",
                "kind": "automation_result",
                "lifecycleState": "past",
                "title": _version_title(source_version),
                "previewLabel": _preview_label("automation", source_version),
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
            }
            if not source_task:
                automation_node["lineageInferred"] = True
                automation_node["lineageWarning"] = "publication_source_metadata_missing"
                release["lineageInferred"] = True
            automation_nodes.append(automation_node)
        release["canPreview"] = bool(
            workflow_capabilities.get("can_preview_publication")
            and str(release.get("version") or "").strip() == current_publication_version
        )
        release["canOpenPublication"] = True
        release["previewLabel"] = _preview_label("publication", release.get("version"))
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
    execution_budget: Mapping[str, Any] | None = None,
    agent_profile: Mapping[str, Any] | None = None,
    mcp: Mapping[str, Any] | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _require_transport_integrity(implementation_brief)
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    source = _webspace_id(webspace_id, _meta)
    topic = _project_topic(kind, project_id, webspace_id=source)
    bound_conversation_id = str(conversation_id or topic.get("conversation_id") or "").strip() or None
    workflow_state = workflow.get_state(workflow_kind, workflow_id)
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
            object_type=workflow_kind,
            object_id=workflow_id,
            webspace_id=source,
            _meta=_meta,
        )
        workflow_state = (
            planned.get("workflow")
            if isinstance(planned.get("workflow"), Mapping)
            else workflow.get_state(workflow_kind, workflow_id)
        )
        change_set = (
            workflow_state.get("change_set")
            if isinstance(workflow_state.get("change_set"), Mapping)
            else {}
        )
    result = dict(automation.start(
        object_type=kind,
        object_id=project_id,
        implementation_brief=implementation_brief,
        webspace_id=source,
        conversation_id=bound_conversation_id,
        brief_path=brief_path,
        change_set_id=str(change_set.get("change_set_id") or "").strip() or None,
        execution_budget=execution_budget,
        agent_profile=agent_profile,
        mcp=mcp,
    ) or {})
    result["execution_scope"] = _execution_scope(kind, project_id)
    return result


@tool("submit_automation", summary="Submit one follow-up Builder Automation turn.", side_effects="local_write")
def submit_automation(
    text: str,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    conversation_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _require_transport_integrity(text)
    kind, project_id = _identity(object_type, object_id)
    source = _webspace_id(webspace_id, _meta)
    topic = _project_topic(kind, project_id, webspace_id=source)
    result = dict(automation.submit(
        text,
        object_type=kind,
        object_id=project_id,
        webspace_id=source,
        conversation_id=str(conversation_id or topic.get("conversation_id") or "").strip() or None,
    ) or {})
    result["execution_scope"] = _execution_scope(kind, project_id)
    return result


@tool(
    "retry_failed_automation",
    summary="Retry the unchanged accepted Builder request after an executor failure.",
    side_effects="local_write",
)
def retry_failed_automation(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    conversation_id: str | None = None,
    execution_budget: Mapping[str, Any] | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    source = _webspace_id(webspace_id, _meta)
    topic = _project_topic(kind, project_id, webspace_id=source)
    result = dict(
        automation.retry_failed(
            object_type=kind,
            object_id=project_id,
            webspace_id=source,
            conversation_id=str(
                conversation_id or topic.get("conversation_id") or ""
            ).strip()
            or None,
            execution_budget=execution_budget,
        )
        or {}
    )
    result["execution_scope"] = _execution_scope(kind, project_id)
    return result


@tool("return_to_prototype", summary="Use the built-in LLM to derive a safe Prototype from Automation.", side_effects="local_write")
def return_to_prototype(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    result = dict(automation.return_to_prototype(
        object_type=kind,
        object_id=project_id,
        webspace_id=_webspace_id(webspace_id, _meta),
    ) or {})
    result["execution_scope"] = _execution_scope(kind, project_id)
    return result


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
    result = dict(automation.recover_validated_result(
        object_type=kind,
        object_id=project_id,
    ) or {})
    result["execution_scope"] = _execution_scope(kind, project_id)
    return result


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
    result = dict(automation.reconcile_checkpoint(
        object_type=kind,
        object_id=project_id,
    ) or {})
    result["execution_scope"] = _execution_scope(kind, project_id)
    return result


@tool(
    "get_subscription_update",
    summary="Inspect one stable subscription and its reviewed update plan.",
    side_effects="none",
)
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


@tool("create_project", summary="Create and select a first-class DEV Project.", side_effects="local_write")
def create_project(
    object_type: str,
    object_id: str,
    template: str | None = None,
    title: str | None = None,
    description: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    primary_kind, project_id = _identity(object_type, object_id)
    if primary_kind == "project":
        raise ValueError("object_type selects the primary component and must be scenario or skill")
    template_id = str(template or "").strip()
    if not template_id or template_id.lower() == "default":
        template_id = "scenario_default" if primary_kind == "scenario" else "skill_default"
    entrypoints = (
        [
            {
                "id": "main",
                "presentation": f"scenario:{project_id}",
                "default": True,
                "bindings": {},
            }
        ]
        if primary_kind == "scenario"
        else []
    )
    created = dict(
        compositions.create_with_primary_component(
            project_id,
            kind=primary_kind,
            component_id=project_id,
            template=template_id,
            title=str(title or project_id).strip(),
            description=str(description or "").strip(),
            entrypoints=entrypoints,
            actor="builder.user",
        )
        or {}
    )
    project = created.get("project") if isinstance(created.get("project"), Mapping) else {}
    catalog = project.get("catalog") if isinstance(project.get("catalog"), Mapping) else {}
    try:
        selected = dict(
            select_preview(
                "project",
                project_id,
                webspace_id=webspace_id,
                _meta=_meta,
            )
            or {}
        )
    except Exception as exc:
        # Creation is durable even if the preview transport disappears between
        # both operations. Return the project instead of inviting a conflicting
        # second create; selecting it later is the safe recovery path.
        selected = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "recovery": "select_project",
        }
    primary = (
        dict(created.get("primary_component"))
        if isinstance(created.get("primary_component"), Mapping)
        else {}
    )
    primary_identity = (
        str(primary.get("kind") or primary_kind).strip().lower().rstrip("s"),
        str(primary.get("id") or project_id).strip(),
    )
    topic = _project_topic(
        "project",
        project_id,
        webspace_id=webspace_id,
        meta=_meta,
        execution_identity=primary_identity,
    )
    return {
        **created,
        "ok": bool(created.get("ok", True)),
        "object_type": "project",
        "object_id": project_id,
        "project_ref": f"project:{project_id}",
        "primary_object_type": primary_kind,
        "primary_object_id": project_id,
        "primary_ref": f"{primary_kind}:{project_id}",
        "target_object_type": primary_kind,
        "target_object_id": project_id,
        "title": str(catalog.get("title") or project_id),
        "description": str(catalog.get("description") or ""),
        "preview": selected,
        "preview_selected": bool(selected.get("ok", True)),
        "conversation_id": topic.get("conversation_id"),
        "topic_id": topic.get("topic_id"),
        "thread_id": topic.get("thread_id"),
    }


@tool("delete_project", summary="Delete a project through the governed developer lifecycle.", side_effects="external_write")
def delete_project(
    confirm: bool = False,
    remove_local: bool = False,
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    if kind == "project":
        raise ValueError("Project aggregate deletion is not supported from Builder in this iteration")
    if not confirm:
        raise ValueError("confirm=true is required to delete a project")
    return projects.delete(kind, project_id, remove_local=remove_local)


@tool("push_project", summary="Checkpoint a DEV project in Forge.", side_effects="local_write")
def push_project(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    message: str | None = None,
    checkpoint_id: str | None = None,
    bump: str = "patch",
    confirmed: bool = False,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    checkpoint_change_id = str(checkpoint_id or "").strip()
    if not checkpoint_change_id:
        raise ValueError("checkpoint_id is required")
    if not confirmed:
        raise ValueError("Checkpoint acceptance requires explicit user confirmation")
    checkpoint_message = message or f"chore(builder): checkpoint {kind} {project_id}"
    workflow_before = workflow.get_state(workflow_kind, workflow_id)
    canonical_change_id, context_packet_digest = _workflow_execution_identity(workflow_before)
    checkpoint_metadata: dict[str, Any] = {
        "change_id": checkpoint_change_id,
        "run_id": checkpoint_change_id,
        "canonical_change_id": canonical_change_id or None,
        "context_packet_digest": context_packet_digest or None,
    }
    checkpoint_results: list[dict[str, Any]] = []
    if kind == "project":
        project_manifest = _composition_manifest(project_id)
        project_manifest = compositions.advance_version(
            project_id,
            bump=bump,
            expected_manifest_digest=str(project_manifest.get("manifest_digest") or ""),
        )
        for ref in _composition_owned_refs(project_id):
            component = _split_component_ref(ref)
            if component is None:
                continue
            component_kind, component_id = component
            component_result = projects.push(
                component_kind,
                component_id,
                message=checkpoint_message,
                metadata={**checkpoint_metadata, "project_ref": f"project:{project_id}"},
            )
            checkpoint_results.append({"ref": ref, **dict(component_result)})
        if not checkpoint_results:
            raise ValueError("Project checkpoint requires at least one owned component")
        primary_ref = f"{workflow_kind}:{workflow_id}"
        primary_checkpoint = next(
            (item for item in checkpoint_results if item.get("ref") == primary_ref),
            None,
        )
        if primary_checkpoint is None:
            raise ValueError(f"Project checkpoint did not include primary component {primary_ref}")
        commit = str(
            primary_checkpoint.get("commit") or primary_checkpoint.get("commit_sha") or ""
        ).strip() or None
        package_digest = str(primary_checkpoint.get("package_digest") or "").strip()
        source_revision = str(
            primary_checkpoint.get("source_revision") or commit or ""
        ).strip()
        if not package_digest or not source_revision:
            raise ValueError("Primary component checkpoint has no immutable package/source identity")
        project_manifest_digest = str(project_manifest.get("manifest_digest") or "").strip()
        result = {
            "ok": True,
            "kind": "project",
            "name": project_id,
            "version": str(project_manifest.get("version") or "DEV"),
            "previous_version": project_manifest.get("previous_version"),
            "version_bump": project_manifest.get("version_bump"),
            "skipped_occupied_versions": list(
                project_manifest.get("skipped_occupied_versions") or []
            ),
            "manifest_digest": project_manifest_digest,
            "package_digest": package_digest,
            "source_revision": source_revision,
            "verification_source_ref": primary_ref,
            "components_pushed": checkpoint_results,
        }
        evidence = _record_project_change(
            kind=kind,
            project_id=project_id,
            action="checkpoint",
            summary=checkpoint_message,
            webspace_id=_webspace_id(webspace_id, _meta),
            commit=commit,
            change_id=checkpoint_change_id,
            meta={
                "canonical_change_id": canonical_change_id or None,
                "context_packet_digest": context_packet_digest or None,
                "project_manifest_digest": project_manifest_digest,
                "verification_source_ref": primary_ref,
                "checkpoint_artifacts": [
                    {
                        "kind": item.get("kind"),
                        "name": item.get("name"),
                        "ref": item.get("ref"),
                        "commit": item.get("commit"),
                        "package_digest": item.get("package_digest"),
                    }
                    for item in checkpoint_results
                ],
            },
        )
        change_id = str(evidence.get("change_id") or "").strip()
        workflow_result = workflow.transition(
            workflow_kind,
            workflow_id,
            "checkpoint_recorded",
            actor="builder.checkpoint",
            metadata={
                "change_id": change_id,
                "run_id": change_id,
                "canonical_change_id": canonical_change_id or None,
                "context_packet_digest": context_packet_digest or None,
                "package_digest": package_digest,
                "source_revision": source_revision,
                "confirmed": True,
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
            "execution_scope": _execution_scope(kind, project_id),
        }
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
            "canonical_change_id": canonical_change_id or None,
            "context_packet_digest": context_packet_digest or None,
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
            "run_id": change_id,
            "canonical_change_id": canonical_change_id or None,
            "context_packet_digest": context_packet_digest or None,
            "package_digest": package_digest,
            "source_revision": source_revision,
            "confirmed": True,
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
        "execution_scope": _execution_scope(kind, project_id),
    }


def _checkpoint_candidate_id(project_id: str, delivery: Mapping[str, Any]) -> str | None:
    version = str(delivery.get("version") or "").strip()
    package_digest = str(delivery.get("package_digest") or "").strip()
    if not version or not package_digest.startswith("sha256:"):
        return None
    return f"{project_id}-{version.replace('.', '-')}-{package_digest[-12:]}"


def _recover_running_checkpoint_candidate(
    project_id: str,
    delivery: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Recover an exact Trial after its external result outlived a local rollback."""

    candidate_id = _checkpoint_candidate_id(project_id, delivery)
    if not candidate_id:
        return None
    try:
        result = projects.get_candidate(candidate_id)
    except Exception:
        return None
    candidate = result.get("candidate") if isinstance(result.get("candidate"), Mapping) else {}
    source_ref = candidate.get("source_ref") if isinstance(candidate.get("source_ref"), Mapping) else {}
    trials = [item for item in candidate.get("trials") or [] if isinstance(item, Mapping)]
    expected = {
        "candidate_id": candidate_id,
        "project_id": project_id,
        "version": str(delivery.get("version") or "").strip(),
        "package_digest": str(delivery.get("package_digest") or "").strip(),
        "source_revision": str(delivery.get("source_revision") or "").strip(),
    }
    actual = {
        "candidate_id": str(candidate.get("candidate_id") or "").strip(),
        "project_id": str(candidate.get("project_id") or "").strip(),
        "version": str(candidate.get("version") or "").strip(),
        "package_digest": str(candidate.get("package_digest") or "").strip(),
        "source_revision": str(source_ref.get("revision") or "").strip(),
    }
    if actual != expected:
        return None
    if str(candidate.get("status") or "").strip() != "trial":
        return None
    if not any(str(item.get("status") or "").strip() == "running" for item in trials):
        return None
    release_digest = str(candidate.get("release_digest") or "").strip()
    if not release_digest:
        return None
    return {
        "ok": True,
        "candidate": dict(candidate),
        "release": {
            "project_id": project_id,
            "version": actual["version"],
            "release_digest": release_digest,
        },
        "trial_workspace": result.get("trial_workspace"),
        "recovered": True,
        "recovery_reason": "external_trial_result_outlived_local_transaction",
    }


def _ensure_trial_placement(
    workflow_kind: str,
    workflow_id: str,
    *,
    result: Mapping[str, Any],
    candidate_id: str,
    release_data: Mapping[str, Any],
    package_digest: str,
    trial_workflow: Mapping[str, Any],
    webspace_id: str | None,
    meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    project = (
        trial_workflow.get("project")
        if isinstance(trial_workflow.get("project"), Mapping)
        else {}
    )
    for placement in project.get("placements") or []:
        if not isinstance(placement, Mapping):
            continue
        result_ref = (
            placement.get("result_ref")
            if isinstance(placement.get("result_ref"), Mapping)
            else {}
        )
        if (
            str(placement.get("kind") or "") == "trial"
            and str(placement.get("status") or "") == "active"
            and str(result_ref.get("id") or "") == candidate_id
        ):
            return dict(trial_workflow)
    trial_activation = (
        result.get("trial_activation")
        if isinstance(result.get("trial_activation"), Mapping)
        else {}
    )
    if not trial_activation:
        raise ValueError("Candidate trial has no signed activation placement")
    activation_target = (
        trial_activation.get("target")
        if isinstance(trial_activation.get("target"), Mapping)
        else {}
    )
    placed = workflow.record_project_placement(
        workflow_kind,
        workflow_id,
        {
            "kind": "trial",
            "result_ref": {
                "kind": "candidate",
                "id": candidate_id,
                "version": str(release_data.get("version") or "").strip(),
                "digest": package_digest,
            },
            "target": {
                "zone": activation_target.get("zone"),
                "subnet_id": activation_target.get("subnet_id"),
                "webspace_id": activation_target.get("webspace_id")
                or _preview_dev_webspace_id(_webspace_id(webspace_id, meta)),
                "space_kind": activation_target.get("space_kind") or "development",
            },
            "scenario_id": activation_target.get("scenario_id") or workflow_id,
            "data_mode": trial_activation.get("data_mode") or "empty",
            "runtime_binding": trial_activation.get("runtime_binding") or {},
            "trial_activation_ref": trial_activation.get("activation_id"),
            "safety": trial_activation.get("safety_evidence") or {},
        },
        expected_generation=int(trial_workflow.get("generation") or 0),
    )
    return (
        dict(placed["workflow"])
        if isinstance(placed.get("workflow"), Mapping)
        else dict(trial_workflow)
    )


def _ensure_stable_placement(
    workflow_kind: str,
    workflow_id: str,
    *,
    owner_kind: str,
    owner_id: str,
    result: Mapping[str, Any],
    published_workflow: Mapping[str, Any],
    webspace_id: str | None,
    meta: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    publication = (
        published_workflow.get("publication")
        if isinstance(published_workflow.get("publication"), Mapping)
        else {}
    )
    delivery = (
        published_workflow.get("delivery")
        if isinstance(published_workflow.get("delivery"), Mapping)
        else {}
    )
    release_id = str(result.get("release") or publication.get("release") or "").strip()
    release_version = release_id.rpartition("@")[2].strip() if "@" in release_id else ""
    if not release_version:
        release_version = str(_project_descriptor(owner_kind, owner_id).get("version") or "").strip()
    release_digest = str(
        delivery.get("release_digest") or result.get("release_digest") or ""
    ).strip()
    if not release_id or not release_version or not release_digest:
        raise ValueError("Published Project release identity is incomplete")

    target_webspace_id = _preview_source_webspace_id(webspace_id, meta)
    project_title = str(
        _project_descriptor(owner_kind, owner_id).get("title") or owner_id
    ).strip()
    materialization = preview.materialize_revision(
        webspace_id=target_webspace_id,
        scenario_id=workflow_id,
        revision=release_version,
        preview_stage="publication",
        preview_label=project_title,
        source_fingerprint=release_digest,
        event_payload={
            "source": "builder.project.publication",
            "source_webspace_id": target_webspace_id,
            "preview_stage": "publication",
            "preview_revision": release_version,
        },
    )
    if materialization.get("ok") is False:
        raise ValueError(
            str(materialization.get("error") or "Project publication materialization failed")
        )

    project = (
        published_workflow.get("project")
        if isinstance(published_workflow.get("project"), Mapping)
        else {}
    )
    for placement in project.get("placements") or []:
        if not isinstance(placement, Mapping):
            continue
        result_ref = (
            placement.get("result_ref")
            if isinstance(placement.get("result_ref"), Mapping)
            else {}
        )
        target = (
            placement.get("target")
            if isinstance(placement.get("target"), Mapping)
            else {}
        )
        if (
            str(placement.get("kind") or "") == "stable"
            and str(placement.get("status") or "") == "active"
            and str(result_ref.get("id") or "") == release_id
            and str(target.get("webspace_id") or "") == target_webspace_id
        ):
            return dict(published_workflow), dict(materialization)

    apply_evidence = (
        result.get("apply_evidence")
        if isinstance(result.get("apply_evidence"), Mapping)
        else {}
    )
    activation = (
        apply_evidence.get("activation")
        if isinstance(apply_evidence.get("activation"), Mapping)
        else delivery.get("activation")
        if isinstance(delivery.get("activation"), Mapping)
        else {}
    )
    placed = workflow.record_project_placement(
        workflow_kind,
        workflow_id,
        {
            "kind": "stable",
            "result_ref": {
                "kind": "release",
                "id": release_id,
                "version": release_version,
                "digest": release_digest,
            },
            "target": {
                "webspace_id": target_webspace_id,
                "space_kind": "workspace",
            },
            "scenario_id": workflow_id,
            "data_mode": "real",
            "runtime_binding": dict(activation),
            "safety": {"status": "verified", "source": "publication_activation"},
        },
        expected_generation=int(published_workflow.get("generation") or 0),
    )
    updated = (
        dict(placed["workflow"])
        if isinstance(placed.get("workflow"), Mapping)
        else dict(published_workflow)
    )
    return updated, dict(materialization)


def _ensure_trial_waiting_before_result(
    kind: str,
    project_id: str,
    *,
    admitted_workflow: Mapping[str, Any] | None,
    run_id: str,
    canonical_change_id: str | None,
    context_packet_digest: str | None,
    package_digest: str,
) -> None:
    """Re-admit only the local waiting state; never repeat Trial activation."""

    admitted_governed = (
        admitted_workflow.get("governed")
        if isinstance(admitted_workflow, Mapping)
        and isinstance(admitted_workflow.get("governed"), Mapping)
        else {}
    )
    if not str(admitted_governed.get("state") or "").strip():
        return
    current = workflow.get_state(kind, project_id)
    delivery = current.get("delivery") if isinstance(current.get("delivery"), Mapping) else {}
    governed = current.get("governed") if isinstance(current.get("governed"), Mapping) else {}
    governed_state = str(governed.get("state") or "").strip()
    # Test doubles and pre-governed compatibility records do not carry the
    # canonical projection. Their transition call remains authoritative.
    if not governed_state:
        return
    if str(delivery.get("status") or "") == "activating" and governed_state == "trial_waiting":
        return
    if str(delivery.get("status") or "") != "checkpoint" or governed_state != "trial_ready":
        raise ValueError("Trial result cannot be reconciled from the current Builder state")
    workflow.transition(
        kind,
        project_id,
        "candidate_preparation_started",
        actor="builder.candidate.reconciliation",
        metadata={
            "run_id": run_id,
            "canonical_change_id": canonical_change_id,
            "context_packet_digest": context_packet_digest,
            "confirmed": True,
            "package_digest": package_digest,
            "idempotency_key": f"{run_id}:waiting-reconcile",
            "reconciliation": "external_trial_result_observed",
        },
    )


def _ensure_publication_waiting_before_result(
    kind: str,
    project_id: str,
    *,
    admitted_workflow: Mapping[str, Any] | None,
    run_id: str,
    candidate_id: str,
    canonical_change_id: str | None,
    context_packet_digest: str | None,
) -> None:
    """Restore only the local publication wait after an observed promotion result."""

    admitted_governed = (
        admitted_workflow.get("governed")
        if isinstance(admitted_workflow, Mapping)
        and isinstance(admitted_workflow.get("governed"), Mapping)
        else {}
    )
    if not str(admitted_governed.get("state") or "").strip():
        return
    current = workflow.get_state(kind, project_id)
    delivery = current.get("delivery") if isinstance(current.get("delivery"), Mapping) else {}
    governed = current.get("governed") if isinstance(current.get("governed"), Mapping) else {}
    governed_state = str(governed.get("state") or "").strip()
    if str(delivery.get("status") or "") == "publication_waiting" and governed_state == "publication_waiting":
        return
    if str(delivery.get("status") or "") != "accepted" or governed_state != "publication_ready":
        raise ValueError("Publication result cannot be reconciled from the current Builder state")
    workflow.transition(
        kind,
        project_id,
        "publication_started",
        actor="builder.publication.reconciliation",
        metadata={
            "run_id": run_id,
            "candidate_id": candidate_id,
            "canonical_change_id": canonical_change_id,
            "context_packet_digest": context_packet_digest,
            "confirmed": True,
            "idempotency_key": f"{run_id}:waiting-reconcile",
            "reconciliation": "external_publication_result_observed",
        },
    )


@tool("publish_project", summary="Validate or publish a DEV project release.", side_effects="external_write")
def publish_project(
    object_type: str = DEFAULT_PROJECT_KIND,
    object_id: str = DEFAULT_PROJECT_ID,
    bump: str = "patch",
    dry_run: bool = True,
    force: bool = False,
    confirmed: bool = False,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind, project_id = _identity(object_type, object_id)
    workflow_kind, workflow_id = _execution_identity(kind, project_id)
    if bump not in {"major", "minor", "patch"}:
        raise ValueError("bump must be major, minor, or patch")
    workflow_before = workflow.get_state(workflow_kind, workflow_id)
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
    canonical_change_id, context_packet_digest = _workflow_execution_identity(workflow_before)
    if not confirmed:
        operation = "Trial activation" if dry_run else "Publication"
        raise ValueError(f"{operation} requires explicit user confirmation")
    if dry_run:
        if kind == "project" and str(delivery.get("status") or "").strip() == "trial":
            candidate_id = str(delivery.get("candidate_id") or "").strip()
            existing = projects.get_candidate(candidate_id)
            candidate = (
                existing.get("candidate")
                if isinstance(existing.get("candidate"), Mapping)
                else {}
            )
            release_digest = str(candidate.get("release_digest") or "").strip()
            package_digest = str(candidate.get("package_digest") or "").strip()
            if (
                not candidate_id
                or str(candidate.get("candidate_id") or "").strip() != candidate_id
                or release_digest != str(delivery.get("release_digest") or "").strip()
                or package_digest != str(delivery.get("package_digest") or "").strip()
            ):
                raise ValueError("Active Builder trial identity differs from its candidate")
            trial_workflow = _ensure_trial_placement(
                workflow_kind,
                workflow_id,
                result=existing,
                candidate_id=candidate_id,
                release_data={"version": candidate.get("version")},
                package_digest=package_digest,
                trial_workflow=workflow_before,
                webspace_id=webspace_id,
                meta=_meta,
            )
            return {
                **dict(existing),
                "dry_run": True,
                "trial_ready": True,
                "recovered": True,
                "recovery_reason": "active_trial_projection_reconciled",
                "workflow": trial_workflow,
                "execution_scope": _execution_scope(kind, project_id),
            }
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
            "canonical_change_id": canonical_change_id or None,
            "context_packet_digest": context_packet_digest or None,
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
        activation_run_id = f"candidate:{project_id}:activate"
        started = workflow.transition(
            workflow_kind,
            workflow_id,
            "candidate_preparation_started",
            actor="builder.candidate",
            metadata={
                "run_id": activation_run_id,
                "canonical_change_id": canonical_change_id or None,
                "context_packet_digest": context_packet_digest or None,
                "confirmed": True,
                "package_digest": str(delivery.get("package_digest") or ""),
                "idempotency_key": f"{activation_run_id}:{delivery.get('package_digest')}",
            },
        )
        try:
            recovered_result = None if stale_candidate_id else _recover_running_checkpoint_candidate(project_id, delivery)
            if recovered_result is not None:
                result = recovered_result
            elif kind == "project":
                result = compositions.prepare_candidate(
                    project_id,
                    source_kind=workflow_kind,
                    source_name=workflow_id,
                    source_revision=str(delivery.get("source_revision") or ""),
                    change_ids=candidate_change_ids,
                    validation_evidence={
                        **validation_evidence,
                        **(
                            {"replaces_candidate_id": stale_candidate_id}
                            if stale_candidate_id
                            else {}
                        ),
                    },
                    idempotency_key=(
                        f"project:{project_id}:{delivery.get('source_revision')}:{stale_candidate_id or 'initial'}"
                    ),
                )
            elif stale_candidate_id:
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
        except Exception as exc:
            workflow.transition(
                workflow_kind,
                workflow_id,
                "candidate_preparation_unknown",
                actor="builder.candidate",
                metadata={"error": str(exc), "canonical_change_id": canonical_change_id or None},
            )
            raise
        if not bool(result.get("ok", True)) or result.get("error"):
            failed = workflow.transition(
                workflow_kind,
                workflow_id,
                "candidate_preparation_failed",
                actor="builder.candidate",
                metadata={
                    "error": result.get("error") or result.get("status") or "candidate_preparation_failed",
                    "canonical_change_id": canonical_change_id or None,
                },
            )
            return {
                **result,
                "dry_run": True,
                "trial_ready": False,
                "workflow": failed.get("workflow"),
                "execution_scope": _execution_scope(kind, project_id),
            }
        candidate = result.get("candidate") if isinstance(result.get("candidate"), Mapping) else {}
        release_data = result.get("release") if isinstance(result.get("release"), Mapping) else {}
        release_digest = str(candidate.get("release_digest") or release_data.get("release_digest") or "").strip()
        package_digest = str(candidate.get("package_digest") or "").strip()
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        if not candidate_id or not release_digest or not package_digest:
            raise ValueError("Candidate preparation returned incomplete immutable identity")
        _ensure_trial_waiting_before_result(
            workflow_kind,
            workflow_id,
            admitted_workflow=(
                started.get("workflow")
                if isinstance(started.get("workflow"), Mapping)
                else None
            ),
            run_id=activation_run_id,
            canonical_change_id=canonical_change_id or None,
            context_packet_digest=context_packet_digest or None,
            package_digest=package_digest,
        )
        workflow_result = workflow.transition(
            workflow_kind,
            workflow_id,
            "candidate_prepared",
            actor="builder.candidate",
            metadata={
                "candidate_id": candidate_id,
                "run_id": f"candidate:{candidate_id}:prepare",
                "canonical_change_id": canonical_change_id or None,
                "context_packet_digest": context_packet_digest or None,
                "release": f"{release_data.get('project_id')}@{release_data.get('version')}",
                "release_digest": release_digest,
                "package_digest": package_digest,
                "base_release": candidate.get("base_release"),
                "base_release_digest": candidate.get("base_release_digest"),
                "trial_workspace": result.get("trial_workspace"),
                "idempotency_key": f"candidate:{candidate_id}:prepared",
                "recovered": bool(result.get("recovered")),
            },
        )
        trial_workflow = (
            workflow_result.get("workflow")
            if isinstance(workflow_result.get("workflow"), Mapping)
            else {}
        )
        if kind == "project":
            trial_workflow = _ensure_trial_placement(
                workflow_kind,
                workflow_id,
                result=result,
                candidate_id=candidate_id,
                release_data=release_data,
                package_digest=package_digest,
                trial_workflow=trial_workflow,
                webspace_id=webspace_id,
                meta=_meta,
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
            "execution_scope": _execution_scope(kind, project_id),
        }

    candidate_id = str(delivery.get("candidate_id") or "").strip()
    delivery_status = str(delivery.get("status") or "").strip()
    governed_before = (
        workflow_before.get("governed")
        if isinstance(workflow_before.get("governed"), Mapping)
        else {}
    )
    partial_publication_wait = (
        delivery_status == "publication_waiting"
        and str(governed_before.get("state") or "").strip() == "publication_ready"
    )
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
            workflow_kind,
            workflow_id,
            "candidate_accepted",
            actor="builder.user",
            metadata={
                "candidate_id": candidate_id,
                "candidate_digest": str(
                    delivery.get("package_digest") or delivery.get("release_digest") or ""
                ),
                "run_id": f"candidate:{candidate_id}:accept",
                "canonical_change_id": canonical_change_id or None,
                "context_packet_digest": context_packet_digest or None,
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
    elif not bool(capabilities.get("can_publish")) and not partial_publication_wait:
        raise ValueError("Publication requires an accepted candidate trial")

    publication_generation = int(
        governed_before.get("generation")
        or workflow_before.get("generation")
        or 0
    )
    # A reconciled Publication is a new, explicitly admitted attempt.  Binding
    # the local idempotency identity to the source generation preserves replay
    # safety inside one attempt without mistaking a post-recovery attempt for
    # the earlier unknown transition.
    publication_run_id = (
        f"candidate:{candidate_id}:publish:g{publication_generation}"
    )
    publication_started = workflow.transition(
        workflow_kind,
        workflow_id,
        "publication_started",
        actor="builder.publication",
        metadata={
            "run_id": publication_run_id,
            "canonical_change_id": canonical_change_id or None,
            "context_packet_digest": context_packet_digest or None,
            "confirmed": True,
            "candidate_id": candidate_id,
            "idempotency_key": f"{publication_run_id}:start",
        },
    )
    try:
        result = projects.promote_candidate(candidate_id)
    except Exception as exc:
        workflow.transition(
            workflow_kind,
            workflow_id,
            "publication_unknown",
            actor="builder.publication",
            metadata={"error": str(exc), "canonical_change_id": canonical_change_id or None},
        )
        raise
    _ensure_publication_waiting_before_result(
        workflow_kind,
        workflow_id,
        admitted_workflow=(
            publication_started.get("workflow")
            if isinstance(publication_started.get("workflow"), Mapping)
            else None
        ),
        run_id=publication_run_id,
        candidate_id=candidate_id,
        canonical_change_id=canonical_change_id or None,
        context_packet_digest=context_packet_digest or None,
    )
    promotion_status = str(result.get("status") or "").strip().lower()
    if promotion_status == "stale":
        workflow.transition(
            workflow_kind,
            workflow_id,
            "publication_failed",
            actor="builder.publication",
            metadata={"error": "candidate_stale", "canonical_change_id": canonical_change_id or None},
        )
        workflow_result = workflow.transition(
            workflow_kind,
            workflow_id,
            "candidate_stale",
            actor="builder.publication",
            metadata={
                "candidate_id": candidate_id,
                "run_id": f"candidate:{candidate_id}:stale",
                "canonical_change_id": canonical_change_id or None,
                "context_packet_digest": context_packet_digest or None,
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
            "execution_scope": _execution_scope(kind, project_id),
        }
    if not bool(result.get("ok", True)) or result.get("error"):
        failed = workflow.transition(
            workflow_kind,
            workflow_id,
            "publication_failed",
            actor="builder.publication",
            metadata={
                "error": result.get("error") or result.get("status") or "publication_failed",
                "canonical_change_id": canonical_change_id or None,
            },
        )
        return {
            **result,
            "workflow": failed.get("workflow"),
            "execution_scope": _execution_scope(kind, project_id),
        }
    successful_promotion_statuses = {
        "completed",
        "promoted",
        "published",
        "stable",
        "succeeded",
        "success",
    }
    if promotion_status and promotion_status not in successful_promotion_statuses:
        failed = workflow.transition(
            workflow_kind,
            workflow_id,
            "publication_failed",
            actor="builder.publication",
            metadata={
                "error": f"candidate_not_promotable:{promotion_status}",
                "canonical_change_id": canonical_change_id or None,
            },
        )
        return {
            **result,
            "ok": False,
            "error": f"Candidate is not promotable (status: {promotion_status})",
            "workflow": failed.get("workflow"),
            "execution_scope": _execution_scope(kind, project_id),
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
        workflow_kind,
        workflow_id,
        "publish",
        actor="builder.publication",
        metadata={
            "version": version,
            "release": release,
            "candidate_id": candidate_id,
            "candidate_digest": str(
                delivery.get("package_digest") or delivery.get("release_digest") or ""
            ),
            "task_id": automation_workflow.get("head_task_id"),
            "run_id": evidence.get("run_id") or evidence.get("change_id"),
            "canonical_change_id": canonical_change_id or None,
            "context_packet_digest": context_packet_digest or None,
            "apply_evidence": result.get("apply_evidence"),
        },
    )
    published_workflow = (
        workflow_result.get("workflow")
        if isinstance(workflow_result.get("workflow"), Mapping)
        else {}
    )
    stable_materialization: dict[str, Any] | None = None
    if kind == "project":
        published_workflow, stable_materialization = _ensure_stable_placement(
            workflow_kind,
            workflow_id,
            owner_kind=kind,
            owner_id=project_id,
            result=result,
            published_workflow=published_workflow,
            webspace_id=webspace_id,
            meta=_meta,
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
        "stable_materialization": stable_materialization,
        "workflow": published_workflow,
        "execution_scope": _execution_scope(kind, project_id),
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
    "accept_prototype",
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
    "get_development_feedback",
    "get_prompt_context",
    "get_project",
    "get_state",
    "list_changes",
    "list_development_feedback",
    "list_project_file_tree",
    "list_project_files",
    "list_project_objects",
    "list_projects",
    "list_templates",
    "link_dependency_checkpoint",
    "merge_change_issues",
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
    "split_change_issue",
    "submit_automation",
    "return_to_prototype",
    "transition_workflow",
    "update_project_metadata",
]
