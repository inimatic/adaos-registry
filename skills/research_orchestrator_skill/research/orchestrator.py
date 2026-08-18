from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from adaos.sdk import chat as sdk_chat
from adaos.sdk import navigation
from adaos.sdk.builder import artifacts as builder_artifacts
from adaos.sdk.builder import development_sessions
from adaos.sdk.builder import preview as builder_preview
from adaos.sdk.developer import artifact_context, compositions, projects
from adaos.sdk.llm import llm_client
from adaos.sdk.skills import invoke as invoke_skill
from adaos.services.agent_context import get_ctx
from adaos.services.skill.artifacts import skill_upload_dir

from research.contracts import (
    materialize_automation_brief,
    materialize_prototype,
    prototype_admission_issues,
    prototype_candidate_schema,
    prototype_quality_issues,
)
from research.compiler import build_compilation
from research.formulation import (
    assemble_candidate,
    provider_schema,
    schema_text_format,
    stage_digest,
    stage_quality_issues,
    stage_schema,
    validate_stage,
)
from research.repository import OrchestratorRepository


_DIRECTION_RE = re.compile(r"^[a-z0-9_.-]+$")
_DIRECTIVE_TEXT_LIMIT = 6000
_FORMULATION_AUDIENCE = "research.formulation"
_IMPLEMENTATION_AUDIENCE = "research.implementation"
_RESEARCH_CONTEXT_PROFILES = {
    "shared": {"default": "allow", "allow": [], "deny": [], "reason": None},
    "evaluation_only": {
        "default": "deny",
        "allow": ["research.evaluation"],
        "deny": [],
        "reason": "Evaluator-only material; hidden from formulation and implementation.",
    },
    "formulation_only": {
        "default": "deny",
        "allow": ["research.formulation"],
        "deny": [],
        "reason": "Formulation-only material; not an implementation input.",
    },
    "implementation_input": {
        "default": "deny",
        "allow": ["research.implementation", "research.evaluation"],
        "deny": [],
        "reason": "Implementation and evaluation input; excluded from formulation.",
    },
}


def _direction_id(value: str) -> str:
    token = str(value or "").strip().lower()
    if not _DIRECTION_RE.fullmatch(token):
        raise ValueError("direction_id must match ^[a-z0-9_.-]+$")
    return token


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _context_profile(value: str) -> dict[str, Any]:
    profile = str(value or "shared").strip().lower()
    if profile not in _RESEARCH_CONTEXT_PROFILES:
        raise ValueError(
            "visibility_profile must be shared, evaluation_only, formulation_only, or implementation_input"
        )
    return copy.deepcopy(_RESEARCH_CONTEXT_PROFILES[profile])


def _directive_trace(
    text: str,
    *,
    actor: str | None,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Describe the caller-visible directive without recording hidden prompts."""

    values = dict(payload or {})
    meta = values.get("_meta") if isinstance(values.get("_meta"), Mapping) else {}
    explicit_actor = str(actor or "").strip()
    actor_id = (
        explicit_actor
        or str(values.get("actor_id") or meta.get("actor_id") or "").strip()
        or str(meta.get("principal_id") or meta.get("user_id") or "").strip()
    )
    has_conversation_origin = bool(
        values.get("conversation_id")
        or values.get("thread_id")
        or meta.get("conversation_id")
        or meta.get("thread_id")
        or meta.get("turn_trace_id")
    )
    origin = str(
        values.get("invocation_origin")
        or meta.get("invocation_origin")
        or ("conversation" if has_conversation_origin else "api")
    ).strip().lower()
    if not actor_id:
        actor_id = "user:conversation" if origin == "conversation" else "api:local"
    actor_label = str(values.get("actor_label") or meta.get("actor_label") or actor_id).strip()
    directive_text = _bounded_text(text, _DIRECTIVE_TEXT_LIMIT)
    return {
        "schema": "adaos.research.directive.v1",
        "actor_id": actor_id[:200],
        "actor_label": actor_label[:200],
        "origin": origin[:80] or "api",
        "text": directive_text,
        "text_digest": "sha256:" + hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest(),
        "truncated": directive_text != str(text or "").strip(),
        "project_to_chat": origin != "conversation",
        "request_id": str(meta.get("request_id") or values.get("request_id") or "").strip() or None,
        "turn_trace_id": str(meta.get("turn_trace_id") or values.get("turn_trace_id") or "").strip() or None,
    }


def _completion_projection(prototype: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    revision = int(prototype.get("revision") or 0)
    review = prototype.get("admission_review") if isinstance(prototype.get("admission_review"), Mapping) else {}
    decision = str(review.get("decision") or "draft")
    blockers = [_bounded_text(item, 280) for item in list(review.get("blockers") or [])]
    explanation = _bounded_text(prototype.get("assistant_message"), 1800)
    if decision == "admitted":
        lead = f"ResearchPrototype revision {revision} passed the automation admission gate and is ready for human acceptance."
    else:
        lead = f"ResearchPrototype revision {revision} was recorded as a reviewable draft; it is not ready for automation."
        if blockers:
            lead += " Blocking issues: " + "; ".join(blockers[:4])
    message = f"{lead}\n\n{explanation}" if explanation else lead
    return message, {
        "candidate_status": "admitted" if decision == "admitted" else "draft",
        "admission_decision": decision,
        "admission_blockers": blockers,
        "model_explanation": explanation,
    }


def _failure_projection(error: BaseException, *, repairs: int) -> tuple[str, dict[str, Any]]:
    raw = _bounded_text(f"{type(error).__name__}: {error}", 2400)
    if "research.prototype.v1.schema.json invalid:" in raw:
        code = "prototype_contract_validation_failed"
        lead = (
            "The generated candidate was rejected by the typed ResearchPrototype contract "
            f"after {repairs} bounded repair attempt(s); no invalid revision was accepted."
        )
    elif "semantic quality gate:" in raw:
        code = "prototype_semantic_gate_failed"
        lead = (
            "The generated candidate matched the JSON shape but failed the deterministic "
            f"research-quality gate after {repairs} bounded repair attempt(s)."
        )
    else:
        code = "formulation_failed"
        lead = "Research formulation did not complete."
    return f"{lead} Technical detail: {raw}", {
        "error_code": code,
        "error_type": type(error).__name__,
        "error": raw,
        "repair_attempts": repairs,
    }


def _json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    candidates = [raw]
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])
    parse_error: ValueError | None = None
    value: Any = None
    for candidate in candidates:
        for normalized in (
            candidate,
            re.sub(r",\s*([}\]])", r"\1", candidate),
        ):
            try:
                value = json.loads(normalized)
                break
            except ValueError as exc:
                parse_error = exc
        if value is not None:
            break
    if value is None:
        if start < 0 or end <= start:
            raise ValueError("LLM response does not contain a JSON object") from None
        raise ValueError(f"LLM response contains invalid JSON: {parse_error}") from None
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value


def _llm_failure(result: Mapping[str, Any], *, operation: str) -> RuntimeError:
    status = str(result.get("status") or "failed")
    error = result.get("error")
    detail = ""
    if isinstance(error, Mapping):
        detail = str(error.get("message") or error.get("code") or "")
    elif error:
        detail = str(error)
    if not detail:
        incomplete = result.get("incomplete_details")
        if isinstance(incomplete, Mapping):
            detail = str(incomplete.get("reason") or "")
    if not detail:
        progress = result.get("progress") if isinstance(result.get("progress"), Mapping) else {}
        events = progress.get("events") if isinstance(progress.get("events"), list) else []
        for event in reversed(events):
            if isinstance(event, Mapping) and event.get("detail"):
                detail = str(event["detail"])
                break
    suffix = f": {detail[:300]}" if detail else ""
    return RuntimeError(f"Root LLM {operation} ended with status={status}{suffix}")


def _mapping_path(value: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = value
        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                current = None
                break
            current = current[key]
        if current not in (None, "", {}, []):
            return current
    return None


def _llm_telemetry(
    submitted: Mapping[str, Any],
    completed: Mapping[str, Any],
    *,
    requested_model: str | None,
    profile_scope: str,
    output_text: str,
    structured_output: bool,
    repair_attempts: int,
) -> dict[str, Any]:
    """Retain reproducibility metadata without persisting hidden prompt text."""

    model = _mapping_path(
        completed,
        ("model",),
        ("resolved_model",),
        ("response", "model"),
        ("result", "model"),
    )
    provider = _mapping_path(
        completed,
        ("provider",),
        ("resolved_provider",),
        ("response", "provider"),
        ("result", "provider"),
    )
    usage = _mapping_path(completed, ("usage",), ("response", "usage"), ("result", "usage"))
    finish_reason = _mapping_path(
        completed,
        ("finish_reason",),
        ("incomplete_details", "reason"),
        ("response", "finish_reason"),
        ("result", "finish_reason"),
    )
    client = submitted.get("_client") if isinstance(submitted.get("_client"), Mapping) else {}
    return {
        "requested_model": requested_model or None,
        "resolved_model": str(model or requested_model or "root-default"),
        "resolved_provider": str(provider or "root"),
        "profile_scope": profile_scope,
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "finish_reason": str(finish_reason or "unknown"),
        "output_characters": len(output_text),
        "structured_output": bool(structured_output),
        "repair_attempts": int(repair_attempts),
        "transport": {
            "fallback": bool(client.get("fallback")),
            "payload_bytes": int(client.get("payload_bytes") or 0),
            "submit_ms": client.get("attempt_ms"),
            "total_submit_ms": client.get("total_ms"),
        },
    }


def _structured_output_unsupported(error: BaseException) -> bool:
    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "json_schema",
            "structured output",
            "structured_output",
            "text.format",
            "invalid schema for response_format",
            "unsupported format",
            "unknown format",
        )
    )


def _repair_prompt(
    *,
    validation_error: str,
    candidate: Mapping[str, Any],
    rules: list[str],
    user_request: str,
    allowed_provenance_refs: list[str],
) -> str:
    """Keep repair instructions outside the JSON object the model must return."""

    return "\n\n".join(
        [
            "Repair the candidate below. Return the corrected candidate JSON object only; do not return this instruction envelope.",
            f"VALIDATION ERRORS:\n{validation_error}",
            f"USER REVISION REQUEST:\n{user_request}",
            "RULES:\n- " + "\n- ".join(rules),
            "ALLOWED PROVENANCE REFS:\n- " + "\n- ".join(allowed_provenance_refs),
            "CANDIDATE JSON TO CORRECT AND RETURN:\n" + json.dumps(dict(candidate), ensure_ascii=False),
        ]
    )


def _normalize_candidate_shape(value: Mapping[str, Any]) -> dict[str, Any]:
    """Correct transport-shaped LLM output without inventing domain content."""

    candidate = copy.deepcopy(dict(value))
    envelope_keys = {
        "allowed_provenance_refs",
        "output_contract",
        "rejected_candidate",
        "rules",
        "task",
        "user_request",
        "validation_error",
        "validation_schema",
    }
    for key in envelope_keys:
        candidate.pop(key, None)

    top_level = (
        "evaluation_plan",
        "constraints",
        "assumptions",
        "open_questions",
        "implementation_requirements",
        "acceptance_checks",
        "readiness",
        "assistant_message",
    )
    experimental = candidate.get("experimental_plan")
    if isinstance(experimental, dict):
        for key in top_level:
            if key not in candidate and key in experimental:
                candidate[key] = experimental.pop(key)
    evaluation = candidate.get("evaluation_plan")
    if isinstance(evaluation, dict):
        for key in top_level[1:]:
            if key not in candidate and key in evaluation:
                candidate[key] = evaluation.pop(key)

    lifted_grounding: list[Any] = []
    hypotheses = candidate.get("hypotheses")
    if isinstance(hypotheses, list):
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict):
                continue
            nested = hypothesis.pop("source_grounding", None)
            if isinstance(nested, list):
                lifted_grounding.extend(nested)
            elif isinstance(nested, Mapping):
                lifted_grounding.append(dict(nested))
    if lifted_grounding and "source_grounding" not in candidate:
        candidate["source_grounding"] = lifted_grounding

    def canonical_enum(raw: Any) -> Any:
        if not isinstance(raw, str):
            return raw
        return re.sub(r"[\s-]+", "_", raw.strip().lower())

    for item in candidate.get("source_grounding") or []:
        if isinstance(item, dict) and "stance" in item:
            item["stance"] = canonical_enum(item["stance"])
    experimental = candidate.get("experimental_plan")
    if isinstance(experimental, dict):
        for stage in experimental.get("stages") or []:
            if isinstance(stage, dict) and "evidence_class" in stage:
                stage["evidence_class"] = canonical_enum(stage["evidence_class"])
        reproducibility = experimental.get("reproducibility")
        pairing = reproducibility.get("pairing") if isinstance(reproducibility, dict) else None
        allocation = pairing.get("allocation") if isinstance(pairing, dict) else None
        if isinstance(allocation, dict) and "strategy" in allocation:
            allocation["strategy"] = canonical_enum(allocation["strategy"])
    for item in candidate.get("implementation_requirements") or []:
        if isinstance(item, dict) and "category" in item:
            item["category"] = canonical_enum(item["category"])
    for item in candidate.get("acceptance_checks") or []:
        if isinstance(item, dict) and "category" in item:
            item["category"] = canonical_enum(item["category"])
    readiness = candidate.get("readiness")
    if isinstance(readiness, dict) and "decision" in readiness:
        readiness["decision"] = canonical_enum(readiness["decision"])
        if "open_questions" not in candidate and "open_questions" in readiness:
            candidate["open_questions"] = readiness.pop("open_questions")
    evaluation = candidate.get("evaluation_plan")
    if isinstance(evaluation, dict):
        normalized_rules: list[Any] = []
        for rule in evaluation.get("decision_rules") or []:
            if isinstance(rule, Mapping):
                normalized_rules.append(
                    str(
                        rule.get("description")
                        or rule.get("rule")
                        or rule.get("text")
                        or ""
                    ).strip()
                )
            else:
                normalized_rules.append(rule)
        if "decision_rules" in evaluation:
            evaluation["decision_rules"] = normalized_rules

    category_aliases = {
        "storage": "data",
        "documentation": "observability",
        "initialization": "reproducibility",
        "logging": "observability",
        "tracking": "observability",
    }
    for index, item in enumerate(candidate.get("implementation_requirements") or [], 1):
        if not isinstance(item, dict):
            continue
        item.setdefault("id", f"REQ-{index}")
        category = str(item.get("category") or "")
        if category in category_aliases:
            item["category"] = category_aliases[category]
    for index, item in enumerate(candidate.get("acceptance_checks") or [], 1):
        if isinstance(item, dict):
            item.setdefault("id", f"AC-{index}")

    if isinstance(experimental, dict):
        reproducibility = experimental.get("reproducibility")
        pairing = reproducibility.get("pairing") if isinstance(reproducibility, dict) else None
        if isinstance(pairing, dict) and not isinstance(pairing.get("allocation"), Mapping):
            for stage in experimental.get("stages") or []:
                if not isinstance(stage, Mapping) or stage.get("evidence_class") != "confirmatory":
                    continue
                budget = stage.get("budget") if isinstance(stage.get("budget"), Mapping) else {}
                units = budget.get("seed_values") or budget.get("planned_seeds") or budget.get("seeds")
                if isinstance(units, list) and units:
                    pairing["allocation"] = {
                        "strategy": "enumerated_units",
                        "planned_units": units,
                        "sample_size": len(units),
                        "predeclared": True,
                    }
                break
    return candidate


def _address_builder_url(url: str, *, direction_id: str, title: str) -> str:
    """Attach a declared first-paint address; the Yjs binding remains canonical."""

    parts = urlsplit(str(url or ""))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(
        {
            "builder_object_type": "skill",
            "builder_object_id": direction_id,
            "builder_object_ref": f"skill:{direction_id}",
            "builder_object_title": title or direction_id,
        }
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _notebook_excerpt(text: str, *, max_characters: int) -> str:
    try:
        notebook = json.loads(text)
    except ValueError:
        return text[:max_characters]
    parts: list[str] = []
    for index, cell in enumerate(notebook.get("cells") or []):
        if not isinstance(cell, Mapping):
            continue
        cell_type = str(cell.get("cell_type") or "unknown")
        source = cell.get("source") or ""
        body = "".join(source) if isinstance(source, list) else str(source)
        if body.strip():
            parts.append(f"\n--- cell {index} ({cell_type}) ---\n{body.strip()}")
        if sum(len(item) for item in parts) >= max_characters:
            break
    return "".join(parts)[:max_characters]


class ResearchOrchestrator:
    def __init__(
        self,
        repository: OrchestratorRepository | None = None,
        *,
        checkpoint: Callable[..., Mapping[str, Any]] | None = None,
    ) -> None:
        self.repository = repository or OrchestratorRepository()
        self._checkpoint = checkpoint or builder_artifacts.local_checkpoint

    def _artifact_owner_id(self, direction_id: str) -> str:
        state = self.repository.get_direction(direction_id)
        return str((state or {}).get("artifact_owner_skill_id") or direction_id)

    def _require_direction_project(self, direction_id: str) -> dict[str, Any]:
        state = self.repository.get_direction(direction_id)
        owner_skill_id = str((state or {}).get("artifact_owner_skill_id") or direction_id)
        description = projects.describe("skill", owner_skill_id)
        manifest = yaml.safe_load(projects.read_file("skill", owner_skill_id, "skill.yaml")["content"]) or {}
        research = manifest.get("research_direction") if isinstance(manifest, Mapping) else None
        if not isinstance(research, Mapping) or research.get("schema") != "adaos.research.direction.v1":
            raise ValueError(f"skill:{owner_skill_id} is not an admitted research artifact custodian")
        project = None
        project_ref = str((state or {}).get("legacy_project_ref") or "")
        if project_ref.startswith("project:"):
            try:
                project = compositions.get(project_ref.partition(":")[2])
            except Exception:
                project = None
        if project is None and state is None:
            candidate = compositions.project_for_component(f"skill:{owner_skill_id}")
            if candidate and "adaos.research.direction.v1" in set(candidate.get("profiles") or []):
                project = candidate
        return {**description, "artifact_owner_skill_id": owner_skill_id, "project": project}

    def _ensure_implementation_project(
        self,
        direction: Mapping[str, Any],
        task: Mapping[str, Any],
    ) -> dict[str, Any]:
        owner_skill_id = str(direction.get("artifact_owner_skill_id") or direction["direction_id"])
        legacy_ref = str(direction.get("legacy_project_ref") or "")
        if legacy_ref.startswith("project:"):
            legacy = compositions.get(legacy_ref.partition(":")[2])
            if f"skill:{owner_skill_id}" in {
                str(item.get("ref") or "") for item in legacy["components"]["owned"]
            }:
                return legacy
        project_id = _direction_id(f"{direction['direction_id']}_implementation")
        try:
            project = compositions.get(project_id)
        except Exception:
            project = compositions.create(
                {
                    "schema": "adaos.project.v1",
                    "kind": "project",
                    "id": project_id,
                    "version": "0.1.0",
                    "profiles": ["adaos.research.implementation.v1"],
                    "components": {
                        "owned": [
                            {
                                "ref": f"skill:{owner_skill_id}",
                                "role": "primary",
                                "exposure": "project_only",
                                "lifecycle": "bound",
                                "relations": ["realizes"],
                            }
                        ],
                        "dependencies": [
                            {
                                "ref": "project:adaos_research_platform",
                                "version": "^0.2",
                                "lifecycle": "shared",
                                "relations": ["presents", "uses"],
                            }
                        ],
                    },
                    "entrypoints": [
                        {
                            "id": "research",
                            "presentation": "scenario:research_workbench",
                            "default": True,
                            "bindings": {
                                "direction_ref": f"research-direction:{direction['direction_id']}",
                                "task_ref": f"research-task:{task['task_id']}",
                            },
                        }
                    ],
                    "catalog": {
                        "title": f"{direction['title']} — implementation",
                        "description": (
                            "Project-scoped implementation for "
                            f"research-task:{task['task_id']}."
                        ),
                        "categories": ["research", "development"],
                        "tags": list(direction.get("tags") or []),
                    },
                    "compatibility": {
                        "required_entrypoints": ["research"],
                        "required_contracts": [
                            "adaos.research.compilation_package.v1",
                            "adaos.research.automation_brief.v1",
                        ],
                        "validation_profiles": [
                            "project.conformance",
                            "research.consumer-contracts",
                        ],
                    },
                    "lifecycle": {
                        "uninstall": {
                            "components": "remove_if_unreferenced",
                            "runtime_data": "retain",
                            "source_artifacts": "retain",
                        }
                    },
                }
            )
        return project

    def create_direction(
        self,
        project_id: str,
        title: str,
        *,
        description: str = "",
        skill_id: str | None = None,
        tags: list[str] | None = None,
        actor: str = "user:local",
    ) -> dict[str, Any]:
        direction_id = _direction_id(project_id)
        owner_skill_id = _direction_id(skill_id or direction_id)
        if self.repository.get_direction(direction_id):
            raise ValueError(f"research-direction:{direction_id} already exists")
        owner_root = projects.resolve_root("skill", owner_skill_id, required=False)
        if owner_root.exists():
            raise ValueError(f"artifact custodian skill:{owner_skill_id} already exists")
        created_owner = False
        try:
            projects.create("skill", owner_skill_id, template="research_direction")
            created_owner = True
            projects.update_metadata(
                "skill",
                owner_skill_id,
                title=f"{str(title or direction_id).strip()} — research assets",
                description=(
                    "Project-only artifact custody and implementation source for "
                    f"research-direction:{direction_id}. {str(description or '').strip()}"
                ).strip(),
            )
            self.repository.initialize(
                direction_id,
                str(title or direction_id).strip(),
                description=str(description or "").strip(),
                tags=list(tags or []),
                artifact_owner_skill_id=owner_skill_id,
            )
        except Exception:
            if created_owner and owner_root.is_dir():
                shutil.rmtree(owner_root)
            raise
        self.repository.activity(
            direction_id,
            "intake",
            "direction_created",
            f"Research direction and artifact custodian skill:{owner_skill_id} were created.",
            {
                "direction_ref": f"research-direction:{direction_id}",
                "artifact_owner_ref": f"skill:{owner_skill_id}",
            },
            actor=actor,
            subject_ref=f"research-direction:{direction_id}",
        )
        return self.get(direction_id)

    def list_directions(self, *, limit: int = 500) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for state in self.repository.list_directions(limit=limit):
            direction_id = str(state["direction_id"])
            owner_skill_id = str(state.get("artifact_owner_skill_id") or direction_id)
            try:
                bundle = artifact_context.source_bundle(owner_skill_id, audience=_FORMULATION_AUDIENCE)
                custody_error = None
            except Exception as exc:
                bundle = {"digest": None, "sources": [], "generation": 0}
                custody_error = _bounded_text(exc, 500)
            prototype = self.repository.get_prototype((state or {}).get("current_prototype_digest"))
            next_steps = (
                self._next_steps(state or {}, bundle, prototype)
                if state
                else [{"id": "initialize", "label": "Initialize direction", "reason": "The direction ledger is not initialized."}]
            )
            activity = self.repository.activities(direction_id, limit=1) if state else []
            latest = activity[-1] if activity else None
            items.append(
                {
                    "id": direction_id,
                    "ref": f"research-direction:{direction_id}",
                    "title": state["title"],
                    "description": state.get("description") or "",
                    "tags": list(state.get("tags") or []),
                    "direction_id": direction_id,
                    "status": str((state or {}).get("status") or "not_initialized"),
                    "stage": str((latest or {}).get("stage") or (state or {}).get("status") or "not_initialized"),
                    "generation": int((state or {}).get("generation") or 0),
                    "updated_at": (latest or {}).get("created_at") or (state or {}).get("updated_at"),
                    "last_activity": latest,
                    "next_step": next_steps[0] if next_steps else None,
                    "blocker": (
                        next_steps[0].get("reason")
                        if next_steps and next_steps[0].get("id") in {"initialize", "attach_sources", "refresh_formulation", "resolve_questions"}
                        else None
                    ),
                    "automation_status": "not_started",
                    "current_prototype_digest": (state or {}).get("current_prototype_digest"),
                    "automation_brief_digest": (state or {}).get("automation_brief_digest"),
                    "artifact_owner_ref": f"skill:{owner_skill_id}",
                    "aggregate_health": "degraded" if custody_error else "ready",
                    "projection_error": custody_error,
                    "task_count": len(self.repository.list_tasks(direction_id)),
                }
            )
        return {"ok": True, "items": items, "count": len(items)}

    def initialize(self, direction_id: str, title: str, *, actor: str = "user:local") -> dict[str, Any]:
        token = _direction_id(direction_id)
        admitted = self._require_direction_project(token)
        legacy_project = admitted.get("project")
        self.repository.initialize(
            token,
            str(title or token).strip(),
            artifact_owner_skill_id=str(admitted["artifact_owner_skill_id"]),
            legacy_project_ref=(legacy_project or {}).get("ref"),
        )
        self.repository.activity(
            token,
            "intake",
            "ready",
            "Research direction initialized.",
            {"legacy_project_ref": (legacy_project or {}).get("ref")},
            actor=actor,
            subject_ref=f"research-direction:{token}",
        )
        return self.get(token)

    def attach_source(
        self,
        direction_id: str,
        path: str,
        *,
        group_id: str = "part0",
        name: str | None = None,
        role: str = "source",
        visibility_profile: str = "shared",
        actor: str = "user:local",
        cleanup_staging: bool = False,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        token = _direction_id(direction_id)
        current = self.repository.get_direction(token)
        if not current:
            self.initialize(token, token, actor=actor)
            current = self.repository.get_direction(token)
        if (current or {}).get("accepted_prototype_digest"):
            raise ValueError("accepted research inputs are immutable; start an explicit new formulation cycle before adding artifacts")
        staging_source: Path | None = None
        if cleanup_staging:
            staging_source = Path(path).resolve()
            ctx = get_ctx()
            allowed_roots = {
                skill_upload_dir(
                    Path(ctx.paths.skills_dir()),
                    "research_orchestrator_skill",
                    purpose="research_direction_intake",
                )
            }
            for skills_root in (Path(ctx.paths.skills_dir()), Path(ctx.paths.dev_skills_dir())):
                runtime_root = skills_root / ".runtime" / "research_orchestrator_skill"
                allowed_roots.update(
                    item.resolve()
                    for item in runtime_root.glob("v*/data/files/uploads/research_direction_intake")
                    if item.is_dir()
                )
            runtime_internal = str(os.getenv("ADAOS_SKILL_INTERNAL_DATA_ROOT") or "").strip()
            if runtime_internal:
                allowed_roots.add(
                    (Path(runtime_internal).resolve().parent / "files" / "uploads" / "research_direction_intake").resolve()
                )
            if not any(staging_source == root or root in staging_source.parents for root in allowed_roots):
                raise ValueError("cleanup_staging is only admitted for the orchestrator intake upload directory")
        owner_skill_id = self._artifact_owner_id(token)
        result = artifact_context.add_path(
            owner_skill_id,
            group_id,
            path,
            name=name,
            role=role,
            origin={"kind": "orchestrator_intake", "actor": actor},
            context_policy=_context_profile(visibility_profile),
            replace_existing=replace_existing,
        )
        staging_cleanup = {"requested": bool(cleanup_staging), "removed": False}
        if staging_source is not None:
            staging_source.unlink(missing_ok=True)
            staging_cleanup["removed"] = not staging_source.exists()
        bundle = artifact_context.source_bundle(owner_skill_id, audience=_FORMULATION_AUDIENCE)
        persisted = self.repository.get_direction(token) or {}
        state = (
            persisted
            if str(persisted.get("current_bundle_digest") or "") == str(bundle["digest"])
            else self.repository.set_bundle(token, str(bundle["digest"]))
        )
        self.repository.activity(
            token,
            "intake",
            "source_replaced"
            if result.get("replaced")
            else "source_added"
            if not result.get("idempotent")
            else "source_reused",
            f"Artifact {result['artifact']['path']} is bound to {result['group']['ref']} generation {result['group']['generation']}.",
            {
                "artifact_digest": result["artifact"]["digest"],
                "replaced_artifact_digest": (result.get("previous_artifact") or {}).get("digest"),
                "artifact_group_digest": result["group"]["digest"],
                "bundle_digest": bundle["digest"],
                "actor": actor,
            },
        )
        return {
            "ok": True,
            "artifact": result["artifact"],
            "artifact_group": result["group"],
            "source_bundle": bundle,
            "direction": state,
            "staging_cleanup": staging_cleanup,
        }

    def set_source_visibility(
        self,
        direction_id: str,
        group_id: str,
        artifact_id: str,
        visibility_profile: str,
        *,
        actor: str = "user:local",
    ) -> dict[str, Any]:
        token = _direction_id(direction_id)
        state = self.repository.get_direction(token)
        if not state:
            raise ValueError("research direction is not initialized")
        if state.get("accepted_prototype_digest"):
            raise ValueError("accepted research inputs are immutable; start a new formulation cycle")
        owner_skill_id = self._artifact_owner_id(token)
        result = artifact_context.set_context_policy(
            owner_skill_id,
            group_id,
            artifact_id,
            _context_profile(visibility_profile),
        )
        bundle = artifact_context.source_bundle(owner_skill_id, audience=_FORMULATION_AUDIENCE)
        if str(state.get("current_bundle_digest") or "") != str(bundle["digest"]):
            state = self.repository.set_bundle(token, str(bundle["digest"]))
        self.repository.activity(
            token,
            "intake",
            "visibility_changed",
            f"Artifact {artifact_id} visibility changed to {visibility_profile}.",
            {
                "group_id": group_id,
                "artifact_id": artifact_id,
                "visibility_profile": visibility_profile,
                "artifact_group_digest": result["group"]["digest"],
                "formulation_bundle_digest": bundle["digest"],
                "actor": actor,
            },
        )
        return {
            "ok": True,
            "artifact": result["artifact"],
            "artifact_group": result["group"],
            "source_bundle": bundle,
            "direction": state,
        }

    def get(self, direction_id: str) -> dict[str, Any]:
        token = _direction_id(direction_id)
        admitted = self._require_direction_project(token)
        project = admitted.get("project")
        owner_skill_id = str(admitted.get("artifact_owner_skill_id") or token)
        state = self.repository.get_direction(token)
        bundle = artifact_context.source_bundle(owner_skill_id, audience=_FORMULATION_AUDIENCE)
        if not state:
            return {
                "ok": True,
                "initialized": False,
                "direction": {
                    "id": token,
                    "direction_id": token,
                    "title": str((project or {}).get("title") or token),
                    "status": "not_initialized",
                    "generation": 0,
                    "project_ref": (project or {}).get("ref"),
                    "artifact_owner_ref": f"skill:{owner_skill_id}",
                },
                "project": project,
                "agenda": None,
                "active_task": None,
                "implementation_tracks": [],
                "active_implementation_track": None,
                "accepted_compilation": None,
                "artifact_groups": artifact_context.groups(owner_skill_id),
                "source_bundle": bundle,
                "current_prototype": None,
                "prototype_stale": False,
                "formulation": {
                    "admission_decision": "needs_discussion",
                    "admission_blockers": ["The direction ledger is not initialized."],
                    "can_accept": False,
                    "context_coverage": {},
                },
                "accepted_prototype": None,
                "automation_brief": None,
                "development_session": None,
                "builder_url": None,
                "next_steps": [
                    {
                        "id": "initialize",
                        "label": "Initialize direction",
                        "reason": "The direction ledger is not initialized.",
                    }
                ],
            }
        prototype = self.repository.get_prototype(state.get("current_prototype_digest"))
        accepted = self.repository.get_prototype(state.get("accepted_prototype_digest"))
        brief = self.repository.get_brief(state.get("automation_brief_digest"))
        tasks = self.repository.list_tasks(token)
        active_task = self.repository.get_task(state.get("active_task_id"))
        tracks = (
            self.repository.list_tracks(str(active_task["task_id"]))
            if active_task
            else []
        )
        active_track = next(
            (item for item in reversed(tracks) if item.get("development_session_id")),
            tracks[-1] if tracks else None,
        )
        if active_track and str(active_track.get("project_ref") or "").startswith("project:"):
            try:
                project = compositions.get(str(active_track["project_ref"]).partition(":")[2])
            except Exception:
                pass
        sessions = (
            development_sessions.list_sessions(project_id=str(project["id"]), limit=20)
            if brief and project
            else []
        )
        development_session = next(
            (
                item
                for item in sessions
                if item.get("session_id") == (active_track or {}).get("development_session_id")
            ),
            sessions[-1] if sessions else None,
        )
        builder_url = None
        if sessions:
            scope = navigation.runtime_scope()
            destination = navigation.webspace_destination(
                zone=str(scope["zone"]),
                subnet_id=str(scope["subnet_id"]),
                webspace_id="desktop-dev",
                space_kind="development",
                expected_scenario_id="builder",
            )
            builder_url = _address_builder_url(
                navigation.build_url(destination, base_url=builder_preview.public_app_base()),
                direction_id=token,
                title=str(state.get("title") or token),
            )
        prototype_stale = bool(
            prototype
            and str(prototype.get("source_bundle_digest") or "") != str(bundle.get("digest") or "")
        )
        admission_review = prototype.get("admission_review") if isinstance((prototype or {}).get("admission_review"), Mapping) else {}
        return {
            "ok": True,
            "initialized": True,
            "direction": {
                **state,
                "ref": f"research-direction:{token}",
                "project_ref": (project or {}).get("ref"),
                "artifact_owner_ref": f"skill:{owner_skill_id}",
            },
            "project": project,
            "agenda": {
                "schema": "adaos.research.agenda.v1",
                "direction_id": token,
                "direction_ref": f"research-direction:{token}",
                "tasks": tasks,
                "active_task_id": (active_task or {}).get("task_id"),
            },
            "active_task": active_task,
            "implementation_tracks": tracks,
            "active_implementation_track": active_track,
            "accepted_compilation": self.repository.get_compilation(
                (active_task or {}).get("accepted_compilation_digest")
            ),
            "artifact_groups": artifact_context.groups(owner_skill_id),
            "source_bundle": bundle,
            "current_prototype": prototype,
            "prototype_stale": prototype_stale,
            "formulation": {
                "admission_decision": admission_review.get("decision") or "needs_discussion",
                "admission_blockers": list(admission_review.get("blockers") or []),
                "can_accept": (
                    bool(prototype)
                    and not prototype_stale
                    and admission_review.get("decision") == "admitted"
                    and str(state.get("accepted_prototype_digest") or "")
                    != str(prototype.get("digest") or "")
                ),
                "context_coverage": (prototype or {}).get("context_coverage") or {},
            },
            "accepted_prototype": accepted,
            "automation_brief": brief,
            "development_session": development_session,
            "builder_url": builder_url,
            "next_steps": self._next_steps(state, bundle, prototype),
        }

    def outline(self, direction_id: str) -> dict[str, Any]:
        """Project one direction aggregate as a generic typed navigation outline."""

        token = _direction_id(direction_id)
        view = self.get(token)
        direction = dict(view["direction"])
        tasks = list((view.get("agenda") or {}).get("tasks") or [])
        tracks = list(view.get("implementation_tracks") or [])
        nodes: list[dict[str, Any]] = []

        def add(
            node_id: str,
            title: str,
            *,
            parent_id: str | None = None,
            kind: str,
            tab: str,
            status: str | None = None,
            subtitle: str | None = None,
            icon: str | None = None,
            task_id: str | None = None,
            track_id: str | None = None,
        ) -> None:
            nodes.append(
                {
                    "node_id": node_id,
                    "parent_id": parent_id,
                    "title": title,
                    "subtitle": subtitle,
                    "kind": kind,
                    "icon": icon,
                    "badge": status,
                    "target": {
                        "tab": tab,
                        "subject_ref": (
                            f"implementation-track:{track_id}"
                            if track_id
                            else f"research-task:{task_id}"
                            if task_id
                            else f"research-direction:{token}"
                        ),
                        "task_id": task_id,
                        "implementation_track_id": track_id,
                    },
                }
            )

        root_id = f"direction:{token}"
        add(
            root_id,
            str(direction.get("title") or token),
            kind="research_direction",
            tab="overview",
            status=str(direction.get("status") or "intake"),
            subtitle=str(direction.get("description") or ""),
            icon="flask-outline",
        )
        add(
            f"{root_id}:sources",
            "Sources",
            parent_id=root_id,
            kind="artifact_collection",
            tab="artifacts",
            status=str(len(view.get("source_bundle", {}).get("sources") or [])),
            icon="file-tray-stacked-outline",
        )
        agenda_id = f"{root_id}:agenda"
        add(
            agenda_id,
            "Research agenda",
            parent_id=root_id,
            kind="research_agenda",
            tab="overview",
            status=str(len(tasks)),
            icon="list-outline",
        )
        for task in tasks:
            task_id = str(task["task_id"])
            task_node_id = f"task:{task_id}"
            add(
                task_node_id,
                str(task.get("title") or task_id),
                parent_id=agenda_id,
                kind="research_task",
                tab="formulation",
                status=str(task.get("status") or "draft"),
                subtitle=str(task.get("research_question") or ""),
                icon="beaker-outline",
                task_id=task_id,
            )
            add(
                f"{task_node_id}:compilation",
                "Accepted compilation",
                parent_id=task_node_id,
                kind="research_compilation",
                tab="compilation",
                status="ready" if task.get("accepted_compilation_digest") else "pending",
                icon="git-network-outline",
                task_id=task_id,
            )
            tracks_id = f"{task_node_id}:implementations"
            task_tracks = [item for item in tracks if item.get("task_id") == task_id]
            add(
                tracks_id,
                "Implementation tracks",
                parent_id=task_node_id,
                kind="implementation_collection",
                tab="development",
                status=str(len(task_tracks)),
                icon="construct-outline",
                task_id=task_id,
            )
            for track in task_tracks:
                track_id = str(track["track_id"])
                track_metadata = dict(track.get("metadata") or {})
                add(
                    f"track:{track_id}",
                    str(track.get("title") or track_id),
                    parent_id=tracks_id,
                    kind="implementation_track",
                    tab="development",
                    status=str(track.get("status") or "planned"),
                    subtitle=str(track.get("primary_target_ref") or ""),
                    icon="code-slash-outline",
                    task_id=task_id,
                    track_id=track_id,
                )
                if track_metadata.get("packet_ref"):
                    add(
                        f"track:{track_id}:packet",
                        "Input packet",
                        parent_id=f"track:{track_id}",
                        kind="calibration_packet",
                        tab="development",
                        status="frozen",
                        subtitle=str(track_metadata.get("packet_digest") or ""),
                        icon="document-lock-outline",
                        task_id=task_id,
                        track_id=track_id,
                    )
                if track_metadata.get("result_ref"):
                    add(
                        f"track:{track_id}:result",
                        "Evaluation result",
                        parent_id=f"track:{track_id}",
                        kind="evaluation_result",
                        tab="evidence",
                        status=(
                            "passed"
                            if (track_metadata.get("metrics") or {}).get("evidence_valid_completion")
                            else "failed"
                        ),
                        subtitle=str(track_metadata.get("result_digest") or ""),
                        icon="shield-checkmark-outline",
                        task_id=task_id,
                        track_id=track_id,
                    )
            for suffix, title, kind, tab, icon in (
                ("studies", "Studies", "study_collection", "studies", "analytics-outline"),
                ("evidence", "Evidence", "evidence_collection", "evidence", "shield-checkmark-outline"),
                ("releases", "Releases", "release_collection", "releases", "cube-outline"),
            ):
                add(
                    f"{task_node_id}:{suffix}",
                    title,
                    parent_id=task_node_id,
                    kind=kind,
                    tab=tab,
                    status="planned",
                    icon=icon,
                    task_id=task_id,
                )
        add(
            f"{root_id}:activity",
            "Activity journal",
            parent_id=root_id,
            kind="activity_journal",
            tab="activity",
            status=str(len(self.repository.activities(token, limit=500))),
            icon="pulse-outline",
        )
        add(
            f"{root_id}:help",
            "Help",
            parent_id=root_id,
            kind="help",
            tab="help",
            icon="help-circle-outline",
        )
        return {
            "ok": True,
            "direction_ref": f"research-direction:{token}",
            "nodes": nodes,
        }

    def lineage(self, direction_id: str) -> dict[str, Any]:
        token = _direction_id(direction_id)
        view = self.get(token)
        task = view.get("active_task") or {}
        tracks = list(view.get("implementation_tracks") or [])
        local_sources = list((view.get("source_bundle") or {}).get("sources") or [])
        calibration = dict((task.get("metadata") or {}).get("calibration") or {})
        admitted_sources = list(calibration.get("admitted_sources") or [])
        lines = [
            f"# {view['direction'].get('title')}",
            "",
            f"**Direction:** `{view['direction'].get('ref')}`  ",
            f"**Task:** `{task.get('ref') or 'not selected'}` · revision `{task.get('revision') or '—'}`  ",
            f"**Accepted compilation:** `{task.get('accepted_compilation_digest') or 'not available'}`",
            "",
            "## Source custody",
            "",
        ]
        lines.extend(
            f"- **owned** `{item.get('name')}` · `{item.get('digest')}`"
            for item in local_sources
        )
        lines.extend(
            f"- **admitted read-only** `{item.get('ref')}` · context `{item.get('context_digest')}`"
            for item in admitted_sources
        )
        if not local_sources and not admitted_sources:
            lines.append("- No source manifests are connected.")
        lines.extend(["", "## Implementation and evaluation lineage", ""])
        for track in tracks:
            metadata = dict(track.get("metadata") or {})
            metrics = dict(metadata.get("metrics") or {})
            failure = metadata.get("failure") if isinstance(metadata.get("failure"), Mapping) else {}
            usage = dict(metadata.get("budget_usage") or {})
            lines.extend(
                [
                    f"### {track.get('condition_id') or track.get('title')}",
                    "",
                    f"- Track: `{track.get('ref')}` · `{track.get('status')}`",
                    f"- Packet: `{metadata.get('packet_ref') or 'not available'}` · `{metadata.get('packet_digest') or '—'}`",
                    f"- Candidate: `{metadata.get('candidate_ref') or track.get('primary_target_ref') or 'not available'}`",
                    f"- Result: `{metadata.get('result_ref') or 'not available'}` · `{metadata.get('result_digest') or '—'}`",
                    f"- Evidence-valid: `{metrics.get('evidence_valid_completion') if metrics else 'not evaluated'}` · protocol drift: `{metrics.get('protocol_drift') if metrics else '—'}`",
                    f"- Failure: `{failure.get('stage') or '—'}` · {failure.get('detail') or '—'}",
                    f"- Budget: tokens `{usage.get('model_tokens', '—')}`, wall `{usage.get('wall_seconds', '—')}` s, attempts `{usage.get('attempts', '—')}`",
                    "",
                ]
            )
        if not tracks:
            lines.append("No implementation tracks are connected.")
        return {
            "ok": True,
            "direction_ref": view["direction"].get("ref"),
            "task_ref": task.get("ref"),
            "local_sources": local_sources,
            "admitted_sources": admitted_sources,
            "tracks": tracks,
            "summary": calibration.get("summary") or {},
            "content": "\n".join(lines),
        }

    def adopt_calibration_lineage(
        self,
        direction_id: str,
        evaluator_task_id: str,
        *,
        budget_view: str = "fixed_downstream",
        actor: str = "user:local",
    ) -> dict[str, Any]:
        """Adopt evaluator-owned immutable records without crossing its storage boundary."""

        token = _direction_id(direction_id)
        direction = self.repository.get_direction(token)
        if not direction:
            raise ValueError("research direction is not initialized")
        task = self.repository.get_task(direction.get("active_task_id"))
        if not task:
            raise ValueError("research direction has no active task")
        response = invoke_skill(
            "research_evaluator_skill",
            "get_calibration_lineage",
            {
                "task_id": str(evaluator_task_id),
                "budget_view": str(budget_view),
            },
            timeout=120,
        )
        if not isinstance(response, Mapping) or not response.get("ok"):
            raise RuntimeError("research evaluator did not return an immutable lineage")
        external_task = response.get("task")
        if not isinstance(external_task, Mapping):
            raise RuntimeError("research evaluator returned no calibration task")
        packets = [dict(item) for item in response.get("packets") or [] if isinstance(item, Mapping)]
        results = [dict(item) for item in response.get("results") or [] if isinstance(item, Mapping)]
        by_attempt = {
            (
                str(item.get("arm_id") or ""),
                int(item.get("attempt_index") or 0),
                str(item.get("budget_view") or ""),
            ): item
            for item in results
        }
        canonical_task_ref = str(task["ref"])
        alias = self.repository.put_alias(
            f"calibration-task:{evaluator_task_id}",
            canonical_task_ref,
            {
                "kind": "evaluation_projection",
                "task_digest": external_task.get("digest"),
                "budget_view": budget_view,
                "source_owner": "skill:research_evaluator_skill",
            },
        )
        admitted_sources: dict[str, dict[str, Any]] = {}
        tracks = []
        for packet in packets:
            arm_id = str(packet.get("arm_id") or "")
            attempt_index = int(packet.get("attempt_index") or 0)
            result = by_attempt.get((arm_id, attempt_index, str(packet.get("budget_view") or "")))
            candidate_id = str(packet.get("candidate_id") or "")
            suffix = re.sub(r"[^a-z0-9]+", "-", arm_id.lower()).strip("-")
            track_id = _direction_id(f"{task['task_id']}.{suffix}.a{attempt_index}")
            metadata = {
                "schema": "adaos.research.calibration_track_metadata.v1",
                "external_task_ref": f"calibration-task:{evaluator_task_id}",
                "external_task_digest": external_task.get("digest"),
                "packet_ref": f"calibration-packet:{packet.get('packet_id')}",
                "packet_digest": packet.get("digest"),
                "budget_view": packet.get("budget_view"),
                "paired_seed": packet.get("paired_seed"),
                "candidate_ref": f"skill:{candidate_id}" if candidate_id else None,
                "result_ref": f"calibration-result:{result.get('result_id')}" if result else None,
                "result_digest": result.get("digest") if result else None,
                "metrics": copy.deepcopy((result or {}).get("metrics") or {}),
                "failure": copy.deepcopy((result or {}).get("failure")),
                "budget_usage": copy.deepcopy((result or {}).get("budget_usage") or {}),
            }
            existed = self.repository.get_track(track_id) is not None
            track = self.repository.create_track(
                token,
                str(task["task_id"]),
                track_id=track_id,
                title=f"{arm_id} · attempt {attempt_index}",
                project_ref=f"project:{candidate_id}" if candidate_id else None,
                primary_target_ref=f"skill:{candidate_id}" if candidate_id else None,
                condition_id=arm_id,
                metadata=metadata,
            )
            if result:
                passed = bool((result.get("metrics") or {}).get("evidence_valid_completion"))
                track = self.repository.record_track_evaluation(
                    track_id,
                    status="evaluated_passed" if passed else "evaluated_failed",
                    metadata=metadata,
                )
            if candidate_id:
                self.repository.put_alias(
                    f"project:{candidate_id}",
                    str(track["ref"]),
                    {
                        "kind": "legacy_calibration_candidate",
                        "packet_digest": packet.get("digest"),
                        "result_digest": (result or {}).get("digest"),
                    },
                )
            for source in packet.get("artifact_inputs") or []:
                source_ref = str(source.get("ref") or "")
                if source_ref:
                    admitted_sources[source_ref] = {
                        **copy.deepcopy(dict(source)),
                        "ownership": "admitted_read_only",
                        "source_owner": source_ref.rsplit("/", 1)[0],
                    }
            if not existed:
                self.repository.activity(
                    token,
                    "evaluation",
                    str(track["status"]),
                    f"Imported immutable calibration lineage for {arm_id} attempt {attempt_index}.",
                    {
                        "task_ref": canonical_task_ref,
                        "track_ref": track["ref"],
                        "packet_digest": packet.get("digest"),
                        "result_digest": (result or {}).get("digest"),
                    },
                    actor=actor,
                    origin="skill:research_evaluator_skill",
                    subject_ref=str(track["ref"]),
                )
            tracks.append(track)
        task = self.repository.merge_task_metadata(
            str(task["task_id"]),
            {
                "calibration": {
                    "external_task_ref": f"calibration-task:{evaluator_task_id}",
                    "task_digest": external_task.get("digest"),
                    "budget_view": budget_view,
                    "summary": copy.deepcopy(response.get("summary") or {}),
                    "admitted_sources": list(admitted_sources.values()),
                    "track_refs": [item["ref"] for item in tracks],
                }
            },
        )
        return {
            "ok": True,
            "direction_ref": f"research-direction:{token}",
            "task": task,
            "tracks": tracks,
            "alias": alias,
            "summary": copy.deepcopy(response.get("summary") or {}),
            "source_owner": "skill:research_evaluator_skill",
        }

    def sync_source_bundle(self, direction_id: str, *, actor: str = "user:local") -> dict[str, Any]:
        token = _direction_id(direction_id)
        state = self.repository.get_direction(token)
        if not state:
            self.initialize(token, token, actor=actor)
            state = self.repository.get_direction(token)
        bundle = artifact_context.source_bundle(
            self._artifact_owner_id(token), audience=_FORMULATION_AUDIENCE
        )
        if not bundle.get("sources"):
            raise ValueError("direction skill artifact groups are empty")
        changed = str((state or {}).get("current_bundle_digest") or "") != str(bundle["digest"])
        if changed:
            self.repository.set_bundle(token, str(bundle["digest"]))
            self.repository.activity(
                token,
                "intake",
                "bundle_synchronized",
                f"Artifact-set generation {bundle['generation']} is ready for formulation.",
                {"bundle_digest": bundle["digest"], "actor": actor},
            )
        return {"ok": True, "changed": changed, "direction": self.repository.get_direction(token), "source_bundle": bundle}

    def open_builder_session(
        self,
        direction_id: str,
        *,
        builder_webspace_id: str = "desktop-dev",
        base_url: str | None = None,
    ) -> dict[str, Any]:
        state = self.get(direction_id)
        session = state.get("development_session")
        if not isinstance(session, Mapping):
            raise ValueError("accept the ResearchPrototype before opening a Builder Development Session")
        brief = state.get("automation_brief")
        if not isinstance(brief, Mapping):
            raise ValueError("the accepted AutomationBrief is unavailable")
        attached = development_sessions.attach_instruction(
            str(session["session_id"]),
            "automation_brief",
            brief,
            expected_digest=str(session["handoff"]["automation_brief_digest"]),
        )
        session = attached["session"]
        binding = development_sessions.bind(str(session["session_id"]), builder_webspace_id)
        track = state.get("active_implementation_track")
        target_ref = str((track or {}).get("primary_target_ref") or session["focus"]["ref"])
        target_kind, _, target_id = target_ref.partition(":")
        selected = builder_preview.select_project(
            target_kind,
            target_id,
            source_webspace_id=builder_webspace_id,
            ensure_ready=True,
            wait_for_rebuild=True,
            publish_event=True,
        )
        scope = navigation.runtime_scope()
        destination = navigation.webspace_destination(
            zone=str(scope["zone"]),
            subnet_id=str(scope["subnet_id"]),
            webspace_id=builder_webspace_id,
            space_kind="development",
            expected_scenario_id="builder",
        )
        builder_url = _address_builder_url(
            navigation.build_url(destination, base_url=base_url or builder_preview.public_app_base()),
            direction_id=direction_id,
            title=str(state["direction"].get("title") or direction_id),
        )
        return {
            "ok": True,
            "url": builder_url,
            "destination": destination,
            "binding": binding["binding"],
            "session": session,
            "instruction": attached["instruction"],
            "builder_selection": selected,
            "codex_started": False,
        }

    @staticmethod
    def _next_steps(state: Mapping[str, Any], bundle: Mapping[str, Any], prototype: Mapping[str, Any] | None) -> list[dict[str, str]]:
        if prototype and str(prototype.get("source_bundle_digest") or "") != str(bundle.get("digest") or ""):
            return [{"id": "refresh_formulation", "label": "Обновить постановку", "reason": "Artifact groups изменились; новая ревизия должна сослаться на актуальный digest."}]
        if state.get("status") == "handoff_ready":
            return [
                {"id": "inspect_brief", "label": "Проверить Automation Brief", "reason": "Он фиксирует точное задание для Codex."},
                {"id": "start_builder_automation", "label": "Запустить Builder Automation", "reason": "Это отдельное решение; ОИ не запускает Codex автоматически."},
            ]
        if not bundle.get("sources"):
            return [{"id": "attach_sources", "label": "Добавить исходные артефакты", "reason": "Постановка должна ссылаться на digest-bound artifact group внутри навыка."}]
        if not prototype:
            return [{"id": "discuss", "label": "Обсудить постановку", "reason": "ОИ создаст первую структурированную ревизию ResearchPrototype."}]
        readiness = prototype.get("readiness") if isinstance(prototype.get("readiness"), Mapping) else {}
        review = prototype.get("admission_review") if isinstance(prototype.get("admission_review"), Mapping) else {}
        if review.get("decision") != "admitted" or readiness.get("decision") != "ready_for_automation" or readiness.get("blocking_questions"):
            return [{"id": "resolve_questions", "label": "Снять блокирующие вопросы", "reason": "Принять можно только готовую к автоматизации ревизию."}]
        return [{"id": "accept_prototype", "label": "Принять точную ревизию", "reason": "Acceptance создаст приватный локальный checkpoint и digest-bound Automation Brief; исходные материалы не публикуются."}]

    def record_prototype(
        self,
        direction_id: str,
        value: Mapping[str, Any],
        *,
        actor: str = "user:local",
        context_coverage: Mapping[str, Any] | None = None,
        formulation_trace: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = _direction_id(direction_id)
        state = self.repository.get_direction(token)
        if not state:
            raise ValueError("research direction is not initialized")
        if state.get("accepted_prototype_digest"):
            raise ValueError("accepted formulation is immutable; create a new Builder change before revising it")
        bundle = artifact_context.source_bundle(self._artifact_owner_id(token), audience=_FORMULATION_AUDIENCE)
        if not bundle.get("sources"):
            raise ValueError("at least one source artifact is required")
        previous = self.repository.get_prototype(state.get("current_prototype_digest"))
        task = self.repository.get_task(state.get("active_task_id"))
        if not task:
            raise ValueError("research direction has no active ResearchTask")
        source_context = self._source_context(bundle) if context_coverage is None else None
        coverage = dict(context_coverage or (source_context or {}).get("coverage") or {})
        prototype = materialize_prototype(
            value,
            direction_id=token,
            source_bundle_digest=str(bundle["digest"]),
            context_coverage=coverage,
            revision=int(previous.get("revision") or 0) + 1 if previous else 1,
            parent_digest=str(previous["digest"]) if previous else None,
            actor=actor,
            formulation_trace=formulation_trace,
            task=task,
            artifact_owner_skill_id=self._artifact_owner_id(token),
        )
        admission_issues = prototype_admission_issues(prototype)
        readiness = value.get("readiness") if isinstance(value.get("readiness"), Mapping) else {}
        if admission_issues and readiness.get("decision") == "ready_for_automation":
            revised_value = dict(value)
            revised_value["readiness"] = {
                "decision": "needs_discussion",
                "blocking_questions": list(dict.fromkeys([*list(readiness.get("blocking_questions") or []), *admission_issues])),
            }
            prototype = materialize_prototype(
                revised_value,
                direction_id=token,
                source_bundle_digest=str(bundle["digest"]),
                context_coverage=coverage,
                revision=int(previous.get("revision") or 0) + 1 if previous else 1,
                parent_digest=str(previous["digest"]) if previous else None,
                actor=actor,
                formulation_trace=formulation_trace,
                task=task,
                artifact_owner_skill_id=self._artifact_owner_id(token),
            )
        stored = self.repository.put_prototype(token, prototype, task_id=str(task["task_id"]))
        self.repository.activity(
            token,
            "formulation",
            "candidate_admitted" if stored.get("admission_review", {}).get("decision") == "admitted" else "candidate_draft",
            f"ResearchPrototype revision {stored['revision']} passed the automation gate." if stored.get("admission_review", {}).get("decision") == "admitted" else f"ResearchPrototype revision {stored['revision']} remains a reviewable draft.",
            {
                "prototype_digest": stored["digest"],
                "source_bundle_digest": stored["source_bundle_digest"],
                "admission_decision": stored.get("admission_review", {}).get("decision"),
                "admission_blockers": stored.get("admission_review", {}).get("blockers") or [],
                "actor": actor,
            },
        )
        return {"ok": True, "prototype": stored, "direction": self.repository.get_direction(token), "next_steps": self._next_steps(self.repository.get_direction(token) or {}, bundle, stored)}

    def _source_context(self, bundle: Mapping[str, Any], *, query: str = "") -> dict[str, Any]:
        selected: list[dict[str, Any]] = []
        sources = [item for item in bundle.get("sources") or [] if isinstance(item, Mapping)]
        remaining = 48_000
        per_source = min(28_000, max(6_000, remaining // max(1, len(sources))))
        unreadable: list[str] = []
        coverage_items: list[dict[str, Any]] = []
        for source in sources:
            if remaining <= 0:
                break
            media = str(source.get("media_type") or "")
            name = str(source.get("name") or "source")
            excerpt = ""
            artifact_ref = str(source.get("artifact_ref") or "")
            try:
                artifact_id = str(source.get("source_id") or "")
                group_id = str(source.get("group_id") or "")
                extracted = artifact_context.extract_text(
                    str(bundle.get("skill_ref") or "skill:research").partition(":")[2],
                    group_id,
                    artifact_id,
                    max_characters=min(per_source, remaining),
                    query=query,
                )
                excerpt = str(extracted.get("content") or "")
                item_coverage = extracted.get("coverage") if isinstance(extracted.get("coverage"), Mapping) else {}
                provenance_refs = [str(item.get("ref")) for item in extracted.get("provenance") or [] if isinstance(item, Mapping) and item.get("ref")]
                coverage_items.append(
                    {
                        "artifact_ref": artifact_ref or extracted.get("artifact_ref"),
                        "digest": source.get("digest"),
                        "strategy": item_coverage.get("strategy") or "unknown",
                        "selected_characters": int(item_coverage.get("selected_characters") or 0),
                        "truncated": bool(item_coverage.get("truncated")),
                        "provenance_refs": provenance_refs,
                        "detail": dict(item_coverage),
                    }
                )
            except (artifact_context.ArtifactContextError, UnicodeDecodeError, ValueError):
                excerpt = "[binary source; use structural inventory]"
                unreadable.append(artifact_ref or name)
            remaining -= len(excerpt)
            selected.append(
                {
                    "name": name,
                    "artifact_ref": artifact_ref,
                    "digest": source.get("digest"),
                    "media_type": media,
                    "role": source.get("role"),
                    "analysis": source.get("analysis"),
                    "excerpt": excerpt,
                }
            )
        truncated_sources = [str(item["artifact_ref"]) for item in coverage_items if item.get("truncated")]
        coverage = {
            "sources_total": len(sources),
            "sources_represented": len(selected),
            "selected_characters": sum(int(item.get("selected_characters") or 0) for item in coverage_items),
            "truncated_sources": truncated_sources,
            "unreadable_sources": unreadable,
            "items": coverage_items,
        }
        return {"sources": selected, "coverage": coverage}

    @staticmethod
    def _dialog(payload: Mapping[str, Any]) -> dict[str, str | None]:
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
        webspace = str(payload.get("webspace_id") or meta.get("webspace_id") or "desktop")
        direction_id = str(payload.get("direction_id") or "research")
        return {
            "webspace_id": webspace,
            "conversation_id": str(payload.get("conversation_id") or meta.get("conversation_id") or f"conv.skill.research_orchestrator_skill.{direction_id}.{webspace}"),
            "thread_id": str(payload.get("thread_id") or meta.get("thread_id") or f"research:{direction_id}"),
            "request_id": str(meta.get("request_id") or "") or None,
            "turn_trace_id": str(meta.get("turn_trace_id") or "") or None,
        }

    @staticmethod
    def _emit(message: str, dialog: Mapping[str, Any], *, group_id: str, phase: str, status: str, seq: int = 0) -> None:
        try:
            sdk_chat.send(
                message,
                conversation_id=str(dialog["conversation_id"]),
                webspace_id=str(dialog["webspace_id"]),
                channel_id="research_orchestrator",
                owner="skill:research_orchestrator_skill",
                route_id="voice_chat",
                actor_id="agent:research_orchestrator_skill:researcher",
                actor_label="Исследователь",
                request_id=dialog.get("request_id"),
                turn_trace_id=dialog.get("turn_trace_id"),
                thread_id=str(dialog.get("thread_id") or "") or None,
                meta={"progress_group_id": group_id, "progress_phase": phase, "progress_status": status, "progress_seq": seq, "progress_label": "Research formulation"},
            )
        except Exception:
            pass

    @staticmethod
    def _emit_directive(
        directive: Mapping[str, Any],
        dialog: Mapping[str, Any],
        *,
        group_id: str,
    ) -> None:
        try:
            sdk_chat.send(
                (
                    f"External research directive · {directive['actor_id']} · {directive['origin']}\n\n"
                    f"{directive['text']}"
                ),
                conversation_id=str(dialog["conversation_id"]),
                webspace_id=str(dialog["webspace_id"]),
                channel_id="research_orchestrator",
                owner="skill:research_orchestrator_skill",
                route_id="voice_chat",
                actor_id=str(directive["actor_id"]),
                actor_label=str(directive["actor_label"]),
                request_id=directive.get("request_id") or dialog.get("request_id"),
                turn_trace_id=directive.get("turn_trace_id") or dialog.get("turn_trace_id"),
                thread_id=str(dialog.get("thread_id") or "") or None,
                meta={
                    "message_kind": "research.directive",
                    "invocation_origin": directive["origin"],
                    "directive_digest": directive["text_digest"],
                    "progress_group_id": group_id,
                    "progress_phase": "directive",
                    "progress_status": "recorded",
                    "progress_seq": -1,
                    "progress_label": "Research directive",
                },
            )
        except Exception:
            pass

    def _run_formulation_stage(
        self,
        *,
        direction_id: str,
        run_id: str,
        stage_index: int,
        stage_name: str,
        stage_input: Mapping[str, Any],
        rules: list[str],
        allowed_source_refs: set[str],
        model: str | None,
        dialog: Mapping[str, Any],
        group_id: str,
        request_id_prefix: str,
        max_tokens: int,
        expected_effect_direction: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        schema = stage_schema(stage_name, allowed_source_refs=allowed_source_refs)
        constrained_schema = provider_schema(schema)
        input_digest = stage_digest(stage_input)
        schema_digest = stage_digest(schema)
        provider_schema_digest = stage_digest(constrained_schema)
        task_scope = f"research.formulation.{stage_name}"
        profile_scope = str(os.getenv("ADAOS_RESEARCH_LLM_PROFILE_SCOPE") or "development").strip().lower()
        base_prompt = {
            "schema": "adaos.research.formulation_stage_input.v1",
            "stage": stage_name,
            "input": dict(stage_input),
            "rules": list(rules),
        }
        self.repository.activity(
            direction_id,
            "formulation",
            "stage_started",
            f"Formulation stage {stage_index}/3 ({stage_name}) started.",
            {
                "run_id": run_id,
                "stage": stage_name,
                "stage_index": stage_index,
                "input_digest": input_digest,
                "schema_digest": schema_digest,
                "provider_schema_digest": provider_schema_digest,
                "model_profile_scope": profile_scope,
            },
        )
        self._emit(
            f"Formulation {stage_index}/3: {stage_name}",
            dialog,
            group_id=group_id,
            phase=stage_name,
            status="working",
            seq=stage_index * 100_000,
        )
        jobs: list[dict[str, Any]] = []
        repair_attempt = 0
        structured_output = True
        submitted: dict[str, Any] = {}
        completed: dict[str, Any] = {}
        output_text = ""
        candidate: dict[str, Any] = {}

        def execute(prompt: Mapping[str, Any], *, suffix: str, structured: bool) -> tuple[dict[str, Any], dict[str, Any], str]:
            payload = dict(prompt)
            if not structured:
                payload["output_schema"] = schema
                payload["fallback_note"] = "Provider-native Structured Outputs are unavailable; follow output_schema exactly and return JSON only."
            stage_request_id = f"{request_id_prefix}-{stage_name}{suffix}"
            submitted = llm_client.submit_response_job(
                [
                    {
                        "role": "system",
                        "content": (
                            f"Produce only the {stage_name} artifact. Do not solve later stages. "
                            "Separate source facts, author interpretations, proposals and unresolved choices. "
                            "Return exactly one JSON object with no Markdown or commentary."
                        ),
                    },
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model=model,
                max_tokens=max_tokens,
                request_id=stage_request_id,
                profile_scope=profile_scope,
                text=schema_text_format(stage_name, schema=schema) if structured else {"format": {"type": "json_object"}},
                prompt_cache_key=f"research:{stage_name[:18]}:{input_digest.removeprefix('sha256:')[:24]}",
                stream=True,
                timeout=30,
            )
            job_id = str(submitted.get("job_id") or "")
            if not job_id:
                raise RuntimeError("Root LLM did not return a job_id")
            job_record: dict[str, Any] = {
                "job_id": job_id,
                "request_id": stage_request_id,
                "structured_output": structured,
                "status": "submitted",
            }
            jobs.append(job_record)
            base_url = str((submitted.get("_client") or {}).get("base_url") or "") or None
            durable_phase = ""

            def progress(value: Mapping[str, Any]) -> None:
                nonlocal durable_phase
                seq = int(value.get("seq") or 0)
                label = str(value.get("label") or value.get("phase") or f"{stage_name} working")
                phase = str(value.get("phase") or value.get("status") or label).strip().lower()
                if phase != durable_phase:
                    durable_phase = phase
                    self.repository.activity(
                        direction_id,
                        "formulation",
                        "stage_progress",
                        label,
                        {"run_id": run_id, "stage": stage_name, "job_id": job_id, "progress": dict(value), "coalesced": True},
                    )
                self._emit(
                    label,
                    dialog,
                    group_id=group_id,
                    phase=stage_name,
                    status="working",
                    seq=stage_index * 100_000 + seq,
                )

            completed = llm_client.wait_response_job(
                job_id,
                base_url=base_url,
                timeout_s=max(60, int(os.getenv("ADAOS_RESEARCH_LLM_STAGE_TIMEOUT_SECONDS") or "480")),
                poll_interval_s=1.5,
                progress_callback=progress,
            )
            job_output = str(completed.get("output_text") or "")
            job_telemetry = _llm_telemetry(
                submitted,
                completed,
                requested_model=model,
                profile_scope=profile_scope,
                output_text=job_output,
                structured_output=structured,
                repair_attempts=0,
            )
            job_record.update(
                {
                    "status": str(completed.get("status") or "unknown"),
                    "resolved_model": job_telemetry["resolved_model"],
                    "resolved_provider": job_telemetry["resolved_provider"],
                    "usage": job_telemetry["usage"],
                    "finish_reason": job_telemetry["finish_reason"],
                    "output_characters": job_telemetry["output_characters"],
                    "transport": job_telemetry["transport"],
                }
            )
            if str(completed.get("status") or "").lower() != "succeeded":
                raise _llm_failure(completed, operation=stage_name)
            return submitted, completed, job_output

        try:
            try:
                submitted, completed, output_text = execute(base_prompt, suffix="", structured=True)
            except Exception as exc:
                if not _structured_output_unsupported(exc):
                    raise
                structured_output = False
                self.repository.activity(
                    direction_id,
                    "formulation",
                    "structured_output_fallback",
                    f"{stage_name} schema-constrained job failed; retrying once with the exact schema in JSON mode.",
                    {
                        "run_id": run_id,
                        "stage": stage_name,
                        "error": _bounded_text(exc, 800),
                        "classified_unsupported": True,
                    },
                )
                submitted, completed, output_text = execute(base_prompt, suffix="-json-fallback", structured=False)

            max_repairs = max(
                1,
                min(int(os.getenv("ADAOS_RESEARCH_LLM_STAGE_REPAIRS") or "3"), 4),
            )
            while True:
                validation_error: Exception | None = None
                try:
                    candidate = validate_stage(stage_name, _json_object(output_text))
                    quality = stage_quality_issues(
                        stage_name,
                        candidate,
                        allowed_source_refs=allowed_source_refs,
                        expected_effect_direction=expected_effect_direction,
                    )
                    if quality:
                        raise ValueError("; ".join(quality))
                except ValueError as exc:
                    validation_error = exc
                if validation_error is None:
                    break
                if repair_attempt >= max_repairs:
                    raise ValueError(
                        f"{stage_name} local contract still fails after {repair_attempt} repairs: "
                        f"{validation_error}"
                    ) from validation_error
                repair_attempt += 1
                self.repository.activity(
                    direction_id,
                    "formulation",
                    "stage_repair",
                    f"{stage_name} failed its local contract; repairing only this stage ({repair_attempt}/{max_repairs}).",
                    {
                        "run_id": run_id,
                        "stage": stage_name,
                        "repair_attempt": repair_attempt,
                        "max_repairs": max_repairs,
                        "validation_error": _bounded_text(validation_error, 1600),
                    },
                )
                repair_prompt = {
                    "schema": "adaos.research.formulation_stage_repair.v1",
                    "stage": stage_name,
                    "repair_attempt": repair_attempt,
                    "max_repairs": max_repairs,
                    "validation_errors": str(validation_error),
                    "rejected_stage": candidate,
                    "rules": list(rules),
                    "allowed_source_refs": sorted(allowed_source_refs),
                    "instruction": (
                        "Correct every listed violation in this stage and no later stage. "
                        "Preserve valid grounded content, do not invent source facts, and remove every field not admitted by the schema. "
                        "Before returning, check each validation_errors clause against the corrected object."
                    ),
                }
                try:
                    submitted, completed, output_text = execute(
                        repair_prompt,
                        suffix=f"-repair-{repair_attempt}",
                        structured=structured_output,
                    )
                except Exception as exc:
                    if not structured_output or not _structured_output_unsupported(exc):
                        raise
                    structured_output = False
                    submitted, completed, output_text = execute(
                        repair_prompt,
                        suffix=f"-repair-{repair_attempt}-json-fallback",
                        structured=False,
                    )

            telemetry = _llm_telemetry(
                submitted,
                completed,
                requested_model=model,
                profile_scope=profile_scope,
                output_text=output_text,
                structured_output=structured_output,
                repair_attempts=repair_attempt,
            )
            telemetry.update(
                {
                    "schema_digest": schema_digest,
                    "provider_schema_digest": provider_schema_digest,
                    "input_digest": input_digest,
                    "task_scope": task_scope,
                    "jobs": jobs,
                    "aggregate_usage": {
                        key: sum(
                            int((item.get("usage") or {}).get(key) or 0)
                            for item in jobs
                            if isinstance(item, Mapping)
                        )
                        for key in ("input_tokens", "output_tokens", "total_tokens")
                    },
                }
            )
            output_digest = stage_digest(candidate)
            self.repository.put_formulation_stage(
                run_id=run_id,
                direction_id=direction_id,
                stage_index=stage_index,
                stage_name=stage_name,
                status="succeeded",
                input_digest=input_digest,
                output_digest=output_digest,
                payload=candidate,
                telemetry=telemetry,
            )
            self.repository.activity(
                direction_id,
                "formulation",
                "stage_completed",
                f"Formulation stage {stage_index}/3 ({stage_name}) passed its typed and semantic gates.",
                {"run_id": run_id, "stage": stage_name, "output_digest": output_digest, "telemetry": telemetry},
            )
            return candidate, telemetry
        except Exception as exc:
            failure_telemetry: dict[str, Any] = {
                "jobs": jobs,
                "error": _bounded_text(exc, 2000),
                "repair_attempts": repair_attempt,
                "schema_digest": schema_digest,
                "provider_schema_digest": provider_schema_digest,
                "input_digest": input_digest,
                "task_scope": task_scope,
            }
            if completed:
                failure_telemetry.update(
                    _llm_telemetry(
                        submitted,
                        completed,
                        requested_model=model,
                        profile_scope=profile_scope,
                        output_text=output_text,
                        structured_output=structured_output,
                        repair_attempts=repair_attempt,
                    )
                )
            self.repository.put_formulation_stage(
                run_id=run_id,
                direction_id=direction_id,
                stage_index=stage_index,
                stage_name=stage_name,
                status="failed",
                input_digest=input_digest,
                output_digest=None,
                payload=candidate,
                telemetry=failure_telemetry,
            )
            try:
                setattr(exc, "repair_attempts", repair_attempt)
            except Exception:
                pass
            raise

    def _discuss_staged(
        self,
        direction_id: str,
        text: str,
        *,
        model: str | None = None,
        actor: str | None = None,
        dialog_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = _direction_id(direction_id)
        state = self.repository.get_direction(token)
        if not state:
            raise ValueError("research direction is not initialized")
        bundle = artifact_context.source_bundle(self._artifact_owner_id(token), audience=_FORMULATION_AUDIENCE)
        if not bundle.get("sources"):
            raise ValueError("attach at least one source before discussion")
        current = self.repository.get_prototype(state.get("current_prototype_digest"))
        caller_payload = {"direction_id": token, **dict(dialog_payload or {})}
        dialog = self._dialog(caller_payload)
        directive = _directive_trace(text, actor=actor, payload=caller_payload)
        actor_id = str(directive["actor_id"])
        generation = int(state["generation"]) + 1
        group_id = f"research-formulation-{token}-{generation}"
        run_id = f"formulation-{token}-{generation}-{uuid.uuid4().hex[:10]}"
        self.repository.activity(
            token,
            "formulation",
            "directive_received",
            f"Research directive recorded from {actor_id} via {directive['origin']}.",
            {"group_id": group_id, "run_id": run_id, "directive": directive, "pipeline": "staged_v1"},
        )
        if bool(directive.get("project_to_chat")):
            self._emit_directive(directive, dialog, group_id=group_id)

        query = "\n".join(
            item
            for item in (
                str(text or "").strip(),
                str((current or {}).get("research_question") or "").strip(),
                "research question hypothesis comparator dataset implementation evaluation metric reproducibility uncertainty evidence",
            )
            if item
        )
        source_context = self._source_context(bundle, query=query)
        exact_refs = sorted(
            {
                str(ref)
                for item in source_context["coverage"].get("items") or []
                for ref in item.get("provenance_refs") or []
            }
        )
        source_ref_map = {f"SRC-{index:03d}": ref for index, ref in enumerate(exact_refs, start=1)}
        exact_to_short = {ref: short for short, ref in source_ref_map.items()}
        allowed_refs = set(source_ref_map)
        llm_source_context = copy.deepcopy(source_context)
        for source in llm_source_context.get("sources") or []:
            if not isinstance(source, dict):
                continue
            excerpt = str(source.get("excerpt") or "")
            for exact, short in exact_to_short.items():
                excerpt = excerpt.replace(exact, short)
            source["excerpt"] = excerpt
        for item in llm_source_context.get("coverage", {}).get("items") or []:
            if isinstance(item, dict):
                item["provenance_refs"] = [
                    exact_to_short[str(ref)]
                    for ref in item.get("provenance_refs") or []
                    if str(ref) in exact_to_short
                ]
        self.repository.activity(
            token,
            "formulation",
            "source_context_prepared",
            "Source artifacts were deterministically compacted and selected for the formulation query.",
            {"run_id": run_id, "coverage": source_context["coverage"]},
        )
        request_identity = str(dialog.get("request_id") or "").strip() or uuid.uuid4().hex
        request_token = re.sub(r"[^A-Za-z0-9_.-]+", "-", request_identity).strip("-._")[:40] or uuid.uuid4().hex
        request_prefix = f"adaos-research-{token}-{bundle['digest'].removeprefix('sha256:')[:12]}-{generation}-{request_token}"
        total_repairs = 0
        stage_telemetry: dict[str, Any] = {}
        try:
            current_problem = (
                {
                    key: current.get(key)
                    for key in ("title", "background", "research_question", "hypotheses", "source_grounding", "constraints", "assumptions", "open_questions")
                }
                if current
                else None
            )
            problem, telemetry = self._run_formulation_stage(
                direction_id=token,
                run_id=run_id,
                stage_index=1,
                stage_name="problem_frame",
                stage_input={
                    "directive": directive["text"],
                    "direction_id": token,
                    "current_problem_frame": current_problem,
                    "source_bundle": {"digest": bundle["digest"], **llm_source_context},
                    "source_reference_policy": "Cite only the short SRC-### ids shown in excerpt headers and coverage; AdaOS resolves them to exact artifact refs after validation.",
                    "epistemic_policy": {
                        "historical_notebook_outputs": "exploratory_untrusted_not_confirmatory",
                        "source_silence": "unknown_not_false",
                        "one_primary_question": True,
                    },
                },
                rules=[
                    "Produce one falsifiable question; do not design the execution protocol in this stage.",
                    "Produce exactly one primary hypothesis for that question and no secondary research questions.",
                    "Name the intervention, comparator, measurable outcome and paired comparison explicitly; avoid vague words such as effectiveness or significance.",
                    "Choose effect_direction deliberately: increase/decrease for a directional claim, difference for a two-sided change claim. Keep the statement and falsification wording consistent with it.",
                    "Frame the hypothesis as a proposal to estimate against a later predeclared practical threshold, not as an already established advantage or a nil-null significance claim.",
                    "Its falsification wording must distinguish evidence against the practical effect from an inconclusive interval; AdaOS will compile the exact decision inequalities after the threshold is chosen.",
                    "Separate direct source observations, author interpretations, hypotheses and unresolved choices.",
                    "Give every hypothesis its motivating SRC-### ids. Keep source observations and author interpretations in source_assessment; AdaOS compiles provenance records deterministically.",
                    "Use exact supplied SRC-### ids only. Never treat historical notebook outputs as confirmation.",
                    "Assess whether the supplied material is sufficient for a question versus an automation-ready protocol.",
                    "Write substantive fields in Russian unless a precise technical identifier is clearer in English.",
                ],
                allowed_source_refs=allowed_refs,
                model=model,
                dialog=dialog,
                group_id=group_id,
                request_id_prefix=request_prefix,
                max_tokens=4_500,
            )
            stage_telemetry["problem_frame"] = telemetry
            total_repairs += int(telemetry.get("repair_attempts") or 0)

            current_protocol = (
                {key: current.get(key) for key in ("experimental_plan", "evaluation_plan", "open_questions")}
                if current
                else None
            )
            protocol, telemetry = self._run_formulation_stage(
                direction_id=token,
                run_id=run_id,
                stage_index=2,
                stage_name="protocol_design",
                stage_input={
                    "directive": directive["text"],
                    "problem_frame": problem,
                    "current_protocol": current_protocol,
                    "adaos_policy": {
                        "workflow_smoke": {"device": "cpu", "epochs": 3, "seed_values": [17], "inference_allowed": False},
                        "confirmation": "must be separately budgeted and is the only inferential stage",
                        "pairing": "predeclare every paired unit; vary only the intervention",
                        "negative_results": "retain_and_report",
                        "ray": "deferred",
                    },
                },
                rules=[
                    "Design exactly separated workflow_smoke and confirmatory stages; smoke never supports a scientific claim.",
                    "In experimental_plan.system_specification enumerate the concrete system, baseline, intervention, data, and measurement components needed to reproduce the protocol. Record exact ordered settings such as layers, algorithms, transforms, optimizer, schedules, and metric definitions; words such as style, suitable, standard, or equivalent are not implementation specifications.",
                    "Mark each system component source_derived, policy_default, or proposed. Cite supplied SRC-### ids for every source-derived component, keep source_refs empty for the other statuses, and put every intentionally invariant detail in locked_invariants.",
                    "Make intervention_boundary identify the only allowed experimental difference. unresolved_choices must contain every missing implementation decision; it must be empty before ready_for_automation.",
                    "Use the supplied CPU smoke policy. Mark other non-source choices as proposed or policy_default, never source_derived.",
                    "Populate all nine keys in decisions_by_area and cite refs only for source-derived choices; AdaOS owns decision ids.",
                    "Resolve every candidate uncertainty from problem_frame into one of those nine decisions. A bounded proposed choice closes it; an optional extension is out of scope and is not a blocker.",
                    "For each decision use blocking_question only when status is unresolved; otherwise it must be the empty string. Do not repeat the same uncertainty in multiple areas.",
                    "In data_policy.evaluation_access separate development/model selection from the final evaluation. Choose selection_source truthfully; AdaOS compiles its exact selection rule. Expose final test only once per trained unit after the seal and prohibit test feedback.",
                    "Follow any source requirement for train/validation/untouched-test separation. Never evaluate final test per epoch or use it to choose checkpoints, hyperparameters, variants, or stopping.",
                    "Declare exact seed_values and make pairing allocation planned_units and sample_size identical to the confirmatory units.",
                    "Use named RNG streams initialization, sampling, augmentation, and analysis; within each pair keep initialization, data order, sampling and augmentation invariant and vary only the intervention.",
                    "Confirmatory stopping must depend only on predeclared budget or safety/failure conditions, never on a desired metric or significance.",
                    "Declare exactly one primary outcome, an operational estimand, uncertainty unit/method, stopping and multiplicity.",
                    "Set decision_spec.effect_direction exactly equal to the primary hypothesis effect_direction and choose one positive numeric practical threshold with an unambiguous unit. AdaOS compiles supported, contradicted and inconclusive inequalities; do not write a competing free-text rule.",
                    "Prefer an exact confirmatory budget justified by the sources. If none is justified, make a bounded proposal or mark the decision unresolved; never reuse the one-seed smoke budget as confirmatory evidence.",
                    "If a safe, reviewable proposal cannot be made, mark that decision unresolved and state one concrete blocking_question on it.",
                    "Write substantive fields and assistant_message in Russian unless a precise technical identifier is clearer in English.",
                ],
                allowed_source_refs=allowed_refs,
                model=model,
                dialog=dialog,
                group_id=group_id,
                request_id_prefix=request_prefix,
                max_tokens=5_500,
                expected_effect_direction=str(problem["hypotheses"][0]["effect_direction"]),
            )
            stage_telemetry["protocol_design"] = telemetry
            total_repairs += int(telemetry.get("repair_attempts") or 0)

            implementation, telemetry = self._run_formulation_stage(
                direction_id=token,
                run_id=run_id,
                stage_index=3,
                stage_name="implementation_contract",
                stage_input={
                    "directive": directive["text"],
                    "problem_frame": problem,
                    "protocol_design": protocol,
                    "target": {
                        "kind": "adaos_skill",
                        "ref": f"skill:{token}",
                        "execution": "current_or_member_node",
                        "ray": "deferred",
                    },
                },
                rules=[
                    "Translate the accepted scientific semantics into independently testable obligations; do not change the protocol.",
                    "Populate every category key. Required categories need at least one item; optional categories may be empty arrays.",
                    "Do not generate ids or enum variants; the AdaOS compiler owns ids and category flattening.",
                    "Verification must name an observable command, assertion, report or artifact rather than subjective review.",
                    "Include durable observability and content-addressed evidence without inventing external services.",
                    "Never require final-test metrics per epoch. Training and validation may be observed during development; the sealed final test is evaluated only according to protocol_design.data_policy.evaluation_access.",
                    "Optional security and recovery obligations must be relevant to the supplied protocol and portable to the declared execution node; do not invent unrelated isolation machinery.",
                    "Write substantive fields and assistant_message in Russian unless a precise technical identifier is clearer in English.",
                ],
                allowed_source_refs=allowed_refs,
                model=model,
                dialog=dialog,
                group_id=group_id,
                request_id_prefix=request_prefix,
                max_tokens=4_500,
            )
            stage_telemetry["implementation_contract"] = telemetry
            total_repairs += int(telemetry.get("repair_attempts") or 0)

            candidate = assemble_candidate(problem, protocol, implementation, source_ref_map=source_ref_map)
            stage_values = {
                "problem_frame": problem,
                "protocol_design": protocol,
                "implementation_contract": implementation,
            }
            compilation = build_compilation(
                direction_id=token,
                task=self.repository.get_task(
                    str((self.repository.get_direction(token) or {}).get("active_task_id") or "")
                ),
                run_id=run_id,
                source_bundle=bundle,
                source_context=source_context,
                problem_frame=problem,
                protocol_design=protocol,
                implementation_contract=implementation,
                source_ref_map=source_ref_map,
            )
            self.repository.put_formulation_stage(
                run_id=run_id,
                direction_id=token,
                stage_index=4,
                stage_name="research_compilation",
                status="completed",
                input_digest=stage_digest(
                    {name: stage_digest(value) for name, value in stage_values.items()}
                ),
                output_digest=str(compilation["digest"]),
                payload=compilation,
                telemetry={
                    "producer": "deterministic_compiler",
                    "traceability_digest": compilation["traceability_graph"]["digest"],
                    "traceability_coverage": compilation["traceability_coverage"]["coverage"],
                },
            )
            if compilation["readiness"]["decision"] != "ready_for_acceptance":
                raise ValueError(
                    "research compilation gate: "
                    + "; ".join(compilation["readiness"]["blockers"])
                )
            self.repository.activity(
                token,
                "compilation",
                "completed",
                "Research compilation produced four facets and passed traceability coverage.",
                {
                    "run_id": run_id,
                    "compilation_digest": compilation["digest"],
                    "traceability_digest": compilation["traceability_graph"]["digest"],
                    "traceability_coverage": compilation["traceability_coverage"]["coverage"],
                },
            )
            formulation_trace = {
                "run_id": run_id,
                "pipeline": "research_compiler_v1",
                "compilation_digest": compilation["digest"],
                "traceability_digest": compilation["traceability_graph"]["digest"],
                "stages": [
                    {
                        "stage": stage_name,
                        "stage_index": stage_index,
                        "input_digest": str(stage_telemetry[stage_name]["input_digest"]),
                        "output_digest": stage_digest(stage_values[stage_name]),
                        "schema_digest": str(stage_telemetry[stage_name]["schema_digest"]),
                        "resolved_model": str(stage_telemetry[stage_name]["resolved_model"]),
                        "resolved_provider": str(stage_telemetry[stage_name]["resolved_provider"]),
                        "structured_output": bool(stage_telemetry[stage_name]["structured_output"]),
                        "repair_attempts": int(stage_telemetry[stage_name]["repair_attempts"]),
                    }
                    for stage_index, stage_name in enumerate(stage_values, start=1)
                ],
            }
            preview = materialize_prototype(
                candidate,
                direction_id=token,
                source_bundle_digest=str(bundle["digest"]),
                context_coverage=source_context["coverage"],
                revision=int(current.get("revision") or 0) + 1 if current else 1,
                parent_digest=str(current["digest"]) if current else None,
                actor=f"llm:{model or 'root-default'}",
                formulation_trace=formulation_trace,
            )
            quality_issues = prototype_quality_issues(preview)
            if quality_issues:
                raise ValueError("staged assembly semantic quality gate: " + "; ".join(quality_issues))
            recorded = self.record_prototype(
                token,
                candidate,
                actor=f"llm:{model or 'root-default'}",
                context_coverage=source_context["coverage"],
                formulation_trace=formulation_trace,
            )
            message, completion = _completion_projection(recorded["prototype"])
            self.repository.activity(
                token,
                "formulation",
                "llm_completed",
                message,
                {
                    "run_id": run_id,
                    "pipeline": "staged_v1",
                    "prototype_digest": recorded["prototype"]["digest"],
                    "directive_digest": directive["text_digest"],
                    "stage_telemetry": stage_telemetry,
                    **completion,
                },
            )
            self._emit(message, dialog, group_id=group_id, phase="completed", status="succeeded", seq=999_999)
            return {
                **recorded,
                "message": message,
                "formulation_run": {
                    "run_id": run_id,
                    "pipeline": "staged_v1",
                    "stages": self.repository.formulation_stages(token, run_id=run_id),
                    "repairs": total_repairs,
                },
            }
        except Exception as exc:
            total_repairs += int(getattr(exc, "repair_attempts", 0) or 0)
            failure_message, failure_detail = _failure_projection(exc, repairs=total_repairs)
            self.repository.activity(
                token,
                "formulation",
                "failed",
                failure_message,
                {
                    "run_id": run_id,
                    "pipeline": "staged_v1",
                    "directive_digest": directive["text_digest"],
                    **failure_detail,
                },
            )
            self._emit(failure_message, dialog, group_id=group_id, phase="failed", status="failed", seq=999_999)
            raise

    def resume_compilation(
        self,
        direction_id: str,
        run_id: str,
        *,
        actor: str = "user:local",
    ) -> dict[str, Any]:
        """Resume the deterministic compiler from three durable successful stages."""

        token = _direction_id(direction_id)
        state = self.repository.get_direction(token)
        if not state:
            raise ValueError("research direction is not initialized")
        rows = self.repository.formulation_stages(token, run_id=str(run_id))
        by_name = {str(item["stage_name"]): dict(item) for item in rows}
        required = ("problem_frame", "protocol_design", "implementation_contract")
        missing = [name for name in required if by_name.get(name, {}).get("status") != "succeeded"]
        if missing:
            raise ValueError(f"cannot resume compilation; successful durable stages are missing: {missing}")
        for name in required:
            expected = str(by_name[name].get("output_digest") or "")
            if expected != stage_digest(by_name[name]["payload"]):
                raise ValueError(f"cannot resume compilation; {name} payload digest drifted")
        events = self.repository.activities(token, limit=500)
        context_event = next(
            (
                item
                for item in reversed(events)
                if item.get("status") == "source_context_prepared"
                and str((item.get("detail") or {}).get("run_id") or "") == str(run_id)
            ),
            None,
        )
        if not context_event:
            raise ValueError("cannot resume compilation; the durable source-context receipt is missing")
        later_source_changes = [
            item
            for item in events
            if int(item.get("seq") or 0) > int(context_event.get("seq") or 0)
            and item.get("status") in {"source_added", "source_replaced", "source_removed", "visibility_changed"}
        ]
        if later_source_changes:
            raise ValueError("cannot resume compilation after source context changed")
        bundle = artifact_context.source_bundle(self._artifact_owner_id(token), audience=_FORMULATION_AUDIENCE)
        coverage = copy.deepcopy(dict((context_event.get("detail") or {}).get("coverage") or {}))
        source_context = {"coverage": coverage}
        exact_refs = sorted(
            {
                str(ref)
                for item in coverage.get("items") or []
                for ref in item.get("provenance_refs") or []
            }
        )
        source_ref_map = {
            f"SRC-{index:03d}": ref
            for index, ref in enumerate(exact_refs, start=1)
        }
        problem = by_name["problem_frame"]["payload"]
        protocol = by_name["protocol_design"]["payload"]
        implementation = by_name["implementation_contract"]["payload"]
        compilation = build_compilation(
            direction_id=token,
            task=self.repository.get_task(
                str((self.repository.get_direction(token) or {}).get("active_task_id") or "")
            ),
            run_id=str(run_id),
            source_bundle=bundle,
            source_context=source_context,
            problem_frame=problem,
            protocol_design=protocol,
            implementation_contract=implementation,
            source_ref_map=source_ref_map,
        )
        stage_values = {
            "problem_frame": problem,
            "protocol_design": protocol,
            "implementation_contract": implementation,
        }
        self.repository.put_formulation_stage(
            run_id=str(run_id),
            direction_id=token,
            stage_index=4,
            stage_name="research_compilation",
            status="completed",
            input_digest=stage_digest(
                {name: stage_digest(value) for name, value in stage_values.items()}
            ),
            output_digest=str(compilation["digest"]),
            payload=compilation,
            telemetry={
                "producer": "deterministic_compiler",
                "resumed": True,
                "traceability_digest": compilation["traceability_graph"]["digest"],
                "traceability_coverage": compilation["traceability_coverage"]["coverage"],
            },
        )
        if compilation["readiness"]["decision"] != "ready_for_acceptance":
            raise ValueError(
                "research compilation gate: "
                + "; ".join(compilation["readiness"]["blockers"])
            )
        formulation_trace = {
            "run_id": str(run_id),
            "pipeline": "research_compiler_v1",
            "compilation_digest": compilation["digest"],
            "traceability_digest": compilation["traceability_graph"]["digest"],
            "stages": [
                {
                    "stage": name,
                    "stage_index": index,
                    "input_digest": str(by_name[name]["input_digest"]),
                    "output_digest": str(by_name[name]["output_digest"]),
                    "schema_digest": str((by_name[name].get("telemetry") or {})["schema_digest"]),
                    "resolved_model": str((by_name[name].get("telemetry") or {})["resolved_model"]),
                    "resolved_provider": str((by_name[name].get("telemetry") or {})["resolved_provider"]),
                    "structured_output": bool((by_name[name].get("telemetry") or {})["structured_output"]),
                    "repair_attempts": int((by_name[name].get("telemetry") or {}).get("repair_attempts") or 0),
                }
                for index, name in enumerate(required, start=1)
            ],
        }
        candidate = assemble_candidate(
            problem,
            protocol,
            implementation,
            source_ref_map=source_ref_map,
        )
        current = self.repository.get_prototype(state.get("current_prototype_digest"))
        preview = materialize_prototype(
            candidate,
            direction_id=token,
            source_bundle_digest=str(bundle["digest"]),
            context_coverage=coverage,
            revision=int(current.get("revision") or 0) + 1 if current else 1,
            parent_digest=str(current["digest"]) if current else None,
            actor=str(actor or "compiler:resume"),
            formulation_trace=formulation_trace,
        )
        quality_issues = prototype_quality_issues(preview)
        if quality_issues:
            raise ValueError("resumed assembly semantic quality gate: " + "; ".join(quality_issues))
        recorded = self.record_prototype(
            token,
            candidate,
            actor=str(actor or "compiler:resume"),
            context_coverage=coverage,
            formulation_trace=formulation_trace,
        )
        self.repository.activity(
            token,
            "compilation",
            "resumed",
            "Research compilation resumed from three digest-verified durable LLM stages.",
            {
                "run_id": str(run_id),
                "compilation_digest": compilation["digest"],
                "prototype_digest": recorded["prototype"]["digest"],
                "actor": str(actor or "compiler:resume"),
            },
        )
        return {
            **recorded,
            "ok": True,
            "resumed": True,
            "compilation": compilation,
            "formulation_run": {
                "run_id": str(run_id),
                "pipeline": "research_compiler_v1",
                "stages": self.repository.formulation_stages(token, run_id=str(run_id)),
            },
        }

    def discuss(
        self,
        direction_id: str,
        text: str,
        *,
        model: str | None = None,
        actor: str | None = None,
        dialog_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        mode = str(os.getenv("ADAOS_RESEARCH_FORMULATION_MODE") or "staged").strip().lower()
        if mode == "single_shot":
            return self._discuss_single_shot(
                direction_id,
                text,
                model=model,
                actor=actor,
                dialog_payload=dialog_payload,
            )
        return self._discuss_staged(
            direction_id,
            text,
            model=model,
            actor=actor,
            dialog_payload=dialog_payload,
        )

    def _discuss_single_shot(
        self,
        direction_id: str,
        text: str,
        *,
        model: str | None = None,
        actor: str | None = None,
        dialog_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = _direction_id(direction_id)
        state = self.repository.get_direction(token)
        if not state:
            raise ValueError("research direction is not initialized")
        bundle = artifact_context.source_bundle(self._artifact_owner_id(token), audience=_FORMULATION_AUDIENCE)
        if not bundle.get("sources"):
            raise ValueError("attach at least one source before discussion")
        current = self.repository.get_prototype(state.get("current_prototype_digest"))
        group_id = f"research-formulation-{token}-{int(state['generation']) + 1}"
        caller_payload = {"direction_id": token, **dict(dialog_payload or {})}
        dialog = self._dialog(caller_payload)
        directive = _directive_trace(text, actor=actor, payload=caller_payload)
        actor = str(directive["actor_id"])
        self.repository.activity(
            token,
            "formulation",
            "directive_received",
            f"Research directive recorded from {actor} via {directive['origin']}.",
            {"group_id": group_id, "directive": directive},
        )
        if bool(directive.get("project_to_chat")):
            self._emit_directive(directive, dialog, group_id=group_id)
        self.repository.activity(
            token,
            "formulation",
            "llm_submitted",
            "Research formulation sent to the configured Root LLM.",
            {
                "group_id": group_id,
                "actor": actor,
                "invocation_origin": directive["origin"],
                "directive_digest": directive["text_digest"],
            },
        )
        self._emit("Анализирую artifact groups и текущую постановку…", dialog, group_id=group_id, phase="submitted", status="working")
        source_context = self._source_context(bundle)
        instructions = {
            "role": "You are a rigorous research-design collaborator. Discuss, but output a machine-valid candidate rather than treating chat as truth.",
            "task": str(text or "").strip(),
            "direction_id": token,
            "current_prototype": current,
            "output_contract": {
                "assistant_message": "clear Russian explanation of what changed and remaining uncertainty",
                "title": "string",
                "background": "string >= 20 chars",
                "research_question": "falsifiable question",
                "hypotheses": [{"id": "H1", "statement": "...", "falsification": "...", "status": "proposed|exploratory|confirmatory"}],
                "source_grounding": [{"claim_id": "H1", "claim": "...", "stance": "observed|interpretation|hypothesis|constraint", "source_refs": ["exact artifact://...#cell/lines ref from source_bundle"]}],
                "evidence_policy": {"historical_results": "exploratory_source_only", "workflow_smoke": "workflow_evidence_only", "negative_results": "retain_and_report"},
                "experimental_plan": {
                    "comparators": ["control", "intervention"],
                    "stages": [
                        {"id": "smoke", "purpose": "...", "evidence_class": "workflow_smoke", "execution_profile": {"node": "current_or_member", "device": "cpu"}, "budget": {"epochs": 3, "seeds": 1}, "inference_allowed": False, "stop_conditions": ["bounded operational condition"]},
                        {"id": "confirmatory", "purpose": "...", "evidence_class": "confirmatory", "execution_profile": {"node": "declared_member"}, "budget": {"seeds": 10}, "inference_allowed": True, "stop_conditions": ["predeclared fixed or sequential condition"]}
                    ],
                    "data_policy": {
                        "dataset": "exact dataset and version",
                        "split_strategy": "...",
                        "evaluation_seal": "...",
                        "leakage_controls": ["..."],
                        "evaluation_access": {
                            "development_split": "...",
                            "selection_source": "validation|fixed_predeclared_final_state|not_applicable",
                            "selection_rule": "...",
                            "final_test_policy": "once_per_trained_unit_after_seal|not_applicable",
                            "test_feedback_prohibited": True,
                        },
                    },
                    "reproducibility": {
                        "rng_streams": [
                            {"id": "initialization", "controls": "..."},
                            {"id": "sampling", "controls": "..."},
                            {"id": "augmentation", "controls": "..."},
                            {"id": "analysis", "controls": "..."}
                        ],
                        "pairing": {
                            "unit": "declared paired unit",
                            "invariant_fields": ["field held identical within each pair"],
                            "varied_fields": ["field deliberately changed between arms"],
                            "allocation": {
                                "strategy": "enumerated_units|digest_bound_manifest|exhaustive",
                                "planned_units": ["predeclared unit id"],
                                "sample_size": 1,
                                "predeclared": True,
                            },
                        },
                        "environment": {"capture": ["code digest", "dependency lock", "hardware"], "requirements": ["..."]}
                    },
                },
                "evaluation_plan": {
                    "primary_estimand": {"name": "...", "population": "...", "contrast": "intervention minus control", "metric": "...", "aggregation": "paired mean or declared robust aggregation"},
                    "outcomes": [{"name": "...", "role": "primary|secondary|diagnostic", "measurement": "...", "unit": "..."}],
                    "uncertainty": {"method": "...", "resampling_unit": "paired unit", "interval": "two-sided interval", "confidence_level": 0.95},
                    "stopping_rule": {"kind": "fixed_budget|sequential_predeclared", "criterion": "...", "adaptation_predeclared": True},
                    "decision_rules": ["..."],
                    "multiplicity": {"family": "...", "strategy": "..."},
                    "practical_significance": "...",
                    "negative_result_policy": "retain, report and interpret negative or inconclusive results without redefining the question"
                },
                "constraints": ["..."],
                "assumptions": ["..."],
                "open_questions": ["..."],
                "implementation_requirements": [{"id": "REQ-1", "category": "execution|data|reproducibility|observability|recovery|evidence|analysis|security", "requirement": "concrete implementation obligation", "verification": "independent command/report/assertion"}],
                "acceptance_checks": [{"id": "AC-1", "category": "workflow|data_integrity|reproducibility|evidence|analysis|failure_recovery|security", "check": "observable pass condition", "evidence": "expected report, artifact or test"}],
                "readiness": {"decision": "needs_discussion|ready_for_automation", "blocking_questions": ["..."]},
            },
            "validation_schema": prototype_candidate_schema(),
            "rules": [
                "Return JSON only.",
                "Cardinality is contractual: hypotheses >= 1, experimental_plan.stages >= 2, evaluation_plan.outcomes >= 1, constraints >= 1, assumptions >= 1, implementation_requirements >= 5, and acceptance_checks >= 4.",
                "Every acceptance check must be distinct and observable; do not merge checks merely to shorten the list.",
                "Treat current_prototype as a fallible draft. Apply every requested revision; do not preserve fields that conflict with the current task.",
                "Do not invent facts absent from sources; put uncertainty into assumptions/open_questions.",
                "Cite exact provenance refs supplied in source_bundle. Never invent an artifact ref or cite an omitted fragment.",
                "Every hypothesis id needs a source_grounding record with stance=hypothesis. Observed source claims require separate non-hypothesis claim ids.",
                "Historical notebook outputs are exploratory source material, never confirmation.",
                "Separate workflow smoke execution from scientific confirmation.",
                "Declare exactly one primary outcome, one operationalized estimand, uncertainty unit/method, multiplicity, practical significance, and a predeclared stopping rule.",
                "Enumerate or digest-bind every planned paired unit before execution and make sample_size match the declared units.",
                "Implementation requirements must cover execution, data, reproducibility, observability and evidence; acceptance checks must cover workflow, data integrity, reproducibility and evidence.",
                "Do not copy instructional example phrases from output_contract into the candidate.",
                "The direction is one AdaOS skill; do not request a direction-specific scenario.",
                "The first implementation must run a bounded deterministic CPU profile on the current/member node; Ray is deferred.",
            ],
            "source_bundle": {"digest": bundle["digest"], **source_context},
        }
        # A transport retry of the same dialog turn must be idempotent, while
        # a deliberate retry after a rejected candidate must start a fresh
        # Root LLM job even though the artifact generation is unchanged.
        turn_identity = str(dialog.get("request_id") or "").strip() or uuid.uuid4().hex
        turn_token = re.sub(r"[^A-Za-z0-9_.-]+", "-", turn_identity).strip("-._")[:48] or uuid.uuid4().hex
        request_id = f"adaos-research-{token}-{bundle['digest'].removeprefix('sha256:')[:16]}-{int(state['generation']) + 1}-{turn_token}"
        repair_attempt = 0
        try:
            submitted = llm_client.submit_response_job(
                [{"role": "system", "content": "Return one JSON object matching the supplied output_contract."}, {"role": "user", "content": json.dumps(instructions, ensure_ascii=False)}],
                model=model,
                max_tokens=7000,
                request_id=request_id,
                profile_scope="research.formulation",
                text={"format": {"type": "json_object"}},
                stream=True,
                timeout=30,
            )
            job_id = str(submitted.get("job_id") or "")
            if not job_id:
                raise RuntimeError("Root LLM did not return a job_id")
            base_url = str((submitted.get("_client") or {}).get("base_url") or "") or None
            self.repository.activity(token, "formulation", "llm_running", "Root LLM job accepted.", {"job_id": job_id, "request_id": request_id})
            durable_progress_phase = ""

            def progress(value: Mapping[str, Any]) -> None:
                nonlocal durable_progress_phase
                seq = int(value.get("seq") or 0)
                label = str(value.get("label") or value.get("phase") or "LLM working")
                phase = str(value.get("phase") or value.get("status") or label).strip().lower()
                if phase != durable_progress_phase:
                    durable_progress_phase = phase
                    self.repository.activity(token, "formulation", "llm_progress", label, {"job_id": job_id, "progress": dict(value), "coalesced": True})
                self._emit(label, dialog, group_id=group_id, phase="progress", status="working", seq=seq)

            completed = llm_client.wait_response_job(job_id, base_url=base_url, timeout_s=280, poll_interval_s=1.5, progress_callback=progress)
            if str(completed.get("status") or "").lower() != "succeeded":
                raise _llm_failure(completed, operation="formulation")
            candidate = _normalize_candidate_shape(
                _json_object(str(completed.get("output_text") or ""))
            )
            recorded: dict[str, Any] | None = None
            while recorded is None:
                try:
                    preview = materialize_prototype(
                        candidate,
                        direction_id=token,
                        source_bundle_digest=str(bundle["digest"]),
                        context_coverage=source_context["coverage"],
                        revision=int(current.get("revision") or 0) + 1 if current else 1,
                        parent_digest=str(current["digest"]) if current else None,
                        actor=f"llm:{model or 'root-default'}",
                    )
                    quality_issues = prototype_quality_issues(preview)
                    if quality_issues:
                        raise ValueError("semantic quality gate: " + "; ".join(quality_issues))
                    recorded = self.record_prototype(
                        token,
                        candidate,
                        actor=f"llm:{model or 'root-default'}",
                        context_coverage=source_context["coverage"],
                    )
                except ValueError as validation_error:
                    if repair_attempt >= 2:
                        recorded = self.record_prototype(
                            token,
                            candidate,
                            actor=f"llm:{model or 'root-default'}",
                            context_coverage=source_context["coverage"],
                        )
                        break
                    repair_attempt += 1
                    repair_request_id = f"{request_id}-repair-{repair_attempt}"
                    self.repository.activity(
                        token,
                        "formulation",
                        "schema_repair",
                        f"Candidate rejected by the typed contract; requesting bounded repair {repair_attempt}/2.",
                        {"validation_error": str(validation_error), "request_id": repair_request_id},
                    )
                    self._emit(
                        f"Структурная проверка не пройдена; исправляю ревизию ({repair_attempt}/2)…",
                        dialog,
                        group_id=group_id,
                        phase="schema_repair",
                        status="working",
                        seq=900000 + repair_attempt,
                    )
                    repair_prompt = _repair_prompt(
                        validation_error=str(validation_error),
                        candidate=candidate,
                        rules=instructions["rules"],
                        user_request=instructions["task"],
                        allowed_provenance_refs=[
                            ref
                            for item in source_context["coverage"].get("items") or []
                            for ref in item.get("provenance_refs") or []
                        ],
                    )
                    repaired_submit = llm_client.submit_response_job(
                        [
                            {"role": "system", "content": "You repair one research candidate. Return the corrected candidate JSON only. Never return an envelope, input keys, a schema, a contract, Markdown fences, or commentary. Keep all required nested fields and satisfy every stated cardinality."},
                            {"role": "user", "content": repair_prompt},
                        ],
                        model=model,
                        max_tokens=9000,
                        request_id=repair_request_id,
                        profile_scope="research.formulation.repair",
                        text={"format": {"type": "json_object"}},
                        stream=True,
                        timeout=30,
                    )
                    job_id = str(repaired_submit.get("job_id") or "")
                    if not job_id:
                        raise RuntimeError("Root LLM repair did not return a job_id")
                    base_url = str((repaired_submit.get("_client") or {}).get("base_url") or "") or None
                    durable_progress_phase = ""
                    repaired = llm_client.wait_response_job(job_id, base_url=base_url, timeout_s=180, poll_interval_s=1.5, progress_callback=progress)
                    if str(repaired.get("status") or "").lower() != "succeeded":
                        raise _llm_failure(repaired, operation="repair")
                    candidate = _normalize_candidate_shape(
                        _json_object(str(repaired.get("output_text") or ""))
                    )
            message, completion = _completion_projection(recorded["prototype"])
            self.repository.activity(
                token,
                "formulation",
                "llm_completed",
                message,
                {
                    "job_id": job_id,
                    "prototype_digest": recorded["prototype"]["digest"],
                    "directive_digest": directive["text_digest"],
                    **completion,
                },
            )
            self._emit(message, dialog, group_id=group_id, phase="completed", status="succeeded", seq=999999)
            return {**recorded, "message": message, "llm_job": {"job_id": job_id, "request_id": request_id, "status": "succeeded"}}
        except Exception as exc:
            failure_message, failure_detail = _failure_projection(exc, repairs=repair_attempt)
            self.repository.activity(
                token,
                "formulation",
                "failed",
                failure_message,
                {
                    "request_id": request_id,
                    "directive_digest": directive["text_digest"],
                    **failure_detail,
                },
            )
            self._emit(failure_message, dialog, group_id=group_id, phase="failed", status="failed", seq=999999)
            raise

    def accept(self, direction_id: str, prototype_digest: str, *, expected_generation: int, idempotency_key: str, actor: str = "user:local") -> dict[str, Any]:
        token = _direction_id(direction_id)

        def operation() -> Mapping[str, Any]:
            state = self.repository.get_direction(token)
            if not state:
                raise ValueError("research direction is not initialized")
            prototype = self.repository.get_prototype(prototype_digest)
            if not prototype or prototype.get("direction", {}).get("id") != token:
                raise ValueError("ResearchPrototype does not belong to this direction")
            if state.get("current_prototype_digest") != prototype_digest:
                raise ValueError("only the current ResearchPrototype can be accepted")
            admission_issues = prototype_admission_issues(prototype)
            if admission_issues:
                raise ValueError("ResearchPrototype does not pass automation admission: " + "; ".join(admission_issues))
            owner_skill_id = self._artifact_owner_id(token)
            bundle = artifact_context.source_bundle(owner_skill_id, audience=_FORMULATION_AUDIENCE)
            if bundle.get("digest") != prototype.get("source_bundle_digest"):
                raise ValueError("artifact groups changed after this ResearchPrototype revision; discuss and review a new revision")
            readiness = prototype.get("readiness") if isinstance(prototype.get("readiness"), Mapping) else {}
            if readiness.get("decision") != "ready_for_automation" or list(readiness.get("blocking_questions") or []):
                raise ValueError("ResearchPrototype still has blocking questions")
            formulation_trace = (
                prototype.get("formulation_trace")
                if isinstance(prototype.get("formulation_trace"), Mapping)
                else {}
            )
            run_id = str(formulation_trace.get("run_id") or "")
            compilation_stage = next(
                (
                    item
                    for item in self.repository.formulation_stages(token, run_id=run_id)
                    if item.get("stage_name") == "research_compilation"
                ),
                None,
            )
            compilation = (
                dict(compilation_stage.get("payload") or {})
                if isinstance(compilation_stage, Mapping)
                else {}
            )
            if (
                not compilation
                or compilation.get("digest") != formulation_trace.get("compilation_digest")
                or compilation.get("source_bundle_digest") != bundle.get("digest")
                or compilation.get("readiness", {}).get("decision") != "ready_for_acceptance"
            ):
                raise ValueError(
                    "ResearchCompilation is missing, stale, or did not pass its traceability gate"
                )
            task = self.repository.get_task(state.get("active_task_id"))
            if not task:
                raise ValueError("research direction has no active ResearchTask")
            prototype_task_id = str((prototype.get("task") or {}).get("id") or task["task_id"])
            if prototype_task_id != str(task["task_id"]):
                raise ValueError("ResearchPrototype belongs to another ResearchTask")
            project = self._ensure_implementation_project(state, task)
            primary_target_ref = f"skill:{owner_skill_id}"
            track_id = f"{task['task_id']}.track-001"
            track = self.repository.create_track(
                token,
                str(task["task_id"]),
                track_id=track_id,
                title="Primary implementation",
                project_ref=str(project["ref"]),
                primary_target_ref=primary_target_ref,
            )
            self.repository.activity(token, "acceptance", "checkpointing", "Creating an exact private local Builder checkpoint for the direction skill; no source is published.", {"prototype_digest": prototype_digest, "actor": actor})
            checkpoint = dict(
                self._checkpoint(
                    kind="skill",
                    artifact_id=owner_skill_id,
                    message=f"research formulation accepted {prototype_digest}",
                    metadata={"research_prototype_digest": prototype_digest, "source_bundle_digest": bundle["digest"], "actor": actor},
                )
            )
            if not any(checkpoint.get(key) for key in ("package_digest", "source_revision", "source_tree", "sha256", "commit")):
                raise ValueError("Builder checkpoint did not return an immutable source identity")
            groups = [
                artifact_context.get_group(owner_skill_id, item["group_id"])
                for item in artifact_context.groups(owner_skill_id)
            ]
            context_views = [
                artifact_context.materialize_context(
                    owner_skill_id,
                    str(item["group_id"]),
                    _IMPLEMENTATION_AUDIENCE,
                )
                for item in groups
            ]
            implementation_bundle = artifact_context.source_bundle(
                owner_skill_id, audience=_IMPLEMENTATION_AUDIENCE
            )
            brief = materialize_automation_brief(
                direction_id=token,
                project=project,
                artifact_groups=groups,
                source_bundle=bundle,
                prototype=prototype,
                checkpoint=checkpoint,
                actor=actor,
                compilation=compilation,
                context_views=context_views,
                implementation_bundle=implementation_bundle,
                task=task,
                implementation_track=track,
                primary_target_ref=primary_target_ref,
                artifact_owner_skill_id=owner_skill_id,
            )
            stored = self.repository.accept(
                token,
                expected_generation=expected_generation,
                prototype=prototype,
                brief=brief,
                task_id=str(task["task_id"]),
                implementation_track_id=track_id,
            )
            self.repository.put_compilation(
                token,
                str(task["task_id"]),
                compilation,
                prototype_digest=str(prototype["digest"]),
                actor=actor,
            )
            session_result = development_sessions.create(
                str(project["id"]),
                automation_brief_digest=str(stored["digest"]),
                research_prototype_digest=str(prototype["digest"]),
                artifact_sources=[
                    {
                        "skill_id": owner_skill_id,
                        "group_id": str(item["group_id"]),
                        "audience": _IMPLEMENTATION_AUDIENCE,
                    }
                    for item in groups
                ],
                subject_refs=[
                    {
                        "kind": "research_direction",
                        "ref": f"research-direction:{token}",
                        "revision": int(state["generation"]),
                    },
                    {
                        "kind": "research_task",
                        "ref": f"research-task:{task['task_id']}",
                        "revision": int(task["revision"]),
                        "digest": str(prototype["digest"]),
                    },
                    {
                        "kind": "implementation_track",
                        "ref": f"implementation-track:{track_id}",
                        "revision": int(track["revision"]),
                    },
                ],
                contract_inputs=[
                    {
                        "kind": "research_compilation",
                        "ref": f"research-compilation:{compilation.get('compilation_id') or compilation['digest']}",
                        "digest": str(compilation["digest"]),
                        "media_type": "application/json",
                    },
                    {
                        "kind": "automation_brief",
                        "ref": f"automation-brief:{stored['brief_id']}",
                        "digest": str(stored["digest"]),
                        "media_type": "application/json",
                    },
                ],
                acceptance_profiles=[
                    "project.conformance",
                    "research.consumer-contracts",
                    "research.traceability",
                ],
                context_members=list(stored["development_scope"]["context_members"]),
                prohibited_actions=list(stored["prohibited_actions"]),
                base_release={
                    "scope": str(checkpoint.get("scope") or "local"),
                    "package_digest": checkpoint.get("package_digest"),
                    "source_revision": checkpoint.get("source_revision") or checkpoint.get("commit"),
                    "source_tree": checkpoint.get("source_tree"),
                    "checkpoint_path": checkpoint.get("stored_path"),
                },
                actor=actor,
            )
            development_sessions.attach_instruction(
                str(session_result["session"]["session_id"]),
                "automation_brief",
                stored,
                expected_digest=str(stored["digest"]),
            )
            compiled_instruction = development_sessions.attach_instruction(
                str(session_result["session"]["session_id"]),
                "research_compilation",
                compilation,
                expected_digest=str(compilation["digest"]),
            )
            session = compiled_instruction["session"]
            track = self.repository.bind_track_development(
                track_id,
                project_ref=str(project["ref"]),
                primary_target_ref=primary_target_ref,
                development_session_id=str(session["session_id"]),
            )
            self.repository.activity(
                token,
                "acceptance",
                "handoff_ready",
                "ResearchTask formulation accepted; its Project-scoped Development Session is ready. Codex was not started.",
                {
                    "task_ref": f"research-task:{task['task_id']}",
                    "implementation_track_ref": f"implementation-track:{track_id}",
                    "project_ref": project["ref"],
                    "prototype_digest": prototype_digest,
                    "compilation_digest": compilation["digest"],
                    "automation_brief_digest": stored["digest"],
                    "development_session_id": session["session_id"],
                    "checkpoint": checkpoint,
                },
                actor=actor,
                subject_ref=f"implementation-track:{track_id}",
            )
            return {
                "ok": True,
                "direction": self.repository.get_direction(token),
                "research_task": self.repository.get_task(str(task["task_id"])),
                "implementation_track": track,
                "project": project,
                "prototype": prototype,
                "research_compilation": compilation,
                "automation_brief": stored,
                "development_session": session,
                "builder_checkpoint": checkpoint,
                "codex_started": False,
            }

        return self.repository.once(str(idempotency_key or "").strip(), "accept_prototype", operation)


__all__ = ["ResearchOrchestrator"]
