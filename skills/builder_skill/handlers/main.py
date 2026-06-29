from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool


SKILL_ID = "builder_skill"
DIALOG_CHANNEL_ID = "builder"
AGENT_ID = "agent:builder_skill:builder"
AGENT_LABEL = "\u0421\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c"
SESSIONS_KEY = "builder_skill.sessions"
CURRENT_KEY = "builder_skill.current_session"
MAX_SESSIONS = 50
WORKBENCH_REFRESH_TOPIC = "builder.workbench.ensure_requested"

_FALLBACK_MEMORY: dict[str, Any] = {}


def _now() -> float:
    return time.time()


def _webspace_id(value: str | None = None, _meta: Mapping[str, Any] | None = None) -> str:
    token = str(value or "").strip()
    if token:
        return token
    if isinstance(_meta, Mapping):
        for key in ("webspace_id", "workspace_id"):
            raw = _meta.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return "default"


def _source_webspace_id(value: str | None = None, _meta: Mapping[str, Any] | None = None) -> str:
    if isinstance(_meta, Mapping):
        for key in ("source_webspace_id", "builder_source_webspace_id"):
            raw = _meta.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    token = _webspace_id(value, _meta)
    if token.endswith("-dev") and len(token) > 4:
        return token[:-4]
    return token


def _paired_dev_webspace_id(source_webspace_id: str) -> str | None:
    try:
        from adaos.services.builder.workbench import dev_webspace_id_for_source

        return dev_webspace_id_for_source(source_webspace_id)
    except Exception:
        source = str(source_webspace_id or "").strip()
        return f"{source}-dev" if source else None


def _scoped_key(base: str, webspace_id: str) -> str:
    return f"{base}.{webspace_id or 'default'}"


def _mem_get(key: str, default: Any = None) -> Any:
    try:
        from adaos.sdk.data import skill_memory

        return skill_memory.get(key, default)
    except Exception:
        return copy.deepcopy(_FALLBACK_MEMORY.get(key, default))


def _mem_set(key: str, value: Any) -> None:
    try:
        from adaos.sdk.data import skill_memory

        skill_memory.set(key, value)
    except Exception:
        _FALLBACK_MEMORY[key] = copy.deepcopy(value)


def _sessions(webspace_id: str) -> dict[str, dict[str, Any]]:
    raw = _mem_get(_scoped_key(SESSIONS_KEY, webspace_id), {})
    return copy.deepcopy(raw) if isinstance(raw, dict) else {}


def _save_sessions(webspace_id: str, sessions: Mapping[str, Mapping[str, Any]]) -> None:
    items = sorted((dict(v) for v in sessions.values()), key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    trimmed = {str(item["id"]): item for item in items[:MAX_SESSIONS] if item.get("id")}
    _mem_set(_scoped_key(SESSIONS_KEY, webspace_id), trimmed)


def _current_session_id(webspace_id: str) -> str | None:
    raw = _mem_get(_scoped_key(CURRENT_KEY, webspace_id))
    token = str(raw or "").strip()
    return token or None


def _set_current_session_id(webspace_id: str, session_id: str) -> None:
    _mem_set(_scoped_key(CURRENT_KEY, webspace_id), str(session_id or "").strip())


def _hash_suffix(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:8]


def _scenario_id_from_idea(idea: str) -> str:
    lowered = str(idea or "").lower()
    if "shopping" in lowered or "shop" in lowered or "\u043f\u043e\u043a\u0443\u043f" in lowered:
        base = "shopping_list"
    elif "todo" in lowered or "\u0437\u0430\u0434\u0430\u0447" in lowered:
        base = "todo_list"
    else:
        ascii_base = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
        base = ascii_base[:40].strip("_") or "prototype_app"
    return f"{base}_{_hash_suffix(idea)}"


def _conversation_id(webspace_id: str) -> str:
    return f"conv.skill.{SKILL_ID}.default.{webspace_id or 'default'}"


def _dialog_state(webspace_id: str) -> dict[str, Any]:
    return {
        "state": "active",
        "dialog_channel_id": DIALOG_CHANNEL_ID,
        "conversation_id": _conversation_id(webspace_id),
        "owner": f"skill:{SKILL_ID}",
        "surface": f"skill:{SKILL_ID}",
        "default_tool": f"{SKILL_ID}.chat",
        "active_agent_id": AGENT_ID,
        "active_agent_label": AGENT_LABEL,
        "active_agent": {
            "id": AGENT_ID,
            "label": AGENT_LABEL,
            "owner": f"skill:{SKILL_ID}",
            "kind": "skill_agent",
            "skill_id": SKILL_ID,
            "channel_id": DIALOG_CHANNEL_ID,
            "memory_scope": "skill_user",
            "gender": "male",
            "voice": "ru-male",
            "icon": "construct-outline",
            "voice_profile": {
                "gender": "male",
                "voice": "ru-male",
                "lang": "ru-RU",
                "browser_voice_hint": "ru-male",
            },
        },
        "memory": {
            "status": "skill_memory_compat",
            "scopes": ["skill_user", "conversation"],
            "owner": f"skill:{SKILL_ID}",
            "active_agent_id": AGENT_ID,
        },
    }


def _chat_meta(_meta: Mapping[str, Any] | None, *, webspace_id: str) -> dict[str, Any]:
    meta = dict(_meta or {})
    meta.pop("webspace_ids", None)
    meta["webspace_id"] = webspace_id
    meta.setdefault("source_webspace_id", _source_webspace_id(webspace_id, _meta))
    meta.setdefault("route_id", "voice_chat")
    meta.setdefault("dialog_channel_id", DIALOG_CHANNEL_ID)
    meta["conversation_id"] = _conversation_id(webspace_id)
    meta["conversation_owner"] = f"skill:{SKILL_ID}"
    meta.setdefault("active_agent_id", AGENT_ID)
    meta.setdefault("active_agent_label", AGENT_LABEL)
    meta.setdefault("active_agent_gender", "male")
    meta.setdefault("active_agent_voice", "ru-male")
    meta.setdefault("active_agent_icon", "construct-outline")
    return meta


def _source_refs(
    *,
    webspace_id: str,
    session: Mapping[str, Any],
    _meta: Mapping[str, Any] | None = None,
    patch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    meta = _chat_meta(_meta, webspace_id=webspace_id)
    refs: dict[str, Any] = {
        "conversation_id": meta.get("conversation_id") or _conversation_id(webspace_id),
        "dialog_channel_id": DIALOG_CHANNEL_ID,
        "owner": f"skill:{SKILL_ID}",
        "session_id": session.get("id"),
        "scenario_id": session.get("scenario_id"),
    }
    for key in ("thread_id", "turn_trace_id", "request_id", "message_id", "input_event_kind"):
        value = str(meta.get(key) or "").strip()
        if value:
            refs[key] = value
    draft_id = str(session.get("draft_id") or "").strip()
    if draft_id:
        refs["draft_id"] = draft_id
    if patch:
        patch_id = str(patch.get("id") or "").strip()
        if patch_id:
            refs["patch_id"] = patch_id
        operation = str(patch.get("operation") or "").strip()
        if operation:
            refs["operation"] = operation
    return refs


def _publish_review_pending_action(
    *,
    webspace_id: str,
    session: Mapping[str, Any],
    request_text: str,
    kind: str,
    summary: str,
    _meta: Mapping[str, Any] | None = None,
    patch: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    refs = _source_refs(webspace_id=webspace_id, session=session, _meta=_meta, patch=patch)
    action_input: dict[str, Any] = {
        "kind": kind,
        "request_text": request_text,
        "side_effect_class": "local_write",
    }
    if patch:
        action_input.update({key: value for key, value in dict(patch).items() if key in {"target", "operation", "summary", "side_effect_class"}})
    try:
        from adaos.services.conversation_safety import classify_action_risk

        action_risk = classify_action_risk(action_input)
    except Exception:
        action_risk = {
            "schema": "adaos.conversation.action_risk.v1",
            "risk_class": "local_write",
            "approval_required": False,
            "mandatory_review": False,
            "reasons": [{"risk_class": "local_write", "reason": "fallback"}],
        }
    try:
        from adaos.services.pending_actions import publish_pending_action

        return publish_pending_action(
            webspace_id=webspace_id,
            kind=kind,
            title="Review Builder change",
            summary=summary,
            request_text=request_text,
            producer={"type": "skill", "skill_id": SKILL_ID},
            owner_scope={
                "owner": f"skill:{SKILL_ID}",
                "webspace_id": webspace_id,
                "conversation_id": refs.get("conversation_id"),
                "thread_id": refs.get("thread_id"),
            },
            domain_ref={
                "skill_id": SKILL_ID,
                "session_id": refs.get("session_id"),
                "scenario_id": refs.get("scenario_id"),
                "draft_id": refs.get("draft_id"),
                "patch_id": refs.get("patch_id"),
                "conversation_id": refs.get("conversation_id"),
                "thread_id": refs.get("thread_id"),
            },
            actions=["preview", "approve", "refuse", "postpone"],
            response_topic="builder.pending_action.response",
            payload_ref={
                "kind": "builder.session",
                "session_id": refs.get("session_id"),
                "scenario_id": refs.get("scenario_id"),
            },
            metadata={
                "source": "builder_skill",
                "source_refs": refs,
                "patch": dict(patch or {}),
                "approval_policy": {
                    "decision": "human_review_required",
                    "reason": "builder_review_pending_action",
                    "action_risk": action_risk,
                },
            },
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "pending_action_publish_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "metadata": {"source_refs": refs},
        }


def _safe_emit_chat(text: str, *, webspace_id: str, _meta: Mapping[str, Any] | None = None) -> None:
    try:
        from adaos.sdk.io.out import chat_append

        source_ws = _source_webspace_id(webspace_id, _meta)
        targets = [source_ws]
        dev_ws = _paired_dev_webspace_id(source_ws)
        if dev_ws and dev_ws not in targets:
            targets.append(dev_ws)
        for target in targets:
            chat_append(text, from_="hub", _meta=_chat_meta(_meta, webspace_id=target))
    except Exception:
        return


def _build_fields(idea: str) -> list[dict[str, Any]]:
    lowered = str(idea or "").lower()
    if "shopping" in lowered or "\u043f\u043e\u043a\u0443\u043f" in lowered:
        return [
            {"id": "item", "type": "string", "label": "\u0422\u043e\u0432\u0430\u0440", "required": True},
            {"id": "quantity", "type": "number", "label": "\u041a\u043e\u043b-\u0432\u043e", "required": False},
            {"id": "category", "type": "string", "label": "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f", "required": False},
            {"id": "done", "type": "boolean", "label": "\u041a\u0443\u043f\u043b\u0435\u043d\u043e", "required": False},
        ]
    return [
        {"id": "title", "type": "string", "label": "\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435", "required": True},
        {"id": "notes", "type": "string", "label": "\u0417\u0430\u043c\u0435\u0442\u043a\u0438", "required": False},
        {"id": "status", "type": "string", "label": "\u0421\u0442\u0430\u0442\u0443\u0441", "required": False},
    ]


def _component_for_field(field: Mapping[str, Any]) -> dict[str, Any]:
    field_type = str(field.get("type") or "string")
    component_type = "checkbox" if field_type == "boolean" else "number_input" if field_type == "number" else "text_input"
    return {
        "id": f"input_{field['id']}",
        "type": component_type,
        "label": field.get("label") or field["id"],
        "binding": f"draft.{field['id']}",
        "visible": True,
    }


def _preview_state(*, session: Mapping[str, Any]) -> dict[str, Any]:
    fields = [dict(item) for item in session.get("fields", []) if isinstance(item, Mapping)]
    datasource_id = str(session.get("datasource_id") or "items")
    table_columns = [{"field": item["id"], "label": item.get("label") or item["id"]} for item in fields]
    stored_mock_rows = session.get("mock_rows")
    mock_rows = [dict(item) for item in stored_mock_rows if isinstance(item, Mapping)] if isinstance(stored_mock_rows, list) else _mock_rows(fields)
    ui = {
        "schema": "adaos.declarative_ui.v1",
        "id": str(session.get("scenario_id") or "prototype"),
        "type": "page",
        "title": session.get("title") or "\u041f\u0440\u043e\u0442\u043e\u0442\u0438\u043f",
        "children": [
            {
                "id": "editor",
                "type": "section",
                "label": "\u0412\u0432\u043e\u0434",
                "children": [_component_for_field(item) for item in fields],
                "actions": [{"id": "add_item", "type": "button", "label": "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c"}],
            },
            {
                "id": "items_table",
                "type": "table",
                "label": "\u0421\u043f\u0438\u0441\u043e\u043a",
                "binding": datasource_id,
                "columns": table_columns,
                "visible": True,
            },
        ],
    }
    if session.get("card_view"):
        ui["children"].append(
            {
                "id": "items_cards",
                "type": "card_list",
                "label": "\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0438",
                "binding": datasource_id,
                "title": f"{{{{{fields[0]['id']}}}}}" if fields else "{{title}}",
                "subtitle": f"{{{{{fields[1]['id']}}}}}" if len(fields) > 1 else "",
                "visible": True,
            }
        )
    return {
        "session_id": session.get("id"),
        "title": session.get("title"),
        "current_ui": ui,
        "datasources": [
            {
                "id": datasource_id,
                "type": "internal_crud",
                "entity": "item",
                "fields": fields,
                "operations": ["create", "read", "update", "delete"],
            }
        ],
        "mock_data": {datasource_id: mock_rows},
        "pending_patches": [item for item in session.get("patches", []) if item.get("status") == "proposed"],
        "version": str(session.get("version") or "v1"),
    }


def _mock_rows(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for index in range(1, 4):
        row: dict[str, Any] = {}
        for field in fields:
            field_id = str(field.get("id") or "")
            field_type = str(field.get("type") or "string")
            if field_type == "number":
                row[field_id] = index
            elif field_type == "boolean":
                row[field_id] = index == 1
            else:
                row[field_id] = f"{field.get('label') or field_id} {index}"
        rows.append(row)
    return rows


def _food_mock_rows(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    products = [
        {"item": "\u041c\u043e\u043b\u043e\u043a\u043e", "quantity": 2, "category": "\u041c\u043e\u043b\u043e\u0447\u043d\u044b\u0435", "done": False, "price": 89.9},
        {"item": "\u0425\u043b\u0435\u0431", "quantity": 1, "category": "\u0411\u0430\u043a\u0430\u043b\u0435\u044f", "done": True, "price": 54.0},
        {"item": "\u042f\u0431\u043b\u043e\u043a\u0438", "quantity": 6, "category": "\u0424\u0440\u0443\u043a\u0442\u044b", "done": False, "price": 129.5},
    ]
    rows: list[dict[str, Any]] = []
    for index, product in enumerate(products, start=1):
        row: dict[str, Any] = {}
        for field in fields:
            field_id = str(field.get("id") or "")
            field_type = str(field.get("type") or "string")
            if field_id in product:
                row[field_id] = product[field_id]
            elif field_id in {"title", "name", "product"}:
                row[field_id] = product["item"]
            elif field_type == "number":
                row[field_id] = index
            elif field_type == "boolean":
                row[field_id] = index == 2
            else:
                row[field_id] = str(field.get("label") or field_id or "value")
        rows.append(row)
    return rows


def _write_webui(artifact_root: str | None, preview_state: Mapping[str, Any]) -> None:
    if not artifact_root:
        return
    root = Path(artifact_root)
    if not root.exists():
        return
    payload = {
        "schema": "adaos.webui.prototype.v1",
        "generated_by": SKILL_ID,
        "preview_state": preview_state,
        "nlu": {
            "llm_hints": {
                "aliases": {"app_id": {"prototype": [str(preview_state.get("title") or "prototype")]}},
                "primary_actions": [
                    {
                        "intent": "builder.chat",
                        "notes": "Prototype UI is edited through builder_skill.chat.",
                    }
                ],
            }
        },
    }
    (root / "webui.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_scenario_page_schema(root, preview_state)


def _form_field_type(field: Mapping[str, Any]) -> str:
    field_type = str(field.get("type") or "string")
    if field_type == "boolean":
        return "toggle"
    if field_type == "number":
        return "number"
    return "text"


def _page_schema_from_preview(preview_state: Mapping[str, Any]) -> dict[str, Any]:
    ui = preview_state.get("current_ui") if isinstance(preview_state.get("current_ui"), Mapping) else {}
    title = str(preview_state.get("title") or ui.get("title") or "Prototype").strip() or "Prototype"
    datasources = preview_state.get("datasources") if isinstance(preview_state.get("datasources"), list) else []
    datasource = datasources[0] if datasources and isinstance(datasources[0], Mapping) else {}
    fields = [dict(item) for item in datasource.get("fields", []) if isinstance(item, Mapping)]
    datasource_id = str(datasource.get("id") or "items").strip() or "items"
    mock_data = preview_state.get("mock_data") if isinstance(preview_state.get("mock_data"), Mapping) else {}
    rows = mock_data.get(datasource_id) if isinstance(mock_data.get(datasource_id), list) else []
    has_card_view = any(
        isinstance(child, Mapping) and str(child.get("type") or "") == "card_list"
        for child in (ui.get("children") if isinstance(ui.get("children"), list) else [])
    )
    widgets: list[dict[str, Any]] = [
        {
            "id": "prototype-form",
            "type": "ui.form",
            "area": "main",
            "title": "Input",
            "inputs": {
                "fields": [
                    {
                        "id": str(field.get("id") or f"field_{index}"),
                        "type": _form_field_type(field),
                        "label": field.get("label") or field.get("id") or f"Field {index + 1}",
                    }
                    for index, field in enumerate(fields)
                ],
                "submitLabel": "Add",
            },
            "actions": [{"on": "submit", "type": "updateState", "params": {"lastPrototypeSubmit": "$event.values"}}],
        },
        {
            "id": "prototype-table",
            "type": "ui.table",
            "area": "main",
            "title": "List",
            "dataSource": {"kind": "static", "value": rows},
            "inputs": {
                "columns": [
                    {
                        "key": str(field.get("id") or f"field_{index}"),
                        "label": field.get("label") or field.get("id") or f"Field {index + 1}",
                    }
                    for index, field in enumerate(fields)
                ],
                "emptyText": "No items yet",
            },
        },
    ]
    if has_card_view:
        first = str(fields[0].get("id") if fields else "title")
        second = str(fields[1].get("id") if len(fields) > 1 else "")
        widgets.append(
            {
                "id": "prototype-cards",
                "type": "ui.list",
                "area": "right",
                "title": "Cards",
                "dataSource": {"kind": "static", "value": rows},
                "inputs": {
                    "variant": "cards",
                    "titleKey": first,
                    "subtitleKey": second,
                    "emptyText": "No cards yet",
                },
            }
        )
    else:
        widgets.append(
            {
                "id": "prototype-summary",
                "type": "item.details",
                "area": "right",
                "title": "Prototype",
                "dataSource": {"kind": "static", "value": {"title": title, "fields": [field.get("label") for field in fields]}},
            }
        )
    return {
        "id": str(ui.get("id") or preview_state.get("session_id") or "builder_prototype"),
        "title": title,
        "layout": {
            "type": "split",
            "pattern": "split",
            "areas": [
                {"id": "main", "role": "main"},
                {"id": "right", "role": "aux"},
            ],
        },
        "widgets": widgets,
    }


def _write_scenario_page_schema(root: Path, preview_state: Mapping[str, Any]) -> None:
    manifest = root / "scenario.json"
    if not manifest.exists():
        return
    try:
        scenario = json.loads(manifest.read_text(encoding="utf-8-sig") or "{}")
    except Exception:
        return
    if not isinstance(scenario, dict):
        return
    scenario.setdefault("type", "desktop")
    scenario.setdefault("title", preview_state.get("title") or scenario.get("name") or scenario.get("id") or "Prototype")
    scenario.setdefault("ui", {})
    scenario["ui"].setdefault("application", {})
    scenario["ui"]["application"].setdefault("version", "0.1")
    scenario["ui"]["application"].setdefault("desktop", {})
    scenario["ui"]["application"]["desktop"]["pageSchema"] = _page_schema_from_preview(preview_state)
    manifest.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _save_session(webspace_id: str, session: dict[str, Any]) -> dict[str, Any]:
    session["updated_at"] = _now()
    sessions = _sessions(webspace_id)
    sessions[str(session["id"])] = copy.deepcopy(session)
    _save_sessions(webspace_id, sessions)
    _set_current_session_id(webspace_id, str(session["id"]))
    return session


def _load_session(webspace_id: str, session_id: str | None = None) -> dict[str, Any] | None:
    sessions = _sessions(webspace_id)
    sid = str(session_id or "").strip() or _current_session_id(webspace_id)
    if sid and sid in sessions:
        return copy.deepcopy(sessions[sid])
    if sessions:
        return copy.deepcopy(max(sessions.values(), key=lambda item: float(item.get("updated_at") or 0)))
    return None


def _message_created(session: Mapping[str, Any]) -> str:
    return (
        f"{AGENT_LABEL}: \u0441\u043e\u0437\u0434\u0430\u043b dev-\u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 "
        f"{session.get('scenario_id')} \u0438 \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a webui. "
        "\u041c\u043e\u0436\u043d\u043e \u0441\u0440\u0430\u0437\u0443 \u043f\u0440\u0430\u0432\u0438\u0442\u044c: "
        "\u0434\u043e\u0431\u0430\u0432\u044c \u043f\u043e\u043b\u0435, \u0443\u0431\u0435\u0440\u0438 \u043f\u043e\u043b\u0435, \u043f\u043e\u043a\u0430\u0436\u0438 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0430\u043c\u0438."
    )


def _extract_field_label(instruction: str) -> str | None:
    quoted = re.search(r"[\"'«](.*?)[\"'»]", instruction)
    if quoted:
        return quoted.group(1).strip()
    match = re.search(r"(?:field|поле)\s+([A-Za-zА-Яа-я0-9 _-]{2,40})", instruction, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _field_id(label: str) -> str:
    lowered = str(label or "").strip().lower()
    known = {
        "\u0446\u0435\u043d\u0430": "price",
        "\u0442\u0435\u043b\u0435\u0444\u043e\u043d": "phone",
        "\u043e\u0440\u0433\u0430\u043d\u0438\u0437\u0430\u0446\u0438\u044f": "organization",
    }
    if lowered in known:
        return known[lowered]
    ascii_id = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return ascii_id or f"field_{_hash_suffix(label)}"


def _workbench_service():
    from adaos.services.builder.workbench import BuilderWorkbenchService

    return BuilderWorkbenchService.from_context()


def _request_workbench_refresh(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from adaos.sdk.data import events

        events.publish(WORKBENCH_REFRESH_TOPIC, payload, source=SKILL_ID)
        return {"ok": True, "topic": WORKBENCH_REFRESH_TOPIC}
    except Exception as exc:
        return {"ok": False, "topic": WORKBENCH_REFRESH_TOPIC, "error": f"{type(exc).__name__}: {exc}"}


def _active_draft_id(session: Mapping[str, Any] | None) -> str | None:
    if not isinstance(session, Mapping):
        return None
    if not str(session.get("artifact_root") or "").strip():
        return None
    return str(session.get("draft_id") or session.get("id") or "").strip() or None


def _runtime_scenario_id(session: Mapping[str, Any] | None) -> str | None:
    if not isinstance(session, Mapping):
        return None
    if not str(session.get("artifact_root") or "").strip():
        return None
    return str(session.get("scenario_id") or "").strip() or None


def _workbench_binding(webspace_id: str) -> dict[str, Any]:
    try:
        binding = _workbench_service().get_workspace_binding(webspace_id)
        return dict(binding) if isinstance(binding, Mapping) else {}
    except Exception:
        return {}


def _session_matches_binding(session: Mapping[str, Any], binding: Mapping[str, Any]) -> bool:
    draft_id = str(binding.get("active_draft_id") or "").strip()
    scenario_id = str(binding.get("runtime_scenario_id") or "").strip()
    if draft_id and str(session.get("draft_id") or session.get("id") or "").strip() == draft_id:
        return True
    if scenario_id and str(session.get("scenario_id") or "").strip() == scenario_id:
        return True
    return not draft_id and not scenario_id


def _target_session(webspace_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    binding = _workbench_binding(webspace_id)
    draft_id = str(binding.get("active_draft_id") or "").strip()
    scenario_id = str(binding.get("runtime_scenario_id") or "").strip()
    sessions = _sessions(webspace_id)
    if draft_id or scenario_id:
        for session in sessions.values():
            if draft_id and str(session.get("draft_id") or session.get("id") or "").strip() == draft_id:
                return copy.deepcopy(session), binding
            if scenario_id and str(session.get("scenario_id") or "").strip() == scenario_id:
                return copy.deepcopy(session), binding
        return None, binding
    session = _load_session(webspace_id)
    if session and _session_matches_binding(session, binding):
        return session, binding
    return None, binding


def _target_required_message(binding: Mapping[str, Any] | None = None) -> str:
    scenario_id = str((binding or {}).get("runtime_scenario_id") or "").strip()
    if scenario_id:
        return (
            f"{AGENT_LABEL}: \u0432 Prompt IDE \u0432\u044b\u0431\u0440\u0430\u043d \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 {scenario_id}, "
            "\u043d\u043e \u044f \u043d\u0435 \u0432\u0438\u0436\u0443 \u0434\u043b\u044f \u043d\u0435\u0433\u043e Builder-\u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a. "
            "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 Builder-\u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a \u0438\u043b\u0438 \u0441\u043e\u0437\u0434\u0430\u0439\u0442\u0435 \u043d\u043e\u0432\u044b\u0439: "
            "\u00ab\u0421\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c, \u0441\u043e\u0437\u0434\u0430\u0439 ...\u00bb."
        )
    return (
        f"{AGENT_LABEL}: \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043e\u0431\u044a\u0435\u043a\u0442 \u0434\u043b\u044f \u0434\u043e\u0440\u0430\u0431\u043e\u0442\u043a\u0438 "
        "\u0432 Prompt IDE (\u043d\u0430\u0432\u044b\u043a \u0438\u043b\u0438 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439). "
        "\u0415\u0441\u043b\u0438 \u043d\u0443\u0436\u0435\u043d \u043d\u043e\u0432\u044b\u0439 \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f, \u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435: "
        "\u00ab\u0421\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c, \u0441\u043e\u0437\u0434\u0430\u0439 ...\u00bb."
    )


def _is_create_request(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        token in lowered
        for token in (
            "create",
            "new app",
            "new scenario",
            "app",
            "scenario",
            "skill",
            "\u0441\u043e\u0437\u0434",
            "\u043d\u043e\u0432\u044b\u0439",
            "\u043f\u0440\u0438\u043b\u043e\u0436",
            "\u0441\u0446\u0435\u043d\u0430\u0440",
            "\u043d\u0430\u0432\u044b\u043a",
        )
    )


def _wants_sample_data(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token in lowered for token in ("sample", "mock", "example", "\u043f\u0440\u0438\u043c\u0435\u0440", "\u0434\u0430\u043d\u043d", "\u043f\u0440\u043e\u0434\u0443\u043a\u0442", "\u043f\u0438\u0442\u0430\u043d", "\u0435\u0434\u0430"))


def _ensure_workbench(
    webspace_id: str,
    *,
    session: Mapping[str, Any] | None = None,
    preview_state: Mapping[str, Any] | None = None,
    active_draft_id: str | None = None,
    runtime_scenario_id: str | None = None,
) -> dict[str, Any]:
    svc = _workbench_service()
    draft_id = str(active_draft_id or _active_draft_id(session) or "").strip() or None
    scenario_id = str(runtime_scenario_id or _runtime_scenario_id(session) or "").strip() or None
    try:
        binding = svc.set_active_draft(
            source_webspace_id=webspace_id,
            active_draft_id=draft_id,
            runtime_scenario_id=scenario_id,
            persist_projection=False,
        )
        snapshot = svc.snapshot(webspace_id, preview_state=preview_state)
        event = _request_workbench_refresh(
            {
                "source_webspace_id": webspace_id,
                "active_draft_id": draft_id,
                "runtime_scenario_id": scenario_id,
                "preview_state": dict(preview_state or {}),
            }
        )
    except Exception as exc:
        return {"ok": False, "error": "workbench_unavailable", "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "binding": binding, "projection": {"ok": True, "snapshot": snapshot, "deferred": True, "event": event}}


def _delete_sessions_for_draft(webspace_id: str, draft_id: str) -> None:
    token = str(draft_id or "").strip()
    if not token:
        return
    sessions = _sessions(webspace_id)
    removed = [sid for sid, session in sessions.items() if str(session.get("draft_id") or session.get("id") or "") == token]
    if not removed:
        return
    for sid in removed:
        sessions.pop(sid, None)
    _save_sessions(webspace_id, sessions)
    current = _current_session_id(webspace_id)
    if current in removed:
        latest = max(sessions.values(), key=lambda item: float(item.get("updated_at") or 0), default=None)
        _set_current_session_id(webspace_id, str(latest.get("id") if latest else ""))


@tool(summary="Start Builder rapid prototyping dialog.", side_effects="local_write")
def start(
    text: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return chat(text=text or "", webspace_id=webspace_id, _meta=_meta)


@tool(summary="Handle Builder dialog turn.", side_effects="local_write")
def chat(
    text: str | None = None,
    webspace_id: str | None = None,
    auto_apply: bool = True,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    utterance = str(text or "").strip()
    session, binding = _target_session(ws)
    if _is_create_request(utterance):
        result = create_scenario_draft(idea=utterance or "prototype app", webspace_id=ws, _meta=_meta)
        if result.get("ok"):
            message = str(result.get("message") or "")
            _safe_emit_chat(message, webspace_id=ws, _meta=_meta)
            return {**result, "dialog": _dialog_state(ws)}
        return {**result, "dialog": _dialog_state(ws)}
    if not session:
        message = _target_required_message(binding)
        _safe_emit_chat(message, webspace_id=ws, _meta=_meta)
        return {
            "ok": True,
            "status": "target_required",
            "needs_selection": True,
            "message": message,
            "binding": binding,
            "dialog": _dialog_state(ws),
        }
    result = update_current_scenario(instruction=utterance, webspace_id=ws, auto_apply=auto_apply, _meta=_meta)
    if result.get("ok"):
        _safe_emit_chat(str(result.get("message") or ""), webspace_id=ws, _meta=_meta)
    return {**result, "dialog": _dialog_state(ws)}


@tool(summary="Create scenario prototype draft.", side_effects="local_write")
def create_scenario_draft(
    idea: str,
    scenario_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    source_idea = str(idea or "").strip() or "prototype app"
    sid = re.sub(r"[^a-z0-9_.-]+", "_", str(scenario_id or "").strip().lower()).strip("._-") or _scenario_id_from_idea(source_idea)
    fields = _build_fields(source_idea)
    session_id = f"builder_session_{_hash_suffix(ws + sid + source_idea)}"
    session = {
        "id": session_id,
        "webspace_id": ws,
        "status": "drafting",
        "title": "\u0421\u043f\u0438\u0441\u043e\u043a \u043f\u043e\u043a\u0443\u043f\u043e\u043a" if "shopping" in sid else sid.replace("_", " ").title(),
        "source_idea": source_idea,
        "scenario_id": sid,
        "datasource_id": "shopping_items" if "shopping" in sid else "prototype_items",
        "fields": fields,
        "patches": [],
        "version": "v1",
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        from adaos.services.builder.workspace import BuilderWorkspaceService

        draft = BuilderWorkspaceService.from_context().create_draft(
            kind="scenario",
            artifact_id=sid,
            source_idea=source_idea,
            template_id="builder_scenario",
            webspace_id=ws,
            source={
                "type": "builder_dialog",
                "utterance": source_idea,
                "side_effect_class": "local_write",
            },
        )
        draft_payload = draft.get("draft") if isinstance(draft.get("draft"), dict) else {}
        session["draft_id"] = draft_payload.get("draft_id")
        session["artifact_root"] = draft.get("artifact_root")
    except Exception as exc:
        session["status"] = "degraded"
        session["draft_error"] = f"{type(exc).__name__}: {exc}"
    preview = _preview_state(session=session)
    _write_webui(str(session.get("artifact_root") or ""), preview)
    session["preview_state"] = preview
    _save_session(ws, session)
    workbench = _ensure_workbench(ws, session=session, preview_state=preview)
    message = _message_created(session)
    if session.get("draft_error"):
        message += f" \u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u0435: dev draft \u043d\u0435 \u0441\u043e\u0437\u0434\u0430\u043d ({session['draft_error']})."
    pending_action = _publish_review_pending_action(
        webspace_id=ws,
        session=session,
        request_text=source_idea,
        kind="builder.scenario_draft.review",
        summary=f"Review Builder draft {sid}",
        _meta=_meta,
    )
    if pending_action and pending_action.get("id"):
        session["pending_action_id"] = pending_action.get("id")
        _save_session(ws, session)
    return {
        "ok": True,
        "session_id": session_id,
        "scenario_id": sid,
        "draft_id": session.get("draft_id"),
        "artifact_root": session.get("artifact_root"),
        "preview_state": preview,
        "workbench": workbench,
        "pending_action": pending_action,
        "message": message,
        "dialog": _dialog_state(ws),
    }


@tool(summary="Update current scenario prototype.", side_effects="local_write")
def update_current_scenario(
    instruction: str,
    webspace_id: str | None = None,
    auto_apply: bool = True,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session, binding = _target_session(ws)
    if not session:
        return {
            "ok": True,
            "status": "target_required",
            "needs_selection": True,
            "message": _target_required_message(binding),
            "binding": binding,
            "dialog": _dialog_state(ws),
        }
    text = str(instruction or "").strip()
    lowered = text.lower()
    patch = {
        "id": f"patch_{_hash_suffix(session['id'] + text + str(_now()))}",
        "target": "ui",
        "operation": "noop",
        "status": "applied" if auto_apply else "proposed",
        "created_by": "llm_agent",
        "created_at": _now(),
        "summary": text,
        "diff": {},
    }
    fields = [dict(item) for item in session.get("fields", []) if isinstance(item, Mapping)]
    if any(token in lowered for token in ("карточ", "card")):
        session["card_view"] = True
        patch["operation"] = "change_view_representation"
        patch["diff"] = {"card_view": True}
    elif any(token in lowered for token in ("убери", "удали", "remove")):
        label = _extract_field_label(text) or text.rsplit(" ", 1)[-1]
        fid = _field_id(label)
        before = len(fields)
        fields = [item for item in fields if str(item.get("id")) != fid and str(item.get("label") or "").lower() != label.lower()]
        session["fields"] = fields
        patch["operation"] = "remove_field"
        patch["diff"] = {"field_id": fid, "removed": before != len(fields), "warning": "existing records may still contain this field"}
    elif _wants_sample_data(text):
        rows = _food_mock_rows(fields)
        session["mock_rows"] = rows
        patch["operation"] = "update_mock_data"
        patch["diff"] = {"datasource_id": session.get("datasource_id") or "items", "rows": rows}
    else:
        label = _extract_field_label(text) or ("\u0426\u0435\u043d\u0430" if "\u0446\u0435\u043d" in lowered or "price" in lowered else None)
        if label:
            fid = _field_id(label)
            if not any(str(item.get("id")) == fid for item in fields):
                field = {"id": fid, "type": "number" if fid == "price" else "string", "label": label, "required": False}
                fields.append(field)
                session["fields"] = fields
                patch["operation"] = "add_field"
                patch["diff"] = {"field": field}
    if patch["operation"] == "noop":
        preview = session.get("preview_state") if isinstance(session.get("preview_state"), dict) else _preview_state(session=session)
        workbench = _ensure_workbench(ws, session=session, preview_state=preview)
        message = (
            f"{AGENT_LABEL}: \u044f \u043d\u0435 \u043d\u0430\u0448\u0435\u043b \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u0430\u043d\u043d\u043e\u0433\u043e "
            f"\u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u0434\u043b\u044f {session.get('scenario_id')}. "
            "\u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435, \u043a\u0430\u043a \u0438\u0437\u043c\u0435\u043d\u0438\u0442\u044c UI: "
            "\u0434\u043e\u0431\u0430\u0432\u044c \u043f\u043e\u043b\u0435, \u0443\u0431\u0435\u0440\u0438 \u043f\u043e\u043b\u0435, "
            "\u043f\u043e\u043a\u0430\u0436\u0438 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0430\u043c\u0438 \u0438\u043b\u0438 \u0441\u0434\u0435\u043b\u0430\u0439 \u043f\u0440\u0438\u043c\u0435\u0440 \u0434\u0430\u043d\u043d\u044b\u0445."
        )
        return {
            "ok": True,
            "status": "noop",
            "session_id": session.get("id"),
            "scenario_id": session.get("scenario_id"),
            "patch": patch,
            "preview_state": preview,
            "workbench": workbench,
            "pending_action": None,
            "message": message,
            "dialog": _dialog_state(ws),
        }
    session.setdefault("patches", []).append(patch)
    session["version"] = f"v{len(session.get('patches') or []) + 1}"
    preview = _preview_state(session=session)
    _write_webui(str(session.get("artifact_root") or ""), preview)
    session["preview_state"] = preview
    _save_session(ws, session)
    workbench = _ensure_workbench(ws, session=session, preview_state=preview)
    pending_action = _publish_review_pending_action(
        webspace_id=ws,
        session=session,
        request_text=text,
        kind="builder.scenario_patch.review",
        summary=f"Review Builder patch {patch['operation']} for {session.get('scenario_id')}",
        _meta=_meta,
        patch=patch,
    )
    if pending_action and pending_action.get("id"):
        patch["pending_action_id"] = pending_action.get("id")
        session["patches"][-1] = patch
        session["pending_action_id"] = pending_action.get("id")
        _save_session(ws, session)
    message = (
        f"{AGENT_LABEL}: \u043e\u0431\u043d\u043e\u0432\u0438\u043b \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f "
        f"{session.get('scenario_id')}. \u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f: {patch['operation']}."
    )
    return {
        "ok": True,
        "session_id": session.get("id"),
        "scenario_id": session.get("scenario_id"),
        "patch": patch,
        "preview_state": preview,
        "workbench": workbench,
        "pending_action": pending_action,
        "message": message,
        "dialog": _dialog_state(ws),
    }


@tool(summary="Get Builder session.", side_effects="none")
def get_session(
    session_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session = _load_session(ws, session_id)
    workbench = _ensure_workbench(ws, session=session, preview_state=(session or {}).get("preview_state") if isinstance(session, dict) else None)
    return {"ok": bool(session), "session": session, "workbench": workbench, "dialog": _dialog_state(ws)}


@tool(summary="Get Builder preview state.", side_effects="none")
def get_preview_state(
    session_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session = _load_session(ws, session_id)
    if not session:
        return {"ok": False, "error": "session_not_found", "preview_state": None, "dialog": _dialog_state(ws)}
    preview = session.get("preview_state") if isinstance(session.get("preview_state"), dict) else _preview_state(session=session)
    workbench = _ensure_workbench(ws, session=session, preview_state=preview)
    return {"ok": True, "session_id": session.get("id"), "preview_state": preview, "workbench": workbench, "dialog": _dialog_state(ws)}


@tool(summary="Ensure paired Builder Prompt IDE dev webspace.", side_effects="local_write")
def ensure_dev_webspace(
    webspace_id: str | None = None,
    active_draft_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session = _load_session(ws)
    explicit_draft_id = str(active_draft_id or "").strip() or None
    if active_draft_id:
        session = dict(session or {})
        session["draft_id"] = explicit_draft_id
    workbench = _ensure_workbench(ws, session=session, active_draft_id=explicit_draft_id or None)
    if not workbench.get("ok"):
        return {**workbench, "dialog": _dialog_state(ws)}
    return {"ok": True, "binding": workbench["binding"], "workbench": workbench, "dialog": _dialog_state(ws)}


@tool(summary="Return Builder workbench binding.", side_effects="none")
def get_workspace_binding(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    binding = _workbench_service().get_workspace_binding(ws)
    return {"ok": True, "binding": binding, "dialog": _dialog_state(ws)}


@tool(summary="Return URL for paired Builder Prompt IDE dev webspace.", side_effects="local_write")
def open_dev_webspace(
    webspace_id: str | None = None,
    base_url: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session, binding = _target_session(ws)
    workbench = _ensure_workbench(ws, session=session)
    if not workbench.get("ok"):
        return {**workbench, "dialog": _dialog_state(ws)}
    result = _workbench_service().open_dev_webspace(ws, base_url=base_url)
    return {**result, "binding": workbench["binding"], "workbench": workbench, "dialog": _dialog_state(ws)}


@tool(summary="Return embedded Voice Chat widget config for Builder workbench.", side_effects="none")
def attach_dialog_widget(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    widget = _workbench_service().dialog_widget_config(ws)
    return {"ok": True, "widget": widget, "dialog": _dialog_state(ws)}


@tool(summary="Switch active Builder development draft.", side_effects="local_write")
def set_active_draft(
    draft_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session = _load_session(ws)
    if draft_id and (not session or str(session.get("draft_id") or "") != str(draft_id).strip()):
        sessions = _sessions(ws)
        for item in sessions.values():
            if str(item.get("draft_id") or item.get("id") or "").strip() == str(draft_id).strip():
                session = item
                break
    workbench = _ensure_workbench(
        ws,
        session=session,
        active_draft_id=str(draft_id or "").strip() or None,
        runtime_scenario_id=_runtime_scenario_id(session),
    )
    if not workbench.get("ok"):
        return {**workbench, "dialog": _dialog_state(ws)}
    return {"ok": True, "binding": workbench["binding"], "workbench": workbench, "dialog": _dialog_state(ws)}


@tool(summary="List Builder skills/scenarios in development.", side_effects="none")
def list_development_skills(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    return {**_workbench_service().list_development_skills(ws), "dialog": _dialog_state(ws)}


@tool(summary="Delete Builder development draft.", side_effects="local_write")
def delete_development_skill(
    draft_id: str,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    result = _workbench_service().delete_development_skill(draft_id, ws)
    if result.get("ok"):
        _delete_sessions_for_draft(ws, draft_id)
    return {**result, "dialog": _dialog_state(ws)}


def handle(topic: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = dict(payload or {})
    if topic.endswith("start"):
        return start(**data)
    if topic.endswith("create_scenario_draft"):
        return create_scenario_draft(**data)
    if topic.endswith("update_current_scenario"):
        return update_current_scenario(**data)
    if topic.endswith("get_preview_state"):
        return get_preview_state(**data)
    if topic.endswith("get_session"):
        return get_session(**data)
    if topic.endswith("ensure_dev_webspace"):
        return ensure_dev_webspace(**data)
    if topic.endswith("get_workspace_binding"):
        return get_workspace_binding(**data)
    if topic.endswith("open_dev_webspace"):
        return open_dev_webspace(**data)
    if topic.endswith("attach_dialog_widget"):
        return attach_dialog_widget(**data)
    if topic.endswith("set_active_draft"):
        return set_active_draft(**data)
    if topic.endswith("list_development_skills"):
        return list_development_skills(**data)
    if topic.endswith("delete_development_skill"):
        return delete_development_skill(**data)
    return chat(**data)
