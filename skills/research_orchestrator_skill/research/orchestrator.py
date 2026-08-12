from __future__ import annotations

import json
import os
import re
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
from adaos.services.agent_context import get_ctx
from adaos.services.skill.artifacts import skill_upload_dir

from research.contracts import (
    materialize_automation_brief,
    materialize_prototype,
    prototype_admission_issues,
    prototype_candidate_schema,
    prototype_quality_issues,
)
from research.repository import OrchestratorRepository


_DIRECTION_RE = re.compile(r"^[a-z0-9_.-]+$")


def _direction_id(value: str) -> str:
    token = str(value or "").strip().lower()
    if not _DIRECTION_RE.fullmatch(token):
        raise ValueError("direction_id must match ^[a-z0-9_.-]+$")
    return token


def _json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM response does not contain a JSON object") from None
        value = json.loads(raw[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value


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

    def _require_direction_project(self, direction_id: str) -> dict[str, Any]:
        description = projects.describe("skill", direction_id)
        manifest = yaml.safe_load(projects.read_file("skill", direction_id, "skill.yaml")["content"]) or {}
        research = manifest.get("research_direction") if isinstance(manifest, Mapping) else None
        if not isinstance(research, Mapping) or research.get("schema") != "adaos.research.direction.v1":
            raise ValueError(f"skill:{direction_id} is not a research_direction project")
        project = compositions.project_for_component(f"skill:{direction_id}")
        if not project or "adaos.research.direction.v1" not in set(project.get("profiles") or []):
            raise ValueError(f"skill:{direction_id} is not owned by an adaos.research.direction.v1 Project")
        return {**description, "project": project}

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
        created = compositions.create_research_direction(
            project_id,
            title=title,
            description=description,
            skill_id=skill_id,
            tags=list(tags or []),
            actor=actor,
        )
        direction_id = str(created["project"]["components"]["owned"][0]["ref"]).partition(":")[2]
        initialized = self.initialize(direction_id, title, actor=actor)
        self.repository.activity(
            direction_id,
            "intake",
            "project_created",
            f"Project {created['project']['ref']} and primary skill:{direction_id} were created atomically.",
            {"project_manifest_digest": created["project"]["manifest_digest"], "actor": actor},
        )
        return initialized

    def list_directions(self, *, limit: int = 500) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        states = {item["direction_id"]: item for item in self.repository.list_directions(limit=5000)}
        for project in compositions.list_projects(profile="adaos.research.direction.v1", limit=limit):
            direction_id = str(project["primary_ref"]).partition(":")[2]
            state = states.get(direction_id)
            bundle = artifact_context.source_bundle(direction_id)
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
                    **project,
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
                }
            )
        return {"ok": True, "items": items, "count": len(items)}

    def initialize(self, direction_id: str, title: str, *, actor: str = "user:local") -> dict[str, Any]:
        token = _direction_id(direction_id)
        self._require_direction_project(token)
        state = self.repository.initialize(token, str(title or token).strip())
        self.repository.activity(token, "intake", "ready", "Research direction initialized.", {"actor": actor})
        return self.get(token)

    def attach_source(
        self,
        direction_id: str,
        path: str,
        *,
        group_id: str = "part0",
        name: str | None = None,
        role: str = "source",
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
        result = artifact_context.add_path(
            token,
            group_id,
            path,
            name=name,
            role=role,
            origin={"kind": "orchestrator_intake", "actor": actor},
            replace_existing=replace_existing,
        )
        staging_cleanup = {"requested": bool(cleanup_staging), "removed": False}
        if staging_source is not None:
            staging_source.unlink(missing_ok=True)
            staging_cleanup["removed"] = not staging_source.exists()
        bundle = artifact_context.source_bundle(token)
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

    def get(self, direction_id: str) -> dict[str, Any]:
        token = _direction_id(direction_id)
        state = self.repository.get_direction(token)
        if not state:
            raise ValueError("research direction is not initialized")
        bundle = artifact_context.source_bundle(token)
        project = self._require_direction_project(token)["project"]
        prototype = self.repository.get_prototype(state.get("current_prototype_digest"))
        accepted = self.repository.get_prototype(state.get("accepted_prototype_digest"))
        brief = self.repository.get_brief(state.get("automation_brief_digest"))
        sessions = development_sessions.list_sessions(project_id=str(project["id"]), limit=20) if brief else []
        builder_url = None
        if sessions:
            scope = navigation.runtime_scope()
            destination = navigation.webspace_destination(
                zone=str(scope["zone"]),
                subnet_id=str(scope["subnet_id"]),
                webspace_id="desktop-dev",
                space_kind="workspace",
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
            "direction": {**state, "project_ref": project["ref"], "primary_skill_ref": f"skill:{token}"},
            "project": project,
            "artifact_groups": artifact_context.groups(token),
            "source_bundle": bundle,
            "current_prototype": prototype,
            "prototype_stale": prototype_stale,
            "formulation": {
                "admission_decision": admission_review.get("decision") or "needs_discussion",
                "admission_blockers": list(admission_review.get("blockers") or []),
                "can_accept": bool(prototype) and not prototype_stale and admission_review.get("decision") == "admitted",
                "context_coverage": (prototype or {}).get("context_coverage") or {},
            },
            "accepted_prototype": accepted,
            "automation_brief": brief,
            "development_session": sessions[-1] if sessions else None,
            "builder_url": builder_url,
            "next_steps": self._next_steps(state, bundle, prototype),
        }

    def sync_source_bundle(self, direction_id: str, *, actor: str = "user:local") -> dict[str, Any]:
        token = _direction_id(direction_id)
        state = self.repository.get_direction(token)
        if not state:
            self.initialize(token, token, actor=actor)
            state = self.repository.get_direction(token)
        bundle = artifact_context.source_bundle(token)
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
        binding = development_sessions.bind(str(session["session_id"]), builder_webspace_id)
        selected = builder_preview.select_project(
            "skill",
            str(state["direction"]["primary_skill_ref"]).partition(":")[2],
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
            space_kind="workspace",
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
    ) -> dict[str, Any]:
        token = _direction_id(direction_id)
        state = self.repository.get_direction(token)
        if not state:
            raise ValueError("research direction is not initialized")
        if state.get("accepted_prototype_digest"):
            raise ValueError("accepted formulation is immutable; create a new Builder change before revising it")
        bundle = artifact_context.source_bundle(token)
        if not bundle.get("sources"):
            raise ValueError("at least one source artifact is required")
        previous = self.repository.get_prototype(state.get("current_prototype_digest"))
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
            )
        stored = self.repository.put_prototype(token, prototype)
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

    def _source_context(self, bundle: Mapping[str, Any]) -> dict[str, Any]:
        selected: list[dict[str, Any]] = []
        sources = [item for item in bundle.get("sources") or [] if isinstance(item, Mapping)]
        remaining = 80_000
        per_source = min(40_000, max(8_000, remaining // max(1, len(sources))))
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

    def discuss(self, direction_id: str, text: str, *, model: str | None = None, actor: str = "user:local", dialog_payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        token = _direction_id(direction_id)
        state = self.repository.get_direction(token)
        if not state:
            raise ValueError("research direction is not initialized")
        bundle = artifact_context.source_bundle(token)
        if not bundle.get("sources"):
            raise ValueError("attach at least one source before discussion")
        current = self.repository.get_prototype(state.get("current_prototype_digest"))
        group_id = f"research-formulation-{token}-{int(state['generation']) + 1}"
        dialog = self._dialog({"direction_id": token, **dict(dialog_payload or {})})
        self.repository.activity(token, "formulation", "llm_submitted", "Research formulation sent to the configured Root LLM.", {"group_id": group_id, "actor": actor})
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
                    "data_policy": {"dataset": "exact dataset and version", "split_strategy": "...", "evaluation_seal": "...", "leakage_controls": ["..."]},
                    "reproducibility": {
                        "rng_streams": [
                            {"id": "initialization", "controls": "..."},
                            {"id": "sampling", "controls": "..."},
                            {"id": "augmentation", "controls": "..."},
                            {"id": "analysis", "controls": "..."}
                        ],
                        "pairing": {"unit": "seed or other paired unit", "invariant_fields": ["..."], "varied_fields": ["..."]},
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
                "implementation_requirements": [{"id": "REQ-1", "requirement": "concrete implementation obligation", "verification": "independent command/report/assertion"}],
                "acceptance_checks": [{"id": "AC-1", "check": "observable pass condition", "evidence": "expected report, artifact or test"}],
                "readiness": {"decision": "needs_discussion|ready_for_automation", "blocking_questions": ["..."]},
            },
            "validation_schema": prototype_candidate_schema(),
            "rules": [
                "Return JSON only.",
                "Cardinality is contractual: hypotheses >= 1, experimental_plan.stages >= 2, evaluation_plan.outcomes >= 1, constraints >= 1, assumptions >= 1, implementation_requirements >= 5, and acceptance_checks >= 4.",
                "Every acceptance check must be distinct and observable; do not merge checks merely to shorten the list.",
                "Do not invent facts absent from sources; put uncertainty into assumptions/open_questions.",
                "Cite exact provenance refs supplied in source_bundle. Never invent an artifact ref or cite an omitted fragment.",
                "Every hypothesis id needs a source_grounding record, and observations must be explicitly separated from interpretations and hypotheses.",
                "Historical notebook outputs are exploratory source material, never confirmation.",
                "Separate workflow smoke execution from scientific confirmation.",
                "Declare exactly one primary outcome, one operationalized estimand, uncertainty unit/method, multiplicity, practical significance, and a predeclared stopping rule.",
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
        try:
            submitted = llm_client.submit_response_job(
                [{"role": "system", "content": "Return one JSON object matching the supplied output_contract."}, {"role": "user", "content": json.dumps(instructions, ensure_ascii=False)}],
                model=model,
                max_tokens=7000,
                request_id=request_id,
                profile_scope="research.formulation",
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
                raise RuntimeError(str(completed.get("error") or "Root LLM job failed"))
            candidate = _json_object(str(completed.get("output_text") or ""))
            recorded: dict[str, Any] | None = None
            repair_attempt = 0
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
                    repair_payload = {
                        "task": "Repair the rejected candidate. Fix the exact validation error, preserve scientifically correct content, and return one complete candidate JSON object (not a patch). Before returning, count every required array and verify all hard constraints.",
                        "validation_error": str(validation_error),
                        "rejected_candidate": candidate,
                        "output_contract": instructions["output_contract"],
                        "validation_schema": instructions["validation_schema"],
                        "rules": instructions["rules"],
                        "user_request": instructions["task"],
                        "source_bundle": instructions["source_bundle"],
                    }
                    repaired_submit = llm_client.submit_response_job(
                        [
                            {"role": "system", "content": "You are a strict JSON contract repairer. Return JSON only, never omit required nested fields, and satisfy every stated minimum cardinality."},
                            {"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False)},
                        ],
                        model=model,
                        max_tokens=7000,
                        request_id=repair_request_id,
                        profile_scope="research.formulation.repair",
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
                        raise RuntimeError(str(repaired.get("error") or "Root LLM repair failed"))
                    candidate = _json_object(str(repaired.get("output_text") or ""))
            message = str(recorded["prototype"].get("assistant_message") or f"ResearchPrototype revision {recorded['prototype']['revision']} is ready.")
            self.repository.activity(token, "formulation", "llm_completed", message, {"job_id": job_id, "prototype_digest": recorded["prototype"]["digest"]})
            self._emit(message, dialog, group_id=group_id, phase="completed", status="succeeded", seq=999999)
            return {**recorded, "message": message, "llm_job": {"job_id": job_id, "request_id": request_id, "status": "succeeded"}}
        except Exception as exc:
            self.repository.activity(token, "formulation", "failed", f"Formulation failed: {type(exc).__name__}: {exc}", {"request_id": request_id})
            self._emit(f"Не удалось сформировать ревизию: {exc}", dialog, group_id=group_id, phase="failed", status="failed", seq=999999)
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
            bundle = artifact_context.source_bundle(token)
            if bundle.get("digest") != prototype.get("source_bundle_digest"):
                raise ValueError("artifact groups changed after this ResearchPrototype revision; discuss and review a new revision")
            readiness = prototype.get("readiness") if isinstance(prototype.get("readiness"), Mapping) else {}
            if readiness.get("decision") != "ready_for_automation" or list(readiness.get("blocking_questions") or []):
                raise ValueError("ResearchPrototype still has blocking questions")
            self.repository.activity(token, "acceptance", "checkpointing", "Creating an exact private local Builder checkpoint for the direction skill; no source is published.", {"prototype_digest": prototype_digest, "actor": actor})
            checkpoint = dict(
                self._checkpoint(
                    kind="skill",
                    artifact_id=token,
                    message=f"research formulation accepted {prototype_digest}",
                    metadata={"research_prototype_digest": prototype_digest, "source_bundle_digest": bundle["digest"], "actor": actor},
                )
            )
            if not any(checkpoint.get(key) for key in ("package_digest", "source_revision", "source_tree", "sha256", "commit")):
                raise ValueError("Builder checkpoint did not return an immutable source identity")
            project = self._require_direction_project(token)["project"]
            groups = [artifact_context.get_group(token, item["group_id"]) for item in artifact_context.groups(token)]
            brief = materialize_automation_brief(
                direction_id=token,
                project=project,
                artifact_groups=groups,
                source_bundle=bundle,
                prototype=prototype,
                checkpoint=checkpoint,
                actor=actor,
            )
            stored = self.repository.accept(token, expected_generation=expected_generation, prototype=prototype, brief=brief)
            session_result = development_sessions.create(
                str(project["id"]),
                automation_brief_digest=str(stored["digest"]),
                research_prototype_digest=str(prototype["digest"]),
                artifact_groups=[str(item["group_id"]) for item in groups],
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
            session = session_result["session"]
            self.repository.activity(token, "acceptance", "handoff_ready", "Research formulation accepted; Automation Brief and scoped Development Session are ready. Codex was not started.", {"prototype_digest": prototype_digest, "automation_brief_digest": stored["digest"], "development_session_id": session["session_id"], "checkpoint": checkpoint})
            return {"ok": True, "direction": self.repository.get_direction(token), "prototype": prototype, "automation_brief": stored, "development_session": session, "builder_checkpoint": checkpoint, "codex_started": False}

        return self.repository.once(str(idempotency_key or "").strip(), "accept_prototype", operation)


__all__ = ["ResearchOrchestrator"]
