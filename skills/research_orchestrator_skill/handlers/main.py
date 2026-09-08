from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool
from adaos.sdk.core.environment import runtime_identity
from adaos.sdk.builder import preview as builder_preview
from adaos.sdk.developer import artifact_context, compositions


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from research.orchestrator import ResearchOrchestrator  # noqa: E402
from research.repository import OrchestratorRepository  # noqa: E402
from research.compiler import project_execution_compilation  # noqa: E402
from research.contracts import project_execution_automation_brief  # noqa: E402


def _orchestrator() -> ResearchOrchestrator:
    return ResearchOrchestrator()


@tool(summary="Ensure the durable formulation schema.", side_effects="local_write")
def ensure_schema() -> dict[str, Any]:
    repository = OrchestratorRepository()
    return {"ok": True, "binding": repository._db.binding.to_dict(), "health": dict(repository._db.health())}


@tool(summary="Project audit contracts into compact digest-bound development inputs.", side_effects="none")
def project_execution_contracts(
    compilation: Mapping[str, Any],
    automation_brief: Mapping[str, Any],
    **_: Any,
) -> dict[str, Any]:
    scientific = project_execution_compilation(compilation)
    engineering = project_execution_automation_brief(
        automation_brief,
        compilation_projection_digest=str(scientific["digest"]),
        protocol_digest=str(scientific["traceability"]["protocol_digest"]),
    )
    return {
        "ok": True,
        "research_compilation": scientific,
        "automation_brief": engineering,
        "audit_compilation_digest": compilation["digest"],
        "audit_automation_brief_digest": automation_brief["digest"],
        "runtime_identity": runtime_identity(),
    }


@tool(summary="Rehydrate the durable research-orchestrator ledger.", side_effects="local_write")
def rehydrate() -> dict[str, Any]:
    return ensure_schema()


@tool(summary="List durable research directions from the authoritative research index.", side_effects="none")
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


@tool(summary="Create one research direction and its artifact-custodian skill atomically.", side_effects="local_write")
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


@tool(summary="Adopt a legacy direction custodian into the research-domain index.", side_effects="local_write")
def initialize_direction(direction_id: str, title: str, actor: str = "user:local", **_: Any) -> dict[str, Any]:
    return _orchestrator().initialize(direction_id, title, actor=actor)


@tool(summary="Create a bounded ResearchTask inside one research direction.", side_effects="local_write")
def create_task(
    direction_id: str,
    title: str,
    task_id: str | None = None,
    research_question: str = "",
    parent_task_id: str | None = None,
    branch_of_task_id: str | None = None,
    dependency_refs: list[str] | None = None,
    activate: bool = False,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().create_task(
        direction_id,
        title,
        task_id=task_id,
        research_question=research_question,
        parent_task_id=parent_task_id,
        branch_of_task_id=branch_of_task_id,
        dependency_refs=dependency_refs,
        activate=activate,
        actor=actor,
    )


@tool(summary="Select the ResearchTask that receives subsequent formulation writes.", side_effects="local_write")
def select_active_task(
    direction_id: str,
    task_id: str,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().select_active_task(direction_id, task_id, actor=actor)


@tool(summary="Attach one immutable source artifact.", side_effects="local_write")
def attach_source(
    direction_id: str,
    path: str,
    group_id: str = "part0",
    name: str | None = None,
    role: str = "source",
    visibility_profile: str = "shared",
    actor: str = "user:local",
    cleanup_staging: bool = False,
    replace_existing: bool = False,
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().attach_source(
        direction_id,
        path,
        group_id=group_id,
        name=name,
        role=role,
        visibility_profile=visibility_profile,
        actor=actor,
        cleanup_staging=cleanup_staging,
        replace_existing=replace_existing,
    )


@tool(summary="Set one source artifact's research-stage visibility profile.", side_effects="local_write")
def set_source_visibility(
    direction_id: str,
    group_id: str,
    artifact_id: str,
    visibility_profile: str,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().set_source_visibility(
        direction_id,
        group_id,
        artifact_id,
        visibility_profile,
        actor=actor,
    )


@tool(summary="Read canonical pre-Codex research direction state.", side_effects="none")
def get_direction(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    try:
        result = _orchestrator().get(
            direction_id,
            task_id=task_id,
            implementation_track_id=implementation_track_id,
        )
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


@tool(summary="Read a typed Direction to Task to Implementation navigation outline.", side_effects="none")
def get_outline(direction_id: str, **_: Any) -> dict[str, Any]:
    return _orchestrator().outline(direction_id)


@tool(summary="Adopt an immutable evaluator-owned calibration lineage.", side_effects="local_write")
def adopt_calibration_lineage(
    direction_id: str,
    evaluator_task_id: str,
    budget_view: str = "fixed_downstream",
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().adopt_calibration_lineage(
        direction_id,
        evaluator_task_id,
        budget_view=budget_view,
        actor=actor,
    )


@tool(summary="Read a joined source-to-result research lineage.", side_effects="none")
def get_lineage(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().lineage(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
    )


@tool(summary="Read Study references connected to one ResearchTask.", side_effects="none")
def get_studies(
    direction_id: str,
    task_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    state = _orchestrator().get(direction_id, task_id=task_id)
    task = state.get("selected_task") or state.get("active_task") or {}
    studies = list((task.get("metadata") or {}).get("matched_studies") or [])
    lines = [
        (
            f"- `{item.get('ref')}` · **{item.get('status') or 'unknown'}**  \n"
            f"Owner: `{item.get('owner_ref')}` · endpoint: `{item.get('primary_endpoint') or 'not declared'}`  \n"
            f"Task digest: `{item.get('external_task_digest')}` · summary: `{item.get('summary_digest')}`"
        )
        for item in studies
    ]
    return {
        "ok": True,
        "direction_ref": state["direction"].get("ref"),
        "task_ref": task.get("ref"),
        "items": studies,
        "count": len(studies),
        "content": "## Studies\n\n" + ("\n\n".join(lines) if lines else "No Study refs are connected to this task."),
    }


@tool(summary="List manifested source artifacts for one direction.", side_effects="none")
def list_artifacts(direction_id: str, **_: Any) -> dict[str, Any]:
    state = _orchestrator().get(direction_id)
    items = []
    for group in state.get("artifact_groups") or []:
        for item in group.get("items") or []:
            path = str(item.get("path") or "")
            media_type = str(item.get("media_type") or "application/octet-stream").lower()
            suffix = Path(path).suffix.lower()
            preview_kind = (
                "markdown"
                if media_type in {"text/markdown", "text/x-markdown"} or suffix in {".md", ".markdown"}
                else "pdf"
                if media_type == "application/pdf" or suffix == ".pdf"
                else "text"
                if media_type.startswith("text/") or suffix in {".txt", ".json", ".yaml", ".yml", ".py", ".ipynb"}
                else "unsupported"
            )
            policy = item.get("context_policy") if isinstance(item.get("context_policy"), Mapping) else {}
            allow = set(str(value) for value in policy.get("allow") or [])
            visibility_profile = (
                "shared"
                if not policy or policy.get("default") == "allow"
                else "evaluation_only"
                if allow == {"research.evaluation"}
                else "formulation_only"
                if allow == {"research.formulation"}
                else "implementation_input"
                if allow == {"research.implementation", "research.evaluation"}
                else "custom"
            )
            items.append(
                {
                    **dict(item),
                    "id": item.get("artifact_id"),
                    "title": item.get("path"),
                    "subtitle": f"{group.get('group_id')} · {item.get('role')} · {item.get('size_bytes')} bytes",
                    "preview": item.get("digest"),
                    "group_id": group.get("group_id"),
                    "preview_kind": preview_kind,
                    "context_policy": item.get("context_policy"),
                    "visibility_profile": visibility_profile,
                }
            )
    return {
        "ok": True,
        "initialized": bool(state.get("initialized", True)),
        "direction_id": direction_id,
        "items": items,
        "count": len(items),
    }


@tool(summary="Preview one manifested text, Markdown, or PDF artifact.", side_effects="none")
def preview_artifact(direction_id: str, group_id: str, artifact_id: str, **_: Any) -> dict[str, Any]:
    state = _orchestrator().get(direction_id)
    skill_id = str(state["direction"]["artifact_owner_ref"]).partition(":")[2]
    resolved = artifact_context.resolve(skill_id, group_id, artifact_id)
    path = str(resolved.get("path") or artifact_id)
    media_type = str(resolved.get("media_type") or "application/octet-stream").lower()
    suffix = Path(path).suffix.lower()
    base = {
        "ok": True,
        "direction_id": direction_id,
        "artifact_id": artifact_id,
        "group_id": group_id,
        "title": path,
        "media_type": media_type,
    }
    if media_type in {"text/markdown", "text/x-markdown"} or suffix in {".md", ".markdown"}:
        return {**base, "preview_kind": "markdown", "content": artifact_context.read_text(skill_id, group_id, artifact_id)}
    if media_type == "application/pdf" or suffix == ".pdf":
        return {
            **base,
            "preview_kind": "pdf",
            "content_path": (
                f"/api/builder/projects/skill/{skill_id}/artifacts/{group_id}/{artifact_id}/content"
            ),
        }
    if media_type.startswith("text/") or suffix in {".txt", ".json", ".yaml", ".yml", ".py", ".ipynb"}:
        language = {
            ".json": "json",
            ".ipynb": "json",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".py": "python",
        }.get(suffix, "text")
        return {
            **base,
            "preview_kind": "text",
            "content": artifact_context.read_text(skill_id, group_id, artifact_id),
            "language": language,
        }
    return {
        **base,
        "ok": False,
        "preview_kind": "unsupported",
        "content": f"Preview is not available for `{media_type}`. The artifact remains digest-bound and available to Builder/Codex.",
    }


def _markdown_lines(items: Any, *, empty: str = "—") -> str:
    values = list(items or []) if isinstance(items, (list, tuple)) else []
    return "\n".join(f"- {item}" for item in values) or empty


@tool(summary="Read the live typed scientific inquiry projection and disposition gate.", side_effects="none")
def get_inquiry_projection(
    direction_id: str,
    task_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    result = _orchestrator().get_inquiry_projection(direction_id, task_id=task_id)
    projection = result["projection"]
    records = projection["records"]
    readiness = projection["readiness"]
    measures = projection["measures"]

    def active(collection: str) -> list[Mapping[str, Any]]:
        return [
            item
            for item in records[collection]
            if item.get("status") in {"proposed", "contested"}
        ]

    frames = [
        f"**{item['id']}** — {item['statement']}  \n"
        f"Scope: `{item['attributes'].get('scope', '—')}` · derivation: `{item['derivation']}`"
        for item in active("problem_frames")
    ]
    dispositions = [
        f"**{item['id']}** → `{item['attributes'].get('disposition', 'unresolved')}` "
        f"(`{item['attributes'].get('assessment_status', 'provisional')}`)  \n"
        f"{item['attributes'].get('rationale') or item['statement']}  \n"
        f"Reconsider when: `{item['attributes'].get('reconsideration_conditions') or []}`"
        for item in active("problem_dispositions")
    ]
    claims = [
        f"**{item['id']}** `{item['attributes'].get('epistemic_status', item['derivation'])}` — "
        f"{item['statement']}"
        for item in active("knowledge_claims")
    ]
    questions = [
        f"**{item['id']}** — {item['statement']}"
        for item in [*active("research_questions"), *active("hypotheses")]
    ]
    tasks = [
        f"**{item['id']}** `{item['attributes'].get('task_kind', 'unknown')}` — "
        f"{item['attributes'].get('objective') or item['statement']}"
        for item in active("task_candidates")
    ]
    searches = [
        f"**{item['id']}** - `{item['attributes'].get('query', '')}`  \n"
        f"Stop: {item['attributes'].get('stop_rule', '-')}"
        for item in active("search_requests")
    ]
    discoveries = list(result.get("source_discoveries") or [])
    latest_discovery = discoveries[-1] if discoveries else {}
    candidates = [
        f"**{item.get('title') or item.get('candidate_id')}** "
        f"`{item.get('discovery_status')}`  \n"
        f"{item.get('url')}  \n"
        f"DOI: `{(item.get('identifiers') or {}).get('doi') or '-'}` - "
        f"OA: `{(item.get('open_access') or {}).get('status') or 'unknown'}` - "
        f"{item.get('relevance') or ''}"
        for item in latest_discovery.get("candidates") or []
    ]
    latest_diff = None
    for activity in reversed(OrchestratorRepository().activities(direction_id, limit=100)):
        detail = activity.get("detail") if isinstance(activity.get("detail"), Mapping) else {}
        if detail.get("semantic_diff"):
            latest_diff = detail["semantic_diff"]
            break
    changes = [
        f"`{item.get('action')}` {item.get('target_type')} **{item.get('target_id')}**"
        for item in (latest_diff or {}).get("changes") or []
    ]
    content = (
        f"## Scientific projection · revision {projection['revision']}\n\n"
        f"**Disposition gate:** `{readiness['decision']}`  \n"
        f"Allowed: `{readiness['admitted_transitions']}`  \n"
        f"Digest: `{projection['digest']}`\n\n"
        f"### Problem frames\n\n{_markdown_lines(frames)}\n\n"
        f"### Problem disposition\n\n{_markdown_lines(dispositions)}\n\n"
        f"### Known, inferred, and proposed claims\n\n{_markdown_lines(claims)}\n\n"
        f"### Questions and hypotheses\n\n{_markdown_lines(questions)}\n\n"
        f"### Candidate next tasks\n\n{_markdown_lines(tasks)}\n\n"
        f"### Search requests\n\n{_markdown_lines(searches)}\n\n"
        f"### Latest source candidates (not evidence)\n\n{_markdown_lines(candidates)}\n\n"
        f"### Blockers\n\n{_markdown_lines(readiness['blockers'])}\n\n"
        f"### Scientific measures\n\n"
        f"Unclassified frames: `{measures['unclassified_problem_count']}` · "
        f"untraceable: `{measures['untraceable_record_count']}` · "
        f"unsupported source claims: `{measures['unsupported_source_claim_count']}` · "
        f"incompatible tasks: `{measures['incompatible_task_count']}`\n\n"
        f"### Latest semantic diff\n\n{_markdown_lines(changes)}"
    )
    return {
        **result,
        "readiness_decision": readiness["decision"],
        "admitted_transitions": readiness["admitted_transitions"],
        "blockers": readiness["blockers"],
        "latest_semantic_diff": latest_diff,
        "content": content,
    }


@tool(summary="Read the evolving human-readable research consensus.", side_effects="none")
def get_consensus(direction_id: str, task_id: str | None = None, **_: Any) -> dict[str, Any]:
    state = _orchestrator().get(direction_id, task_id=task_id)
    prototype = state.get("accepted_prototype") or state.get("current_prototype") or {}
    brief = state.get("automation_brief") or {}
    direction = state["direction"]
    if not prototype:
        return {
            "ok": True,
            "initialized": bool(state.get("initialized", True)),
            "direction_id": direction_id,
            "status": "not_formulated",
            "accepted": False,
            "can_accept": False,
            "admission_decision": "needs_discussion",
            "content": "## Current draft\n\nNo ResearchPrototype has been formulated yet. Discuss the question, falsifiable hypotheses, protocol, estimand, uncertainty, and unresolved decisions.",
        }
    hypotheses = [f"**{item.get('id', 'H')}** — {item.get('statement', '')}  \nFalsified when: {item.get('falsification', 'not declared')}" for item in prototype.get("hypotheses") or [] if isinstance(item, Mapping)]
    grounding = [f"**{item.get('claim_id', 'claim')}** `{item.get('stance', 'unknown')}` — {item.get('claim', '')}  \nSources: {', '.join(f'`{ref}`' for ref in item.get('source_refs') or [])}" for item in prototype.get("source_grounding") or [] if isinstance(item, Mapping)]
    plan = prototype.get("experimental_plan") if isinstance(prototype.get("experimental_plan"), Mapping) else {}
    stages = [f"**{item.get('id', 'stage')}** `{item.get('evidence_class', 'unknown')}` — {item.get('purpose', '')}  \nInference: `{item.get('inference_allowed')}` · budget: `{item.get('budget', {})}`" for item in plan.get("stages") or [] if isinstance(item, Mapping)]
    evaluation = prototype.get("evaluation_plan") if isinstance(prototype.get("evaluation_plan"), Mapping) else {}
    readiness = prototype.get("readiness") if isinstance(prototype.get("readiness"), Mapping) else {}
    review = prototype.get("admission_review") if isinstance(prototype.get("admission_review"), Mapping) else {}
    coverage = prototype.get("context_coverage") if isinstance(prototype.get("context_coverage"), Mapping) else {}
    requirements = brief.get("implementation_requirements") or prototype.get("implementation_requirements") or []
    checks = brief.get("acceptance_checks") or prototype.get("acceptance_checks") or []
    accepted = bool(state.get("automation_brief"))
    admission_decision = str(review.get("decision") or "needs_discussion")
    status = "accepted" if accepted else "admitted draft" if admission_decision == "admitted" else "draft — needs discussion"
    estimand = evaluation.get("primary_estimand") if isinstance(evaluation.get("primary_estimand"), Mapping) else {}
    uncertainty = evaluation.get("uncertainty") if isinstance(evaluation.get("uncertainty"), Mapping) else {}
    stopping = evaluation.get("stopping_rule") if isinstance(evaluation.get("stopping_rule"), Mapping) else {}
    multiplicity = evaluation.get("multiplicity") if isinstance(evaluation.get("multiplicity"), Mapping) else {}
    open_questions = list(prototype.get("open_questions") or [])
    blocking_questions = list(readiness.get("blocking_questions") or [])
    blockers = list(review.get("blockers") or [])
    requirement_lines = [f"**{item.get('id', 'REQ')}** — {item.get('requirement', '')}  \nVerify: {item.get('verification', '')}" if isinstance(item, Mapping) else str(item) for item in requirements]
    check_lines = [f"**{item.get('id', 'AC')}** — {item.get('check', '')}  \nEvidence: {item.get('evidence', '')}" if isinstance(item, Mapping) else str(item) for item in checks]
    content = (
        f"## {'Accepted formulation' if accepted else 'Current draft'}\n\n"
        f"**Status:** `{status}` · **revision:** `{prototype.get('revision', '—')}` · **AdaOS gate:** `{admission_decision}`\n\n"
        f"### Source-context coverage\n\nRepresented `{coverage.get('sources_represented', 0)}/{coverage.get('sources_total', 0)}` artifacts; selected `{coverage.get('selected_characters', 0)}` characters.  \nTruncated: `{coverage.get('truncated_sources') or []}` · unreadable: `{coverage.get('unreadable_sources') or []}`\n\n"
        f"### Research question\n\n{prototype.get('research_question') or '—'}\n\n"
        f"### Source-grounded claims\n\n{_markdown_lines(grounding)}\n\n"
        f"### Falsifiable hypotheses\n\n{_markdown_lines(hypotheses)}\n\n"
        f"### Experimental stages\n\n{_markdown_lines(stages)}\n\n"
        f"### Primary estimand\n\n**{estimand.get('name', '—')}** — {estimand.get('contrast', '—')} on {estimand.get('population', '—')}; metric `{estimand.get('metric', '—')}`, aggregation `{estimand.get('aggregation', '—')}`.\n\n"
        f"### Inference contract\n\nUncertainty: `{uncertainty}`  \nStopping: `{stopping}`  \nMultiplicity: `{multiplicity}`  \nPractical significance: {evaluation.get('practical_significance') or '—'}  \nNegative results: {evaluation.get('negative_result_policy') or '—'}\n\n"
        f"### Constraints and assumptions\n\n**Constraints**\n{_markdown_lines(prototype.get('constraints'))}\n\n**Assumptions**\n{_markdown_lines(prototype.get('assumptions'))}\n\n"
        f"### Open questions\n\n{_markdown_lines(open_questions)}\n\n"
        f"### Readiness blockers\n\n{_markdown_lines([*blocking_questions, *blockers])}\n\n"
        f"### Automation requirements\n\n{_markdown_lines(requirement_lines)}\n\n"
        f"### Acceptance checks\n\n{_markdown_lines(check_lines)}"
    )
    return {
        "ok": True,
        "direction_id": direction_id,
        "status": status,
        "accepted": accepted,
        "can_accept": not accepted and admission_decision == "admitted",
        "admission_decision": admission_decision,
        "admission_blockers": blockers,
        "context_coverage": coverage,
        "prototype_digest": prototype.get("digest"),
        "automation_brief_digest": direction.get("automation_brief_digest"),
        "content": content,
    }


@tool(summary="Synchronize manifested artifact groups into orchestration state.", side_effects="local_write")
def sync_source_bundle(direction_id: str, actor: str = "user:local", **_: Any) -> dict[str, Any]:
    return _orchestrator().sync_source_bundle(direction_id, actor=actor)


@tool(summary="Record a schema-valid ResearchPrototype candidate.", side_effects="local_write")
def record_prototype(direction_id: str, prototype: Mapping[str, Any], actor: str = "user:local", **_: Any) -> dict[str, Any]:
    return _orchestrator().record_prototype(direction_id, prototype, actor=actor)


@tool(summary="Discuss and materialize a ResearchPrototype through the configured Root LLM.", side_effects="local_write")
def chat(
    direction_id: str,
    text: str,
    task_id: str | None = None,
    workflow_smoke_policy_id: str | None = None,
    formulation_inheritance_policy_id: str | None = None,
    model: str | None = None,
    actor: str | None = None,
    invocation_origin: str | None = None,
    _meta: Mapping[str, Any] | None = None,
    **payload: Any,
) -> dict[str, Any]:
    dialog_payload = dict(payload)
    if task_id:
        dialog_payload["task_id"] = task_id
    if workflow_smoke_policy_id:
        dialog_payload["workflow_smoke_policy_id"] = workflow_smoke_policy_id
    if formulation_inheritance_policy_id:
        dialog_payload["formulation_inheritance_policy_id"] = (
            formulation_inheritance_policy_id
        )
    if invocation_origin:
        dialog_payload["invocation_origin"] = invocation_origin
    if _meta:
        dialog_payload["_meta"] = dict(_meta)
    return _orchestrator().discuss(
        direction_id,
        text,
        model=model,
        actor=actor,
        dialog_payload=dialog_payload,
    )


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
def get_automation_brief(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    state = _orchestrator().get(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
    )
    brief = state.get("automation_brief")
    return {
        "ok": True,
        "available": brief is not None,
        "initialized": bool(state.get("initialized", True)),
        "direction": state["direction"],
        "automation_brief": brief,
        "content": (
            __import__("json").dumps(brief, ensure_ascii=False, indent=2)
            if brief
            else "Automation Brief has not been created yet."
        ),
        "language": "json",
        "codex_started": False,
    }


@tool(summary="Discuss an idea and update only its typed scientific projection.", side_effects="local_write")
def inquiry_chat(
    direction_id: str,
    text: str,
    task_id: str | None = None,
    model: str | None = None,
    actor: str | None = None,
    invocation_origin: str | None = None,
    _meta: Mapping[str, Any] | None = None,
    **payload: Any,
) -> dict[str, Any]:
    dialog_payload = dict(payload)
    if task_id:
        dialog_payload["task_id"] = task_id
    if invocation_origin:
        dialog_payload["invocation_origin"] = invocation_origin
    if _meta:
        dialog_payload["_meta"] = dict(_meta)
    return _orchestrator().discuss_inquiry(
        direction_id,
        text,
        model=model,
        actor=actor,
        dialog_payload=dialog_payload,
    )


@tool(summary="Discover source candidates for active typed SearchRequests.", side_effects="external_read")
def discover_inquiry_sources(
    direction_id: str,
    task_id: str | None = None,
    model: str | None = None,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().discover_inquiry_sources(
        direction_id,
        task_id=task_id,
        model=model,
        actor=actor,
    )


@tool(summary="Reconcile missing Researcher LLM usage from durable Root jobs.", side_effects="external_read")
def reconcile_inquiry_usage(
    direction_id: str,
    task_id: str | None = None,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().reconcile_inquiry_usage(
        direction_id,
        task_id=task_id,
        actor=actor,
    )


@tool(summary="Record one externally reviewed inquiry projection patch.", side_effects="local_write")
def record_inquiry_turn(
    direction_id: str,
    text: str,
    patch: Mapping[str, Any],
    task_id: str | None = None,
    actor: str = "user:local",
    patch_actor_kind: str = "human",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().record_inquiry_turn(
        direction_id,
        text,
        patch,
        actor=actor,
        patch_actor_kind=patch_actor_kind,
        dialog_payload={"task_id": task_id} if task_id else None,
    )


@tool(summary="Record a human decision over one exact inquiry projection.", side_effects="local_write")
def decide_inquiry_projection(
    direction_id: str,
    decision: str,
    rationale: str,
    accepted_by: str = "user:local",
    task_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().accept_inquiry(
        direction_id,
        decision=decision,
        rationale=rationale,
        accepted_by=accepted_by,
        task_id=task_id,
    )


@tool(summary="Bind and open the exact pre-Codex Development Session in Builder.", side_effects="local_write")
def open_builder_session(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    builder_webspace_id: str = "desktop-dev",
    base_url: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().open_builder_session(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        builder_webspace_id=builder_webspace_id,
        base_url=base_url,
    )


@tool(summary="Supersede a Development Session when the admitted consumer ABI changes.", side_effects="local_write")
def refresh_development_contract(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    actor: str = "system:research_orchestrator",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().refresh_development_contract(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        actor=actor,
    )


@tool(summary="Branch an immutable research realization onto its current Development Session.", side_effects="local_write")
def branch_implementation_track(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    reason: str = "realization_repair",
    actor: str = "system:research_orchestrator",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().branch_implementation_track(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        reason=reason,
        actor=actor,
    )


@tool(summary="Start one-shot Builder Automation from the exact bound Development Session.", side_effects="external_write")
def start_implementation(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    builder_webspace_id: str | None = None,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().start_implementation(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        builder_webspace_id=builder_webspace_id,
        actor=actor,
    )


@tool(summary="Synchronize Builder Automation into the durable research activity ledger.", side_effects="local_write")
def sync_implementation(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    builder_webspace_id: str | None = None,
    actor: str = "system:research_orchestrator",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().sync_implementation(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        builder_webspace_id=builder_webspace_id,
        actor=actor,
    )


@tool(summary="Prepare an isolated reviewed ProjectRelease candidate through Builder.", side_effects="external_write")
def prepare_project_release(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    builder_webspace_id: str | None = None,
    bump: str = "patch",
    confirmed: bool = False,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().prepare_project_release(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        builder_webspace_id=builder_webspace_id,
        bump=bump,
        confirmed=confirmed,
        actor=actor,
    )


@tool(summary="Promote the exact reviewed ProjectRelease candidate through Builder.", side_effects="external_write")
def publish_project_release(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    builder_webspace_id: str | None = None,
    bump: str = "patch",
    confirmed: bool = False,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().publish_project_release(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        builder_webspace_id=builder_webspace_id,
        bump=bump,
        confirmed=confirmed,
        actor=actor,
    )


@tool(summary="Bind an exact ProjectRelease and runner to a ResearchManager Study and Experiment.", side_effects="external_write")
def instantiate_study(
    direction_id: str,
    idempotency_key: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().instantiate_study(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        actor=actor,
        idempotency_key=idempotency_key,
    )


@tool(summary="Create a fresh Experiment campaign for the same immutable StudyRealization.", side_effects="external_write")
def repeat_study_experiment(
    direction_id: str,
    idempotency_key: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    reason: str = "execution_recovery",
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().repeat_study_experiment(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        reason=reason,
        actor=actor,
        idempotency_key=idempotency_key,
    )


@tool(summary="Lock the compiled protocol and submit its bounded CPU workflow smoke.", side_effects="external_write")
def start_study_smoke(
    direction_id: str,
    idempotency_key: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    confirmed: bool = False,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().start_study_smoke(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        confirmed=confirmed,
        actor=actor,
        idempotency_key=idempotency_key,
    )


@tool(summary="Reconcile ResearchManager attempts into the durable research activity ledger.", side_effects="local_write")
def sync_study(
    direction_id: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    actor: str = "system:research_orchestrator",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().sync_study(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        actor=actor,
    )


@tool(summary="Finalize an independently verified, non-inferential workflow Evidence bundle.", side_effects="external_write")
def finalize_workflow_evidence(
    direction_id: str,
    idempotency_key: str,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    actor: str = "system:research_orchestrator",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().finalize_workflow_evidence(
        direction_id,
        task_id=task_id,
        implementation_track_id=implementation_track_id,
        actor=actor,
        idempotency_key=idempotency_key,
    )


@tool(summary="Read durable research formulation activity.", side_effects="none")
def get_activity(
    direction_id: str,
    limit: int = 200,
    task_id: str | None = None,
    implementation_track_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    repository = OrchestratorRepository()
    events = repository.activities(direction_id, limit)
    stages = repository.formulation_stages(direction_id, limit=30)
    if task_id:
        task_ref = f"research-task:{task_id}"
        track_ref = (
            f"implementation-track:{implementation_track_id}"
            if implementation_track_id
            else None
        )
        events = [
            item
            for item in events
            if item.get("subject_ref") in {None, task_ref, track_ref}
            or item.get("detail", {}).get("task_ref") == task_ref
            or item.get("detail", {}).get("implementation_track_ref") == track_ref
        ]
        stages = [item for item in stages if item.get("task_id") == task_id]
    stage_summaries = [
        {
            "run_id": item["run_id"],
            "stage_index": item["stage_index"],
            "stage_name": item["stage_name"],
            "status": item["status"],
            "input_digest": item["input_digest"],
            "output_digest": item.get("output_digest"),
            "resolved_model": item["telemetry"].get("resolved_model"),
            "resolved_provider": item["telemetry"].get("resolved_provider"),
            "provider_job_id": item["telemetry"].get("provider_job_id"),
            "structured_output": item["telemetry"].get("structured_output"),
            "repair_attempts": item["telemetry"].get("repair_attempts", 0),
            "aggregate_usage": item["telemetry"].get("aggregate_usage") or item["telemetry"].get("usage") or {},
            "created_at": item["created_at"],
        }
        for item in stages
    ]
    usage_summary = _research_usage_summary(events, stage_summaries)
    researcher = usage_summary["researcher_llm"]
    builder = usage_summary["builder_codex"]
    stage_lines = "\n".join(
        f"- `{item['run_id']}` · **{item['stage_index']}/4 {item['stage_name']}** · `{item['status']}` · "
        f"model `{item.get('resolved_model') or 'unknown'}` · structured `{item.get('structured_output')}` · repairs `{item.get('repair_attempts', 0)}`"
        f" · tokens `{(item.get('aggregate_usage') or {}).get('total_tokens', 0)}`"
        for item in reversed(stage_summaries)
    )
    event_lines = "\n".join(f"- `{item['seq']:03d}` **{item['stage']} / {item['status']}** — {item['message']}" for item in events)
    content = (
        f"## Token accounting\n\n"
        f"- Researcher LLM: `{researcher['total_tokens']}` known tokens across "
        f"`{researcher['provider_reported_jobs']}` provider-reported jobs; "
        f"unknown jobs `{researcher['unknown_usage_jobs']}`.\n"
        f"- Builder Codex: `{builder['total_tokens']}` known tokens across "
        f"`{builder['provider_reported_runs']}` Builder runs; "
        f"unknown runs `{builder['unknown_usage_runs']}`.\n"
        f"- Interactive Codex session tokens are excluded.\n\n"
        f"## Formulation stages\n\n{stage_lines or 'No staged formulation runs yet.'}\n\n"
        f"## Activity events\n\n{event_lines or 'No activity events yet.'}"
    )
    return {
        "ok": True,
        "direction_id": direction_id,
        "events": events,
        "formulation_stages": stage_summaries,
        "usage_summary": usage_summary,
        "content": content,
    }


def _research_usage_summary(
    events: list[Mapping[str, Any]],
    stages: list[Mapping[str, Any]],
) -> dict[str, Any]:
    researcher_jobs: dict[str, Mapping[str, Any]] = {}
    builder_runs: dict[str, Mapping[str, Any]] = {}

    for item in stages:
        usage = item.get("aggregate_usage") if isinstance(item.get("aggregate_usage"), Mapping) else {}
        if not usage or str(item.get("resolved_model") or "") == "not_invoked":
            continue
        key = str(item.get("provider_job_id") or f"stage:{item.get('run_id')}:{item.get('stage_name')}")
        researcher_jobs[key] = usage
    for event in events:
        detail = event.get("detail") if isinstance(event.get("detail"), Mapping) else {}
        usage = detail.get("usage") if isinstance(detail.get("usage"), Mapping) else None
        provider_job_id = str(detail.get("provider_job_id") or "").strip()
        if usage is not None and provider_job_id:
            researcher_jobs[provider_job_id] = usage
        accounting = (
            detail.get("codex_usage_accounting")
            if isinstance(detail.get("codex_usage_accounting"), Mapping)
            else None
        )
        budget = detail.get("budget_usage") if isinstance(detail.get("budget_usage"), Mapping) else {}
        observed = budget.get("observed") if isinstance(budget.get("observed"), Mapping) else {}
        automation = detail.get("automation") if isinstance(detail.get("automation"), Mapping) else {}
        run_id = str(
            (accounting or {}).get("task_id")
            or automation.get("task_id")
            or ""
        ).strip()
        if run_id:
            builder_runs[run_id] = accounting or (
                {**observed, "accuracy": "provider_reported"}
                if int(observed.get("model_tokens") or 0) > 0
                else {"total_tokens": None, "accuracy": "unavailable"}
            )

    def aggregate(values: Mapping[str, Mapping[str, Any]], *, run_label: str) -> dict[str, Any]:
        known = 0
        unknown = 0
        totals = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }
        for usage in values.values():
            total = usage.get("total_tokens")
            if total is None and "model_tokens" in usage:
                total = usage.get("model_tokens")
            accuracy = str(usage.get("accuracy") or "")
            if total is None or accuracy == "unavailable":
                unknown += 1
                continue
            known += 1
            totals["input_tokens"] += int(usage.get("input_tokens") or 0)
            totals["cached_input_tokens"] += int(usage.get("cached_input_tokens") or 0)
            totals["output_tokens"] += int(usage.get("output_tokens") or 0)
            totals["reasoning_tokens"] += int(usage.get("reasoning_tokens") or 0)
            totals["total_tokens"] += int(total or 0)
        return {
            **totals,
            f"provider_reported_{run_label}": known,
            f"unknown_usage_{run_label}": unknown,
            "exact_total_available": unknown == 0,
        }

    return {
        "researcher_llm": aggregate(researcher_jobs, run_label="jobs"),
        "builder_codex": aggregate(builder_runs, run_label="runs"),
        "interactive_codex_included": False,
    }


@tool(summary="Read the exact persisted artifacts and telemetry for one formulation run.", side_effects="none")
def get_formulation_run(direction_id: str, run_id: str | None = None, **_: Any) -> dict[str, Any]:
    repository = OrchestratorRepository()
    selected_run = str(run_id or "").strip()
    if not selected_run:
        recent = repository.formulation_stages(direction_id, limit=1)
        selected_run = str(recent[0]["run_id"]) if recent else ""
    stages = repository.formulation_stages(direction_id, run_id=selected_run) if selected_run else []
    lines = [
        f"- **{item['stage_index']}/4 {item['stage_name']}** · `{item['status']}` · "
        f"model `{item['telemetry'].get('resolved_model') or 'unknown'}` · "
        f"structured `{item['telemetry'].get('structured_output')}` · "
        f"repairs `{item['telemetry'].get('repair_attempts', 0)}` · "
        f"tokens `{(item['telemetry'].get('aggregate_usage') or item['telemetry'].get('usage') or {}).get('total_tokens', 0)}`"
        for item in stages
    ]
    return {
        "ok": bool(stages),
        "direction_id": direction_id,
        "run_id": selected_run or None,
        "stages": stages,
        "content": "## Formulation run\n\n" + ("\n".join(lines) if lines else "No formulation run was found."),
    }


def _compilation_markdown(compilation: Mapping[str, Any], facet: str) -> str:
    facets = compilation.get("facets") if isinstance(compilation.get("facets"), Mapping) else {}
    selected = facets.get(facet) if isinstance(facets.get(facet), Mapping) else {}
    payload = selected.get("payload") if isinstance(selected.get("payload"), Mapping) else {}
    if facet == "source_analysis":
        inventory = [
            f"**{item.get('name', 'source')}** · `{item.get('role', 'source')}` · `{item.get('digest', '')}`  \n"
            f"Extraction: `{item.get('extraction', {}).get('strategy', 'unknown')}` · "
            f"selected `{item.get('extraction', {}).get('selected_characters', 0)}` chars · "
            f"truncated `{item.get('extraction', {}).get('truncated', False)}`"
            for item in payload.get("inventory") or []
            if isinstance(item, Mapping)
        ]
        facts = [
            f"{item.get('claim', '')}  \nSources: {', '.join(f'`{ref}`' for ref in item.get('source_refs') or [])}"
            for item in payload.get("observed_facts") or []
            if isinstance(item, Mapping)
        ]
        interpretations = [
            f"{item.get('claim', '')}  \nSources: {', '.join(f'`{ref}`' for ref in item.get('source_refs') or [])}"
            for item in payload.get("author_interpretations") or []
            if isinstance(item, Mapping)
        ]
        return (
            f"## Source Analysis\n\n**Digest:** `{selected.get('digest')}` · **sufficiency:** `{payload.get('sufficiency')}`\n\n"
            f"### Inventory\n\n{_markdown_lines(inventory)}\n\n"
            f"### Observed facts\n\n{_markdown_lines(facts)}\n\n"
            f"### Author interpretations\n\n{_markdown_lines(interpretations)}\n\n"
            f"### Coverage limitations\n\n{_markdown_lines(payload.get('coverage_limitations'))}\n\n"
            f"### Unresolved source decisions\n\n{_markdown_lines(payload.get('unresolved_decisions'))}"
        )
    if facet == "research_problem":
        hypotheses = [
            f"**{item.get('id', 'H')}** — {item.get('statement', '')}  \nFalsification: {item.get('falsification', '')}"
            for item in payload.get("hypotheses") or []
            if isinstance(item, Mapping)
        ]
        return (
            f"## Research Problem\n\n**Digest:** `{selected.get('digest')}`\n\n"
            f"### Background\n\n{payload.get('background', '—')}\n\n"
            f"### Primary question\n\n{payload.get('research_question', '—')}\n\n"
            f"### Hypothesis\n\n{_markdown_lines(hypotheses)}\n\n"
            f"### Constraints\n\n{_markdown_lines(payload.get('constraints'))}\n\n"
            f"### Assumptions\n\n{_markdown_lines(payload.get('assumptions'))}\n\n"
            f"### Open questions\n\n{_markdown_lines(payload.get('open_questions'))}"
        )
    if facet == "experimental_protocol":
        plan = payload.get("experimental_plan") if isinstance(payload.get("experimental_plan"), Mapping) else {}
        evaluation = payload.get("evaluation_plan") if isinstance(payload.get("evaluation_plan"), Mapping) else {}
        stages = [
            f"**{item.get('id', 'stage')}** `{item.get('evidence_class', 'unknown')}` — {item.get('purpose', '')}  \n"
            f"Device `{item.get('execution_profile', {}).get('device')}` · budget `{item.get('budget')}` · inference `{item.get('inference_allowed')}`"
            for item in plan.get("stages") or []
            if isinstance(item, Mapping)
        ]
        decisions = [
            f"**{area}** `{item.get('status')}` — {item.get('value_summary', '')}"
            for area, item in (payload.get("decisions_by_area") or {}).items()
            if isinstance(item, Mapping)
        ]
        return (
            f"## Experimental Protocol\n\n**Digest:** `{selected.get('digest')}`\n\n"
            f"**Comparators:** {', '.join(f'`{item}`' for item in plan.get('comparators') or [])}\n\n"
            f"### Stages\n\n{_markdown_lines(stages)}\n\n"
            f"### Protocol decisions\n\n{_markdown_lines(decisions)}\n\n"
            f"### Primary estimand\n\n`{evaluation.get('primary_estimand', {})}`\n\n"
            f"### Evaluation contract\n\nUncertainty `{evaluation.get('uncertainty', {})}`  \n"
            f"Stopping `{evaluation.get('stopping_rule', {})}`  \nMultiplicity `{evaluation.get('multiplicity', {})}`"
        )
    if facet == "engineering_contract":
        requirements = [
            f"**{category}** — {item.get('requirement', '')}  \nVerify: {item.get('verification', '')}"
            for category, values in (payload.get("requirements_by_category") or {}).items()
            for item in values or []
            if isinstance(item, Mapping)
        ]
        checks = [
            f"**{category}** — {item.get('check', '')}  \nEvidence: {item.get('evidence', '')}"
            for category, values in (payload.get("checks_by_category") or {}).items()
            for item in values or []
            if isinstance(item, Mapping)
        ]
        return (
            f"## Engineering Contract\n\n**Digest:** `{selected.get('digest')}`\n\n"
            f"### Requirements\n\n{_markdown_lines(requirements)}\n\n"
            f"### Acceptance checks\n\n{_markdown_lines(checks)}"
        )
    traceability = compilation.get("traceability_coverage") if isinstance(compilation.get("traceability_coverage"), Mapping) else {}
    findings = [
        f"`{item.get('requirement_id')}` — {'covered' if item.get('covered') else 'missing'} · "
        f"{' → '.join(item.get('path') or []) or 'no path'}"
        for item in traceability.get("findings") or []
        if isinstance(item, Mapping)
    ]
    return (
        f"## Traceability\n\n**Graph:** `{compilation.get('traceability_graph', {}).get('digest')}` · "
        f"**coverage:** `{traceability.get('covered', 0)}/{traceability.get('total', 0)}` · "
        f"**readiness:** `{compilation.get('readiness', {}).get('decision', 'unknown')}`\n\n"
        f"### Required paths\n\n{_markdown_lines(findings)}\n\n"
        f"### Blockers\n\n{_markdown_lines(compilation.get('readiness', {}).get('blockers'))}"
    )


@tool(summary="Resume deterministic research compilation from durable successful LLM stages.", side_effects="local_write")
def resume_compilation(
    direction_id: str,
    run_id: str,
    actor: str = "user:local",
    **_: Any,
) -> dict[str, Any]:
    return _orchestrator().resume_compilation(direction_id, run_id, actor=actor)


@tool(summary="Read the latest digest-bound research compilation facets and traceability.", side_effects="none")
def get_compilation(
    direction_id: str,
    task_id: str | None = None,
    run_id: str | None = None,
    facet: str = "traceability",
    **_: Any,
) -> dict[str, Any]:
    state = _orchestrator().get(direction_id, task_id=task_id)
    prototype = state.get("accepted_prototype") or state.get("current_prototype") or {}
    trace = prototype.get("formulation_trace") if isinstance(prototype.get("formulation_trace"), Mapping) else {}
    selected_run = str(run_id or trace.get("run_id") or "").strip()
    stages = OrchestratorRepository().formulation_stages(direction_id, run_id=selected_run) if selected_run else []
    selected_task_id = str((state.get("selected_task") or state.get("active_task") or {}).get("task_id") or "")
    if selected_task_id:
        stages = [item for item in stages if item.get("task_id") == selected_task_id]
    stage = next((item for item in stages if item.get("stage_name") == "research_compilation"), None)
    accepted_record = state.get("accepted_compilation_record") or {}
    compilation = (
        dict(accepted_record.get("payload") or {})
        if isinstance(accepted_record, Mapping) and accepted_record.get("payload")
        else dict(stage.get("payload") or {})
        if isinstance(stage, Mapping)
        else {}
    )
    if not compilation:
        return {
            "ok": True,
            "available": False,
            "direction_id": direction_id,
            "task_id": selected_task_id or None,
            "run_id": selected_run or None,
            "content": "Research Compilation has not been produced yet.",
        }
    facets = compilation.get("facets") if isinstance(compilation.get("facets"), Mapping) else {}
    traceability = compilation.get("traceability_coverage") if isinstance(compilation.get("traceability_coverage"), Mapping) else {}
    lines = [
        f"- **{name.replace('_', ' ').title()}** — `{item.get('digest')}`"
        for name, item in facets.items()
        if isinstance(item, Mapping)
    ]
    selected_facet = str(facet or "traceability").strip()
    if selected_facet not in {*facets, "traceability"}:
        selected_facet = "traceability"
    content = _compilation_markdown(compilation, selected_facet)
    return {
        "ok": True,
        "available": True,
        "direction_id": direction_id,
        "task_id": selected_task_id or None,
        "compilation_record": accepted_record or None,
        "run_id": selected_run,
        "compilation": compilation,
        "facets": facets,
        "traceability": traceability,
        "selected_facet": selected_facet,
        "facet_index": lines,
        "content": content,
    }


@tool(summary="Explain the next admitted steps for text or voice surfaces.", side_effects="none")
def next_steps(direction_id: str, **_: Any) -> dict[str, Any]:
    state = _orchestrator().get(direction_id)
    steps = list(state.get("next_steps") or [])
    message = " ".join(f"{index + 1}. {item['label']}: {item['reason']}" for index, item in enumerate(steps))
    return {"ok": True, "direction_id": direction_id, "message": message, "speech_text": message, "steps": steps}
