from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.builder import automation as builder_automation
from adaos.sdk.core.decorators import tool


def _webspace_id(webspace_id: str | None, meta: Mapping[str, Any] | None) -> str:
    metadata = meta if isinstance(meta, Mapping) else {}
    return str(webspace_id or metadata.get("webspace_id") or metadata.get("source_webspace_id") or "desktop").strip() or "desktop"


def _locale(meta: Mapping[str, Any] | None) -> str:
    metadata = meta if isinstance(meta, Mapping) else {}
    value = str(metadata.get("locale") or metadata.get("language") or "ru").lower()
    return "en" if value.startswith("en") else "ru"


def _message(status: str, *, locale: str, iteration: int = 0) -> str:
    messages = {
        "ru": {
            "queued": "Задача автоматизации поставлена в очередь.",
            "automation_queued": f"Итерация {iteration} поставлена в очередь.",
            "automation_busy": "Текущая итерация ещё выполняется. Новое уточнение можно отправить после её завершения.",
            "completed": "Автоматизация завершена. Можно отправить следующее уточнение.",
            "failed": "Автоматизация завершилась с ошибкой. Откройте состояние задачи для диагностики.",
            "idle": "Для выбранного проекта ещё нет сессии автоматизации.",
            "status": "Состояние автоматизации обновлено.",
        },
        "en": {
            "queued": "The automation task has been queued.",
            "automation_queued": f"Iteration {iteration} has been queued.",
            "automation_busy": "The current iteration is still running. Submit the next instruction after it finishes.",
            "completed": "Automation is complete. You can submit another instruction.",
            "failed": "Automation failed. Open the task state for diagnostics.",
            "idle": "The selected project does not have an automation session yet.",
            "status": "Automation state refreshed.",
        },
    }
    return messages[locale].get(status, messages[locale]["status"])


def _response(result: Mapping[str, Any], *, locale: str, fallback_status: str) -> dict[str, Any]:
    automation = result.get("automation") if isinstance(result.get("automation"), Mapping) else {}
    raw_status = str(result.get("status") or automation.get("status") or fallback_status)
    message_status = raw_status
    if raw_status == "automation_session_not_found":
        message_status = "idle"
    elif raw_status in {"starting", "queued"}:
        message_status = "queued"
    elif raw_status in {"cancelled", "expired"}:
        message_status = "failed"
    response = {
        "ok": bool(result.get("ok")),
        "handled": bool(result.get("handled", result.get("ok"))),
        "status": raw_status,
        "message": _message(
            message_status,
            locale=locale,
            iteration=int(automation.get("iteration") or 0),
        ),
        "automation": dict(automation),
    }
    if result.get("error"):
        response["error"] = str(result["error"])
    if isinstance(result.get("task"), Mapping):
        response["task"] = dict(result["task"])
    return response


@tool(summary="Start Builder Automation from an implementation brief.", side_effects="local_write")
def start(
    object_type: str,
    object_id: str,
    implementation_brief: str,
    webspace_id: str | None = None,
    conversation_id: str | None = None,
    brief_path: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = builder_automation.start(
        object_type=object_type,
        object_id=object_id,
        implementation_brief=implementation_brief,
        webspace_id=_webspace_id(webspace_id, _meta),
        conversation_id=conversation_id,
        brief_path=brief_path,
    )
    return _response(result, locale=_locale(_meta), fallback_status="queued")


@tool(summary="Submit a Builder Automation implementation turn.", side_effects="local_write")
def chat(
    text: str,
    object_type: str | None = None,
    object_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = builder_automation.submit(
        text=text,
        object_type=object_type,
        object_id=object_id,
        webspace_id=_webspace_id(webspace_id, _meta),
    )
    if not result.get("automation"):
        state = builder_automation.get_state(
            object_type=object_type,
            object_id=object_id,
            webspace_id=_webspace_id(webspace_id, _meta),
        )
        result = {**result, "automation": state.get("automation")}
    return _response(result, locale=_locale(_meta), fallback_status="automation_session_not_found")


@tool(summary="Get the current Builder Automation projection.", side_effects="none")
def get_state(
    object_type: str | None = None,
    object_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = builder_automation.get_state(
        object_type=object_type,
        object_id=object_id,
        webspace_id=_webspace_id(webspace_id, _meta),
    )
    if not result.get("ok"):
        result = {**result, "status": str(result.get("error") or "automation_session_not_found")}
    return _response(result, locale=_locale(_meta), fallback_status="idle")


__all__ = ["chat", "get_state", "start"]
