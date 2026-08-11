from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool
from adaos.sdk.builder import preview as builder_preview
from adaos.sdk.developer import compositions


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from research.orchestrator import ResearchOrchestrator
from research.repository import OrchestratorRepository


def _orchestrator() -> ResearchOrchestrator:
    return ResearchOrchestrator()


@tool(summary="Ensure the durable formulation schema.", side_effects="local_write")
def ensure_schema() -> dict[str, Any]:
    repository = OrchestratorRepository()
    return {"ok": True, "binding": repository._db.binding.to_dict(), "health": dict(repository._db.health())}


@tool(summary="Rehydrate the durable research-orchestrator ledger.", side_effects="local_write")
def rehydrate() -> dict[str, Any]:
    return ensure_schema()


@tool(summary="List local research Projects with durable formulation status.", side_effects="none")
def list_directions(limit: int = 500, **_: Any) -> dict[str, Any]:
    return _orchestrator().list_directions(limit=limit)


@tool(summary="Resolve a research-direction focus from the paired Builder selection.", side_effects="none")
def resolve_focus(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    metadata = _meta if isinstance(_meta, Mapping) else {}
    candidate = str(webspace_id or metadata.get("webspace_id") or metadata.get("source_webspace_id") or "desktop").strip() or "desktop"
    current_scenario = str(metadata.get("scenario_id") or metadata.get("current_scenario") or "research_workbench").strip()
    try:
        source = builder_preview.action_source_webspace_id(candidate, current_scenario_id=current_scenario)
        binding = builder_preview.get_binding(source)
        selection = binding.get("selection") if isinstance(binding.get("selection"), Mapping) else {}
        kind = str(selection.get("object_type") or "").strip().lower().rstrip("s")
        direction_id = str(selection.get("object_id") or "").strip()
        project = compositions.project_for_component(f"skill:{direction_id}") if kind == "skill" and direction_id else None
        if not project or "adaos.research.direction.v1" not in set(project.get("profiles") or []):
            raise ValueError("paired Builder selection is not a research direction")
        state = _orchestrator().get(direction_id)
        return {
            "ok": True,
            "selected": True,
            "direction_id": direction_id,
            "title": state["direction"].get("title") or direction_id,
            "conversation_id": f"conv.skill.research_orchestrator_skill.{direction_id}.desktop",
            "source_webspace_id": source,
            "message": f"Focused from Builder: skill:{direction_id}",
        }
    except (RuntimeError, ValueError):
        return {
            "ok": True,
            "selected": False,
            "message": "Choose a research direction from the portfolio.",
        }


@tool(summary="Create one Project and its primary research-direction skill atomically.", side_effects="local_write")
def create_direction(
    project_id: str,
    title: str,
    description: str = "",
    skill_id: str | None = None,
    tags: list[str] | None = None,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().create_direction(
        project_id,
        title,
        description=description,
        skill_id=skill_id,
        tags=tags,
        actor=actor,
    )


@tool(summary="Initialize one research direction skill project.", side_effects="local_write")
def initialize_direction(direction_id: str, title: str, actor: str = "user:local", **_: Any) -> dict[str, Any]:
    return _orchestrator().initialize(direction_id, title, actor=actor)


@tool(summary="Attach one immutable source artifact.", side_effects="local_write")
def attach_source(
    direction_id: str,
    path: str,
    group_id: str = "part0",
    name: str | None = None,
    role: str = "source",
    actor: str = "user:local",
    cleanup_staging: bool = False,
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().attach_source(
        direction_id,
        path,
        group_id=group_id,
        name=name,
        role=role,
        actor=actor,
        cleanup_staging=cleanup_staging,
    )


@tool(summary="Read canonical pre-Codex research direction state.", side_effects="none")
def get_direction(direction_id: str, **_: Any) -> dict[str, Any]:
    try:
        result = _orchestrator().get(direction_id)
        direction = result["direction"]
        bundle = result["source_bundle"]
        prototype = result.get("current_prototype") or {}
        sources = "\n".join(
            f"- `{item.get('name')}` — `{item.get('digest')}` ({item.get('role')})"
            for item in bundle.get("sources") or []
        ) or "- исходники пока не добавлены"
        steps = "\n".join(
            f"{index + 1}. **{item.get('label')}** — {item.get('reason')}"
            for index, item in enumerate(result.get("next_steps") or [])
        )
        result["content"] = (
            f"## {direction.get('title')}\n\n"
            f"**Стадия:** `{direction.get('status')}` · **generation:** `{direction.get('generation')}`\n\n"
            f"**SourceBundle:** `{bundle.get('digest')}`\n\n{sources}\n\n"
            f"### Текущая постановка\n\n"
            f"{prototype.get('research_question') or 'Ещё не сформулирована.'}\n\n"
            f"### Следующие шаги\n\n{steps or 'Нет доступных шагов.'}"
        )
        result["conversation_id"] = f"conv.skill.research_orchestrator_skill.{direction_id}.desktop"
        return result
    except ValueError as exc:
        return {"ok": False, "initialized": False, "direction_id": direction_id, "message": str(exc), "next_steps": [{"id": "initialize", "label": "Инициализировать направление", "reason": "ОИ ещё не связал этот skill project с formulation ledger."}]}


@tool(summary="List manifested source artifacts for one direction.", side_effects="none")
def list_artifacts(direction_id: str, **_: Any) -> dict[str, Any]:
    state = _orchestrator().get(direction_id)
    items = []
    for group in state.get("artifact_groups") or []:
        for item in group.get("items") or []:
            items.append(
                {
                    **dict(item),
                    "id": item.get("artifact_id"),
                    "title": item.get("path"),
                    "subtitle": f"{group.get('group_id')} · {item.get('role')} · {item.get('size_bytes')} bytes",
                    "preview": item.get("digest"),
                    "group_id": group.get("group_id"),
                }
            )
    return {"ok": True, "direction_id": direction_id, "items": items, "count": len(items)}


@tool(summary="Synchronize manifested artifact groups into orchestration state.", side_effects="local_write")
def sync_source_bundle(direction_id: str, actor: str = "user:local", **_: Any) -> dict[str, Any]:
    return _orchestrator().sync_source_bundle(direction_id, actor=actor)


@tool(summary="Record a schema-valid ResearchPrototype candidate.", side_effects="local_write")
def record_prototype(direction_id: str, prototype: Mapping[str, Any], actor: str = "user:local", **_: Any) -> dict[str, Any]:
    return _orchestrator().record_prototype(direction_id, prototype, actor=actor)


@tool(summary="Discuss and materialize a ResearchPrototype through the configured Root LLM.", side_effects="local_write")
def chat(direction_id: str, text: str, model: str | None = None, actor: str = "user:local", **payload: Any) -> dict[str, Any]:
    return _orchestrator().discuss(direction_id, text, model=model, actor=actor, dialog_payload=payload)


@tool(summary="Accept an exact ResearchPrototype and produce the pre-Codex AutomationBrief.", side_effects="external_write")
def accept_prototype(
    direction_id: str,
    prototype_digest: str,
    expected_generation: int,
    idempotency_key: str,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().accept(
        direction_id,
        prototype_digest,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key,
        actor=actor,
    )


@tool(summary="Read the accepted digest-bound AutomationBrief.", side_effects="none")
def get_automation_brief(direction_id: str, **_: Any) -> dict[str, Any]:
    state = _orchestrator().get(direction_id)
    brief = state.get("automation_brief")
    return {"ok": brief is not None, "direction": state["direction"], "automation_brief": brief, "content": __import__("json").dumps(brief, ensure_ascii=False, indent=2) if brief else "Automation Brief ещё не сформирован.", "language": "json", "codex_started": False}


@tool(summary="Bind and open the exact pre-Codex Development Session in Builder.", side_effects="local_write")
def open_builder_session(
    direction_id: str,
    builder_webspace_id: str = "desktop",
    base_url: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().open_builder_session(
        direction_id,
        builder_webspace_id=builder_webspace_id,
        base_url=base_url,
    )


@tool(summary="Read durable research formulation activity.", side_effects="none")
def get_activity(direction_id: str, limit: int = 200, **_: Any) -> dict[str, Any]:
    repository = OrchestratorRepository()
    events = repository.activities(direction_id, limit)
    content = "\n".join(f"- `{item['seq']:03d}` **{item['stage']} / {item['status']}** — {item['message']}" for item in events)
    return {"ok": True, "direction_id": direction_id, "events": events, "content": content or "Событий пока нет."}


@tool(summary="Explain the next admitted steps for text or voice surfaces.", side_effects="none")
def next_steps(direction_id: str, **_: Any) -> dict[str, Any]:
    state = _orchestrator().get(direction_id)
    steps = list(state.get("next_steps") or [])
    message = " ".join(f"{index + 1}. {item['label']}: {item['reason']}" for index, item in enumerate(steps))
    return {"ok": True, "direction_id": direction_id, "message": message, "speech_text": message, "steps": steps}
