from __future__ import annotations

import json
import logging
import os
import time
from copy import deepcopy
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import skill_memory_get, skill_memory_set
from adaos.sdk.io.out import stream_publish
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config
from adaos.services.webspace_id import coerce_webspace_id

_RECEIVER_NOTES = "notebook_skill.notes"
_RECEIVER_EDITOR = "notebook_skill.editor"
_RECEIVER_LATEST = "notebook_skill.latest"
_STREAM_RECEIVERS = (_RECEIVER_NOTES, _RECEIVER_EDITOR, _RECEIVER_LATEST)
_SKILL_NAME = "notebook_skill"
_MAX_NOTES = 64
_MAX_CONTENT_BYTES = 32000
_MAX_ATTACHMENTS_PER_NOTE = 8
_NOTE_PREFIX = "note-"
_DEFAULT_WEBSPACE_ID = "desktop"
_SHARED_WEBSPACE_IDS = ("desktop", "desktop-dev", "default")
_STATE_MEMORY_PREFIX = "notebook_state.v1"
_LOG = logging.getLogger(_SKILL_NAME)
_LIST_PREVIEW_CHARS = 420
_LIST_TITLE_CHARS = 160
_WIDGET_PREVIEW_CHARS = 1200


def _now() -> float:
    return time.time()


def _now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts or _now())))


def _default_note() -> dict[str, Any]:
    now = _now()
    return {
        "id": "note-1",
        "content": "",
        "attachments": [],
        "created_at": now,
        "updated_at": now,
        "version": 0,
    }


_STATE: dict[str, Any] = {
    "notes": {"note-1": _default_note()},
    "order": ["note-1"],
    "display_note_id": "note-1",
    "editing_note_id": "",
    "next_id": 2,
}
_LOADED_WEBSPACES: set[str] = set()
_FALLBACK_MEMORY: dict[str, Any] = {}


def _memory_key(webspace_id: str) -> str:
    return f"{_STATE_MEMORY_PREFIX}.{coerce_webspace_id(webspace_id, fallback=_DEFAULT_WEBSPACE_ID)}"


def _state_webspace_ids(webspace_id: str) -> list[str]:
    primary = coerce_webspace_id(webspace_id, fallback=_DEFAULT_WEBSPACE_ID)
    result: list[str] = []
    for candidate in (primary, *_SHARED_WEBSPACE_IDS):
        token = str(candidate or "").strip()
        if token and token not in result:
            result.append(token)
    return result


def _mem_get(key: str, default: Any = None) -> Any:
    try:
        return skill_memory_get(key, default)
    except Exception:
        return deepcopy(_FALLBACK_MEMORY.get(key, default))


def _mem_set(key: str, value: Any) -> None:
    try:
        skill_memory_set(key, value)
    except Exception:
        _FALLBACK_MEMORY[key] = deepcopy(value)


def _coerce_note(note_id: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    token = str(value.get("id") or note_id or "").strip()
    if not token:
        return None
    created = value.get("created_at")
    updated = value.get("updated_at")
    try:
        created_at = float(created or 0) or _now()
    except Exception:
        created_at = _now()
    try:
        updated_at = float(updated or 0) or created_at
    except Exception:
        updated_at = created_at
    attachments = value.get("attachments")
    return {
        "id": token,
        "content": _clean_content(value.get("content") or ""),
        "attachments": [dict(item) for item in attachments if isinstance(item, Mapping)] if isinstance(attachments, list) else [],
        "created_at": created_at,
        "updated_at": updated_at,
        "version": int(value.get("version") or 0),
    }


def _max_next_id(notes: Mapping[str, Any], fallback: int = 2) -> int:
    max_id = max(1, int(fallback or 2) - 1)
    for note_id in notes:
        token = str(note_id or "")
        if not token.startswith(_NOTE_PREFIX):
            continue
        try:
            max_id = max(max_id, int(token[len(_NOTE_PREFIX):]))
        except Exception:
            continue
    return max_id + 1


def _apply_stored_state(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    raw_notes = value.get("notes")
    if not isinstance(raw_notes, Mapping):
        return False
    notes: dict[str, dict[str, Any]] = {}
    for note_id, note in raw_notes.items():
        coerced = _coerce_note(str(note_id), note)
        if coerced is not None:
            notes[coerced["id"]] = coerced
    if not notes:
        return False
    order = [str(item) for item in value.get("order") or [] if str(item) in notes]
    for note_id in notes:
        if note_id not in order:
            order.append(note_id)
    display_note_id = str(value.get("display_note_id") or "").strip()
    if display_note_id not in notes:
        display_note_id = order[0]
    editing_note_id = str(value.get("editing_note_id") or "").strip()
    if editing_note_id not in notes:
        editing_note_id = ""
    _STATE["notes"] = notes
    _STATE["order"] = order
    _STATE["display_note_id"] = display_note_id
    _STATE["editing_note_id"] = editing_note_id
    _STATE["next_id"] = _max_next_id(notes, int(value.get("next_id") or 2))
    return True


def _state_freshness(value: Any) -> tuple[int, float, int]:
    if not isinstance(value, Mapping):
        return (0, 0.0, 0)
    raw_notes = value.get("notes")
    if not isinstance(raw_notes, Mapping):
        return (0, 0.0, 0)
    meaningful = 0
    best_updated = 0.0
    version_total = 0
    for note in raw_notes.values():
        if not isinstance(note, Mapping):
            continue
        content = str(note.get("content") or "").strip()
        attachments = note.get("attachments")
        has_attachments = isinstance(attachments, list) and bool(attachments)
        try:
            version = max(0, int(note.get("version") or 0))
        except Exception:
            version = 0
        version_total += version
        if content or has_attachments or version > 0:
            meaningful = 1
        try:
            best_updated = max(best_updated, float(note.get("updated_at") or 0.0))
        except Exception:
            pass
    try:
        best_updated = max(best_updated, float(value.get("updated_at") or 0.0))
    except Exception:
        pass
    return (meaningful, best_updated, version_total)


def _state_payload() -> dict[str, Any]:
    return {
        "schema": "notebook_skill.state.v1",
        "notes": deepcopy(_STATE.get("notes") or {}),
        "order": list(_STATE.get("order") or []),
        "display_note_id": str(_STATE.get("display_note_id") or ""),
        "editing_note_id": str(_STATE.get("editing_note_id") or ""),
        "next_id": int(_STATE.get("next_id") or 2),
        "updated_at": _now(),
    }


def _load_state(webspace_id: str, *, force: bool = False) -> bool:
    ws = coerce_webspace_id(webspace_id, fallback=_DEFAULT_WEBSPACE_ID)
    if not force and ws in _LOADED_WEBSPACES:
        return True
    best_value: Any = None
    best_key = ""
    best_rank = (0, 0.0, 0)
    for candidate in _state_webspace_ids(ws):
        key = _memory_key(candidate)
        value = _mem_get(key, None)
        rank = _state_freshness(value)
        if rank > best_rank:
            best_value = value
            best_key = key
            best_rank = rank
    loaded = _apply_stored_state(best_value)
    for candidate in _state_webspace_ids(ws):
        _LOADED_WEBSPACES.add(candidate)
    if loaded:
        if best_key and best_key != _memory_key(ws):
            _persist_state(ws)
            _LOG.info("notebook state rehydrated from alias key=%s webspace=%s", best_key, ws)
    return loaded


def _persist_state(webspace_id: str) -> None:
    payload = _state_payload()
    for ws in _state_webspace_ids(webspace_id):
        _mem_set(_memory_key(ws), payload)


def _prepare_state(webspace_id: str, *, force: bool = False) -> None:
    _load_state(webspace_id, force=force)
    _ensure_default_note()


def _payload(evt: Any) -> dict[str, Any]:
    raw = getattr(evt, "payload", evt)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _webspace_id(payload: Mapping[str, Any] | None = None) -> str:
    body = payload if isinstance(payload, Mapping) else {}
    meta = body.get("_meta") if isinstance(body.get("_meta"), Mapping) else {}
    raw = body.get("webspace_id") or body.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    return coerce_webspace_id(raw, fallback=_DEFAULT_WEBSPACE_ID)


def _local_subnet_id() -> str:
    try:
        subnet_id = str(getattr(load_config(), "subnet_id", "") or "").strip()
        if subnet_id:
            return subnet_id
    except Exception:
        pass
    try:
        return str(getattr(get_ctx().settings, "subnet_id", "") or "").strip()
    except Exception:
        return ""


def _root_base_candidates(explicit: str | None = None) -> list[str]:
    candidates = [
        explicit or "",
        os.getenv("PUBLIC_ROOT_BASE") or "",
        os.getenv("ADAOS_API_BASE") or "",
        os.getenv("ROOT_BASE_URL") or "",
    ]
    try:
        candidates.append(str(getattr(get_ctx().settings, "api_base", "") or ""))
    except Exception:
        pass
    try:
        candidates.append(str(getattr(getattr(load_config(), "root_settings", None), "base_url", "") or ""))
    except Exception:
        pass
    candidates.append("https://api.inimatic.com")
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        token = str(item or "").strip().rstrip("/")
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out or ["https://api.inimatic.com"]


def _clean_content(value: Any) -> str:
    text = str(value or "")
    if text.startswith(("$event.", "$state.", "$client.")):
        text = ""
    return _truncate_utf8(text, _MAX_CONTENT_BYTES)


def _has_resolved_content(payload: Mapping[str, Any]) -> bool:
    if "content" not in payload or payload.get("content") is None:
        return False
    text = str(payload.get("content") or "")
    return not text.startswith(("$event.", "$state.", "$client."))


def _allows_empty_content(payload: Mapping[str, Any]) -> bool:
    source = str(payload.get("source") or payload.get("content_source") or "").strip().lower()
    return source in {"editor_change", "explicit_clear"}


def _preview(content: str, *, limit: int = 120) -> str:
    text = " ".join(str(content or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _bounded_text(value: Any, *, limit: int) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = "..."
    prefix = _truncate_utf8(text, max(0, limit - len(suffix.encode("utf-8")))).rstrip()
    return prefix + suffix


def _truncate_utf8(value: Any, max_bytes: int) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[: max(0, int(max_bytes))].decode("utf-8", errors="ignore")


def _content_lines(content: Any) -> list[str]:
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text.split("\n") if text else []


def _note_heading(note: Mapping[str, Any], *, fallback: str = "New note") -> str:
    lines = _content_lines(note.get("content"))
    if not lines:
        return fallback
    heading = str(lines[0] or "").strip()
    return _bounded_text(heading, limit=_LIST_TITLE_CHARS) or fallback


def _note_card_preview(note: Mapping[str, Any], *, limit: int = 420) -> str:
    lines = _content_lines(note.get("content"))
    if len(lines) <= 1:
        return ""
    text = "\n".join(line.strip() for line in lines[1:]).strip()
    return _bounded_text(text, limit=limit)


def _promote_note(note_id: str) -> None:
    token = str(note_id or "").strip()
    if not token:
        return
    _STATE["order"] = [item for item in list(_STATE.get("order") or []) if item != token]
    _STATE["order"].insert(0, token)


def _display_note() -> dict[str, Any]:
    notes = _STATE["notes"]
    selected = str(_STATE.get("display_note_id") or "").strip()
    if selected in notes:
        return notes[selected]
    note_id = _STATE["order"][0] if _STATE["order"] else "note-1"
    _STATE["display_note_id"] = note_id
    return notes[note_id]


def _latest_note() -> dict[str, Any]:
    notes = _STATE["notes"]
    for note_id in list(_STATE.get("order") or []):
        note = notes.get(note_id)
        if isinstance(note, dict):
            return note
    return _display_note()


def _editing_note() -> dict[str, Any] | None:
    note_id = str(_STATE.get("editing_note_id") or "").strip()
    note = _STATE["notes"].get(note_id)
    return note if isinstance(note, dict) else None


def _clean_note_id_token(value: Any) -> str:
    token = str(value or "").strip()
    if not token or token.startswith("$") or token.lower() in {"null", "none", "undefined"}:
        return ""
    return token


def _current_note_id() -> str:
    notes = _STATE.get("notes") if isinstance(_STATE.get("notes"), Mapping) else {}
    for key in ("editing_note_id", "display_note_id"):
        note_id = _clean_note_id_token(_STATE.get(key))
        if note_id and isinstance(notes.get(note_id), dict):
            return note_id
    for item in list(_STATE.get("order") or []):
        note_id = _clean_note_id_token(item)
        if note_id and isinstance(notes.get(note_id), dict):
            return note_id
    return ""


def _explicit_note_id_from_payload(payload: Mapping[str, Any]) -> str:
    for key in ("note_id", "id"):
        token = _clean_note_id_token(payload.get(key))
        if token:
            return token
    event = payload.get("event")
    if isinstance(event, Mapping):
        token = _clean_note_id_token(event.get("note_id") or event.get("id"))
        if token:
            return token
    return ""


def _note_id_from_payload(payload: Mapping[str, Any], *, fallback_selected: bool = True) -> str:
    explicit = _explicit_note_id_from_payload(payload)
    if explicit:
        return explicit
    return _current_note_id() if fallback_selected else ""


def _note_list_items() -> list[dict[str, Any]]:
    display_id = str(_STATE.get("display_note_id") or "")
    editing_id = str(_STATE.get("editing_note_id") or "")
    items: list[dict[str, Any]] = []
    for note_id in list(_STATE.get("order") or []):
        note = _STATE["notes"].get(note_id)
        if not isinstance(note, Mapping):
            continue
        updated = float(note.get("updated_at") or 0)
        preview = _note_card_preview(note, limit=_LIST_PREVIEW_CHARS)
        attachments = [_project_attachment(item) for item in list(note.get("attachments") or []) if isinstance(item, Mapping)]
        image = next(
            (
                str(item.get("url") or "")
                for item in attachments
                if isinstance(item, Mapping) and item.get("kind") == "photo" and item.get("url")
            ),
            "",
        )
        items.append(
            {
                "id": note_id,
                "title": _note_heading(note),
                "subtitle": _now_iso(updated) if updated else "",
                "content": preview,
                "text": preview,
                "preview": preview,
                "description": preview,
                "selected": "selected" if note_id == display_id else "",
                "editing": "editing" if note_id == editing_id else "",
                "attachment_count": len(attachments),
                "image": image or None,
                "updated_at": updated or None,
                "version": int(note.get("version") or 0),
            }
        )
    return items


def _snapshot() -> dict[str, Any]:
    display_note = _display_note()
    latest_note = _latest_note()
    display_preview = _note_card_preview(display_note, limit=_WIDGET_PREVIEW_CHARS)
    latest_preview = _note_card_preview(latest_note, limit=_WIDGET_PREVIEW_CHARS)
    editing_note = _editing_note()
    editor_note = editing_note or display_note
    display_attachments = [_project_attachment(item) for item in list(display_note.get("attachments") or []) if isinstance(item, Mapping)]
    latest_attachments = [_project_attachment(item) for item in list(latest_note.get("attachments") or []) if isinstance(item, Mapping)]
    editor = {
        "id": editor_note["id"],
        "content": editor_note["content"],
        "attachments": [_project_attachment(item) for item in list(editor_note.get("attachments") or []) if isinstance(item, Mapping)],
        "updated_at": editor_note.get("updated_at"),
        "updated_label": _now_iso(float(editor_note.get("updated_at") or _now())),
        "version": int(editor_note.get("version") or 0),
        "editing": editing_note is not None,
    }
    items = _note_list_items()
    return {
        "ok": True,
        "selected_note_id": display_note["id"],
        "display_note_id": display_note["id"],
        "editing_note_id": str(_STATE.get("editing_note_id") or ""),
        "display": {
            "id": display_note["id"],
            "title": _note_heading(display_note),
            "content": display_preview,
            "text": display_preview,
            "preview": display_preview,
            "description": display_preview,
            "attachment_count": len(display_attachments),
            "updated_at": display_note.get("updated_at"),
            "updated_label": _now_iso(float(display_note.get("updated_at") or _now())),
            "version": int(display_note.get("version") or 0),
        },
        "editor": editor,
        "notes": {"items": items},
        "latest": {
            "id": latest_note["id"],
            "title": _note_heading(latest_note),
            "content": latest_preview,
            "text": latest_preview,
            "preview": latest_preview,
            "description": latest_preview,
            "attachment_count": len(latest_attachments),
            "updated_at": latest_note.get("updated_at"),
            "updated_label": _now_iso(float(latest_note.get("updated_at") or _now())),
            "version": int(latest_note.get("version") or 0),
        },
        "widget": {
            "items": [
                {
                    "id": latest_note["id"],
                    "title": _note_heading(latest_note),
                    "subtitle": _now_iso(float(latest_note.get("updated_at") or _now())),
                    "content": latest_preview,
                    "text": latest_preview,
                    "preview": latest_preview,
                    "description": latest_preview,
                    "attachment_count": len(latest_attachments),
                    "updated_at": latest_note.get("updated_at"),
                }
            ]
        },
        "updated_at": _now(),
    }


def _notes_stream_payload(snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    snap = snapshot if isinstance(snapshot, Mapping) else _snapshot()
    notes = snap.get("notes") if isinstance(snap.get("notes"), Mapping) else {}
    items = [_stream_list_item(item) for item in list(notes.get("items") or []) if isinstance(item, Mapping)]
    return {
        "ok": True,
        "_stream_rev": _stream_revision(snap),
        "_stream_require_revision": True,
        "selected_note_id": str(snap.get("selected_note_id") or ""),
        "display_note_id": str(snap.get("display_note_id") or ""),
        "editing_note_id": str(snap.get("editing_note_id") or ""),
        "items": items,
        "updated_at": snap.get("updated_at"),
    }


def _editor_stream_payload(snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    snap = snapshot if isinstance(snapshot, Mapping) else _snapshot()
    editor = snap.get("editor") if isinstance(snap.get("editor"), Mapping) else {}
    return {
        "ok": True,
        "_stream_rev": _stream_revision(snap),
        "_stream_require_revision": True,
        "selected_note_id": str(snap.get("selected_note_id") or ""),
        "editing_note_id": str(snap.get("editing_note_id") or ""),
        "editor": _stream_editor(editor),
        "updated_at": snap.get("updated_at"),
    }


def _latest_stream_payload(snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    snap = snapshot if isinstance(snapshot, Mapping) else _snapshot()
    widget = snap.get("widget") if isinstance(snap.get("widget"), Mapping) else {}
    widget_items = widget.get("items") if isinstance(widget.get("items"), list) else []
    return {
        "ok": True,
        "_stream_rev": _stream_revision(snap),
        "_stream_require_revision": True,
        "widget": {"items": [_stream_widget_item(item) for item in widget_items if isinstance(item, Mapping)]},
        "updated_at": snap.get("updated_at"),
    }


def _without_empty(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None and item != ""}


def _stream_list_item(item: Mapping[str, Any]) -> dict[str, Any]:
    preview = _bounded_text(item.get("preview") or item.get("text") or item.get("description") or item.get("content"), limit=_LIST_PREVIEW_CHARS)
    return _without_empty(
        {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "subtitle": str(item.get("subtitle") or ""),
            "preview": preview,
            "selected": item.get("selected"),
            "editing": item.get("editing"),
            "attachment_count": item.get("attachment_count"),
            "image": item.get("image"),
            "updated_at": item.get("updated_at"),
            "version": item.get("version"),
        }
    )


def _stream_widget_item(item: Mapping[str, Any]) -> dict[str, Any]:
    text = _bounded_text(item.get("text") or item.get("preview") or item.get("description") or item.get("content"), limit=_WIDGET_PREVIEW_CHARS)
    return _without_empty(
        {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "subtitle": str(item.get("subtitle") or ""),
            "text": text,
            "attachment_count": item.get("attachment_count"),
            "updated_at": item.get("updated_at"),
        }
    )


def _stream_editor(editor: Mapping[str, Any]) -> dict[str, Any]:
    attachments = editor.get("attachments") if isinstance(editor.get("attachments"), list) else []
    return _without_empty(
        {
            "id": str(editor.get("id") or ""),
            "content": str(editor.get("content") or ""),
            "attachments": [
                _project_attachment(item)
                for item in attachments[:_MAX_ATTACHMENTS_PER_NOTE]
                if isinstance(item, Mapping)
            ],
            "updated_at": editor.get("updated_at"),
            "updated_label": editor.get("updated_label"),
            "version": editor.get("version"),
            "editing": editor.get("editing"),
        }
    )


def _stream_revision(snapshot: Mapping[str, Any]) -> int:
    notes = snapshot.get("notes") if isinstance(snapshot.get("notes"), Mapping) else {}
    items = notes.get("items") if isinstance(notes.get("items"), list) else []
    best_updated = 0.0
    version_total = 0
    for item in items:
        if not isinstance(item, Mapping):
            continue
        try:
            best_updated = max(best_updated, float(item.get("updated_at") or 0.0))
        except Exception:
            pass
        try:
            version_total += max(0, int(item.get("version") or 0))
        except Exception:
            pass
    if best_updated <= 0:
        try:
            best_updated = float(snapshot.get("updated_at") or 0.0)
        except Exception:
            best_updated = _now()
    # Milliseconds plus a bounded version suffix stays within JavaScript's
    # safe integer range and still advances for rapid consecutive saves.
    return int(best_updated * 1000) * 1000 + min(version_total, 999)


def _stream_payload(receiver: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if receiver == _RECEIVER_EDITOR:
        return _editor_stream_payload(snapshot)
    if receiver == _RECEIVER_LATEST:
        return _latest_stream_payload(snapshot)
    return _notes_stream_payload(snapshot)


def _publish(
    snapshot: Mapping[str, Any] | None = None,
    *,
    webspace_id: str | None = None,
    receivers: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    snap = dict(snapshot or _snapshot())
    ws = coerce_webspace_id(webspace_id, fallback=_DEFAULT_WEBSPACE_ID)
    selected = tuple(dict.fromkeys(receivers or _STREAM_RECEIVERS))
    for receiver in selected:
        if receiver not in _STREAM_RECEIVERS:
            continue
        try:
            stream_publish(receiver, _stream_payload(receiver, snap), _meta={"webspace_id": ws})
        except Exception:
            _LOG.warning(
                "failed to publish notebook stream receiver=%s webspace=%s",
                receiver,
                ws,
                exc_info=True,
            )
    return snap


def _note_ack(note: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(note.get("id") or ""),
        "version": int(note.get("version") or 0),
        "updated_at": note.get("updated_at"),
    }


def _published_ack(snapshot: Mapping[str, Any], *, receivers: tuple[str, ...] | None = None) -> dict[str, Any]:
    return {
        "status": "published",
        "receivers": list(receivers or _STREAM_RECEIVERS),
        "selected_note_id": str(snapshot.get("selected_note_id") or ""),
        "editing_note_id": str(snapshot.get("editing_note_id") or ""),
        "updated_at": snapshot.get("updated_at"),
    }



def _ensure_default_note() -> None:
    if _STATE["notes"] and _STATE["order"]:
        return
    note = _default_note()
    _STATE["notes"] = {note["id"]: note}
    _STATE["order"] = [note["id"]]
    _STATE["display_note_id"] = note["id"]
    _STATE["editing_note_id"] = ""
    _STATE["next_id"] = 2


@tool("get_notebook_snapshot")
def get_notebook_snapshot(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _prepare_state(ws)
    return {"ok": True, "snapshot": _snapshot()}


@tool("refresh_notebook")
def refresh_notebook(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _prepare_state(ws, force=True)
    snap = _publish(webspace_id=ws)
    return {"ok": True, **_published_ack(snap)}


@tool("create_note")
def create_note(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _prepare_state(ws)
    if len(_STATE["order"]) >= _MAX_NOTES:
        return {"ok": False, "error": "note_limit_reached", "limit": _MAX_NOTES}
    content = _clean_content(body.get("content") or "")
    note_id = f"{_NOTE_PREFIX}{int(_STATE.get('next_id') or 1)}"
    _STATE["next_id"] = int(_STATE.get("next_id") or 1) + 1
    now = _now()
    note = {
        "id": note_id,
        "content": content,
        "attachments": [],
        "created_at": now,
        "updated_at": now,
        "version": 1 if content else 0,
    }
    _STATE["notes"][note_id] = note
    _promote_note(note_id)
    _STATE["display_note_id"] = note_id
    _STATE["editing_note_id"] = note_id
    _persist_state(ws)
    snap = _publish(webspace_id=ws)
    return {"ok": True, "note": _note_ack(note), **_published_ack(snap)}


@tool("select_note")
def select_note(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _prepare_state(ws)
    note_id = _note_id_from_payload(body, fallback_selected=False)
    if note_id not in _STATE["notes"]:
        return {"ok": False, "error": "note_not_found", "note_id": note_id}
    _STATE["display_note_id"] = note_id
    if bool(body.get("edit")):
        _STATE["editing_note_id"] = note_id
    elif "edit" in body:
        _STATE["editing_note_id"] = ""
    _persist_state(ws)
    snap = _publish(webspace_id=ws)
    return {"ok": True, "note": _note_ack(_STATE["notes"][note_id]), **_published_ack(snap)}


@tool("save_note")
def save_note(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _prepare_state(ws)
    note_id = _note_id_from_payload(body)
    note = _STATE["notes"].get(note_id)
    if not isinstance(note, dict):
        return {"ok": False, "error": "note_not_found", "note_id": note_id}
    if not _has_resolved_content(body):
        return {"ok": False, "error": "content_required", "note_id": note_id, "note": _note_ack(note)}
    content = _clean_content(body.get("content"))
    if not content and str(note.get("content") or "") and not _allows_empty_content(body):
        return {"ok": False, "error": "stale_empty_content", "note_id": note_id, "note": _note_ack(note)}
    note["content"] = content
    note["updated_at"] = _now()
    note["version"] = int(note.get("version") or 0) + 1
    _STATE["display_note_id"] = note_id
    _STATE["editing_note_id"] = note_id
    _promote_note(note_id)
    _persist_state(ws)
    snap = _publish(webspace_id=ws)
    return {"ok": True, "note": _note_ack(note), **_published_ack(snap)}


def _default_attachment_purpose(kind: str) -> str:
    return "photos" if str(kind or "").strip() == "photo" else "attachments"


def _clean_upload_relative_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/").lstrip("/")
    if not raw or ":" in raw or len(raw.encode("utf-8")) > 512:
        return ""
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    return "/".join(parts)


def _fallback_upload_relative_path(*, purpose: str, name: str) -> str:
    purpose_token = str(purpose or "").strip()
    name_token = str(name or "").strip()
    if not purpose_token or not name_token:
        return ""
    return _clean_upload_relative_path(f"uploads/{purpose_token}/{name_token}")


def _config_text_attr(name: str) -> str:
    try:
        conf = getattr(get_ctx(), "config", None)
        value = getattr(conf, name, None) if conf is not None else None
        if value:
            return str(value).strip()
    except Exception:
        pass
    try:
        value = getattr(load_config(), name, None)
        if value:
            return str(value).strip()
    except Exception:
        pass
    return ""


def _local_api_base_url() -> str:
    candidates = [
        os.getenv("ADAOS_LOCAL_API_URL") or "",
        os.getenv("ADAOS_LOCAL_API_BASE") or "",
        os.getenv("ADAOS_RUNTIME_API_URL") or "",
        os.getenv("ADAOS_CONTROL_API_URL") or "",
        _config_text_attr("local_api_url"),
        "http://127.0.0.1:8777",
    ]
    for item in candidates:
        token = str(item or "").strip().rstrip("/")
        if not token:
            continue
        if "://" not in token:
            token = f"http://{token}"
        token = token.replace("://0.0.0.0", "://127.0.0.1").replace("://[::]", "://127.0.0.1")
        return token
    return "http://127.0.0.1:8777"


def _local_api_token() -> str:
    for item in (os.getenv("ADAOS_TOKEN") or "", _config_text_attr("token"), "dev-local-token"):
        token = str(item or "").strip()
        if token:
            return token
    return "dev-local-token"


def _append_token_query(url: str) -> str:
    token = _local_api_token()
    if not token or "token=" in url:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode({'token': token})}"


def _skill_file_url(relative_path: Any, *, download: bool = False) -> str:
    rel = _clean_upload_relative_path(relative_path)
    if not rel:
        return ""
    encoded = "/".join(quote(part, safe="") for part in rel.split("/"))
    url = f"{_local_api_base_url()}/api/skills/{_SKILL_NAME}/files/content/{encoded}"
    params: list[tuple[str, str]] = []
    if download:
        params.append(("download", "1"))
    token = _local_api_token()
    if token:
        params.append(("token", token))
    return f"{url}?{urlencode(params)}" if params else url


def _browser_safe_url(raw_url: Any) -> str:
    raw = str(raw_url or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith("file:") or "\\" in raw:
        return ""
    content_path = f"/api/skills/{_SKILL_NAME}/files/content/"
    if raw.startswith(content_path):
        return _append_token_query(f"{_local_api_base_url()}{raw}")
    dev_origins = ("http://127.0.0.1:8100", "http://localhost:8100")
    for origin in dev_origins:
        if lowered.startswith(f"{origin}{content_path}".lower()):
            return _append_token_query(f"{_local_api_base_url()}{raw[len(origin):]}")
    return raw


def _format_size(value: Any) -> str:
    try:
        size = int(value or 0)
    except Exception:
        return ""
    if size <= 0:
        return ""
    units = ("B", "KB", "MB", "GB")
    amount = float(size)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if amount < 1024 or candidate == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} B"
    return f"{amount:.1f} {unit}".rstrip("0").rstrip(".")


def _attachment_summary(mime: Any, size_bytes: Any) -> str:
    parts = [part for part in (str(mime or "").strip(), _format_size(size_bytes)) if part]
    return " | ".join(parts)


def _attachment_icon(kind: str, mime: Any) -> str:
    mime_token = str(mime or "").strip().lower()
    if str(kind or "").strip() == "photo" or mime_token.startswith("image/"):
        return "image-outline"
    return "document-attach-outline"


def _compact_artifact_ref(
    artifact: Mapping[str, Any],
    *,
    purpose: str,
    name: str,
    relative_path: str,
    mime: str | None,
    size_bytes: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    artifact_id = _bounded_text(artifact.get("artifact_id") or artifact.get("id"), limit=256).strip()
    sha256 = _bounded_text(artifact.get("sha256"), limit=64).strip()
    if not artifact_id and sha256:
        artifact_id = f"skill_file:{_SKILL_NAME}:{purpose}:{sha256[:16]}"
    if artifact_id:
        out["artifact_id"] = artifact_id
        out["id"] = artifact_id
    if sha256:
        out["sha256"] = sha256
    if purpose:
        out["purpose"] = purpose
    if name:
        out["name"] = name
    if relative_path:
        out["relative_path"] = relative_path
    if mime:
        out["mime"] = mime
    if size_bytes:
        out["size_bytes"] = size_bytes
    return out


def _project_attachment(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(value or {})
    artifact = raw.get("artifact_ref") if isinstance(raw.get("artifact_ref"), Mapping) else {}
    kind = "photo" if str(raw.get("kind") or "").strip() == "photo" else "file"
    name = _bounded_text(raw.get("name") or artifact.get("name") or artifact.get("filename") or "attachment", limit=256).strip()
    mime = _bounded_text(raw.get("mime") or artifact.get("mime"), limit=128).strip() or None
    size_bytes = raw.get("size_bytes") or artifact.get("size_bytes")
    purpose = _bounded_text(artifact.get("purpose") or _default_attachment_purpose(kind), limit=64).strip()
    relative_path = _clean_upload_relative_path(raw.get("relative_path") or artifact.get("relative_path"))
    if not relative_path:
        relative_path = _fallback_upload_relative_path(purpose=purpose, name=name)
    url = _skill_file_url(relative_path) or _browser_safe_url(raw.get("url"))
    download_url = _skill_file_url(relative_path, download=True) or _browser_safe_url(raw.get("download_url"))
    attachment_id = _bounded_text(raw.get("id") or artifact.get("artifact_id") or artifact.get("id"), limit=256).strip()
    out: dict[str, Any] = {
        "id": attachment_id or None,
        "kind": kind,
        "name": name,
        "mime": mime,
        "size_bytes": size_bytes,
        "summary": _bounded_text(raw.get("summary"), limit=256).strip() or _attachment_summary(mime, size_bytes),
        "icon": _bounded_text(raw.get("icon"), limit=64).strip() or _attachment_icon(kind, mime),
        "relative_path": relative_path or None,
        "url": url or None,
        "download_url": download_url or None,
    }
    if url:
        out["path"] = url
    compact_ref = _compact_artifact_ref(
        artifact,
        purpose=purpose,
        name=name,
        relative_path=relative_path,
        mime=mime,
        size_bytes=size_bytes,
    )
    if compact_ref:
        out["artifact_ref"] = compact_ref
    return {key: item for key, item in out.items() if item is not None and item != ""}


def _safe_upload_ref(
    upload: Mapping[str, Any],
    artifact: Mapping[str, Any],
    *,
    default_purpose: str = "attachments",
) -> dict[str, Any]:
    sha256 = str(upload.get("sha256") or artifact.get("sha256") or "").strip()
    purpose = str(upload.get("purpose") or artifact.get("purpose") or default_purpose).strip() or default_purpose
    name = str(upload.get("name") or artifact.get("name") or artifact.get("filename") or "").strip()
    relative_path = _clean_upload_relative_path(upload.get("relative_path") or artifact.get("relative_path"))
    if not relative_path:
        relative_path = _fallback_upload_relative_path(purpose=purpose, name=name)
    ref: dict[str, Any] = {}
    artifact_id = str(upload.get("artifact_id") or artifact.get("artifact_id") or artifact.get("id") or "").strip()
    if sha256:
        ref["sha256"] = sha256
        ref["artifact_id"] = f"skill_file:{_SKILL_NAME}:{purpose}:{sha256[:16]}"
        ref["id"] = ref["artifact_id"]
    elif artifact_id.startswith(f"skill_file:{_SKILL_NAME}:"):
        ref["artifact_id"] = artifact_id
        ref["id"] = artifact_id
    if purpose:
        ref["purpose"] = purpose
    if name:
        ref["name"] = name
    if relative_path:
        ref["relative_path"] = relative_path
    mime = str(upload.get("mime") or artifact.get("mime") or "").strip()
    if mime:
        ref["mime"] = mime
    size_bytes = upload.get("size_bytes") or artifact.get("size_bytes")
    if size_bytes:
        ref["size_bytes"] = size_bytes
    return ref


def _attach_note_file(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _prepare_state(ws)
    requested_note_id = _explicit_note_id_from_payload(body)
    note_id = requested_note_id or _note_id_from_payload(body)
    note = _STATE["notes"].get(note_id)
    if not isinstance(note, dict) and requested_note_id:
        fallback_note_id = _current_note_id()
        fallback_note = _STATE["notes"].get(fallback_note_id)
        if isinstance(fallback_note, dict):
            _LOG.warning(
                "attach_note_file note_id fallback: requested=%s fallback=%s webspace=%s",
                requested_note_id,
                fallback_note_id,
                ws,
            )
            note_id = fallback_note_id
            note = fallback_note
    if not isinstance(note, dict):
        return {"ok": False, "error": "note_not_found", "note_id": note_id}
    attachments = note.get("attachments") if isinstance(note.get("attachments"), list) else []
    if len(attachments) >= _MAX_ATTACHMENTS_PER_NOTE:
        return {
            "ok": False,
            "error": "attachment_limit_reached",
            "note_id": note_id,
            "limit": _MAX_ATTACHMENTS_PER_NOTE,
        }
    raw_note_id = str(body.get("note_id") or "").strip()
    if raw_note_id and raw_note_id != note_id and not requested_note_id:
        _LOG.warning("attach_note_file unresolved note_id token: raw=%s fallback=%s webspace=%s", raw_note_id, note_id, ws)
    artifact = body.get("artifact_ref") if isinstance(body.get("artifact_ref"), Mapping) else {}
    file_meta = body.get("file") if isinstance(body.get("file"), Mapping) else {}
    upload = body.get("upload") if isinstance(body.get("upload"), Mapping) else {}
    kind = "photo" if str(body.get("kind") or "").strip() == "photo" else "file"
    safe_ref = _safe_upload_ref(upload, artifact, default_purpose=_default_attachment_purpose(kind))
    name = str(upload.get("name") or file_meta.get("name") or artifact.get("filename") or artifact.get("name") or "attachment").strip()
    mime = str(upload.get("mime") or file_meta.get("mime") or artifact.get("mime") or "").strip() or None
    size_bytes = upload.get("size_bytes") or file_meta.get("size_bytes") or artifact.get("size_bytes")
    relative_path = str(safe_ref.get("relative_path") or "").strip()
    url = _skill_file_url(relative_path)
    download_url = _skill_file_url(relative_path, download=True)
    attachment = {
        "id": f"att-{int(_now() * 1000)}",
        "kind": kind,
        "name": name,
        "mime": mime,
        "size_bytes": size_bytes,
        "summary": _attachment_summary(mime, size_bytes),
        "icon": _attachment_icon(kind, mime),
        "artifact_ref": safe_ref or dict(artifact),
        "relative_path": relative_path or None,
        "url": url or None,
        "download_url": download_url or None,
        "path": url or None,
    }
    note["attachments"] = attachments
    attachments.append(attachment)
    note["updated_at"] = _now()
    note["version"] = int(note.get("version") or 0) + 1
    _STATE["display_note_id"] = note_id
    _STATE["editing_note_id"] = note_id
    _promote_note(note_id)
    _persist_state(ws)
    snap = _publish(webspace_id=ws)
    return {
        "ok": True,
        "attachment": deepcopy(attachment),
        "note": _note_ack(note),
        **_published_ack(snap),
    }


@tool("attach_note_upload")
def attach_note_upload(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return _attach_note_file(payload, **kwargs)


@tool("attach_note_file")
def attach_note_file(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return _attach_note_file(payload, **kwargs)


@tool("delete_note")
def delete_note(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _prepare_state(ws)
    note_id = _note_id_from_payload(body)
    if note_id not in _STATE["notes"]:
        return {"ok": False, "error": "note_not_found", "note_id": note_id}
    _STATE["notes"].pop(note_id, None)
    _STATE["order"] = [item for item in _STATE["order"] if item != note_id]
    _ensure_default_note()
    fallback = _STATE["order"][0]
    _STATE["display_note_id"] = fallback
    _STATE["editing_note_id"] = fallback
    _persist_state(ws)
    snap = _publish(webspace_id=ws)
    return {"ok": True, "deleted_note_id": note_id, **_published_ack(snap)}


def _send_telegram_text(
    text: str,
    *,
    chat_id: str = "",
    bot_id: str = "",
    hub_id: str = "",
    root_base: str = "",
    webspace_id: str = _DEFAULT_WEBSPACE_ID,
) -> dict[str, Any]:
    import requests

    target_chat = str(chat_id or os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    target_bot = str(bot_id or os.getenv("TG_BOT_ID") or os.getenv("TELEGRAM_BOT_ID") or "").strip()
    target_hub = str(hub_id or "").strip() or _local_subnet_id()
    if not target_chat and not target_hub:
        return {"ok": False, "error": "hub_id_or_chat_id_required"}
    body: dict[str, Any] = {
        "messages": [{"type": "text", "text": text}],
        "_meta": {"webspace_id": webspace_id, "route_id": "telegram", "skill_name": "notebook_skill"},
    }
    if target_chat:
        body["chat_id"] = target_chat
    if target_bot:
        body["bot_id"] = target_bot
    if target_hub:
        body["hub_id"] = target_hub

    result: dict[str, Any] | None = None
    tried: list[str] = []
    for root_url in _root_base_candidates(root_base):
        tried.append(root_url)
        try:
            resp = requests.post(
                f"{root_url.rstrip('/')}/io/tg/send",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=(2.0, 8.0),
            )
            try:
                data = resp.json() if resp.content else {}
            except Exception:
                data = {"body": (resp.text or "")[:300]}
            if 200 <= int(resp.status_code or 0) < 300 and bool(data.get("ok", True)):
                return {
                    "ok": True,
                    "transport": "root_tg_send",
                    "root_url": root_url,
                    "tried_roots": tried,
                    "hub_id": target_hub,
                    "chat_id": target_chat,
                    "bot_id": target_bot,
                    "result": data,
                }
            result = {
                "ok": False,
                "error": str(data.get("error") or f"root_tg_send_http_{resp.status_code}"),
                "status": int(resp.status_code or 0),
                "root_url": root_url,
                "tried_roots": list(tried),
                "hub_id": target_hub,
                "chat_id": target_chat,
                "bot_id": target_bot,
                "result": data,
            }
            if result["error"] != "pairing_not_found" and result["status"] not in {404, 503}:
                break
        except Exception as exc:
            result = {
                "ok": False,
                "error": "root_tg_send_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "root_url": root_url,
                "tried_roots": list(tried),
                "hub_id": target_hub,
                "chat_id": target_chat,
                "bot_id": target_bot,
            }
    return result or {"ok": False, "error": "root_tg_send_failed", "tried_roots": tried}


@tool("send_note_to_telegram")
def send_note_to_telegram(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _prepare_state(ws)
    note_id = _note_id_from_payload(body)
    note = _STATE["notes"].get(note_id)
    if not isinstance(note, Mapping):
        return {"ok": False, "error": "note_not_found", "note_id": note_id}
    content = str(note.get("content") or "").strip()
    text = content
    if not text:
        return {"ok": False, "error": "empty_note", "note_id": note_id}
    result = _send_telegram_text(
        text[:4000],
        chat_id=str(body.get("chat_id") or ""),
        bot_id=str(body.get("bot_id") or ""),
        hub_id=str(body.get("hub_id") or ""),
        root_base=str(body.get("root_base") or ""),
        webspace_id=ws,
    )
    return {"ok": bool(result.get("ok")), "telegram": result, "note_id": note_id}


@tool("reset_notebook")
def reset_notebook(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    note = _default_note()
    _STATE["notes"] = {note["id"]: note}
    _STATE["order"] = [note["id"]]
    _STATE["display_note_id"] = note["id"]
    _STATE["editing_note_id"] = ""
    _STATE["next_id"] = 2
    _LOADED_WEBSPACES.add(ws)
    _persist_state(ws)
    snap = _publish(webspace_id=ws)
    return {"ok": True, **_published_ack(snap)}


@tool("notebook_persist_state")
def notebook_persist_state(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _prepare_state(ws, force=True)
    _persist_state(ws)
    return {"ok": True, "webspace_id": ws, "state": _state_payload()}


@tool("notebook_rehydrate")
def notebook_rehydrate(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _load_state(ws, force=True)
    _ensure_default_note()
    snap = _publish(webspace_id=ws)
    return {"ok": True, "webspace_id": ws, **_published_ack(snap)}


@subscribe("webio.stream.snapshot.requested", receivers=_STREAM_RECEIVERS)
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    body = _payload(evt)
    receiver = str(body.get("receiver") or "").strip()
    if receiver not in _STREAM_RECEIVERS:
        return
    ws = _webspace_id(body)
    _prepare_state(ws, force=True)
    _publish(webspace_id=ws, receivers=(receiver,))
