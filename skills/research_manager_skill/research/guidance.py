"""Channel-neutral, workflow-aware guidance for governed experiments."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


_STATE_ACTIONS = (
    ("draft", ("edit_conditions", "submit_review")),
    ("review", ("edit_conditions", "lock")),
    ("locked", ("start_preflight", "start_confirmatory")),
    ("running", ("reconcile", "cancel")),
    ("cancelling", ("reconcile",)),
    ("results_ready", ("inspect_results", "finalize")),
    ("failed", ("inspect_attempts", "retry")),
    ("cancelled", ("inspect_attempts", "retry")),
    ("finalized", ("verify_evidence", "inspect_tracker")),
)

_COPY = {
    "en": {
        "overview": "This workbench governs one reproducible experiment from editable conditions to immutable evidence.",
        "states": {
            "draft": "The conditions are editable and have not yet entered review.",
            "review": "Review the current immutable revision before locking it.",
            "locked": "The revision is locked and can be submitted to an execution profile.",
            "running": "Physical attempts are active or awaiting reconciliation.",
            "cancelling": "Cancellation was requested; reconcile until every attempt is terminal.",
            "results_ready": "All current runs succeeded and the paired result can be reviewed.",
            "failed": "At least one current physical attempt failed or was lost.",
            "cancelled": "The current physical attempts are cancelled.",
            "finalized": "The result and normalized tracker export are immutable evidence.",
        },
        "actions": {
            "edit_conditions": ("Review or edit conditions", "Check data, arms, profiles, randomization and analysis before review."),
            "submit_review": ("Submit for review", "Freeze the current revision as the review candidate."),
            "lock": ("Lock the revision", "Accept the reviewed conditions and prevent in-place mutation."),
            "start_preflight": ("Start CPU preflight", "Run the bounded workflow-validation profile first."),
            "start_confirmatory": ("Start confirmatory run", "Use only when the protocol and compute budget are ready."),
            "reconcile": ("Reconcile attempts", "Ingest progress, terminal states, observations and artifacts."),
            "cancel": ("Cancel active attempts", "Request cooperative cancellation without changing logical run identity."),
            "inspect_results": ("Inspect paired results", "Review the primary contrast and initialization lineage."),
            "finalize": ("Finalize evidence", "Fix the result only after tracker delivery and artifact checks pass."),
            "inspect_attempts": ("Inspect attempts", "Identify failed, cancelled or lost physical attempts."),
            "retry": ("Retry a run", "Create a new physical attempt while preserving trial and run identity."),
            "verify_evidence": ("Verify immutable evidence", "Recompute tracker and artifact digest checks."),
            "inspect_tracker": ("Inspect tracker", "Use the provider UI for detailed telemetry, not as governance truth."),
        },
    },
    "ru": {
        "overview": "Этот рабочий стол управляет одним воспроизводимым экспериментом: от редактируемых условий до неизменяемого свидетельства.",
        "states": {
            "draft": "Условия можно редактировать; ревью еще не начато.",
            "review": "Проверьте текущую неизменяемую ревизию перед блокировкой.",
            "locked": "Ревизия заблокирована и готова к запуску выбранного профиля.",
            "running": "Физические попытки выполняются или ожидают сверки.",
            "cancelling": "Отмена запрошена; выполняйте сверку до терминального состояния всех попыток.",
            "results_ready": "Текущие запуски завершены, парный результат готов к проверке.",
            "failed": "Как минимум одна текущая физическая попытка завершилась ошибкой или потеряна.",
            "cancelled": "Текущие физические попытки отменены.",
            "finalized": "Результат и нормализованный экспорт трекера зафиксированы как неизменяемое свидетельство.",
        },
        "actions": {
            "edit_conditions": ("Проверить или изменить условия", "Проверьте данные, варианты, профили, рандомизацию и анализ до ревью."),
            "submit_review": ("Отправить на ревью", "Зафиксировать текущую ревизию как кандидата для проверки."),
            "lock": ("Заблокировать ревизию", "Принять проверенные условия и запретить изменение на месте."),
            "start_preflight": ("Запустить CPU preflight", "Сначала выполните ограниченный профиль проверки workflow."),
            "start_confirmatory": ("Запустить подтверждающий профиль", "Используйте его только при готовом протоколе и бюджете."),
            "reconcile": ("Сверить попытки", "Загрузить прогресс, состояния, наблюдения и артефакты."),
            "cancel": ("Отменить активные попытки", "Запросить кооперативную отмену без смены идентичности логического запуска."),
            "inspect_results": ("Проверить парные результаты", "Проверьте основной контраст и общую инициализацию."),
            "finalize": ("Зафиксировать свидетельство", "Фиксируйте результат после доставки трекера и проверки артефактов."),
            "inspect_attempts": ("Проверить попытки", "Найдите ошибочные, отмененные или потерянные физические попытки."),
            "retry": ("Повторить запуск", "Создать новую физическую попытку, сохранив Trial и Run."),
            "verify_evidence": ("Проверить свидетельство", "Повторно проверить дайджесты экспорта трекера и артефактов."),
            "inspect_tracker": ("Открыть трекер", "Используйте UI провайдера для телеметрии, но не как источник governance truth."),
        },
    },
}

_TOOLS = {
    "submit_review": "submit_experiment_review",
    "lock": "lock_experiment",
    "start_preflight": "start_experiment",
    "start_confirmatory": "start_experiment",
    "reconcile": "reconcile_experiment",
    "cancel": "cancel_experiment",
    "retry": "retry_run",
    "finalize": "finalize_experiment",
    "verify_evidence": "verify_experiment_result",
}


def describe(
    status: Mapping[str, Any],
    *,
    locale: str = "ru",
    channel: str = "text",
    section: str = "all",
    available_actions: Sequence[str] | None = None,
) -> dict[str, Any]:
    selected_locale = "ru" if str(locale).lower().startswith("ru") else "en"
    selected_channel = str(channel or "text").lower()
    if selected_channel not in {"web", "text", "voice"}:
        selected_channel = "text"
    lifecycle = dict(status.get("lifecycle") or {})
    state = str(lifecycle.get("state") or "draft")
    copy = _COPY[selected_locale]
    actions = []
    state_actions = next((items for key, items in _STATE_ACTIONS if key == state), ())
    allowed_actions = (
        {str(item).strip() for item in available_actions if str(item).strip()}
        if available_actions is not None
        else None
    )
    for priority, action_id in enumerate(state_actions, start=1):
        if allowed_actions is not None and action_id not in allowed_actions:
            continue
        label, description = copy["actions"][action_id]
        actions.append(
            {
                "id": action_id,
                "label": label,
                "description": description,
                "tool": _TOOLS.get(action_id),
                "priority": priority,
            }
        )
    state_text = copy["states"].get(state, state)
    next_text = " ".join(f"{index}. {item['label']}." for index, item in enumerate(actions, start=1))
    speech_text = f"{state_text} " + (
        ("Следующие шаги: " if selected_locale == "ru" else "Next steps: ") + next_text
        if actions
        else ("Дальнейших действий нет." if selected_locale == "ru" else "There are no further actions.")
    )
    overview = str(copy["overview"])
    text = overview if section == "overview" else speech_text if section == "next_steps" else f"{overview}\n\n{speech_text}"
    return {
        "schema": "adaos.scenario.guidance_projection.v1",
        "ok": True,
        "locale": selected_locale,
        "channel": selected_channel,
        "section": section,
        "overview": overview,
        "workflow": {
            "state": state,
            "generation": int(lifecycle.get("generation") or 0),
            "description": state_text,
        },
        "next_actions": actions,
        "text": text,
        "speech_text": speech_text,
        "message": speech_text if selected_channel == "voice" else text,
    }


__all__ = ["describe"]
