"""Deterministic, local tools for the Workflow Lab dashboard."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from adaos.sdk.core.decorators import tool


_REQUESTS: tuple[dict[str, Any], ...] = (
    {
        "id": "req-101",
        "title": "Onboard vendor portal • Подключить портал поставщика",
        "status": "in_review",
        "statusLabel": "In review • На проверке",
        "summary": "Security review pending. • Ожидается проверка безопасности.",
        "priority": 3,
        "ownerLabel": "A. Rivera",
        "updatedAt": "2026-08-05T14:20:00Z",
        "updatedAtLabel": "5 Aug 2026 • 5 авг. 2026",
    },
    {
        "id": "req-102",
        "title": "Migrate billing jobs • Перенести задания биллинга",
        "status": "blocked",
        "statusLabel": "Blocked • Заблокировано",
        "summary": "Waiting on an infrastructure window. • Ожидается окно инфраструктуры.",
        "priority": 5,
        "ownerLabel": "I. Petrov",
        "updatedAt": "2026-08-05T09:10:00Z",
        "updatedAtLabel": "5 Aug 2026 • 5 авг. 2026",
    },
    {
        "id": "req-103",
        "title": "A/B experiment setup • Настроить A/B-эксперимент",
        "status": "in_progress",
        "statusLabel": "In progress • В работе",
        "summary": "Variant allocation QA. • Проверка распределения вариантов.",
        "priority": 2,
        "ownerLabel": "M. Chen",
        "updatedAt": "2026-08-04T18:00:00Z",
        "updatedAtLabel": "4 Aug 2026 • 4 авг. 2026",
    },
    {
        "id": "req-104",
        "title": "Auth policy update • Обновить политику доступа",
        "status": "pending",
        "statusLabel": "Pending • Ожидает",
        "summary": "Needs an approver assignment. • Требуется назначить согласующего.",
        "priority": 1,
        "ownerLabel": "Unassigned • Не назначен",
        "updatedAt": "2026-08-03T12:00:00Z",
        "updatedAtLabel": "3 Aug 2026 • 3 авг. 2026",
    },
)

_ACTIONS: dict[str, tuple[dict[str, str], ...]] = {
    "in_review": (
        {"id": "approve", "label": "Approve • Утвердить", "nextStatus": "approved"},
        {"id": "request_changes", "label": "Request changes • Запросить правки", "nextStatus": "changes_requested"},
    ),
    "blocked": (
        {"id": "unblock", "label": "Unblock • Разблокировать", "nextStatus": "in_progress"},
    ),
    "pending": (
        {"id": "start_work", "label": "Start work • Начать работу", "nextStatus": "in_progress"},
        {"id": "assign", "label": "Assign approver • Назначить согласующего", "nextStatus": "in_review"},
    ),
    "in_progress": (
        {"id": "send_review", "label": "Send to review • Отправить на проверку", "nextStatus": "in_review"},
    ),
    "approved": (),
    "changes_requested": (
        {"id": "start_work", "label": "Start work • Начать работу", "nextStatus": "in_progress"},
    ),
}

_STATUS_LABELS = {
    "approved": "Approved • Утверждено",
    "changes_requested": "Changes requested • Нужны правки",
    "in_progress": "In progress • В работе",
    "in_review": "In review • На проверке",
}


def _request(request_id: str) -> dict[str, Any]:
    for item in _REQUESTS:
        if item["id"] == request_id:
            return deepcopy(item)
    raise ValueError(f"unknown request_id: {request_id}")


@tool(summary="Return deterministic local dashboard records.", side_effects="none")
def get_dashboard(selected_request_id: str = "req-101") -> dict[str, Any]:
    selected = _request(selected_request_id)
    return {
        "ok": True,
        "source": "local_fixture",
        "requests": deepcopy(list(_REQUESTS)),
        "selectedRequest": selected,
        "nextActions": deepcopy(list(_ACTIONS[selected["status"]])),
    }


@tool(summary="Preview a valid request transition without external writes.", side_effects="none")
def apply_action(request_id: str, action: str) -> dict[str, Any]:
    current = _request(str(request_id).strip())
    action_id = str(action).strip()
    allowed = {item["id"]: item for item in _ACTIONS[current["status"]]}
    if action_id not in allowed:
        return {
            "ok": False,
            "error": "action_not_allowed",
            "request": current,
            "nextActions": deepcopy(list(allowed.values())),
        }
    next_status = allowed[action_id]["nextStatus"]
    current["status"] = next_status
    current["statusLabel"] = _STATUS_LABELS[next_status]
    return {
        "ok": True,
        "source": "local_fixture",
        "appliedAction": action_id,
        "request": current,
        "nextActions": deepcopy(list(_ACTIONS[next_status])),
    }
