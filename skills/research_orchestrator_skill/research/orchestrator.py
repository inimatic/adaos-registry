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
    digest as contract_digest,
    materialize_automation_brief,
    materialize_prototype,
    prototype_admission_issues,
    prototype_candidate_schema,
    prototype_quality_issues,
)
from research.compiler import build_compilation
from research.formulation import (
    DEFAULT_WORKFLOW_SMOKE_POLICY,
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
        skill_invoker: Callable[..., Mapping[str, Any]] | None = None,
    ) -> None:
        self.repository = repository or OrchestratorRepository()
        self._checkpoint = checkpoint or builder_artifacts.local_checkpoint
        self._invoke_skill = skill_invoker or invoke_skill

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
        del task  # Task identity belongs to the immutable Development Session, not the distributable Project.

        def reconcile(project: Mapping[str, Any]) -> dict[str, Any]:
            payload = {
                key: copy.deepcopy(value)
                for key, value in project.items()
                if key not in {"ref", "manifest_digest", "source_path"}
            }
            changed = False
            direction_ref = f"research-direction:{direction['direction_id']}"
            for entrypoint in payload.get("entrypoints") or []:
                if str(entrypoint.get("id") or "") != "research":
                    continue
                bindings = entrypoint.setdefault("bindings", {})
                if bindings.get("direction_ref") != direction_ref:
                    bindings["direction_ref"] = direction_ref
                    changed = True
                # A Project is the distributable implementation envelope. A selected
                # ResearchTask is mutable workflow state and is frozen separately in
                # DevelopmentSession.subject_refs/contract_inputs. Keeping it here made
                # a reused Project advertise the first task forever.
                if "task_ref" in bindings:
                    bindings.pop("task_ref", None)
                    changed = True
            catalog = payload.get("catalog") or {}
            desired_description = f"Project-scoped implementation workspace for {direction_ref}."
            if str(catalog.get("description") or "") != desired_description:
                catalog["description"] = desired_description
                changed = True
            if not changed:
                return dict(project)
            return compositions.replace(
                str(payload["id"]),
                payload,
                expected_manifest_digest=str(project["manifest_digest"]),
            )

        owner_skill_id = str(direction.get("artifact_owner_skill_id") or direction["direction_id"])
        legacy_ref = str(direction.get("legacy_project_ref") or "")
        if legacy_ref.startswith("project:"):
            legacy = compositions.get(legacy_ref.partition(":")[2])
            if f"skill:{owner_skill_id}" in {
                str(item.get("ref") or "") for item in legacy["components"]["owned"]
            }:
                return reconcile(legacy)
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
                            },
                        }
                    ],
                    "catalog": {
                        "title": f"{direction['title']} — implementation",
                        "description": (
                            "Project-scoped implementation workspace for "
                            f"research-direction:{direction['direction_id']}."
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
        return reconcile(project)

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

    def create_task(
        self,
        direction_id: str,
        title: str,
        *,
        task_id: str | None = None,
        research_question: str = "",
        parent_task_id: str | None = None,
        branch_of_task_id: str | None = None,
        dependency_refs: list[str] | None = None,
        activate: bool = False,
        actor: str = "user:local",
    ) -> dict[str, Any]:
        token = _direction_id(direction_id)
        direction = self.repository.get_direction(token)
        if not direction:
            raise ValueError("research direction is not initialized")
        candidate_id = _direction_id(
            task_id
            or f"{token}.task-{len(self.repository.list_tasks(token)) + 1:03d}"
        )
        task = self.repository.create_task(
            token,
            task_id=candidate_id,
            title=str(title or candidate_id).strip(),
            research_question=str(research_question or "").strip(),
            parent_task_id=parent_task_id,
            branch_of_task_id=branch_of_task_id,
            dependency_refs=dependency_refs,
        )
        if activate:
            direction = self.repository.set_active_task(token, candidate_id)
        self.repository.activity(
            token,
            "agenda",
            "task_created",
            f"ResearchTask {task['ref']} was created{' and activated' if activate else ''}.",
            {
                "task_ref": task["ref"],
                "parent_task_id": task.get("parent_task_id"),
                "branch_of_task_id": task.get("branch_of_task_id"),
                "dependency_refs": task.get("dependency_refs") or [],
                "activated": bool(activate),
            },
            actor=actor,
            subject_ref=str(task["ref"]),
        )
        return {
            "ok": True,
            "direction": direction,
            "task": task,
            "agenda": self.get(token, task_id=candidate_id).get("agenda"),
        }

    def select_active_task(
        self,
        direction_id: str,
        task_id: str,
        *,
        actor: str = "user:local",
    ) -> dict[str, Any]:
        token = _direction_id(direction_id)
        selected_task_id = _direction_id(task_id)
        previous = self.repository.get_direction(token)
        if not previous:
            raise ValueError("research direction is not initialized")
        direction = self.repository.set_active_task(token, selected_task_id)
        if previous.get("active_task_id") != selected_task_id:
            self.repository.activity(
                token,
                "agenda",
                "task_activated",
                f"ResearchTask research-task:{selected_task_id} became the active formulation task.",
                {
                    "previous_task_ref": (
                        f"research-task:{previous['active_task_id']}"
                        if previous.get("active_task_id")
                        else None
                    ),
                    "task_ref": f"research-task:{selected_task_id}",
                },
                actor=actor,
                subject_ref=f"research-task:{selected_task_id}",
            )
        return self.get(token, task_id=selected_task_id)

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

    def get(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
    ) -> dict[str, Any]:
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
        tasks = self.repository.list_tasks(token)
        active_task = self.repository.get_task(state.get("active_task_id"))
        selected_task = self.repository.get_task(task_id) if task_id else active_task
        if selected_task and selected_task.get("direction_id") != token:
            raise ValueError("selected ResearchTask belongs to another direction")
        if task_id and not selected_task:
            raise ValueError("selected ResearchTask does not exist")
        tracks = (
            self.repository.list_tracks(str(selected_task["task_id"]))
            if selected_task
            else []
        )
        selected_track = (
            self.repository.get_track(implementation_track_id)
            if implementation_track_id
            else next(
                (item for item in reversed(tracks) if item.get("development_session_id")),
                tracks[-1] if tracks else None,
            )
        )
        if selected_track and (
            selected_track.get("direction_id") != token
            or selected_track.get("task_id") != (selected_task or {}).get("task_id")
        ):
            raise ValueError("selected ImplementationTrack belongs to another ResearchTask")
        if implementation_track_id and not selected_track:
            raise ValueError("selected ImplementationTrack does not exist")
        compilation_record = self.repository.get_compilation_record(
            (selected_task or {}).get("accepted_compilation_digest")
        )
        prototype = self.repository.get_prototype(
            (selected_task or {}).get("current_prototype_digest")
            or (
                state.get("current_prototype_digest")
                if (selected_task or {}).get("task_id") == state.get("active_task_id")
                else None
            )
        )
        accepted = self.repository.get_prototype(
            (compilation_record or {}).get("prototype_digest")
            or (
                state.get("accepted_prototype_digest")
                if (selected_task or {}).get("task_id") == state.get("active_task_id")
                else None
            )
        )
        brief = None
        if selected_task:
            cursor = selected_track
            visited: set[str] = set()
            while isinstance(cursor, Mapping):
                cursor_id = str(cursor.get("track_id") or "")
                if not cursor_id or cursor_id in visited:
                    break
                visited.add(cursor_id)
                brief = self.repository.get_brief_for_task(
                    str(selected_task["task_id"]),
                    implementation_track_id=cursor_id,
                )
                if brief:
                    break
                parent_id = str(cursor.get("parent_track_id") or "")
                cursor = self.repository.get_track(parent_id) if parent_id else None
            if brief is None:
                brief = self.repository.get_brief_for_task(str(selected_task["task_id"]))
        if selected_track and str(selected_track.get("project_ref") or "").startswith("project:"):
            try:
                project = compositions.get(str(selected_track["project_ref"]).partition(":")[2])
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
                if item.get("session_id") == (selected_track or {}).get("development_session_id")
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
        agenda_payload = {
            "schema": "adaos.research.agenda.v1",
            "direction_id": token,
            "direction_ref": f"research-direction:{token}",
            "revision": int(state.get("revision") or 1)
            + sum(int(item.get("revision") or 1) for item in tasks),
            "tasks": tasks,
            "active_task_id": (active_task or {}).get("task_id"),
        }
        agenda_payload["digest"] = contract_digest(agenda_payload)
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
            "agenda": agenda_payload,
            "active_task": active_task,
            "selected_task": selected_task,
            "implementation_tracks": tracks,
            "active_implementation_track": selected_track,
            "selected_implementation_track": selected_track,
            "accepted_compilation": (compilation_record or {}).get("payload"),
            "accepted_compilation_record": compilation_record,
            "artifact_groups": artifact_context.groups(owner_skill_id),
            "source_bundle": bundle,
            "current_prototype": prototype,
            "prototype_stale": prototype_stale,
            "formulation": {
                "admission_decision": admission_review.get("decision") or "needs_discussion",
                "admission_blockers": list(admission_review.get("blockers") or []),
                "can_accept": (
                    bool(prototype)
                    and (selected_task or {}).get("task_id") == state.get("active_task_id")
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
            "next_steps": self._next_steps(state, bundle, prototype, track=selected_track),
        }

    def outline(self, direction_id: str) -> dict[str, Any]:
        """Project one direction aggregate as a generic typed navigation outline."""

        token = _direction_id(direction_id)
        view = self.get(token)
        direction = dict(view["direction"])
        tasks = list((view.get("agenda") or {}).get("tasks") or [])
        tracks = [
            track
            for task in tasks
            for track in self.repository.list_tracks(str(task["task_id"]))
        ]
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
            matched_studies = list((task.get("metadata") or {}).get("matched_studies") or [])
            for suffix, title, kind, tab, icon, status in (
                ("studies", "Studies", "study_collection", "studies", "analytics-outline", str(len(matched_studies))),
                ("evidence", "Evidence", "evidence_collection", "evidence", "shield-checkmark-outline", "planned"),
                ("releases", "Releases", "release_collection", "releases", "cube-outline", "planned"),
            ):
                add(
                    f"{task_node_id}:{suffix}",
                    title,
                    parent_id=task_node_id,
                    kind=kind,
                    tab=tab,
                    status=status,
                    icon=icon,
                    task_id=task_id,
                )
            for study in matched_studies:
                study_ref = str(study.get("ref") or "")
                add(
                    f"study:{study.get('study_id') or contract_digest(study)[-16:]}",
                    str(study.get("study_id") or study_ref or "Study"),
                    parent_id=f"{task_node_id}:studies",
                    kind="research_study",
                    tab="evidence",
                    status=str(study.get("status") or "unknown"),
                    subtitle=str(study.get("primary_endpoint") or study.get("owner_ref") or ""),
                    icon="analytics-outline",
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

    def lineage(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
    ) -> dict[str, Any]:
        token = _direction_id(direction_id)
        view = self.get(
            token,
            task_id=task_id,
            implementation_track_id=implementation_track_id,
        )
        task = view.get("selected_task") or view.get("active_task") or {}
        tracks = list(view.get("implementation_tracks") or [])
        if implementation_track_id:
            tracks = [item for item in tracks if item.get("track_id") == implementation_track_id]
        local_sources = list((view.get("source_bundle") or {}).get("sources") or [])
        calibration = dict((task.get("metadata") or {}).get("calibration") or {})
        matched_studies = list((task.get("metadata") or {}).get("matched_studies") or [])
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
        lines.extend(["", "## Matched studies", ""])
        lines.extend(
            f"- `{item.get('ref')}` · `{item.get('status')}` · endpoint `{item.get('primary_endpoint') or 'not declared'}` · owner `{item.get('owner_ref')}`"
            for item in matched_studies
        )
        if not matched_studies:
            lines.append("- No Study refs are connected.")
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
            "matched_studies": matched_studies,
            "accepted_compilation": view.get("accepted_compilation_record"),
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
        response = self._invoke_skill(
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
        compilation_record = self.repository.latest_compilation_for_task(str(task["task_id"]))
        if not compilation_record:
            prototype = self.repository.get_prototype(
                task.get("current_prototype_digest")
                or direction.get("accepted_prototype_digest")
            )
            trace = (
                prototype.get("formulation_trace")
                if isinstance((prototype or {}).get("formulation_trace"), Mapping)
                else {}
            )
            run_id = str(trace.get("run_id") or "")
            stage = next(
                (
                    item
                    for item in self.repository.formulation_stages(token, run_id=run_id)
                    if item.get("stage_name") == "research_compilation"
                    and item.get("task_id") == task["task_id"]
                ),
                None,
            )
            compilation = dict(stage.get("payload") or {}) if isinstance(stage, Mapping) else {}
            if (
                prototype
                and compilation
                and compilation.get("digest") == trace.get("compilation_digest")
                and compilation.get("source_bundle_digest") == prototype.get("source_bundle_digest")
            ):
                compilation_record = self.repository.put_compilation(
                    token,
                    str(task["task_id"]),
                    compilation,
                    prototype_digest=str(prototype["digest"]),
                    actor=actor,
                )
                self.repository.activity(
                    token,
                    "compilation",
                    "legacy_compilation_adopted",
                    "The digest-verified accepted ResearchCompilation was re-homed under the canonical ResearchTask.",
                    {
                        "task_ref": task["ref"],
                        "compilation_ref": compilation_record["ref"],
                        "compilation_digest": compilation_record["digest"],
                    },
                    actor=actor,
                    subject_ref=str(compilation_record["ref"]),
                )
                task = self.repository.get_task(str(task["task_id"])) or task
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
                "matched_studies": [
                    {
                        "schema": "adaos.research.study_ref.v1",
                        "study_id": f"{evaluator_task_id}:{budget_view}",
                        "ref": f"study:{evaluator_task_id}:{budget_view}",
                        "owner_ref": "skill:research_evaluator_skill",
                        "external_task_ref": f"calibration-task:{evaluator_task_id}",
                        "external_task_digest": external_task.get("digest"),
                        "status": "complete" if (response.get("summary") or {}).get("complete") else "incomplete",
                        "primary_endpoint": (response.get("summary") or {}).get("primary_endpoint"),
                        "summary_digest": (response.get("summary") or {}).get("digest"),
                    }
                ],
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
            "compilation": compilation_record,
            "matched_studies": list(task.get("metadata", {}).get("matched_studies") or []),
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
        task_id: str | None = None,
        implementation_track_id: str | None = None,
        builder_webspace_id: str = "desktop-dev",
        base_url: str | None = None,
    ) -> dict[str, Any]:
        state = self.get(
            direction_id,
            task_id=task_id,
            implementation_track_id=implementation_track_id,
        )
        selected_task = state.get("selected_task")
        if isinstance(selected_task, Mapping):
            self._ensure_implementation_project(state["direction"], selected_task)
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
    def _artifact_sources_from_development_session(
        session: Mapping[str, Any],
    ) -> list[dict[str, str]]:
        """Project immutable artifact inputs back to the SDK creation API."""

        sources: list[dict[str, str]] = []
        for item in session.get("artifact_inputs") or []:
            if not isinstance(item, Mapping):
                continue
            ref = str(item.get("ref") or "").strip()
            prefix = "artifact://skill/"
            if not ref.startswith(prefix):
                raise ValueError(f"unsupported Development Session artifact ref: {ref}")
            owner_and_group = ref[len(prefix) :]
            owner, separator, group_id = owner_and_group.partition("/")
            if not separator or not owner or not group_id:
                raise ValueError(f"invalid Development Session artifact ref: {ref}")
            source = {"skill_id": owner, "group_id": group_id}
            audience = str(item.get("audience") or "").strip()
            if audience:
                source["audience"] = audience
            sources.append(source)
        return sources

    @staticmethod
    def _development_instruction_value(
        session_id: str,
        kind: str,
    ) -> tuple[dict[str, Any], bool]:
        """Return producer content, never the SDK verification envelope.

        ``development_sessions.get_instruction`` deliberately returns a receipt
        containing ``value``.  Older callers could accidentally attach that
        receipt as a new instruction.  Bounded recursive unwrapping repairs such
        a session without weakening the descriptor/content verification already
        performed by the SDK.
        """

        result: Mapping[str, Any] = development_sessions.get_instruction(
            session_id, kind
        )
        unwrapped_receipt = False
        for depth in range(3):
            value = result.get("value")
            if not isinstance(value, Mapping):
                break
            if depth:
                unwrapped_receipt = True
            result = value
        if "digest" not in result:
            raise ValueError(f"Development Session {kind} instruction has no producer digest")
        return dict(result), unwrapped_receipt

    def refresh_development_contract(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
        actor: str = "system:research_orchestrator",
    ) -> dict[str, Any]:
        """Supersede a Development Session after an admitted consumer ABI changes.

        Accepted scientific and engineering instructions remain immutable.  The
        refreshed session differs only in the exact consumer-owned contract and
        records both session identities in the research activity ledger.  This
        lets Builder re-evaluate (and, when necessary, repair) a candidate against
        the current ABI without mutating the historical handoff that produced it.
        """

        state = self.get(
            direction_id,
            task_id=task_id,
            implementation_track_id=implementation_track_id,
        )
        track = state.get("active_implementation_track")
        previous = state.get("development_session")
        if not isinstance(track, Mapping) or not isinstance(previous, Mapping):
            raise ValueError("implementation track has no Development Session")

        current_contract = dict(
            self._invoke_skill(
                "research_manager_skill",
                "get_runner_contract",
                {},
                timeout=60,
            )
        )
        current_digest = str(current_contract.get("digest") or "").strip()
        if (
            current_contract.get("schema") != "adaos.contract.operation_set.v1"
            or current_contract.get("contract") != "adaos.research.runner.v1"
            or current_digest
            != contract_digest(
                {key: item for key, item in current_contract.items() if key != "digest"}
            )
        ):
            raise ValueError("ResearchManager returned an invalid runner consumer contract")

        previous_contract, contract_envelope_nested = self._development_instruction_value(
            str(previous["session_id"]), "consumer_contract"
        )
        brief, brief_envelope_nested = self._development_instruction_value(
            str(previous["session_id"]), "automation_brief"
        )
        compilation, compilation_envelope_nested = self._development_instruction_value(
            str(previous["session_id"]), "research_compilation"
        )
        previous_digest = str(previous_contract.get("digest") or "").strip()
        instruction_envelope_nested = any(
            (
                contract_envelope_nested,
                brief_envelope_nested,
                compilation_envelope_nested,
            )
        )
        if previous_digest == current_digest and not instruction_envelope_nested:
            return {
                "ok": True,
                "reused": True,
                "development_session": dict(previous),
                "implementation_track": dict(track),
                "consumer_contract_digest": current_digest,
            }

        project_ref = str(previous.get("project_ref") or "").strip()
        project_kind, separator, project_id = project_ref.partition(":")
        if separator != ":" or project_kind != "project" or not project_id:
            raise ValueError("Development Session has an invalid project_ref")

        contract_inputs = []
        for item in previous.get("contract_inputs") or []:
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            if str(value.get("kind") or "") == "consumer_contract":
                value["digest"] = current_digest
            contract_inputs.append(value)
        if not any(
            str(item.get("kind") or "") == "consumer_contract"
            for item in contract_inputs
        ):
            raise ValueError("Development Session has no consumer_contract input")

        requirements: list[dict[str, Any]] = []
        for item in previous.get("acceptance_requirements") or []:
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            if str(value.get("id") or "") == "research.consumer-contracts":
                value["parameters"] = {
                    **dict(value.get("parameters") or {}),
                    "execute_workflow_smoke": True,
                }
            requirements.append(value)

        primary_targets = [
            str(item.get("ref") or "")
            for item in dict(previous.get("targets") or {}).get("primary") or []
            if isinstance(item, Mapping) and str(item.get("ref") or "").strip()
        ]
        secondary_targets = [
            str(item.get("ref") or "")
            for item in dict(previous.get("targets") or {}).get("secondary") or []
            if isinstance(item, Mapping) and str(item.get("ref") or "").strip()
        ]
        handoff = (
            dict(previous.get("handoff"))
            if isinstance(previous.get("handoff"), Mapping)
            else {}
        )
        session_seed = contract_digest(
            {
                "predecessor": previous["session_id"],
                "consumer_contract_digest": current_digest,
            }
        ).removeprefix("sha256:")[:16]
        created = development_sessions.create(
            project_id,
            automation_brief_digest=str(handoff.get("automation_brief_digest") or "") or None,
            research_prototype_digest=str(handoff.get("research_prototype_digest") or "") or None,
            artifact_sources=self._artifact_sources_from_development_session(previous),
            subject_refs=[
                dict(item)
                for item in previous.get("subject_refs") or []
                if isinstance(item, Mapping)
            ],
            contract_inputs=contract_inputs,
            acceptance_profiles=[str(item) for item in previous.get("acceptance_profiles") or []],
            acceptance_requirements=requirements,
            request=str(handoff.get("request") or "") or None,
            execution_budget=(
                dict(handoff["execution_budget"])
                if isinstance(handoff.get("execution_budget"), Mapping)
                else None
            ),
            agent_profile=(
                dict(handoff["agent_profile"])
                if isinstance(handoff.get("agent_profile"), Mapping)
                else None
            ),
            primary_targets=primary_targets,
            secondary_targets=secondary_targets,
            context_members=[
                dict(item)
                for item in previous.get("context_members") or []
                if isinstance(item, Mapping)
            ],
            prohibited_actions=[str(item) for item in handoff.get("prohibited_actions") or []],
            base_release=(
                dict(previous["base_release"])
                if isinstance(previous.get("base_release"), Mapping)
                else None
            ),
            focus_ref=str(dict(previous.get("focus") or {}).get("ref") or "") or None,
            session_id=f"dev_{project_id}_{session_seed}",
            actor=actor,
        )
        session = dict(created["session"])
        for kind, instruction in (
            ("automation_brief", brief),
            ("research_compilation", compilation),
        ):
            attached = development_sessions.attach_instruction(
                str(session["session_id"]),
                kind,
                instruction,
                expected_digest=str(instruction.get("digest") or ""),
            )
            session = dict(attached["session"])
        attached = development_sessions.attach_instruction(
            str(session["session_id"]),
            "consumer_contract",
            current_contract,
            expected_digest=current_digest,
        )
        session = dict(attached["session"])
        has_immutable_realization = any(
            track.get(key)
            for key in (
                "candidate_release_digest",
                "project_release_digest",
                "study_id",
                "experiment_id",
            )
        )
        if has_immutable_realization:
            updated_track = self._successor_implementation_track(
                state,
                track,
                session,
                reason="consumer_contract_refresh",
                actor=actor,
            )
        else:
            updated_track = self.repository.bind_track_development(
                str(track["track_id"]),
                project_ref=project_ref,
                primary_target_ref=str(track["primary_target_ref"]),
                development_session_id=str(session["session_id"]),
            )
        event_identity = contract_digest(
            {
                "track_ref": track["ref"],
                "previous_session_id": previous["session_id"],
                "development_session_id": session["session_id"],
                "previous_consumer_contract_digest": previous_digest,
                "consumer_contract_digest": current_digest,
            }
        )
        self.repository.activity(
            str(state["direction"]["direction_id"]),
            "implementation",
            "consumer_contract_refreshed",
            (
                "Development Session superseded to normalize verified instruction envelopes; accepted producer content remains unchanged."
                if instruction_envelope_nested and previous_digest == current_digest
                else "Development Session superseded because the admitted consumer ABI changed; accepted scientific instructions remain unchanged."
            ),
            {
                "task_ref": (state.get("selected_task") or {}).get("ref"),
                "implementation_track_ref": track["ref"],
                "previous_development_session_id": previous["session_id"],
                "development_session_id": session["session_id"],
                "previous_consumer_contract_digest": previous_digest,
                "consumer_contract_digest": current_digest,
                "instruction_envelope_normalized": instruction_envelope_nested,
                "actor": actor,
            },
            actor=actor,
            origin="skill:research_manager_skill",
            subject_ref=str(track["ref"]),
            source_event_id=f"consumer-contract-refresh:{event_identity}",
        )
        return {
            "ok": True,
            "reused": False,
            "previous_development_session_id": previous["session_id"],
            "previous_consumer_contract_digest": previous_digest,
            "consumer_contract_digest": current_digest,
            "instruction_envelope_normalized": instruction_envelope_nested,
            "development_session": session,
            "implementation_track": updated_track,
        }

    def _successor_implementation_track(
        self,
        state: Mapping[str, Any],
        parent: Mapping[str, Any],
        session: Mapping[str, Any],
        *,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        """Create an idempotent branch before changing an immutable realization."""

        task = state.get("selected_task")
        if not isinstance(task, Mapping):
            raise ValueError("ImplementationTrack successor requires one ResearchTask")
        identity = contract_digest(
            {
                "parent_track_ref": parent["ref"],
                "development_session_id": session["session_id"],
                "reason": reason,
            }
        ).removeprefix("sha256:")[:12]
        track_id = f"{task['task_id']}.track-{identity}"
        successor = self.repository.create_track(
            str(state["direction"]["direction_id"]),
            str(task["task_id"]),
            track_id=track_id,
            title=f"{str(parent.get('title') or 'Implementation')} · successor",
            project_ref=str(parent.get("project_ref") or session.get("project_ref") or "") or None,
            primary_target_ref=str(parent.get("primary_target_ref") or "") or None,
            condition_id=(str(parent.get("condition_id") or "") or None),
            parent_track_id=str(parent["track_id"]),
            metadata={
                "lineage": {
                    "reason": reason,
                    "parent_track_ref": parent["ref"],
                    "parent_candidate_release_digest": parent.get("candidate_release_digest"),
                    "parent_project_release_ref": parent.get("project_release_ref"),
                    "parent_study_id": parent.get("study_id"),
                    "parent_experiment_id": parent.get("experiment_id"),
                    "source_development_session_id": session["session_id"],
                }
            },
        )
        scoped_session = self._clone_development_session_for_track(
            session,
            track_ref=str(successor["ref"]),
            track_revision=int(successor.get("revision") or 1),
            actor=actor,
        )
        successor = self.repository.bind_track_development(
            str(successor["track_id"]),
            project_ref=str(parent.get("project_ref") or session.get("project_ref") or ""),
            primary_target_ref=str(parent.get("primary_target_ref") or ""),
            development_session_id=str(scoped_session["session_id"]),
        )
        successor = self.repository.record_track_evaluation(
            str(successor["track_id"]),
            status="development_ready",
            metadata={
                **dict(successor.get("metadata") or {}),
                "lineage": {
                    **dict(dict(successor.get("metadata") or {}).get("lineage") or {}),
                    "development_session_id": scoped_session["session_id"],
                },
            },
        )
        event_identity = contract_digest(
            {
                "parent_track_ref": parent["ref"],
                "successor_track_ref": successor["ref"],
                "development_session_id": scoped_session["session_id"],
                "reason": reason,
            }
        )
        self.repository.activity(
            str(state["direction"]["direction_id"]),
            "implementation",
            "successor_track_created",
            "A successor ImplementationTrack was created; the predecessor release, Study, and Experiment remain immutable.",
            {
                "task_ref": task.get("ref"),
                "parent_implementation_track_ref": parent["ref"],
                "implementation_track_ref": successor["ref"],
                "development_session_id": scoped_session["session_id"],
                "reason": reason,
                "actor": actor,
            },
            actor=actor,
            origin="skill:research_orchestrator_skill",
            subject_ref=str(successor["ref"]),
            source_event_id=f"implementation-track-successor:{event_identity}",
        )
        return successor

    def _clone_development_session_for_track(
        self,
        source: Mapping[str, Any],
        *,
        track_ref: str,
        track_revision: int,
        actor: str,
    ) -> dict[str, Any]:
        """Clone immutable inputs while rebinding the track subject explicitly."""

        project_ref = str(source.get("project_ref") or "").strip()
        project_kind, separator, project_id = project_ref.partition(":")
        if separator != ":" or project_kind != "project" or not project_id:
            raise ValueError("Development Session has an invalid project_ref")
        subjects: list[dict[str, Any]] = []
        replaced = False
        for item in source.get("subject_refs") or []:
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            if str(value.get("kind") or "") == "implementation_track":
                value = {
                    "kind": "implementation_track",
                    "ref": track_ref,
                    "revision": max(1, int(track_revision)),
                }
                replaced = True
            subjects.append(value)
        if not replaced:
            subjects.append(
                {
                    "kind": "implementation_track",
                    "ref": track_ref,
                    "revision": max(1, int(track_revision)),
                }
            )
        handoff = (
            dict(source.get("handoff"))
            if isinstance(source.get("handoff"), Mapping)
            else {}
        )
        requirements = [
            dict(item)
            for item in source.get("acceptance_requirements") or []
            if isinstance(item, Mapping)
        ]
        identity = contract_digest(
            {
                "source_development_session_id": source["session_id"],
                "implementation_track_ref": track_ref,
            }
        ).removeprefix("sha256:")[:16]
        created = development_sessions.create(
            project_id,
            automation_brief_digest=str(handoff.get("automation_brief_digest") or "") or None,
            research_prototype_digest=str(handoff.get("research_prototype_digest") or "") or None,
            artifact_sources=self._artifact_sources_from_development_session(source),
            subject_refs=subjects,
            contract_inputs=[
                dict(item)
                for item in source.get("contract_inputs") or []
                if isinstance(item, Mapping)
            ],
            acceptance_profiles=[str(item) for item in source.get("acceptance_profiles") or []],
            acceptance_requirements=requirements,
            request=str(handoff.get("request") or "") or None,
            execution_budget=(
                dict(handoff["execution_budget"])
                if isinstance(handoff.get("execution_budget"), Mapping)
                else None
            ),
            agent_profile=(
                dict(handoff["agent_profile"])
                if isinstance(handoff.get("agent_profile"), Mapping)
                else None
            ),
            primary_targets=[
                str(item.get("ref") or "")
                for item in dict(source.get("targets") or {}).get("primary") or []
                if isinstance(item, Mapping) and str(item.get("ref") or "").strip()
            ],
            secondary_targets=[
                str(item.get("ref") or "")
                for item in dict(source.get("targets") or {}).get("secondary") or []
                if isinstance(item, Mapping) and str(item.get("ref") or "").strip()
            ],
            context_members=[
                dict(item)
                for item in source.get("context_members") or []
                if isinstance(item, Mapping)
            ],
            prohibited_actions=[str(item) for item in handoff.get("prohibited_actions") or []],
            base_release=(
                dict(source["base_release"])
                if isinstance(source.get("base_release"), Mapping)
                else None
            ),
            focus_ref=str(dict(source.get("focus") or {}).get("ref") or "") or None,
            session_id=f"dev_{project_id}_{identity}",
            actor=actor,
        )
        session = dict(created["session"])
        for kind in ("automation_brief", "research_compilation", "consumer_contract"):
            instruction, _ = self._development_instruction_value(
                str(source["session_id"]), kind
            )
            attached = development_sessions.attach_instruction(
                str(session["session_id"]),
                kind,
                instruction,
                expected_digest=str(instruction.get("digest") or ""),
            )
            session = dict(attached["session"])
        return session

    def branch_implementation_track(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
        reason: str = "realization_repair",
        actor: str = "system:research_orchestrator",
    ) -> dict[str, Any]:
        """Branch a released/evaluated realization onto its current exact session."""

        state = self.get(
            direction_id,
            task_id=task_id,
            implementation_track_id=implementation_track_id,
        )
        parent = state.get("active_implementation_track")
        session = state.get("development_session")
        if not isinstance(parent, Mapping) or not isinstance(session, Mapping):
            raise ValueError("ImplementationTrack branch requires a bound Development Session")
        successor = self._successor_implementation_track(
            state,
            parent,
            session,
            reason=str(reason or "realization_repair").strip() or "realization_repair",
            actor=actor,
        )
        return {
            "ok": True,
            "parent_implementation_track": dict(parent),
            "implementation_track": successor,
            "development_session": development_sessions.get(
                str(successor["development_session_id"])
            ),
        }

    @staticmethod
    def _next_steps(
        state: Mapping[str, Any],
        bundle: Mapping[str, Any],
        prototype: Mapping[str, Any] | None,
        *,
        track: Mapping[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        if prototype and str(prototype.get("source_bundle_digest") or "") != str(bundle.get("digest") or ""):
            return [{"id": "refresh_formulation", "label": "Обновить постановку", "reason": "Artifact groups изменились; новая ревизия должна сослаться на актуальный digest."}]
        track_state = str((track or {}).get("status") or "")
        if (track or {}).get("experiment_id"):
            return [{"id": "sync_study", "label": "Sync Study", "reason": "Import the latest ResearchManager attempts and evidence into the durable activity journal."}]
        if (track or {}).get("project_release_ref"):
            return [{"id": "instantiate_study", "label": "Instantiate Study", "reason": "Bind the accepted compilation, exact ProjectRelease, runner, and sealed dataset splits."}]
        if (track or {}).get("candidate_release_digest"):
            return [{"id": "publish_project_release", "label": "Publish reviewed release", "reason": "Promote only the exact candidate digest reviewed in Builder."}]
        if track_state == "implementation_complete":
            return [{"id": "prepare_project_release", "label": "Prepare release candidate", "reason": "Run the Project trial and freeze a reviewable ProjectRelease digest."}]
        if track_state in {"implementation_running", "implementation_failed"}:
            return [{"id": "sync_implementation", "label": "Sync Builder", "reason": "Import the current Builder Automation state and failure diagnostics."}]
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
            raise ValueError(
                "active ResearchTask has an accepted immutable formulation; create and "
                "activate a new branch ResearchTask before recording a revision"
            )
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
        expected_experimental_signature: Mapping[str, Any] | None = None,
        required_workflow_smoke: Mapping[str, Any] | None = None,
        expected_protocol_digest: str | None = None,
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
                        expected_experimental_signature=expected_experimental_signature,
                        required_workflow_smoke=required_workflow_smoke,
                        expected_protocol_digest=expected_protocol_digest,
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
                    "input": copy.deepcopy(dict(stage_input)),
                    "rules": list(rules),
                    "allowed_source_refs": sorted(allowed_source_refs),
                    "instruction": (
                        "Correct every listed violation in this stage and no later stage. "
                        "Treat input, including the directive and upstream typed artifacts, as immutable authority; rejected_stage is not authoritative. "
                        "Preserve only content that remains semantically aligned with that input, do not invent source facts, and remove every field not admitted by the schema. "
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
        requested_task_id = str((dialog_payload or {}).get("task_id") or "").strip()
        active_task_id = str(state.get("active_task_id") or "")
        if requested_task_id and requested_task_id != active_task_id:
            raise ValueError(
                "selected ResearchTask is read-only until it is explicitly activated for formulation"
            )
        if state.get("accepted_prototype_digest"):
            raise ValueError(
                "active ResearchTask has an accepted immutable formulation; create and "
                "activate a new branch ResearchTask before requesting a revision"
            )
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
            {"group_id": group_id, "run_id": run_id, "directive": directive, "pipeline": "staged_v1", "task_ref": f"research-task:{active_task_id}"},
            subject_ref=f"research-task:{active_task_id}",
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
                    for key in ("title", "background", "research_question", "hypotheses", "source_grounding", "constraints", "assumptions", "open_questions", "experimental_signature")
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
                    "Emit experimental_signature as the immutable typed identity for later stages: stable dataset, baseline and intervention ids/labels/specifications, the single intervention boundary, and the primary outcome. Copy agreed directive semantics exactly even when the sources only motivate rather than prove them.",
                    "The experimental_signature baseline and intervention ids must be distinct. It identifies the proposed experiment and is not a claim that its effect is already established.",
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
                        "workflow_smoke": copy.deepcopy(DEFAULT_WORKFLOW_SMOKE_POLICY),
                        "confirmation": "must be separately budgeted and is the only inferential stage",
                        "pairing": "predeclare every paired unit; vary only the intervention",
                        "negative_results": "retain_and_report",
                        "ray": "deferred",
                        "runner_contract": "adaos.research.runner.v1",
                        "comparison_identity": "stable lowercase arm ids plus one exact primary minuend/subtrahend",
                    },
                },
                rules=[
                    "Design exactly separated workflow_smoke and confirmatory stages; smoke never supports a scientific claim.",
                    "In comparison_design give every comparator a stable lowercase machine id. The comparators array must contain either all ordered arm ids or all ordered arm labels, never a mixture. Declare exactly one baseline and at least one intervention, and bind the primary estimand to two declared arm ids as minuend and subtrahend.",
                    "Copy experimental_signature identity fields exactly: comparator ids and labels, dataset_id and dataset label, system subject, intervention boundary, and primary outcome name/measurement/unit. Do not substitute a related experiment or rephrase these identity fields.",
                    "In experimental_plan.system_specification enumerate the concrete system, baseline, intervention, data, and measurement components needed to reproduce the protocol. Record exact ordered settings such as layers, algorithms, transforms, optimizer, schedules, and metric definitions; words such as style, suitable, standard, or equivalent are not implementation specifications.",
                    "Mark each system component source_derived, policy_default, or proposed. Cite supplied SRC-### ids for every source-derived component, keep source_refs empty for the other statuses, and put every intentionally invariant detail in locked_invariants.",
                    "Make intervention_boundary identify the only allowed experimental difference. unresolved_choices must contain every missing implementation decision; it must be empty before ready_for_automation.",
                    "Use the supplied CPU smoke policy. Mark other non-source choices as proposed or policy_default, never source_derived.",
                    "For workflow_smoke set execution_profile.network_mode=offline, input_policy.readiness=required_before_execution, budget.workload.mode=bounded, and provide non-empty named limits that make the complete run practical on CPU. Epoch count alone is not a bound. A deterministic_contract_fixture is preferred for engineering conformance when the accepted scientific dataset is not preprovisioned; it remains non-inferential.",
                    "For confirmatory execution use input_policy.source=accepted_dataset. Declare whether its workload is full or bounded without silently inheriting the smoke subset.",
                    "Populate all nine keys in decisions_by_area and cite refs only for source-derived choices; AdaOS owns decision ids.",
                    "Resolve every candidate uncertainty from problem_frame into one of those nine decisions. A bounded proposed choice closes it; an optional extension is out of scope and is not a blocker.",
                    "For each decision use blocking_question only when status is unresolved; otherwise it must be the empty string. Do not repeat the same uncertainty in multiple areas.",
                    "In data_policy.evaluation_access separate development/model selection from the final evaluation. Choose selection_source truthfully; AdaOS compiles its exact selection rule. Expose final test only once per trained unit after the seal and prohibit test feedback.",
                    "Follow any source requirement for train/validation/untouched-test separation. Never evaluate final test per epoch or use it to choose checkpoints, hyperparameters, variants, or stopping.",
                    "Declare exact integer RNG seed_values. Never use labels such as S1 as seeds. Make pairing allocation planned_units exactly equal, in the same order, to the confirmatory integer seed_values and make sample_size equal their count.",
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
                expected_experimental_signature=problem["experimental_signature"],
                required_workflow_smoke=DEFAULT_WORKFLOW_SMOKE_POLICY,
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
                    "protocol_digest": stage_digest(protocol),
                    "target": {
                        "kind": "adaos_skill",
                        "ref": f"skill:{token}",
                        "execution": "current_or_member_node",
                        "ray": "deferred",
                    },
                },
                rules=[
                    "Translate the accepted scientific semantics into independently testable obligations; do not change the protocol.",
                    "Include explicit execution and data obligations that verify every workflow_smoke workload limit, input source/readiness policy, network mode, and wall-clock bound in machine-readable run evidence.",
                    "Copy protocol_digest and experimental_signature ids/outcome exactly into scientific_bindings and bind runner_contract=adaos.research.runner.v1.",
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
                expected_experimental_signature=problem["experimental_signature"],
                expected_protocol_digest=stage_digest(protocol),
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
                "Research compilation produced five facets and passed traceability coverage.",
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
        requested_task_id = str((dialog_payload or {}).get("task_id") or "").strip()
        active_task_id = str(state.get("active_task_id") or "")
        if requested_task_id and requested_task_id != active_task_id:
            raise ValueError(
                "selected ResearchTask is read-only until it is explicitly activated for formulation"
            )
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
            {"group_id": group_id, "directive": directive, "task_ref": f"research-task:{active_task_id}"},
            subject_ref=f"research-task:{active_task_id}",
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
                        {"id": "smoke", "purpose": "...", "evidence_class": "workflow_smoke", "execution_profile": {"node": "current_or_member", "device": "cpu", "network_mode": "offline"}, "budget": {"epochs": 3, "seed_values": [17], "max_wall_time_minutes": 30, "workload": {"mode": "bounded", "limits": [{"name": "domain_unit", "maximum": 128, "unit": "items"}]}}, "input_policy": {"source": "deterministic_contract_fixture", "readiness": "required_before_execution", "sampling": "deterministic_seeded"}, "inference_allowed": False, "stop_conditions": ["bounded operational condition"]},
                        {"id": "confirmatory", "purpose": "...", "evidence_class": "confirmatory", "execution_profile": {"node": "declared_member", "device": "cuda", "network_mode": "offline"}, "budget": {"epochs": 120, "seed_values": [1, 2, 3], "max_wall_time_minutes": 10080, "workload": {"mode": "full", "limits": []}}, "input_policy": {"source": "accepted_dataset", "readiness": "required_before_execution", "sampling": "full"}, "inference_allowed": True, "stop_conditions": ["predeclared fixed or sequential condition"]}
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
                                "planned_units": [1, 2, 3],
                                "sample_size": 3,
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
                "A workflow smoke must be mechanically bounded by named workload limits, use an explicit input source/readiness policy, and fit inside its wall-clock budget; epochs alone are not a workload bound.",
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
            consumer_contract = dict(
                self._invoke_skill(
                    "research_manager_skill",
                    "get_runner_contract",
                    {},
                    timeout=60,
                )
            )
            declared_consumer_digest = str(consumer_contract.get("digest") or "")
            if (
                consumer_contract.get("schema") != "adaos.contract.operation_set.v1"
                or consumer_contract.get("contract") != "adaos.research.runner.v1"
                or declared_consumer_digest
                != contract_digest(
                    {key: item for key, item in consumer_contract.items() if key != "digest"}
                )
            ):
                raise ValueError("ResearchManager returned an invalid runner consumer contract")
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
                    {
                        "kind": "consumer_contract",
                        "ref": "contract:adaos.research.runner.v1",
                        "digest": declared_consumer_digest,
                        "media_type": "application/json",
                    },
                ],
                acceptance_profiles=[
                    "project.conformance",
                    "research.consumer-contracts",
                    "research.traceability",
                ],
                acceptance_requirements=[
                    {
                        "id": "research.consumer-contracts",
                        "profile": "research.consumer-contracts",
                        "provider_ref": "skill:research_manager_skill",
                        "operation": "validate_development_candidate",
                        "required": True,
                        "timeout_seconds": 300,
                        "parameters": {"execute_workflow_smoke": True},
                    },
                    {
                        "id": "research.traceability",
                        "profile": "research.traceability",
                        "provider_ref": "skill:research_manager_skill",
                        "operation": "validate_development_candidate",
                        "required": True,
                        "timeout_seconds": 120,
                    },
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
            consumer_instruction = development_sessions.attach_instruction(
                str(compiled_instruction["session"]["session_id"]),
                "consumer_contract",
                consumer_contract,
                expected_digest=declared_consumer_digest,
            )
            session = consumer_instruction["session"]
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

        return self.repository.once(
            str(idempotency_key or "").strip(),
            "accept_prototype",
            operation,
        )

    @staticmethod
    def _component_identity(ref: str) -> tuple[str, str]:
        kind, separator, component_id = str(ref or "").strip().partition(":")
        if separator != ":" or kind not in {"skill", "scenario"} or not component_id:
            raise ValueError("implementation target must be an exact skill: or scenario: ref")
        return kind, component_id

    @staticmethod
    def _release_identity(value: Mapping[str, Any]) -> dict[str, str | None]:
        candidate = value.get("candidate") if isinstance(value.get("candidate"), Mapping) else {}
        release = value.get("release") if isinstance(value.get("release"), Mapping) else {}
        workflow = value.get("workflow") if isinstance(value.get("workflow"), Mapping) else {}
        delivery = workflow.get("delivery") if isinstance(workflow.get("delivery"), Mapping) else {}
        return {
            "candidate_id": str(
                candidate.get("candidate_id")
                or delivery.get("candidate_id")
                or value.get("candidate_id")
                or ""
            ).strip() or None,
            "release_digest": str(
                candidate.get("release_digest")
                or release.get("release_digest")
                or delivery.get("release_digest")
                or value.get("release_digest")
                or ""
            ).strip() or None,
            "package_digest": str(
                candidate.get("package_digest")
                or delivery.get("package_digest")
                or value.get("package_digest")
                or ""
            ).strip() or None,
            "version": str(
                release.get("version")
                or value.get("version")
                or value.get("published_version")
                or ""
            ).strip() or None,
        }

    def _observed_builder_trial_identity(
        self,
        kind: str,
        target_id: str,
    ) -> dict[str, str | None] | None:
        """Adopt an exact Builder Trial that completed before local binding.

        Builder and the research repository are independent durable owners.  A
        process interruption may therefore leave the Builder result committed
        while the implementation track has not yet recorded its release
        digest.  Read-only reconciliation avoids repeating activation and only
        accepts Builder's complete immutable identity.
        """

        response = dict(
            self._invoke_skill(
                "builder_sdk_control_skill",
                "get_workflow",
                {"object_type": kind, "object_id": target_id},
                timeout=60,
            )
        )
        delivery = response.get("delivery") if isinstance(response.get("delivery"), Mapping) else {}
        governed = response.get("governed") if isinstance(response.get("governed"), Mapping) else {}
        if (
            str(delivery.get("status") or "").strip() != "trial"
            or str(governed.get("state") or "").strip() != "trial_review"
        ):
            return None
        identity = self._release_identity({"workflow": response})
        if not identity["candidate_id"] or not identity["release_digest"] or not identity["package_digest"]:
            return None
        return identity

    def start_implementation(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
        builder_webspace_id: str | None = None,
        actor: str = "user:local",
    ) -> dict[str, Any]:
        state = self.get(
            direction_id,
            task_id=task_id,
            implementation_track_id=implementation_track_id,
        )
        selected_task = state.get("selected_task")
        if isinstance(selected_task, Mapping):
            self._ensure_implementation_project(state["direction"], selected_task)
        track = state.get("active_implementation_track")
        session = state.get("development_session")
        if not isinstance(track, Mapping) or not isinstance(session, Mapping):
            raise ValueError("an accepted compilation and bound Development Session are required")
        kind, target_id = self._component_identity(str(track.get("primary_target_ref") or ""))
        webspace = str(builder_webspace_id or "").strip() or (
            "research-dev-" + hashlib.sha256(str(track["ref"]).encode("utf-8")).hexdigest()[:20]
        )
        development_sessions.bind(str(session["session_id"]), webspace)
        current = self._invoke_skill(
            "builder_sdk_control_skill",
            "get_automation",
            {"object_type": kind, "object_id": target_id, "webspace_id": webspace},
            timeout=120,
        )
        current_status = str((current or {}).get("status") or "").lower()
        builder_session = (
            current.get("session")
            if isinstance(current.get("session"), Mapping)
            else {}
        )
        current_development_session_id = str(
            builder_session.get("development_session_id") or ""
        ).strip()
        incoming_development_session_id = str(session["session_id"])
        development_session_rebase = bool(
            current_development_session_id
            and current_development_session_id != incoming_development_session_id
        )
        if development_session_rebase and current_status in {
            "completed",
            "succeeded",
            "failed",
            "cancelled",
        }:
            response = dict(
                self._invoke_skill(
                    "builder_sdk_control_skill",
                    "submit_automation",
                    {
                        "object_type": kind,
                        "object_id": target_id,
                        "webspace_id": webspace,
                        "text": (
                            "Rebase the terminal Automation result onto the newly compiled, "
                            "digest-bound Development Session selected by the research "
                            "orchestrator. Treat its instruction envelope as the only current "
                            "scientific, engineering, and consumer-contract authority."
                        ),
                    },
                    timeout=180,
                )
            )
            reused = False
            recovery_iteration = False
        elif current_status in {"queued", "starting", "working", "running", "completed"}:
            response = dict(current)
            reused = True
            recovery_iteration = False
        elif current_status in {"failed", "cancelled"}:
            response = dict(
                self._invoke_skill(
                    "builder_sdk_control_skill",
                    "submit_automation",
                    {
                        "object_type": kind,
                        "object_id": target_id,
                        "webspace_id": webspace,
                        "text": (
                            "Rebase the terminal Automation result onto the newly compiled, "
                            "digest-bound Development Session selected by the research "
                            "orchestrator. Treat its instruction envelope as the only current "
                            "scientific, engineering, and consumer-contract authority."
                            if development_session_rebase
                            else
                            "Retry the unchanged digest-bound Development Session after a "
                            "recorded infrastructure failure. Do not add, remove, reinterpret, "
                            "or broaden any scientific or engineering requirement."
                        ),
                    },
                    timeout=180,
                )
            )
            reused = False
            recovery_iteration = not development_session_rebase
        else:
            response = dict(
                self._invoke_skill(
                    "builder_sdk_control_skill",
                    "start_automation",
                    {"object_type": kind, "object_id": target_id, "webspace_id": webspace},
                    timeout=180,
                )
            )
            reused = False
            recovery_iteration = False
        projection = response.get("automation") if isinstance(response.get("automation"), Mapping) else response
        status = str(projection.get("status") or response.get("status") or "submitted")
        task_ref = str(projection.get("task_id") or response.get("task_id") or "")
        normalized = {
            "completed": "implementation_complete",
            "succeeded": "implementation_complete",
            "failed": "implementation_failed",
            "cancelled": "implementation_failed",
        }.get(status.lower(), "implementation_running")
        track = self.repository.record_track_evaluation(
            str(track["track_id"]),
            status=normalized,
            metadata={
                **dict(track.get("metadata") or {}),
                "automation": {
                    "status": status.lower(),
                    "task_id": task_ref or None,
                    "phase": projection.get("phase") or response.get("phase"),
                    "updated_at": projection.get("updated_at") or response.get("updated_at"),
                    "development_session_rebase": development_session_rebase,
                },
            },
        )
        event_source = task_ref or contract_digest(
            {"track_ref": track["ref"], "status": status, "session_id": session["session_id"]}
        )
        self.repository.activity(
            str(state["direction"]["direction_id"]),
            "implementation",
            status,
            (
                "Builder Automation recovery iteration started for the unchanged exact "
                "Development Session."
                if recovery_iteration
                else "Builder Automation was rebased onto the current exact Development Session."
                if development_session_rebase
                else f"Builder Automation {'reused' if reused else 'started'} for the exact Development Session."
            ),
            {
                "task_ref": (state.get("selected_task") or {}).get("ref"),
                "implementation_track_ref": track["ref"],
                "development_session_id": session["session_id"],
                "builder_webspace_id": webspace,
                "automation_task_id": task_ref or None,
                "recovery_iteration": recovery_iteration,
                "development_session_rebase": development_session_rebase,
                "actor": actor,
            },
            actor=actor,
            origin="skill:builder_sdk_control_skill",
            subject_ref=str(track["ref"]),
            source_event_id=f"automation-start:{event_source}",
        )
        return {
            "ok": bool(response.get("ok", True)),
            "reused": reused,
            "recovery_iteration": recovery_iteration,
            "development_session_rebase": development_session_rebase,
            "direction_ref": state["direction"]["ref"],
            "task_ref": (state.get("selected_task") or {}).get("ref"),
            "implementation_track_ref": track["ref"],
            "development_session_id": session["session_id"],
            "builder_webspace_id": webspace,
            "automation": response,
        }

    def sync_implementation(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
        builder_webspace_id: str | None = None,
        actor: str = "system:research_orchestrator",
    ) -> dict[str, Any]:
        state = self.get(
            direction_id,
            task_id=task_id,
            implementation_track_id=implementation_track_id,
        )
        track = state.get("active_implementation_track")
        session = state.get("development_session")
        if not isinstance(track, Mapping) or not isinstance(session, Mapping):
            raise ValueError("implementation track has no Development Session")
        kind, target_id = self._component_identity(str(track.get("primary_target_ref") or ""))
        webspace = str(builder_webspace_id or "").strip() or (
            "research-dev-" + hashlib.sha256(str(track["ref"]).encode("utf-8")).hexdigest()[:20]
        )
        development_sessions.bind(str(session["session_id"]), webspace)
        response = dict(
            self._invoke_skill(
                "builder_sdk_control_skill",
                "get_automation",
                {"object_type": kind, "object_id": target_id, "webspace_id": webspace},
                timeout=120,
            )
        )
        projection = response.get("automation") if isinstance(response.get("automation"), Mapping) else response
        status = str(projection.get("status") or response.get("status") or "unknown").lower()
        normalized = {
            "completed": "implementation_complete",
            "succeeded": "implementation_complete",
            "failed": "implementation_failed",
            "cancelled": "implementation_failed",
            "working": "implementation_running",
            "running": "implementation_running",
            "queued": "implementation_running",
            "starting": "implementation_running",
        }.get(status, str(track.get("status") or "development_ready"))
        metadata = {
            **dict(track.get("metadata") or {}),
            "automation": {
                "status": status,
                "task_id": projection.get("task_id") or response.get("task_id"),
                "phase": projection.get("phase") or response.get("phase"),
                "updated_at": projection.get("updated_at") or response.get("updated_at"),
                "failure_id": projection.get("failure_id") or response.get("failure_id"),
                "failure_stage": projection.get("failure_stage") or response.get("failure_stage"),
                "failure_message": projection.get("error") or response.get("failure_message"),
                "progress_message": response.get("progress_message"),
            },
        }
        updated_track = self.repository.record_track_evaluation(
            str(track["track_id"]),
            status=normalized,
            metadata=metadata,
        )
        event_identity = contract_digest(
            {
                "task_id": metadata["automation"].get("task_id"),
                "status": status,
                "phase": metadata["automation"].get("phase"),
                "updated_at": metadata["automation"].get("updated_at"),
                "failure_id": metadata["automation"].get("failure_id"),
            }
        )
        self.repository.activity(
            str(state["direction"]["direction_id"]),
            "implementation",
            status,
            str(
                metadata["automation"].get("progress_message")
                or metadata["automation"].get("failure_message")
                or f"Builder Automation is {status}."
            ),
            {
                "task_ref": (state.get("selected_task") or {}).get("ref"),
                "implementation_track_ref": track["ref"],
                "development_session_id": session["session_id"],
                "automation": metadata["automation"],
            },
            actor=actor,
            origin="skill:builder_sdk_control_skill",
            subject_ref=str(track["ref"]),
            source_event_id=f"automation-state:{event_identity}",
        )
        return {"ok": bool(response.get("ok", True)), "track": updated_track, "automation": response}

    def prepare_project_release(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
        builder_webspace_id: str | None = None,
        bump: str = "patch",
        confirmed: bool = False,
        actor: str = "user:local",
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("candidate trial preparation requires explicit confirmation")
        synced = self.sync_implementation(
            direction_id,
            task_id=task_id,
            implementation_track_id=implementation_track_id,
            builder_webspace_id=builder_webspace_id,
            actor=actor,
        )
        track = dict(synced["track"])
        if track.get("candidate_release_digest"):
            return {"ok": True, "reused": True, "track": track}
        if track.get("status") != "implementation_complete":
            raise ValueError("ProjectRelease candidate requires a completed Builder Automation")
        kind, target_id = self._component_identity(str(track["primary_target_ref"]))
        observed_identity = self._observed_builder_trial_identity(kind, target_id)
        if observed_identity is not None:
            track = self.repository.bind_track_release(
                str(track["track_id"]),
                candidate_release_digest=str(observed_identity["release_digest"]),
            )
            self.repository.activity(
                str(track["direction_id"]),
                "release",
                "trial_ready",
                "Observed and adopted the exact Builder ProjectRelease trial after local reconciliation.",
                {"implementation_track_ref": track["ref"], **observed_identity, "reconciled": True},
                actor=actor,
                origin="skill:builder_sdk_control_skill",
                subject_ref=str(track["ref"]),
                source_event_id=(
                    f"release-candidate:{observed_identity['candidate_id']}:"
                    f"{observed_identity['release_digest']}"
                ),
            )
            return {
                "ok": True,
                "reused": True,
                "reconciled": True,
                "track": track,
                "identity": observed_identity,
            }
        webspace = str(builder_webspace_id or "").strip() or (
            "research-dev-" + hashlib.sha256(str(track["ref"]).encode("utf-8")).hexdigest()[:20]
        )
        response = dict(
            self._invoke_skill(
                "builder_sdk_control_skill",
                "publish_project",
                {
                    "object_type": kind,
                    "object_id": target_id,
                    "webspace_id": webspace,
                    "bump": bump,
                    "dry_run": True,
                    "confirmed": True,
                },
                timeout=240,
            )
        )
        identity = self._release_identity(response)
        if not identity["candidate_id"] or not identity["release_digest"] or not identity["package_digest"]:
            raise ValueError("Builder candidate did not return complete immutable release identity")
        track = self.repository.bind_track_release(
            str(track["track_id"]),
            candidate_release_digest=str(identity["release_digest"]),
        )
        self.repository.activity(
            str(track["direction_id"]),
            "release",
            "trial_ready",
            "Builder prepared an isolated ProjectRelease candidate for review.",
            {"implementation_track_ref": track["ref"], **identity},
            actor=actor,
            origin="skill:builder_sdk_control_skill",
            subject_ref=str(track["ref"]),
            source_event_id=f"release-candidate:{identity['candidate_id']}:{identity['release_digest']}",
        )
        return {"ok": True, "reused": False, "track": track, "candidate": response, "identity": identity}

    def publish_project_release(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
        builder_webspace_id: str | None = None,
        bump: str = "patch",
        confirmed: bool = False,
        actor: str = "user:local",
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("ProjectRelease promotion requires explicit confirmation")
        state = self.get(direction_id, task_id=task_id, implementation_track_id=implementation_track_id)
        track = state.get("active_implementation_track")
        if not isinstance(track, Mapping):
            raise ValueError("implementation track is required")
        if track.get("project_release_ref"):
            return {"ok": True, "reused": True, "track": dict(track)}
        candidate_digest = str(track.get("candidate_release_digest") or "").strip()
        if not candidate_digest:
            raise ValueError("prepare and review a ProjectRelease candidate before promotion")
        kind, target_id = self._component_identity(str(track["primary_target_ref"]))
        webspace = str(builder_webspace_id or "").strip() or (
            "research-dev-" + hashlib.sha256(str(track["ref"]).encode("utf-8")).hexdigest()[:20]
        )
        response = dict(
            self._invoke_skill(
                "builder_sdk_control_skill",
                "publish_project",
                {
                    "object_type": kind,
                    "object_id": target_id,
                    "webspace_id": webspace,
                    "bump": bump,
                    "dry_run": False,
                    "confirmed": True,
                },
                timeout=300,
            )
        )
        if response.get("error") or response.get("ok") is False or response.get("requires_reapply"):
            raise RuntimeError(f"Builder did not promote the candidate: {response.get('error') or response.get('status')}")
        identity = self._release_identity(response)
        promoted_digest = str(identity.get("release_digest") or candidate_digest)
        if promoted_digest != candidate_digest:
            raise ValueError("promoted ProjectRelease digest differs from the reviewed candidate")
        project_id = str(track.get("project_ref") or "project:unknown").partition(":")[2]
        project_release_ref = f"project-release:{project_id}:{promoted_digest}"
        updated = self.repository.bind_track_release(
            str(track["track_id"]),
            candidate_release_digest=candidate_digest,
            project_release_ref=project_release_ref,
            project_release_digest=promoted_digest,
        )
        self.repository.activity(
            str(track["direction_id"]),
            "release",
            "release_ready",
            "The reviewed ProjectRelease candidate was promoted through Builder.",
            {"implementation_track_ref": track["ref"], "project_release_ref": project_release_ref, **identity},
            actor=actor,
            origin="skill:builder_sdk_control_skill",
            subject_ref=str(track["ref"]),
            source_event_id=f"project-release:{promoted_digest}",
        )
        return {"ok": True, "reused": False, "track": updated, "release": response}

    @staticmethod
    def _validated_split_bindings(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        raw = value.get("split_bindings") if isinstance(value.get("split_bindings"), Mapping) else value.get("splits")
        if not isinstance(raw, Mapping):
            raise ValueError("runner dataset_status must return split_bindings")
        result: dict[str, dict[str, Any]] = {}
        for role in ("validation", "robustness", "test"):
            item = raw.get(role)
            if not isinstance(item, Mapping):
                raise ValueError(f"runner dataset_status omits the {role} split binding")
            projected = {
                "digest": str(item.get("digest") or "").strip(),
                "dataset_digest": str(item.get("dataset_digest") or "").strip(),
                "locator": str(item.get("locator") or "").strip(),
                "sealed": bool(item.get("sealed")),
            }
            if any(not projected[key] for key in ("digest", "dataset_digest", "locator")):
                raise ValueError(f"runner {role} split binding has incomplete immutable identity")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", projected["digest"]):
                raise ValueError(f"runner {role} split digest is not sha256")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", projected["dataset_digest"]):
                raise ValueError(f"runner {role} dataset digest is not sha256")
            result[role] = projected
        if len({item["digest"] for item in result.values()}) != 3:
            raise ValueError("validation, robustness, and sealed test split digests must be distinct")
        if len({item["dataset_digest"] for item in result.values()}) != 1:
            raise ValueError("all split bindings must resolve to one immutable dataset")
        if result["test"]["sealed"] is not True:
            raise ValueError("runner test split binding must be sealed")
        return result

    @staticmethod
    def _manager_conditions(plan: Mapping[str, Any], *, runner_id: str, dataset_digest: str) -> dict[str, Any]:
        execution: dict[str, Any] = {}
        for profile_id, profile in dict(plan["execution"]).items():
            seeds = list(dict(profile)["seeds"])
            if any(isinstance(seed, bool) or not isinstance(seed, int) for seed in seeds):
                raise ValueError("the current ResearchManager execution ABI requires integer seed units")
            profile_value = dict(profile)
            evidence_class = str(profile_value["evidence_class"])
            manager_profile = (
                "preflight" if evidence_class == "workflow_smoke"
                else "confirmatory" if evidence_class == "confirmatory"
                else str(profile_id)
            )
            if manager_profile in execution:
                raise ValueError(f"ExperimentPlan maps multiple stages to ResearchManager profile {manager_profile}")
            execution[manager_profile] = {
                "source_stage_id": str(profile_id),
                "epochs": int(profile_value["epochs"]),
                "seeds": seeds,
                "device": str(profile_value["device"]),
                "network_mode": str(
                    profile_value.get("network_mode") or "unrestricted"
                ),
                "workers": 0,
                "wall_time_s": int(profile_value["max_wall_time_minutes"]) * 60,
                "workload": copy.deepcopy(
                    dict(profile_value.get("workload") or {})
                ),
                "input_policy": copy.deepcopy(
                    dict(profile_value.get("input_policy") or {})
                ),
                "evidence_class": evidence_class,
                "inference_allowed": bool(profile_value["inference_allowed"]),
            }
        analysis = dict(plan["analysis"])
        randomization = dict(plan["randomization"])
        dataset = dict(plan["dataset"])
        runner_contract = dict(plan["runner_contract"])
        result_record = runner_contract.get("result_record")
        if not isinstance(result_record, Mapping):
            raise ValueError(
                "Study instantiation requires ExperimentPlan v1.1 canonical result_record paths"
            )
        return {
            "dataset": {
                "name": str(dataset["logical_name"]),
                "version": str(dataset_digest),
                "policy_digest": str(dataset["policy_digest"]),
                "split_strategy": str(dataset["split_strategy"]),
                "evaluation_seal": str(dataset["evaluation_seal"]),
            },
            "operators": copy.deepcopy(dict(plan["operators"])),
            "execution": execution,
            "randomization": {
                "named_streams": copy.deepcopy(list(randomization["named_streams"])),
                "paired": True,
                "unit": str(randomization["unit"]),
                "invariant_fields": copy.deepcopy(list(randomization["invariant_fields"])),
                "varied_fields": copy.deepcopy(list(randomization["varied_fields"])),
            },
            "analysis": {
                "primary_metric": str(analysis["primary_metric"]),
                "primary_estimand": str(analysis["primary_estimand"]),
                "primary_contrast": copy.deepcopy(dict(analysis["primary_contrast"])),
                "paired": True,
                "result_metric_path": str(
                    result_record["primary_metric_path"]
                ),
                "result_step_path": str(
                    result_record["step_path"]
                ),
                "initialization_digest_path": str(
                    result_record["pairing_identity_path"]
                ),
                "uncertainty": copy.deepcopy(dict(analysis["uncertainty"])),
                "stopping_rule": copy.deepcopy(dict(analysis["stopping_rule"])),
            },
            "tracker": {"provider": "local-tracker", "required_delivery": "durable-before-finalize"},
            "runner": {
                "provider": runner_id,
                "contract": "adaos.research.runner.v1",
                "data_owner": runner_id,
            },
        }

    def instantiate_study(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
        actor: str = "user:local",
        idempotency_key: str,
    ) -> dict[str, Any]:
        state = self.get(direction_id, task_id=task_id, implementation_track_id=implementation_track_id)
        track = state.get("active_implementation_track")
        compilation_record = state.get("accepted_compilation_record")
        session = state.get("development_session")
        if not isinstance(track, Mapping) or not isinstance(compilation_record, Mapping) or not isinstance(session, Mapping):
            raise ValueError("accepted compilation, implementation track, and Development Session are required")
        if not track.get("project_release_ref") or not track.get("project_release_digest"):
            raise ValueError("Study instantiation requires an exact promoted ProjectRelease")
        compilation = dict(compilation_record["payload"])
        facet = dict(dict(compilation.get("facets") or {}).get("experiment_plan") or {})
        plan = facet.get("payload") if isinstance(facet.get("payload"), Mapping) else None
        if not isinstance(plan, Mapping):
            raise ValueError("accepted ResearchCompilation has no compiled ExperimentPlan")
        _, runner_id = self._component_identity(str(track["primary_target_ref"]))
        dataset_status = dict(
            self._invoke_skill(runner_id, "dataset_status", {}, timeout=180)
        )
        if dataset_status.get("ready") is False:
            raise ValueError("runner dataset is not ready for Study instantiation")
        splits = self._validated_split_bindings(dataset_status)
        problem = dict(dict(compilation["facets"])["research_problem"]["payload"])
        hypotheses = [dict(item) for item in problem.get("hypotheses") or [] if isinstance(item, Mapping)]
        if not hypotheses:
            raise ValueError("accepted compilation has no falsifiable hypothesis")
        realization = {
            "direction_ref": state["direction"]["ref"],
            "task_ref": compilation_record["task_ref"],
            "compilation_ref": compilation_record["ref"],
            "compilation_digest": compilation_record["digest"],
            "implementation_track_ref": track["ref"],
            "development_session_id": session["session_id"],
            "project_release_ref": track["project_release_ref"],
            "project_release_digest": track["project_release_digest"],
            "runner_ref": f"skill:{runner_id}",
            "runner_contract": "adaos.research.runner.v1",
        }
        study = dict(
            self._invoke_skill(
                "research_manager_skill",
                "create_compiled_study",
                {
                    "title": str(problem.get("title") or state["direction"]["title"]),
                    "hypothesis": str(hypotheses[0]["statement"]),
                    "protocol": {
                        "schema": "adaos.research.compiled_protocol.v1",
                        "compilation_ref": compilation_record["ref"],
                        "compilation_digest": compilation_record["digest"],
                        "experimental_protocol": dict(compilation["facets"])["experimental_protocol"]["payload"],
                        "experiment_plan_digest": plan["digest"],
                    },
                    "analysis_plan": dict(plan["analysis"]),
                    "splits": splits,
                    "realization": realization,
                    "mode": "confirmatory" if any(item["evidence_class"] == "confirmatory" for item in dict(plan["execution"]).values()) else "exploratory",
                    "study_id": None,
                    "idempotency_key": f"{idempotency_key}:study",
                },
                timeout=180,
            )
        )
        study_record = dict(study.get("study") or {})
        realization_record = dict(study.get("realization") or {})
        study_id = str(study_record.get("record_id") or study_record.get("study_id") or "")
        if not study_id or not realization_record.get("record_id") or not realization_record.get("digest"):
            raise RuntimeError("ResearchManager returned incomplete StudyRealization identity")
        conditions = self._manager_conditions(
            plan,
            runner_id=runner_id,
            dataset_digest=splits["validation"]["dataset_digest"],
        )
        experiment = dict(
            self._invoke_skill(
                "research_manager_skill",
                "create_experiment",
                {
                    "study_id": study_id,
                    "slug": "primary",
                    "title": str(problem.get("title") or "Primary experiment"),
                    "purpose": str(problem.get("research_question") or hypotheses[0]["statement"]),
                    "conditions": conditions,
                    "experiment_id": None,
                    "idempotency_key": f"{idempotency_key}:experiment",
                },
                timeout=180,
            )
        )
        experiment_record = dict(experiment.get("experiment") or {})
        experiment_id = str(experiment_record.get("record_id") or "")
        if not experiment_id:
            raise RuntimeError("ResearchManager returned no Experiment identity")
        track = self.repository.bind_track_study(
            str(track["track_id"]),
            study_id=study_id,
            study_realization_ref=f"study-realization:{realization_record['record_id']}",
            study_realization_digest=str(realization_record["digest"]),
            runner_ref=f"skill:{runner_id}",
            experiment_id=experiment_id,
        )
        selected_task = state.get("selected_task") or state.get("active_task") or {}
        existing_studies = list(dict(selected_task.get("metadata") or {}).get("matched_studies") or [])
        study_ref = {
            "schema": "adaos.research.study_ref.v1",
            "study_id": study_id,
            "ref": f"study:{study_id}",
            "owner_ref": "skill:research_manager_skill",
            "status": "draft",
            "compilation_digest": compilation_record["digest"],
            "project_release_digest": track["project_release_digest"],
            "realization_ref": track["study_realization_ref"],
            "experiment_ref": f"experiment:{experiment_id}",
        }
        studies_by_ref = {str(item.get("ref")): dict(item) for item in existing_studies if isinstance(item, Mapping)}
        studies_by_ref[study_ref["ref"]] = study_ref
        self.repository.merge_task_metadata(
            str(selected_task["task_id"]),
            {"matched_studies": list(studies_by_ref.values())},
        )
        self.repository.activity(
            str(state["direction"]["direction_id"]),
            "study",
            "experiment_ready",
            "ResearchManager bound the accepted compilation and ProjectRelease to a draft CPU-capable Study and Experiment.",
            {
                "task_ref": compilation_record["task_ref"],
                "implementation_track_ref": track["ref"],
                "study_ref": study_ref["ref"],
                "study_realization_ref": track["study_realization_ref"],
                "experiment_ref": study_ref["experiment_ref"],
                "runner_ref": track["runner_ref"],
            },
            actor=actor,
            origin="skill:research_manager_skill",
            subject_ref=str(track["ref"]),
            source_event_id=f"study-realization:{realization_record['record_id']}",
        )
        return {
            "ok": True,
            "track": track,
            "study": study,
            "experiment": experiment,
            "experiment_plan": dict(plan),
            "dataset_status": dataset_status,
        }

    def start_study_smoke(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
        confirmed: bool = False,
        actor: str = "user:local",
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("locking the compiled protocol and starting CPU smoke requires explicit confirmation")
        state = self.get(direction_id, task_id=task_id, implementation_track_id=implementation_track_id)
        track = state.get("active_implementation_track")
        if not isinstance(track, Mapping) or not track.get("study_id") or not track.get("experiment_id"):
            raise ValueError("instantiate the Study and Experiment before starting smoke")
        study_id = str(track["study_id"])
        experiment_id = str(track["experiment_id"])
        study = dict(
            self._invoke_skill(
                "research_manager_skill", "get_study", {"study_id": study_id}, timeout=120
            )
        )
        study_lifecycle = dict(study.get("workflow") or {})
        if study_lifecycle.get("state") == "draft":
            self._invoke_skill(
                "research_manager_skill",
                "advance_workflow",
                {
                    "study_id": study_id,
                    "command": "submit_protocol_review",
                    "expected_generation": int(study_lifecycle.get("generation") or 0),
                    "idempotency_key": f"{idempotency_key}:study-review",
                    "actor": actor,
                    "evidence_refs": [str(track["study_realization_digest"])],
                },
                timeout=120,
            )
        experiment = dict(
            self._invoke_skill(
                "research_manager_skill", "get_experiment", {"experiment_id": experiment_id}, timeout=120
            )
        )
        lifecycle = dict(experiment.get("lifecycle") or {})
        if lifecycle.get("state") == "draft":
            self._invoke_skill(
                "research_manager_skill",
                "submit_experiment_review",
                {
                    "experiment_id": experiment_id,
                    "expected_generation": int(lifecycle.get("generation") or 0),
                    "idempotency_key": f"{idempotency_key}:experiment-review",
                    "actor": actor,
                },
                timeout=120,
            )
            experiment = dict(
                self._invoke_skill(
                    "research_manager_skill", "get_experiment", {"experiment_id": experiment_id}, timeout=120
                )
            )
            lifecycle = dict(experiment.get("lifecycle") or {})
        if lifecycle.get("state") == "review":
            self._invoke_skill(
                "research_manager_skill",
                "lock_experiment",
                {
                    "experiment_id": experiment_id,
                    "expected_generation": int(lifecycle.get("generation") or 0),
                    "idempotency_key": f"{idempotency_key}:lock",
                    "actor": actor,
                },
                timeout=120,
            )
            experiment = dict(
                self._invoke_skill(
                    "research_manager_skill", "get_experiment", {"experiment_id": experiment_id}, timeout=120
                )
            )
            lifecycle = dict(experiment.get("lifecycle") or {})
        study = dict(
            self._invoke_skill(
                "research_manager_skill", "get_study", {"study_id": study_id}, timeout=120
            )
        )
        study_lifecycle = dict(study.get("workflow") or {})
        if study_lifecycle.get("state") == "locked":
            self._invoke_skill(
                "research_manager_skill",
                "advance_workflow",
                {
                    "study_id": study_id,
                    "command": "approve_smoke",
                    "expected_generation": int(study_lifecycle.get("generation") or 0),
                    "idempotency_key": f"{idempotency_key}:study-smoke",
                    "actor": actor,
                    "evidence_refs": [str(track["study_realization_digest"])],
                },
                timeout=120,
            )
        reused = lifecycle.get("state") in {"running", "results_ready", "finalized"}
        if lifecycle.get("state") == "locked":
            started = dict(
                self._invoke_skill(
                    "research_manager_skill",
                    "start_experiment",
                    {
                        "experiment_id": experiment_id,
                        "profile": "preflight",
                        "expected_generation": int(lifecycle.get("generation") or 0),
                        "idempotency_key": f"{idempotency_key}:start-preflight",
                        "actor": actor,
                    },
                    timeout=240,
                )
            )
            reused = False
        elif reused:
            started = {"ok": True, "reused": True, "lifecycle": lifecycle}
        else:
            raise ValueError(f"Experiment cannot start smoke from lifecycle state {lifecycle.get('state')}")
        self.repository.activity(
            str(track["direction_id"]),
            "study",
            "smoke_started" if not reused else "smoke_reused",
            "ResearchManager admitted the exact compiled preflight and submitted its CPU attempts.",
            {
                "implementation_track_ref": track["ref"],
                "study_ref": f"study:{study_id}",
                "experiment_ref": f"experiment:{experiment_id}",
                "profile": "preflight",
            },
            actor=actor,
            origin="skill:research_manager_skill",
            subject_ref=str(track["ref"]),
            source_event_id=f"experiment-start:{experiment_id}:preflight",
        )
        return {"ok": True, "reused": reused, "track": dict(track), "start": started}

    def sync_study(
        self,
        direction_id: str,
        *,
        task_id: str | None = None,
        implementation_track_id: str | None = None,
        actor: str = "system:research_orchestrator",
    ) -> dict[str, Any]:
        state = self.get(direction_id, task_id=task_id, implementation_track_id=implementation_track_id)
        track = state.get("active_implementation_track")
        if not isinstance(track, Mapping) or not track.get("experiment_id"):
            raise ValueError("implementation track has no ResearchManager Experiment")
        experiment_id = str(track["experiment_id"])
        reconciled = dict(
            self._invoke_skill(
                "research_manager_skill",
                "reconcile_experiment",
                {"experiment_id": experiment_id, "actor": actor},
                timeout=180,
            )
        )
        experiment = dict(
            self._invoke_skill(
                "research_manager_skill", "get_experiment", {"experiment_id": experiment_id}, timeout=120
            )
        )
        lifecycle = dict(experiment.get("lifecycle") or {})
        attempts = list(experiment.get("attempts") or [])
        event_identity = contract_digest(
            {
                "experiment_id": experiment_id,
                "lifecycle": lifecycle,
                "attempts": [
                    {"attempt_id": item.get("attempt_id"), "status": item.get("status")}
                    for item in attempts
                    if isinstance(item, Mapping)
                ],
            }
        )
        self.repository.activity(
            str(track["direction_id"]),
            "study",
            str(lifecycle.get("state") or "unknown"),
            f"ResearchManager reconciliation observed {len(attempts)} attempt(s); Experiment is {lifecycle.get('state') or 'unknown'}.",
            {
                "implementation_track_ref": track["ref"],
                "study_ref": f"study:{track['study_id']}",
                "experiment_ref": f"experiment:{experiment_id}",
                "lifecycle": lifecycle,
                "attempts": attempts,
            },
            actor=actor,
            origin="skill:research_manager_skill",
            subject_ref=str(track["ref"]),
            source_event_id=f"experiment-state:{event_identity}",
        )
        return {"ok": True, "track": dict(track), "reconciliation": reconciled, "experiment": experiment}


__all__ = ["ResearchOrchestrator"]
