from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from copy import deepcopy
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import skill_memory_get, skill_memory_set
from adaos.sdk.io.out import stream_publish
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config
from adaos.services.webspace_id import coerce_webspace_id

try:
    from adaos.sdk.data import ctx_subnet
except Exception:
    class _MissingCtxSubnet:
        def set(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("ctx_subnet_unavailable")

    ctx_subnet = _MissingCtxSubnet()

_RECEIVER_NOTES = "notebook_skill.notes"
_SKILL_NAME = "notebook_skill"
_MAX_NOTES = 64
_MAX_CONTENT_CHARS = 32000
_NOTE_PREFIX = "note-"
_DEFAULT_WEBSPACE_ID = "desktop"
_STATE_MEMORY_PREFIX = "notebook_state.v1"
_LOG = logging.getLogger(_SKILL_NAME)
_PROJECTION_TIMEOUT_S = 8.0


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
_RELOAD_REPUBLISH_DELAYS: tuple[float, ...] = (1.0, 3.0, 8.0, 15.0)


def _memory_key(webspace_id: str) -> str:
    return f"{_STATE_MEMORY_PREFIX}.{coerce_webspace_id(webspace_id, fallback=_DEFAULT_WEBSPACE_ID)}"


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
    loaded = _apply_stored_state(_mem_get(_memory_key(ws), None))
    _LOADED_WEBSPACES.add(ws)
    return loaded


def _persist_state(webspace_id: str) -> None:
    payload = _state_payload()
    for ws in _projection_webspace_ids(webspace_id):
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
    if len(text) > _MAX_CONTENT_CHARS:
        return text[:_MAX_CONTENT_CHARS]
    return text


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


def _content_lines(content: Any) -> list[str]:
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text.split("\n") if text else []


def _note_heading(note: Mapping[str, Any], *, fallback: str = "New note") -> str:
    lines = _content_lines(note.get("content"))
    if not lines:
        return fallback
    heading = str(lines[0] or "").strip()
    return heading or fallback


def _note_card_preview(note: Mapping[str, Any], *, limit: int = 420) -> str:
    lines = _content_lines(note.get("content"))
    if len(lines) <= 1:
        return ""
    text = "\n".join(line.strip() for line in lines[1:]).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


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


def _note_id_from_payload(payload: Mapping[str, Any], *, fallback_selected: bool = True) -> str:
    for key in ("note_id", "id"):
        token = str(payload.get(key) or "").strip()
        if token and not token.startswith("$"):
            return token
    event = payload.get("event")
    if isinstance(event, Mapping):
        token = str(event.get("note_id") or event.get("id") or "").strip()
        if token:
            return token
    return (
        str(_STATE.get("editing_note_id") or _STATE.get("display_note_id") or "")
        if fallback_selected
        else ""
    )


def _note_list_items() -> list[dict[str, Any]]:
    display_id = str(_STATE.get("display_note_id") or "")
    editing_id = str(_STATE.get("editing_note_id") or "")
    items: list[dict[str, Any]] = []
    for note_id in list(_STATE.get("order") or []):
        note = _STATE["notes"].get(note_id)
        if not isinstance(note, Mapping):
            continue
        updated = float(note.get("updated_at") or 0)
        content = str(note.get("content") or "")
        preview = _note_card_preview(note)
        attachments = list(note.get("attachments") or [])
        items.append(
            {
                "id": note_id,
                "title": _note_heading(note),
                "subtitle": _now_iso(updated) if updated else "",
                "content": content,
                "text": preview,
                "preview": preview,
                "description": preview,
                "selected": "selected" if note_id == display_id else "",
                "editing": "editing" if note_id == editing_id else "",
                "attachment_count": len(attachments),
                "image": next((item for item in attachments if isinstance(item, Mapping) and item.get("kind") == "photo"), None),
                "updated_at": updated or None,
                "version": int(note.get("version") or 0),
            }
        )
    return items


def _snapshot() -> dict[str, Any]:
    display_note = _display_note()
    latest_note = _latest_note()
    latest_preview = _note_card_preview(latest_note)
    editing_note = _editing_note()
    editor_note = editing_note or display_note
    editor = {
        "id": editor_note["id"],
        "content": editor_note["content"],
        "attachments": list(editor_note.get("attachments") or []),
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
            "content": display_note["content"],
            "attachments": list(display_note.get("attachments") or []),
            "updated_at": display_note.get("updated_at"),
            "updated_label": _now_iso(float(display_note.get("updated_at") or _now())),
            "version": int(display_note.get("version") or 0),
        },
        "editor": editor,
        "notes": {"items": items},
        "latest": {
            "id": latest_note["id"],
            "title": _note_heading(latest_note),
            "content": latest_note["content"],
            "text": latest_preview,
            "preview": latest_preview,
            "description": latest_preview,
            "attachments": list(latest_note.get("attachments") or []),
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
                    "content": latest_note["content"],
                    "text": latest_preview,
                    "preview": latest_preview,
                    "description": latest_preview,
                    "attachments": list(latest_note.get("attachments") or []),
                    "updated_at": latest_note.get("updated_at"),
                }
            ]
        },
        "updated_at": _now(),
    }


def _notes_stream_payload(snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    snap = snapshot if isinstance(snapshot, Mapping) else _snapshot()
    notes = snap.get("notes") if isinstance(snap.get("notes"), Mapping) else {}
    display = snap.get("display") if isinstance(snap.get("display"), Mapping) else {}
    editor = snap.get("editor") if isinstance(snap.get("editor"), Mapping) else {}
    latest = snap.get("latest") if isinstance(snap.get("latest"), Mapping) else {}
    widget = snap.get("widget") if isinstance(snap.get("widget"), Mapping) else {}
    return {
        "ok": True,
        "_stream_rev": _stream_revision(snap),
        "_stream_require_revision": True,
        "selected_note_id": str(snap.get("selected_note_id") or ""),
        "display_note_id": str(snap.get("display_note_id") or ""),
        "editing_note_id": str(snap.get("editing_note_id") or ""),
        "display": deepcopy(dict(display)),
        "editor": deepcopy(dict(editor)),
        "latest": deepcopy(dict(latest)),
        "widget": deepcopy(dict(widget)),
        "items": list(notes.get("items") or []),
        "updated_at": snap.get("updated_at"),
    }


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


def _plain_json(value: Any) -> Any:
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        try:
            raw = to_json()
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
            return raw
        except Exception:
            return None
    if isinstance(value, Mapping):
        return {str(k): _plain_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(v) for v in value]
    return value


def _is_y_map(value: Any) -> bool:
    return callable(getattr(value, "get", None)) and callable(getattr(value, "set", None)) and callable(getattr(value, "to_json", None))


def _json_equal(left: Any, right: Any) -> bool:
    return _plain_json(left) == _plain_json(right)


def _write_notebook_snapshot_to_doc(ydoc: Any, txn: Any, snapshot: Mapping[str, Any]) -> bool:
    payload = deepcopy(dict(snapshot))
    data = ydoc.get_map("data")
    desktop = data.get("desktop")
    if _is_y_map(desktop):
        current = desktop.get("notebook")
        if _json_equal(current, payload):
            return False
        desktop.set(txn, "notebook", payload)
        return True

    desktop_payload = _plain_json(desktop)
    if not isinstance(desktop_payload, dict):
        desktop_payload = {}
    if _json_equal(desktop_payload.get("notebook"), payload):
        return False
    desktop_payload["notebook"] = payload
    data.set(txn, "desktop", desktop_payload)
    return True


def _projection_webspace_ids(webspace_id: str) -> list[str]:
    primary = coerce_webspace_id(webspace_id, fallback=_DEFAULT_WEBSPACE_ID)
    candidates = [primary, _DEFAULT_WEBSPACE_ID, "default"]
    result: list[str] = []
    for candidate in candidates:
        token = str(candidate or "").strip()
        if token and token not in result:
            result.append(token)
    return result


async def _project_notebook_snapshot_webspace_async(snapshot: Mapping[str, Any], webspace_id: str) -> None:
    from adaos.services.yjs.doc import async_get_ydoc, mutate_live_room
    from adaos.services.yjs.store import get_ystore_for_webspace

    ws = coerce_webspace_id(webspace_id, fallback=_DEFAULT_WEBSPACE_ID)
    source = f"{_SKILL_NAME}.projection"
    owner = f"skill:{_SKILL_NAME}"
    channel = "projection.yjs.notebook"
    persisted_updates: list[dict[str, Any]] = []

    def _on_store_update(meta: dict[str, Any]) -> None:
        persisted_updates.append(dict(meta or {}))

    def _mutator(doc: Any, txn: Any) -> None:
        _write_notebook_snapshot_to_doc(doc, txn, snapshot)

    def _mutate_live() -> None:
        mutate_live_room(
            ws,
            _mutator,
            root_names=["data"],
            source=source,
            owner=owner,
            channel=channel,
            governed=True,
        )

    try:
        _mutate_live()
    except Exception:
        _LOG.warning("failed to project notebook snapshot via live room webspace=%s", ws, exc_info=True)

    async with async_get_ydoc(
        ws,
        load_mark_roots=["data"],
        governed=True,
        publish_live_room=False,
        write_source=source,
        write_owner=owner,
        write_channel=channel,
        write_update_callback=_on_store_update,
    ) as ydoc:
        with ydoc.begin_transaction() as txn:
            _write_notebook_snapshot_to_doc(ydoc, txn, snapshot)
    if persisted_updates:
        await get_ystore_for_webspace(ws).backup_to_disk(compact_runtime=True, backup_kind="notebook_projection")
    try:
        _mutate_live()
    except Exception:
        _LOG.warning("failed to refresh notebook snapshot in live room webspace=%s", ws, exc_info=True)


async def _project_notebook_snapshot_async(snapshot: Mapping[str, Any], webspace_id: str) -> None:
    for ws in _projection_webspace_ids(webspace_id):
        await _project_notebook_snapshot_webspace_async(snapshot, ws)


def _project_notebook_snapshot(snapshot: Mapping[str, Any], webspace_id: str) -> None:
    snap = deepcopy(dict(snapshot))
    ws = coerce_webspace_id(webspace_id, fallback=_DEFAULT_WEBSPACE_ID)
    try:
        ctx_subnet.set("notebook.snapshot", snap, webspace_id=ws)
    except Exception:
        _LOG.warning("failed to project notebook snapshot via ctx_subnet webspace=%s", ws, exc_info=True)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_project_notebook_snapshot_async(snap, ws))
        return

    errors: list[BaseException] = []

    def _runner() -> None:
        try:
            asyncio.run(_project_notebook_snapshot_async(snap, ws))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_runner, name=f"{_SKILL_NAME}-projection", daemon=True)
    thread.start()
    thread.join(_PROJECTION_TIMEOUT_S)
    if thread.is_alive():
        raise TimeoutError(f"notebook snapshot projection timed out after {_PROJECTION_TIMEOUT_S:.1f}s")
    if errors:
        raise errors[0]


def _publish(snapshot: Mapping[str, Any] | None = None, *, webspace_id: str | None = None) -> dict[str, Any]:
    snap = dict(snapshot or _snapshot())
    ws = coerce_webspace_id(webspace_id, fallback=_DEFAULT_WEBSPACE_ID)
    try:
        _project_notebook_snapshot(snap, ws)
    except Exception:
        _LOG.warning("failed to schedule notebook snapshot projection webspace=%s", ws, exc_info=True)
    try:
        stream_publish(_RECEIVER_NOTES, _notes_stream_payload(snap), _meta={"webspace_id": ws})
    except Exception:
        _LOG.warning("failed to publish notebook notes stream webspace=%s", ws, exc_info=True)
    return snap


def _schedule_delayed_republish(webspace_id: str) -> None:
    delays = tuple(float(item) for item in _RELOAD_REPUBLISH_DELAYS if float(item) > 0)
    if not delays:
        return
    ws = coerce_webspace_id(webspace_id, fallback=_DEFAULT_WEBSPACE_ID)

    def _runner() -> None:
        for delay in delays:
            time.sleep(delay)
            try:
                _prepare_state(ws, force=True)
                _publish(webspace_id=ws)
            except Exception:
                _LOG.warning("failed delayed notebook republish webspace=%s delay=%s", ws, delay, exc_info=True)

    threading.Thread(target=_runner, name=f"{_SKILL_NAME}-reload-republish", daemon=True).start()



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
    snap = _publish(webspace_id=ws)
    return {"ok": True, "snapshot": snap}


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
    return {"ok": True, "note": deepcopy(note), "snapshot": snap}


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
    return {"ok": True, "note": deepcopy(_STATE["notes"][note_id]), "snapshot": snap}


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
        return {"ok": False, "error": "content_required", "note_id": note_id, "note": deepcopy(note)}
    content = _clean_content(body.get("content"))
    if not content and str(note.get("content") or "") and not _allows_empty_content(body):
        return {"ok": False, "error": "stale_empty_content", "note_id": note_id, "note": deepcopy(note)}
    note["content"] = content
    note["updated_at"] = _now()
    note["version"] = int(note.get("version") or 0) + 1
    _STATE["display_note_id"] = note_id
    _STATE["editing_note_id"] = note_id
    _promote_note(note_id)
    _persist_state(ws)
    snap = _publish(webspace_id=ws)
    return {"ok": True, "note": deepcopy(note), "snapshot": snap}


def _safe_upload_ref(upload: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, Any]:
    sha256 = str(upload.get("sha256") or artifact.get("sha256") or "").strip()
    purpose = str(upload.get("purpose") or artifact.get("purpose") or "attachments").strip() or "attachments"
    name = str(upload.get("name") or artifact.get("name") or artifact.get("filename") or "").strip()
    ref: dict[str, Any] = {}
    if sha256:
        ref["sha256"] = sha256
        ref["artifact_id"] = f"skill_file:{_SKILL_NAME}:{purpose}:{sha256[:16]}"
    if purpose:
        ref["purpose"] = purpose
    if name:
        ref["name"] = name
    return ref


def _attach_note_file(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _prepare_state(ws)
    note_id = _note_id_from_payload(body)
    note = _STATE["notes"].get(note_id)
    if not isinstance(note, dict):
        return {"ok": False, "error": "note_not_found", "note_id": note_id}
    artifact = body.get("artifact_ref") if isinstance(body.get("artifact_ref"), Mapping) else {}
    file_meta = body.get("file") if isinstance(body.get("file"), Mapping) else {}
    upload = body.get("upload") if isinstance(body.get("upload"), Mapping) else {}
    kind = "photo" if str(body.get("kind") or "").strip() == "photo" else "file"
    safe_ref = _safe_upload_ref(upload, artifact)
    attachment = {
        "id": f"att-{int(_now() * 1000)}",
        "kind": kind,
        "name": str(upload.get("name") or file_meta.get("name") or artifact.get("filename") or artifact.get("name") or "attachment").strip(),
        "mime": str(upload.get("mime") or file_meta.get("mime") or artifact.get("mime") or "").strip() or None,
        "size_bytes": upload.get("size_bytes") or file_meta.get("size_bytes") or artifact.get("size_bytes"),
        "artifact_ref": safe_ref or dict(artifact),
        "path": body.get("path") or artifact.get("path") or artifact.get("local_path") or artifact.get("stored_path"),
    }
    note.setdefault("attachments", []).append(attachment)
    note["updated_at"] = _now()
    note["version"] = int(note.get("version") or 0) + 1
    _STATE["display_note_id"] = note_id
    _STATE["editing_note_id"] = note_id
    _promote_note(note_id)
    _persist_state(ws)
    snap = _publish(webspace_id=ws)
    return {"ok": True, "attachment": deepcopy(attachment), "note": deepcopy(note), "snapshot": snap}


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
    return {"ok": True, "deleted_note_id": note_id, "snapshot": snap}


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
    return {"ok": True, "snapshot": snap}


@tool("notebook_persist_state")
def notebook_persist_state(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    ws = _webspace_id(body)
    _ensure_default_note()
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
    return {"ok": True, "webspace_id": ws, "snapshot": snap}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    body = _payload(evt)
    if str(body.get("receiver") or "").strip() != _RECEIVER_NOTES:
        return
    ws = _webspace_id(body)
    _prepare_state(ws, force=True)
    _publish(webspace_id=ws)


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    body = _payload(evt)
    if str(body.get("receiver") or "").strip() != _RECEIVER_NOTES:
        return
    if str(body.get("action") or "").strip().lower() == "unsubscribed":
        return
    ws = _webspace_id(body)
    _prepare_state(ws, force=True)
    _publish(webspace_id=ws)


@subscribe("node.yjs.control.completed")
def on_yjs_control_completed(evt: Any) -> None:
    body = _payload(evt)
    action = str(body.get("action") or "").strip().lower()
    if action not in {"reload", "reset", "restore"}:
        return
    ws = _webspace_id(body)
    _prepare_state(ws, force=True)
    _publish(webspace_id=ws)
    _schedule_delayed_republish(ws)
