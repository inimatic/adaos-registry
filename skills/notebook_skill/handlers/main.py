from __future__ import annotations

import os
import time
from copy import deepcopy
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
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
_MAX_NOTES = 64
_MAX_CONTENT_CHARS = 32000
_NOTE_PREFIX = "note-"


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


def _payload(evt: Any) -> dict[str, Any]:
    raw = getattr(evt, "payload", evt)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _webspace_id(payload: Mapping[str, Any] | None = None) -> str:
    body = payload if isinstance(payload, Mapping) else {}
    meta = body.get("_meta") if isinstance(body.get("_meta"), Mapping) else {}
    raw = body.get("webspace_id") or body.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    return coerce_webspace_id(raw, fallback="default")


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
    if text.startswith("$event."):
        text = ""
    if len(text) > _MAX_CONTENT_CHARS:
        return text[:_MAX_CONTENT_CHARS]
    return text


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
        attachments = list(note.get("attachments") or [])
        items.append(
            {
                "id": note_id,
                "title": _note_heading(note),
                "subtitle": _now_iso(updated) if updated else "",
                "text": content,
                "preview": _note_card_preview(note),
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
    return {
        "ok": True,
        "selected_note_id": str(snap.get("selected_note_id") or ""),
        "items": list(notes.get("items") or []),
        "updated_at": snap.get("updated_at"),
    }


def _publish(snapshot: Mapping[str, Any] | None = None, *, webspace_id: str | None = None) -> dict[str, Any]:
    snap = dict(snapshot or _snapshot())
    ws = coerce_webspace_id(webspace_id, fallback="default")
    try:
        ctx_subnet.set("notebook.snapshot", snap, webspace_id=ws)
    except Exception:
        pass
    try:
        stream_publish(_RECEIVER_NOTES, _notes_stream_payload(snap), _meta={"webspace_id": ws})
    except Exception:
        pass
    return snap


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
    _ensure_default_note()
    snap = _publish(webspace_id=_webspace_id(body))
    return {"ok": True, "snapshot": snap}


@tool("create_note")
def create_note(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
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
    snap = _publish(webspace_id=_webspace_id(body))
    return {"ok": True, "note": deepcopy(note), "snapshot": snap}


@tool("select_note")
def select_note(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    note_id = _note_id_from_payload(body, fallback_selected=False)
    if note_id not in _STATE["notes"]:
        return {"ok": False, "error": "note_not_found", "note_id": note_id}
    _STATE["display_note_id"] = note_id
    if bool(body.get("edit")):
        _STATE["editing_note_id"] = note_id
    snap = _publish(webspace_id=_webspace_id(body))
    return {"ok": True, "note": deepcopy(_STATE["notes"][note_id]), "snapshot": snap}


@tool("save_note")
def save_note(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    note_id = _note_id_from_payload(body)
    note = _STATE["notes"].get(note_id)
    if not isinstance(note, dict):
        return {"ok": False, "error": "note_not_found", "note_id": note_id}
    note["content"] = _clean_content(body.get("content"))
    note["updated_at"] = _now()
    note["version"] = int(note.get("version") or 0) + 1
    _STATE["display_note_id"] = note_id
    _STATE["editing_note_id"] = note_id
    _promote_note(note_id)
    snap = _publish(webspace_id=_webspace_id(body))
    return {"ok": True, "note": deepcopy(note), "snapshot": snap}


@tool("attach_note_file")
def attach_note_file(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    note_id = _note_id_from_payload(body)
    note = _STATE["notes"].get(note_id)
    if not isinstance(note, dict):
        return {"ok": False, "error": "note_not_found", "note_id": note_id}
    artifact = body.get("artifact_ref") if isinstance(body.get("artifact_ref"), Mapping) else {}
    file_meta = body.get("file") if isinstance(body.get("file"), Mapping) else {}
    kind = "photo" if str(body.get("kind") or "").strip() == "photo" else "file"
    attachment = {
        "id": f"att-{int(_now() * 1000)}",
        "kind": kind,
        "name": str(file_meta.get("name") or artifact.get("filename") or artifact.get("name") or "attachment").strip(),
        "mime": str(file_meta.get("mime") or artifact.get("mime") or "").strip() or None,
        "size_bytes": file_meta.get("size_bytes") or artifact.get("size_bytes"),
        "artifact_ref": dict(artifact),
        "path": body.get("path") or artifact.get("path") or artifact.get("local_path") or artifact.get("stored_path"),
    }
    note.setdefault("attachments", []).append(attachment)
    note["updated_at"] = _now()
    note["version"] = int(note.get("version") or 0) + 1
    _STATE["display_note_id"] = note_id
    _STATE["editing_note_id"] = note_id
    _promote_note(note_id)
    snap = _publish(webspace_id=_webspace_id(body))
    return {"ok": True, "attachment": deepcopy(attachment), "note": deepcopy(note), "snapshot": snap}


@tool("delete_note")
def delete_note(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    note_id = _note_id_from_payload(body)
    if note_id not in _STATE["notes"]:
        return {"ok": False, "error": "note_not_found", "note_id": note_id}
    _STATE["notes"].pop(note_id, None)
    _STATE["order"] = [item for item in _STATE["order"] if item != note_id]
    _ensure_default_note()
    fallback = _STATE["order"][0]
    _STATE["display_note_id"] = fallback
    _STATE["editing_note_id"] = fallback
    snap = _publish(webspace_id=_webspace_id(body))
    return {"ok": True, "deleted_note_id": note_id, "snapshot": snap}


def _send_telegram_text(
    text: str,
    *,
    chat_id: str = "",
    bot_id: str = "",
    hub_id: str = "",
    root_base: str = "",
    webspace_id: str = "default",
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
        webspace_id=_webspace_id(body),
    )
    return {"ok": bool(result.get("ok")), "telegram": result, "note_id": note_id}


@tool("reset_notebook")
def reset_notebook(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload or {})
    body.update({k: v for k, v in kwargs.items() if v is not None})
    note = _default_note()
    _STATE["notes"] = {note["id"]: note}
    _STATE["order"] = [note["id"]]
    _STATE["display_note_id"] = note["id"]
    _STATE["editing_note_id"] = ""
    _STATE["next_id"] = 2
    snap = _publish(webspace_id=_webspace_id(body))
    return {"ok": True, "snapshot": snap}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    body = _payload(evt)
    if str(body.get("receiver") or "").strip() != _RECEIVER_NOTES:
        return
    _publish(webspace_id=_webspace_id(body))


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    body = _payload(evt)
    if str(body.get("receiver") or "").strip() != _RECEIVER_NOTES:
        return
    if str(body.get("action") or "").strip().lower() == "unsubscribed":
        return
    _publish(webspace_id=_webspace_id(body))
