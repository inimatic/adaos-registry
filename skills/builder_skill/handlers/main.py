from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable as IterableABC
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from adaos.sdk.core.decorators import subscribe, tool


SKILL_ID = "builder_skill"
DIALOG_CHANNEL_ID = "builder"
AGENT_ID = "agent:builder_skill:builder"
AGENT_LABEL = "\u041a\u043e\u043d\u0441\u0442\u0440\u0443\u043a\u0442\u043e\u0440"
SESSIONS_KEY = "builder_skill.sessions"
CURRENT_KEY = "builder_skill.current_session"
MAX_SESSIONS = 50
WORKBENCH_REFRESH_TOPIC = "builder.workbench.ensure_requested"
PROMPT_IDE_SCENARIO_ID = "prompt_engineer_scenario"
CHAT_APPEND_TIMEOUT_S = 0.75
PENDING_ACTION_TIMEOUT_S = 1.5
PROMPT_SELECTION_ASYNC_TOPICS = ("prompt.project.changed", "builder.preview.selected")
BUILDER_MEMORY_FILE = "builder_memory.md"
BUILDER_SYSTEM_PROMPT_FILE = "builder_system_prompt.md"
PROMPT_TZ_BASE_FILE = Path("tz") / "base_tz.md"
PROMPT_REVISION_FILES = (
    PROMPT_TZ_BASE_FILE,
    Path(BUILDER_MEMORY_FILE),
    Path(BUILDER_SYSTEM_PROMPT_FILE),
    Path("prepare") / "general_prompt.md",
    Path("generate") / "general_prompt.md",
)
BUILDER_WEBUI_SCHEMA_FORCED_DEFS = (
    "formInputs",
    "formField",
    "formInputType",
    "formFieldType",
    "formOption",
    "formValidation",
    "formBranching",
    "formQuiz",
)
BUILDER_FORM_CHOICE_FIELD_TYPES = {
    "select",
    "dropdown",
    "choice",
    "enum",
    "combobox",
    "searchableselect",
    "searchable_select",
    "singlechoice",
    "single_choice",
    "radio",
    "radiogroup",
    "radio_group",
    "multichoice",
    "multi_choice",
    "multiplechoice",
    "multiple_choice",
    "checkboxes",
    "checkboxgroup",
    "checkbox_group",
    "chips",
    "tags",
}
BUILDER_FORM_GRID_FIELD_TYPES = {
    "singlechoicegrid",
    "single_choice_grid",
    "multiplechoicegrid",
    "multiple_choice_grid",
    "radiogrid",
    "checkboxgrid",
    "checkbox_grid",
    "multichoicegrid",
    "multi_choice_grid",
    "ratinggrid",
    "rating_grid",
}

_FALLBACK_MEMORY: dict[str, Any] = {}
_LOG = logging.getLogger("adaos.skills.builder_skill")


def _now() -> float:
    return time.time()


def _webspace_id(value: str | None = None, _meta: Mapping[str, Any] | None = None) -> str:
    token = str(value or "").strip()
    if token:
        return token
    if isinstance(_meta, Mapping):
        for key in ("webspace_id", "workspace_id"):
            raw = _meta.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return "default"


def _source_webspace_id(value: str | None = None, _meta: Mapping[str, Any] | None = None) -> str:
    if isinstance(_meta, Mapping):
        for key in ("source_webspace_id", "builder_source_webspace_id"):
            raw = _meta.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    token = _webspace_id(value, _meta)
    if token.endswith("-dev") and len(token) > 4:
        return token[:-4]
    return token


def _scenario_id_from_prompt_topic(value: Any) -> str:
    token = str(value or "").strip()
    prefix = "prompt-project:scenario:"
    if not token.startswith(prefix):
        return ""
    scenario_id = token[len(prefix) :].strip()
    if not scenario_id or any(ch in scenario_id for ch in "\\/"):
        return ""
    return scenario_id


def _requested_scenario_id_from_meta(_meta: Mapping[str, Any] | None = None) -> str:
    if not isinstance(_meta, Mapping):
        return ""
    for key in ("thread_id", "conversation_thread_id", "conversation_topic_id", "topic_id"):
        scenario_id = _scenario_id_from_prompt_topic(_meta.get(key))
        if scenario_id:
            return scenario_id
    topic = _meta.get("builder_topic") if isinstance(_meta.get("builder_topic"), Mapping) else {}
    for key in ("scenario_id", "project_id"):
        token = str(topic.get(key) or "").strip()
        if token:
            return token
    for key in ("thread_id", "topic_id"):
        scenario_id = _scenario_id_from_prompt_topic(topic.get(key))
        if scenario_id:
            return scenario_id
    return ""


def _align_workbench_binding_to_meta(webspace_id: str, _meta: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    scenario_id = _requested_scenario_id_from_meta(_meta)
    if not scenario_id:
        return None
    try:
        svc = _workbench_service()
        binding = svc.get_workspace_binding(webspace_id)
        current = str(binding.get("runtime_scenario_id") or "").strip()
        if current == scenario_id:
            return binding
        return svc.set_active_draft(
            source_webspace_id=webspace_id,
            active_draft_id=None,
            runtime_scenario_id=scenario_id,
            persist_projection=True,
        )
    except Exception:
        _LOG.debug("failed to align builder workbench binding to meta scenario=%s", scenario_id, exc_info=True)
        return None


def _paired_dev_webspace_id(source_webspace_id: str) -> str | None:
    try:
        from adaos.services.builder.workbench import dev_webspace_id_for_source

        return dev_webspace_id_for_source(source_webspace_id)
    except Exception:
        source = str(source_webspace_id or "").strip()
        return f"{source}-dev" if source else None


def _scoped_key(base: str, webspace_id: str) -> str:
    return f"{base}.{webspace_id or 'default'}"


def _mem_get(key: str, default: Any = None) -> Any:
    try:
        from adaos.sdk.data import skill_memory

        return skill_memory.get(key, default)
    except Exception:
        return copy.deepcopy(_FALLBACK_MEMORY.get(key, default))


def _mem_set(key: str, value: Any) -> None:
    try:
        from adaos.sdk.data import skill_memory

        skill_memory.set(key, value)
    except Exception:
        _FALLBACK_MEMORY[key] = copy.deepcopy(value)


def _mem_set_many(values: Mapping[str, Any]) -> None:
    payload = {str(key): copy.deepcopy(value) for key, value in values.items()}
    if not payload:
        return
    try:
        from adaos.sdk.data import skill_env

        env = skill_env.read_env()
        env.update(payload)
        skill_env.write_env(env)
    except Exception:
        _FALLBACK_MEMORY.update(payload)


def _sessions(webspace_id: str) -> dict[str, dict[str, Any]]:
    raw = _mem_get(_scoped_key(SESSIONS_KEY, webspace_id), {})
    return copy.deepcopy(raw) if isinstance(raw, dict) else {}


def _trim_sessions(sessions: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    items = sorted((dict(v) for v in sessions.values()), key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    return {str(item["id"]): item for item in items[:MAX_SESSIONS] if item.get("id")}


def _save_sessions(webspace_id: str, sessions: Mapping[str, Mapping[str, Any]]) -> None:
    _mem_set(_scoped_key(SESSIONS_KEY, webspace_id), _trim_sessions(sessions))


def _current_session_id(webspace_id: str) -> str | None:
    raw = _mem_get(_scoped_key(CURRENT_KEY, webspace_id))
    token = str(raw or "").strip()
    return token or None


def _set_current_session_id(webspace_id: str, session_id: str) -> None:
    _mem_set(_scoped_key(CURRENT_KEY, webspace_id), str(session_id or "").strip())


def _hash_suffix(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:8]


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _elapsed_ms(started_at: float) -> float:
    return round(max(0.0, (time.perf_counter() - started_at) * 1000.0), 3)


def _builder_revision_materialization_enabled() -> bool:
    raw = os.getenv("ADAOS_BUILDER_REVISION_MATERIALIZATION_FAST_PATH")
    if raw is None:
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _builder_revision_materialization_delay_s() -> float:
    raw = os.getenv("ADAOS_BUILDER_REVISION_MATERIALIZATION_DELAY_S")
    if raw is None:
        return 0.0
    try:
        value = float(str(raw or "").strip())
    except Exception:
        return 0.0
    return max(0.0, min(value, 10.0))


def _builder_revision_chat_emit_delay_s() -> float:
    raw = os.getenv("ADAOS_BUILDER_REVISION_CHAT_EMIT_DELAY_S")
    if raw is None:
        return 0.5
    try:
        value = float(str(raw or "").strip())
    except Exception:
        return 0.5
    return max(0.0, min(value, 10.0))


def _webui_source_fingerprint(payload: Mapping[str, Any] | None) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    try:
        return hashlib.sha1(_compact_json(_repair_text_tree(dict(payload))).encode("utf-8")).hexdigest()
    except Exception:
        return None


def _meta_user_id(_meta: Mapping[str, Any] | None) -> str:
    if isinstance(_meta, Mapping):
        for key in ("user_id", "current_user_id", "profile_id", "actor_user_id"):
            token = str(_meta.get(key) or "").strip()
            if token:
                return token
    return "guest"


def _meta_roles(_meta: Mapping[str, Any] | None) -> list[str]:
    raw: Any = None
    if isinstance(_meta, Mapping):
        raw = _meta.get("roles")
        if raw is None:
            raw = _meta.get("user_roles")
        if raw is None:
            raw = _meta.get("role")
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, IterableABC) and not isinstance(raw, (bytes, bytearray, str, Mapping)):
        items = raw
    else:
        items = []
    roles: list[str] = []
    seen: set[str] = set()
    for item in items:
        token = str(item or "").strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        roles.append(token)
    roles.sort()
    return roles


def _read_text_file(path: Path, *, limit: int | None = None) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception:
        return ""
    if limit is not None and limit >= 0:
        return text[:limit]
    return text


def _write_text_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value or ""), encoding="utf-8")


def _invalidate_scenario_runtime_caches(root: Path, reason: str) -> None:
    scenario_id = str(root.name or "").strip()
    if not scenario_id:
        return
    try:
        from adaos.services.scenarios import loader as scenarios_loader  # pylint: disable=import-outside-toplevel

        scenarios_loader.invalidate_cache(scenario_id=scenario_id, space="dev")
        scenarios_loader.invalidate_cache(scenario_id=scenario_id, space="workspace")
    except Exception:
        _LOG.debug("failed to invalidate scenario loader cache scenario=%s", scenario_id, exc_info=True)
    try:
        from adaos.services.scenario import webspace_runtime  # pylint: disable=import-outside-toplevel

        invalidate = getattr(webspace_runtime, "_invalidate_resolved_webspace_cache", None)
        if callable(invalidate):
            invalidate(scenario_id=scenario_id, reason=reason)
    except Exception:
        _LOG.debug("failed to invalidate resolved webspace cache scenario=%s", scenario_id, exc_info=True)


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _project_artifact_root(session: Mapping[str, Any]) -> Path | None:
    token = str(session.get("artifact_root") or "").strip()
    if not token:
        return None
    try:
        path = Path(token)
    except Exception:
        return None
    try:
        if path.exists() and path.is_dir():
            return path
    except Exception:
        return None
    return None


def _builder_llm_timeout_s() -> float:
    raw = os.getenv("ADAOS_BUILDER_LLM_TIMEOUT_S")
    try:
        value = float(raw) if raw else 150.0
    except (TypeError, ValueError):
        value = 150.0
    return max(30.0, min(value, 300.0))


def _builder_llm_max_tokens() -> int:
    raw = os.getenv("ADAOS_BUILDER_LLM_MAX_TOKENS")
    try:
        value = int(raw) if raw else 5000
    except (TypeError, ValueError):
        value = 5000
    return max(1000, min(value, 12000))


def _builder_llm_temperature() -> float:
    raw = os.getenv("ADAOS_BUILDER_LLM_TEMPERATURE")
    try:
        value = float(raw) if raw else 0.2
    except (TypeError, ValueError):
        value = 0.2
    return max(0.0, min(value, 1.0))


def _builder_llm_model_from_meta(_meta: Mapping[str, Any] | None = None) -> str | None:
    meta = dict(_meta or {}) if isinstance(_meta, Mapping) else {}
    for key in ("builder_llm_model", "llm_model", "model"):
        token = str(meta.get(key) or "").strip()
        if token:
            return token
    return None


def _builder_llm_env_model() -> str | None:
    token = str(os.getenv("ADAOS_BUILDER_LLM_MODEL") or "").strip()
    return token or None


def _builder_llm_model(_meta: Mapping[str, Any] | None = None) -> str | None:
    return _builder_llm_model_from_meta(_meta) or _builder_llm_env_model()


def _builder_llm_model_from_session(session: Mapping[str, Any] | None) -> str | None:
    if not isinstance(session, Mapping):
        return None
    root = _project_artifact_root(session)
    if root is None:
        return None
    state = _load_json_file(root / "prompt_state.json")
    if not isinstance(state, Mapping):
        return None
    for key in ("builder_llm_model", "llm_model", "llm_profile_id"):
        token = str(state.get(key) or "").strip()
        if token:
            return token
    return None


def _builder_llm_model_for_session(
    session: Mapping[str, Any] | None,
    _meta: Mapping[str, Any] | None = None,
) -> str | None:
    return _builder_llm_model_from_meta(_meta) or _builder_llm_model_from_session(session) or _builder_llm_env_model()


def _builder_llm_prompt_profile(model: str | None = None) -> dict[str, Any]:
    model_hint = (
        str(model or "").strip()
        or _builder_llm_model()
        or str(os.getenv("ADAOS_LLM_MODEL") or "").strip()
        or str(os.getenv("OPENAI_RESPONSES_MODEL") or "").strip()
        or "root-default"
    )
    provider = str(os.getenv("ADAOS_BUILDER_LLM_PROVIDER") or os.getenv("ADAOS_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        lowered_model = model_hint.lower()
        provider = "openai" if lowered_model.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")) else "root-default"
    profile_id = str(os.getenv("ADAOS_BUILDER_LLM_PROMPT_PROFILE") or "").strip().lower()
    if not profile_id:
        profile_id = "default"
    return {
        "schema": "adaos.builder.llm_prompt_profile.v1",
        "id": profile_id,
        "provider": provider,
        "model": model_hint,
        "temperature": _builder_llm_temperature(),
        "strategy": "compact_abi_plus_affordance_map",
        "variant_policy": "Prompt profiles may vary by provider/model, but the output contract remains adaos.webui.v1.",
    }


def _builder_llm_request_id(
    *,
    session: Mapping[str, Any],
    instruction: str,
    current_payload: Mapping[str, Any],
    attempt: int,
) -> str:
    seed = {
        "schema": "adaos.builder.llm_request_id.v1",
        "scenario_id": session.get("scenario_id"),
        "session_id": session.get("id"),
        "revision": session.get("ui_revision") or session.get("version"),
        "instruction": instruction,
        "current_webui_json": current_payload,
        "attempt": attempt,
    }
    digest = hashlib.sha256(_compact_json(seed).encode("utf-8", errors="ignore")).hexdigest()
    return f"builder-ui-{digest[:32]}"


def _builder_llm_job_request_id(
    *,
    session: Mapping[str, Any],
    instruction: str,
    current_payload: Mapping[str, Any],
    attempt: int,
    job_nonce: str | None = None,
) -> str:
    base_id = _builder_llm_request_id(
        session=session,
        instruction=instruction,
        current_payload=current_payload,
        attempt=attempt,
    )
    nonce = str(job_nonce or "").strip()
    if not nonce:
        return base_id
    suffix = _hash_suffix(f"{nonce}:{attempt}")
    return f"{base_id}-job-{suffix}"


def _looks_like_timeout(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token in lowered for token in ("timed out", "timeout", "read operation timed out", "504"))


def _looks_like_llm_request_id_conflict(exc: Exception | str) -> bool:
    code = str(getattr(exc, "error_code", "") or "").strip()
    if code == "llm_request_id_conflict":
        return True
    payload = getattr(exc, "payload", None)
    if isinstance(payload, Mapping):
        values: list[Any] = [payload.get("code"), payload.get("error")]
        detail = payload.get("detail")
        if isinstance(detail, Mapping):
            values.extend([detail.get("code"), detail.get("error")])
        for value in values:
            if str(value or "").strip() == "llm_request_id_conflict":
                return True
    return "llm_request_id_conflict" in str(exc or "")


def _scenario_id_from_idea(idea: str) -> str:
    lowered = _repair_mojibake_text(idea).lower()
    if "shopping" in lowered or "shop" in lowered or "\u043f\u043e\u043a\u0443\u043f" in lowered:
        base = "shopping_list"
    elif "todo" in lowered or "\u0437\u0430\u0434\u0430\u0447" in lowered:
        base = "todo_list"
    else:
        ascii_base = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
        base = ascii_base[:40].strip("_") or "prototype_app"
    return f"{base}_{_hash_suffix(idea)}"


def _conversation_id(webspace_id: str) -> str:
    return f"conv.skill.{SKILL_ID}.default.{webspace_id or 'default'}"


def _prompt_project_topic_id(session: Mapping[str, Any] | None = None, binding: Mapping[str, Any] | None = None) -> str:
    source = session if isinstance(session, Mapping) else {}
    fallback = binding if isinstance(binding, Mapping) else {}
    scenario_id = str(source.get("scenario_id") or fallback.get("runtime_scenario_id") or "").strip()
    if not scenario_id:
        return ""
    return f"prompt-project:scenario:{scenario_id}"


def _builder_topic_ref(
    webspace_id: str,
    *,
    session: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(_meta or {})
    existing_topic = meta.get("builder_topic") if isinstance(meta.get("builder_topic"), Mapping) else {}
    thread_id = str(meta.get("thread_id") or meta.get("conversation_thread_id") or meta.get("conversation_topic_id") or "").strip()
    topic_id = str(meta.get("topic_id") or "").strip()
    session = session if isinstance(session, Mapping) else {}
    binding = binding if isinstance(binding, Mapping) else {}
    prompt_topic_id = _prompt_project_topic_id(session=session, binding=binding)
    if prompt_topic_id and (
        thread_id != prompt_topic_id
        or (topic_id and topic_id != prompt_topic_id)
    ):
        thread_id = prompt_topic_id
        topic_id = prompt_topic_id
        existing_topic = {}
    if thread_id:
        topic = {k: v for k, v in dict(existing_topic or {}).items() if v is not None}
        scenario_id = str(session.get("scenario_id") or binding.get("runtime_scenario_id") or "").strip() or None
        topic["schema"] = "adaos.conversation.topic_ref.v1"
        topic["thread_id"] = thread_id
        topic["topic_id"] = topic_id or thread_id
        topic["topic_kind"] = "builder_scenario" if prompt_topic_id else "builder_runtime"
        topic["webspace_id"] = webspace_id
        topic["source_webspace_id"] = webspace_id
        topic["active_draft_id"] = str(session.get("draft_id") or binding.get("active_draft_id") or "").strip() or None
        topic["scenario_id"] = scenario_id
        topic["project_id"] = scenario_id or topic.get("active_draft_id")
        topic["dev_webspace_id"] = str(binding.get("dev_webspace_id") or _paired_dev_webspace_id(webspace_id) or "").strip() or None
        topic["conversation_id"] = _conversation_id(webspace_id)
        topic["channel_id"] = DIALOG_CHANNEL_ID
        topic["owner"] = f"skill:{SKILL_ID}"
        return topic
    session_topic = session.get("topic_ref") if isinstance(session.get("topic_ref"), Mapping) else {}
    if session_topic and str(session_topic.get("thread_id") or session_topic.get("topic_id") or "").strip():
        topic = {k: v for k, v in dict(session_topic).items() if v is not None}
        topic.setdefault("schema", "adaos.conversation.topic_ref.v1")
        topic.setdefault("topic_id", str(session_topic.get("topic_id") or session_topic.get("thread_id") or "").strip())
        topic.setdefault("thread_id", str(session_topic.get("thread_id") or session_topic.get("topic_id") or "").strip())
        if prompt_topic_id and (
            str(topic.get("thread_id") or "").strip() != prompt_topic_id
            or str(topic.get("topic_id") or "").strip() != prompt_topic_id
        ):
            topic["thread_id"] = prompt_topic_id
            topic["topic_id"] = prompt_topic_id
        scenario_id = str(session.get("scenario_id") or binding.get("runtime_scenario_id") or "").strip() or None
        topic["topic_kind"] = "builder_scenario" if prompt_topic_id else str(topic.get("topic_kind") or "builder_runtime")
        topic["webspace_id"] = webspace_id
        topic["source_webspace_id"] = webspace_id
        topic["active_draft_id"] = str(session.get("draft_id") or binding.get("active_draft_id") or "").strip() or None
        topic["scenario_id"] = scenario_id
        topic["project_id"] = scenario_id or topic.get("active_draft_id")
        topic["dev_webspace_id"] = str(binding.get("dev_webspace_id") or _paired_dev_webspace_id(webspace_id) or "").strip() or None
        topic["conversation_id"] = _conversation_id(webspace_id)
        topic["channel_id"] = DIALOG_CHANNEL_ID
        topic["owner"] = f"skill:{SKILL_ID}"
        topic.setdefault("stored", bool(session_topic.get("stored")))
        return topic
    try:
        from adaos.services.conversation_links import ensure_builder_topic

        topic = ensure_builder_topic(
            webspace_id,
            active_draft_id=str(session.get("draft_id") or binding.get("active_draft_id") or "").strip() or None,
            scenario_id=str(session.get("scenario_id") or binding.get("runtime_scenario_id") or "").strip() or None,
            dev_webspace_id=str(binding.get("dev_webspace_id") or _paired_dev_webspace_id(webspace_id) or "").strip() or None,
        )
        if prompt_topic_id and isinstance(topic, Mapping):
            normalized = {k: v for k, v in dict(topic).items() if v is not None}
            normalized["thread_id"] = prompt_topic_id
            normalized["topic_id"] = prompt_topic_id
            normalized.setdefault("topic_kind", "builder_runtime")
            normalized.setdefault("webspace_id", webspace_id)
            normalized.setdefault("source_webspace_id", webspace_id)
            normalized.setdefault("conversation_id", _conversation_id(webspace_id))
            normalized.setdefault("channel_id", DIALOG_CHANNEL_ID)
            normalized.setdefault("owner", f"skill:{SKILL_ID}")
            return normalized
        return topic
    except Exception:
        token = str(session.get("draft_id") or session.get("scenario_id") or binding.get("runtime_scenario_id") or "default").strip()
        token = re.sub(r"[^A-Za-z0-9_.:-]+", ".", token).strip(".") or "default"
        scenario_token = str(session.get("scenario_id") or binding.get("runtime_scenario_id") or "").strip()
        if scenario_token:
            scenario_safe = re.sub(r"[^A-Za-z0-9_.:-]+", ".", scenario_token).strip(".") or token
            fallback_topic_id = f"prompt-project:scenario:{scenario_safe}"
            fallback_thread_id = fallback_topic_id
        else:
            fallback_topic_id = f"builder:{webspace_id}:{token}"
            fallback_thread_id = f"thread.builder.{webspace_id}.{token}"
        return {
            "schema": "adaos.conversation.topic_ref.v1",
            "topic_id": fallback_topic_id,
            "thread_id": fallback_thread_id,
            "topic_kind": "builder_runtime",
            "webspace_id": webspace_id,
            "source_webspace_id": webspace_id,
            "active_draft_id": str(session.get("draft_id") or binding.get("active_draft_id") or "").strip() or None,
            "scenario_id": str(session.get("scenario_id") or binding.get("runtime_scenario_id") or "").strip() or None,
            "dev_webspace_id": str(binding.get("dev_webspace_id") or _paired_dev_webspace_id(webspace_id) or "").strip() or None,
            "conversation_id": _conversation_id(webspace_id),
            "channel_id": DIALOG_CHANNEL_ID,
            "owner": f"skill:{SKILL_ID}",
            "stored": False,
        }


def _dialog_state(webspace_id: str, *, topic_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
    topic = dict(topic_ref or {}) if isinstance(topic_ref, Mapping) else {}
    state = {
        "state": "active",
        "dialog_channel_id": DIALOG_CHANNEL_ID,
        "conversation_id": _conversation_id(webspace_id),
        "owner": f"skill:{SKILL_ID}",
        "surface": f"skill:{SKILL_ID}",
        "default_tool": f"{SKILL_ID}.chat",
        "active_agent_id": AGENT_ID,
        "active_agent_label": AGENT_LABEL,
        "active_agent": {
            "id": AGENT_ID,
            "label": AGENT_LABEL,
            "owner": f"skill:{SKILL_ID}",
            "kind": "skill_agent",
            "skill_id": SKILL_ID,
            "channel_id": DIALOG_CHANNEL_ID,
            "memory_scope": "skill_user",
            "gender": "male",
            "voice": "ru-male",
            "icon": "construct-outline",
            "voice_profile": {
                "gender": "male",
                "voice": "ru-male",
                "lang": "ru-RU",
                "browser_voice_hint": "ru-male",
            },
        },
        "memory": {
            "status": "skill_memory_compat",
            "scopes": ["skill_user", "conversation"],
            "owner": f"skill:{SKILL_ID}",
            "active_agent_id": AGENT_ID,
        },
    }
    if topic:
        state["thread_id"] = str(topic.get("thread_id") or "").strip() or None
        state["topic_id"] = str(topic.get("topic_id") or "").strip() or None
        state["topic"] = {k: v for k, v in topic.items() if k != "stored"}
    return state


def _chat_meta(
    _meta: Mapping[str, Any] | None,
    *,
    webspace_id: str,
    session: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
    topic_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(_meta or {})
    meta.pop("webspace_ids", None)
    meta["webspace_id"] = webspace_id
    meta.setdefault("source_webspace_id", _source_webspace_id(webspace_id, _meta))
    meta.setdefault("route_id", "voice_chat")
    meta.setdefault("dialog_channel_id", DIALOG_CHANNEL_ID)
    meta["conversation_id"] = _conversation_id(webspace_id)
    meta["conversation_owner"] = f"skill:{SKILL_ID}"
    prompt_topic_id = _prompt_project_topic_id(session=session, binding=binding)
    if prompt_topic_id:
        # The client may still carry a topic from the previously selected Prompt IDE
        # project. The Builder runtime session is the source of truth for project
        # scoped chat history, so replace stale topic fields before resolving refs.
        meta["conversation_topic_id"] = prompt_topic_id
        meta["conversation_thread_id"] = prompt_topic_id
        meta["thread_id"] = prompt_topic_id
        meta["topic_id"] = prompt_topic_id
        meta.pop("builder_topic", None)
    meta.setdefault("active_agent_id", AGENT_ID)
    meta.setdefault("active_agent_label", AGENT_LABEL)
    meta.setdefault("active_agent_gender", "male")
    meta.setdefault("active_agent_voice", "ru-male")
    meta.setdefault("active_agent_icon", "construct-outline")
    topic = dict(topic_ref or {}) if isinstance(topic_ref, Mapping) else _builder_topic_ref(
        webspace_id,
        session=session,
        binding=binding,
        _meta=meta,
    )
    if prompt_topic_id:
        scenario_id = str((session or {}).get("scenario_id") or (binding or {}).get("runtime_scenario_id") or "").strip()
        active_draft_id = str((session or {}).get("draft_id") or (binding or {}).get("active_draft_id") or "").strip()
        source_ws = str(meta.get("source_webspace_id") or _source_webspace_id(webspace_id, _meta)).strip() or webspace_id
        dev_ws = str((binding or {}).get("dev_webspace_id") or _paired_dev_webspace_id(source_ws) or "").strip()
        topic = {k: v for k, v in dict(topic or {}).items() if v is not None}
        topic["schema"] = "adaos.conversation.topic_ref.v1"
        topic["thread_id"] = prompt_topic_id
        topic["topic_id"] = prompt_topic_id
        topic["topic_kind"] = "builder_scenario"
        topic["webspace_id"] = webspace_id
        topic["source_webspace_id"] = source_ws
        topic["active_draft_id"] = active_draft_id or None
        topic["scenario_id"] = scenario_id or None
        topic["dev_webspace_id"] = dev_ws or None
        topic["project_id"] = scenario_id or active_draft_id or None
        topic["conversation_id"] = _conversation_id(webspace_id)
        topic["channel_id"] = DIALOG_CHANNEL_ID
        topic["owner"] = f"skill:{SKILL_ID}"
    thread_id = str(topic.get("thread_id") or "").strip()
    topic_id = str(topic.get("topic_id") or "").strip()
    if thread_id:
        meta["thread_id"] = thread_id
        meta["conversation_thread_id"] = thread_id
        meta["conversation_topic_id"] = thread_id
    if topic_id:
        meta["topic_id"] = topic_id
    if topic:
        meta.setdefault("builder_topic", {k: v for k, v in topic.items() if k != "stored"})
    return meta


def _is_api_tool_call(_meta: Mapping[str, Any] | None) -> bool:
    meta = dict(_meta or {}) if isinstance(_meta, Mapping) else {}
    return str(meta.get("action_source") or "").strip() == "api_tool_call"


def _api_request_chat_meta(_meta: Mapping[str, Any] | None) -> dict[str, Any]:
    meta = dict(_meta or {}) if isinstance(_meta, Mapping) else {}
    meta.setdefault("action_source", "api_tool_call")
    meta.setdefault("origin_label", "API")
    meta["active_agent_id"] = "api"
    meta["active_agent_label"] = "API"
    meta["recipient_label"] = AGENT_LABEL
    return meta


def _source_refs(
    *,
    webspace_id: str,
    session: Mapping[str, Any],
    _meta: Mapping[str, Any] | None = None,
    patch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    meta = _chat_meta(_meta, webspace_id=webspace_id, session=session)
    refs: dict[str, Any] = {
        "conversation_id": meta.get("conversation_id") or _conversation_id(webspace_id),
        "dialog_channel_id": DIALOG_CHANNEL_ID,
        "owner": f"skill:{SKILL_ID}",
        "session_id": session.get("id"),
        "scenario_id": session.get("scenario_id"),
    }
    for key in ("thread_id", "topic_id", "turn_trace_id", "request_id", "message_id", "input_event_kind"):
        value = str(meta.get(key) or "").strip()
        if value:
            refs[key] = value
    draft_id = str(session.get("draft_id") or "").strip()
    if draft_id:
        refs["draft_id"] = draft_id
    if patch:
        patch_id = str(patch.get("id") or "").strip()
        if patch_id:
            refs["patch_id"] = patch_id
        operation = str(patch.get("operation") or "").strip()
        if operation:
            refs["operation"] = operation
    return refs


def _publish_review_pending_action(
    *,
    webspace_id: str,
    session: Mapping[str, Any],
    request_text: str,
    kind: str,
    summary: str,
    _meta: Mapping[str, Any] | None = None,
    patch: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    refs = _source_refs(webspace_id=webspace_id, session=session, _meta=_meta, patch=patch)
    request_text = _display_request_text(request_text, patch)
    summary = _repair_mojibake_text(summary)
    action_input: dict[str, Any] = {
        "kind": kind,
        "request_text": request_text,
        "side_effect_class": "local_write",
    }
    if patch:
        action_input.update({key: value for key, value in dict(patch).items() if key in {"target", "operation", "summary", "side_effect_class"}})
    try:
        from adaos.services.conversation_safety import classify_action_risk

        action_risk = classify_action_risk(action_input)
    except Exception:
        action_risk = {
            "schema": "adaos.conversation.action_risk.v1",
            "risk_class": "local_write",
            "approval_required": False,
            "mandatory_review": False,
            "reasons": [{"risk_class": "local_write", "reason": "fallback"}],
        }
    try:
        from adaos.services.pending_actions import publish_pending_action
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

        def _publish() -> dict[str, Any]:
            return publish_pending_action(
                webspace_id=webspace_id,
                kind=kind,
                title="Review Builder change",
                summary=summary,
                request_text=request_text,
                producer={"type": "skill", "skill_id": SKILL_ID},
                owner_scope={
                    "owner": f"skill:{SKILL_ID}",
                    "webspace_id": webspace_id,
                    "conversation_id": refs.get("conversation_id"),
                    "thread_id": refs.get("thread_id"),
                },
                domain_ref={
                    "skill_id": SKILL_ID,
                    "session_id": refs.get("session_id"),
                    "scenario_id": refs.get("scenario_id"),
                    "draft_id": refs.get("draft_id"),
                    "patch_id": refs.get("patch_id"),
                    "operation": refs.get("operation"),
                    "conversation_id": refs.get("conversation_id"),
                    "thread_id": refs.get("thread_id"),
                },
                actions=["preview", "approve", "refuse", "postpone"],
                response_topic="builder.pending_action.response",
                payload_ref={
                    "kind": "builder.session",
                    "session_id": refs.get("session_id"),
                    "scenario_id": refs.get("scenario_id"),
                },
                metadata={
                    "source": "builder_skill",
                    "source_refs": refs,
                    "patch": dict(patch or {}),
                    "approval_policy": {
                        "decision": "human_review_required",
                        "reason": "builder_review_pending_action",
                        "action_risk": action_risk,
                    },
                },
            )

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(_publish)
            try:
                return future.result(timeout=PENDING_ACTION_TIMEOUT_S)
            except FuturesTimeoutError:
                future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                pool = None
                return {
                    "ok": False,
                    "error": "pending_action_publish_timeout",
                    "timeout_s": PENDING_ACTION_TIMEOUT_S,
                    "metadata": {"source_refs": refs},
                }
        finally:
            if pool is not None:
                pool.shutdown(wait=True)
    except Exception as exc:
        return {
            "ok": False,
            "error": "pending_action_publish_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "metadata": {"source_refs": refs},
        }


def _safe_emit_chat(
    text: str,
    *,
    webspace_id: str,
    _meta: Mapping[str, Any] | None = None,
    session: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
    topic_ref: Mapping[str, Any] | None = None,
    actions: Sequence[Mapping[str, Any]] | None = None,
    from_: str = "hub",
) -> None:
    try:
        from adaos.sdk.io.out import chat_append
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

        source_ws = _source_webspace_id(webspace_id, _meta)
        targets = [source_ws]
        dev_ws = _paired_dev_webspace_id(source_ws)
        if dev_ws and dev_ws not in targets:
            targets.append(dev_ws)
        for target in targets:
            meta = _chat_meta(_meta, webspace_id=target, session=session, binding=binding, topic_ref=topic_ref)
            if from_ == "hub" and _is_api_tool_call(_meta):
                meta.setdefault("recipient_label", "API")

            def _append_chat() -> Mapping[str, bool]:
                try:
                    return chat_append(text, from_=from_, actions=actions, _meta=meta)
                except TypeError:
                    return chat_append(text, from_=from_, _meta=meta)

            pool = ThreadPoolExecutor(max_workers=1)
            try:
                future = pool.submit(_append_chat)
                try:
                    future.result(timeout=CHAT_APPEND_TIMEOUT_S)
                except FuturesTimeoutError:
                    future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    pool = None
            finally:
                if pool is not None:
                    pool.shutdown(wait=True)
    except Exception:
        return


def _schedule_safe_emit_chat(
    text: str,
    *,
    webspace_id: str,
    _meta: Mapping[str, Any] | None = None,
    session: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
    topic_ref: Mapping[str, Any] | None = None,
    actions: Sequence[Mapping[str, Any]] | None = None,
    delay_s: float | None = None,
) -> dict[str, Any]:
    effective_delay = _builder_revision_chat_emit_delay_s() if delay_s is None else max(0.0, min(float(delay_s), 10.0))
    def _runner() -> None:
        _safe_emit_chat(
            text,
            webspace_id=webspace_id,
            _meta=_meta,
            session=session,
            binding=binding,
            topic_ref=topic_ref,
            actions=actions,
        )

    if effective_delay <= 0:
        thread = threading.Thread(target=_runner, name=f"builder-chat-emit:{webspace_id}", daemon=True)
        thread.start()
        return {"scheduled": True, "mode": "thread", "delay_s": 0.0}

    timer = threading.Timer(effective_delay, _runner)
    timer.name = f"builder-chat-emit:{webspace_id}"
    timer.daemon = True
    timer.start()
    return {"scheduled": True, "mode": "timer_thread", "delay_s": effective_delay}


def _event_payload(evt: Any) -> dict[str, Any]:
    payload = getattr(evt, "payload", None)
    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(evt, Mapping):
        return dict(evt)
    return {}


@subscribe("builder.pending_action.response")
async def _on_builder_pending_action_response(evt: Any) -> None:
    payload = _event_payload(evt)
    action = payload.get("pending_action") if isinstance(payload.get("pending_action"), Mapping) else {}
    response = payload.get("response") if isinstance(payload.get("response"), Mapping) else {}
    domain_ref = payload.get("domain_ref") if isinstance(payload.get("domain_ref"), Mapping) else action.get("domain_ref") if isinstance(action, Mapping) else {}
    response_action_id = str(payload.get("response_action_id") or response.get("response_action_id") or "").strip()
    webspace_id = _source_webspace_id(str(payload.get("webspace_id") or action.get("webspace_id") or ""), None)
    session_id = str(domain_ref.get("session_id") or "").strip()
    patch_id = str(domain_ref.get("patch_id") or "").strip()
    pending_action_id = str(payload.get("pending_action_id") or action.get("id") or "").strip()
    operation = str(domain_ref.get("operation") or "").strip()
    if not webspace_id or response_action_id not in {"approve", "refuse"}:
        return
    session = _load_session(webspace_id, session_id or None)
    if not session:
        return
    if operation == "delete_draft":
        draft_id = str(domain_ref.get("draft_id") or session.get("draft_id") or "").strip()
        binding = _workbench_binding(webspace_id)
        topic = _builder_topic_ref(webspace_id, session=session, binding=binding)
        if response_action_id == "approve" and draft_id:
            result = delete_development_skill(draft_id=draft_id, webspace_id=webspace_id)
            if result.get("ok"):
                message = f"{AGENT_LABEL}: \u0443\u0434\u0430\u043b\u0438\u043b \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a {draft_id}."
            else:
                message = f"{AGENT_LABEL}: \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0443\u0434\u0430\u043b\u0438\u0442\u044c {draft_id}: {result.get('error') or 'unknown_error'}."
        else:
            message = f"{AGENT_LABEL}: \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u0435 {draft_id or session.get('scenario_id')} \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u043e."
        _safe_emit_chat(message, webspace_id=webspace_id, session=session, binding=binding, topic_ref=topic)
        return
    patches = [dict(item) for item in session.get("patches", []) if isinstance(item, Mapping)]
    matched = False
    matched_patch: dict[str, Any] | None = None
    for patch in patches:
        if patch_id and str(patch.get("id") or "") == patch_id:
            matched = True
        elif pending_action_id and str(patch.get("pending_action_id") or "") == pending_action_id:
            matched = True
        else:
            continue
        patch["review_status"] = "approved" if response_action_id == "approve" else "refused"
        patch["reviewed_at"] = _now()
        patch["review_response_id"] = pending_action_id or None
        if response_action_id == "approve":
            patch["status"] = "applied"
        matched_patch = patch
        break
    if not matched:
        return
    session["patches"] = patches
    if pending_action_id and str(session.get("pending_action_id") or "") == pending_action_id:
        session.pop("pending_action_id", None)
    session["user_summary"] = _draft_user_summary(session)
    if (
        matched_patch
        and matched_patch.get("operation") == "llm_webui_transform"
        and isinstance(session.get("preview_state"), Mapping)
    ):
        preview = copy.deepcopy(dict(session["preview_state"]))
    else:
        preview = _preview_state(session=session)
    preview = _repair_text_tree(dict(preview))
    if (
        matched_patch
        and matched_patch.get("operation") == "llm_webui_transform"
        and isinstance(session.get("webui_payload"), Mapping)
    ):
        _write_webui_payload(str(session.get("artifact_root") or ""), session["webui_payload"])
    else:
        _write_webui(str(session.get("artifact_root") or ""), preview)
    session["preview_state"] = preview
    _save_session(webspace_id, session)
    workbench = _ensure_workbench(webspace_id, session=session, preview_state=preview)
    binding = workbench.get("binding") if isinstance(workbench.get("binding"), Mapping) else {}
    topic = _builder_topic_ref(webspace_id, session=session, binding=binding)
    if response_action_id == "approve":
        message = f"{AGENT_LABEL}: \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f {session.get('scenario_id')} \u0443\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u044b."
    else:
        message = (
            f"{AGENT_LABEL}: \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0438\u0435 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439 {session.get('scenario_id')} "
            "\u0437\u0430\u0444\u0438\u043a\u0441\u0438\u0440\u043e\u0432\u0430\u043d\u043e. Rollback \u0434\u043b\u044f \u044d\u0442\u043e\u0439 \u0432\u0435\u0442\u043a\u0438 \u0435\u0449\u0435 \u043d\u0435 \u0440\u0435\u0430\u043b\u0438\u0437\u043e\u0432\u0430\u043d."
        )
    _safe_emit_chat(message, webspace_id=webspace_id, session=session, binding=binding, topic_ref=topic)


def _build_fields(idea: str) -> list[dict[str, Any]]:
    lowered = str(idea or "").lower()
    if "shopping" in lowered or "\u043f\u043e\u043a\u0443\u043f" in lowered:
        return [
            {"id": "item", "type": "string", "label": "\u0422\u043e\u0432\u0430\u0440", "required": True},
            {"id": "quantity", "type": "number", "label": "\u041a\u043e\u043b-\u0432\u043e", "required": False},
            {"id": "category", "type": "string", "label": "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f", "required": False},
            {"id": "done", "type": "boolean", "label": "\u041a\u0443\u043f\u043b\u0435\u043d\u043e", "required": False},
        ]
    return [
        {"id": "title", "type": "string", "label": "\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435", "required": True},
        {"id": "notes", "type": "string", "label": "\u0417\u0430\u043c\u0435\u0442\u043a\u0438", "required": False},
        {"id": "status", "type": "string", "label": "\u0421\u0442\u0430\u0442\u0443\u0441", "required": False},
    ]


def _component_for_field(field: Mapping[str, Any]) -> dict[str, Any]:
    field_type = str(field.get("type") or "string")
    component_type = (
        "checkbox"
        if field_type == "boolean"
        else "number_input"
        if field_type == "number"
        else "date_input"
        if field_type == "date"
        else "text_input"
    )
    return {
        "id": f"input_{field['id']}",
        "type": component_type,
        "label": field.get("label") or field["id"],
        "binding": f"draft.{field['id']}",
        "visible": True,
    }


def _ui_texts(session: Mapping[str, Any]) -> dict[str, str]:
    if str(session.get("ui_locale") or "").strip().lower().startswith("en"):
        return {
            "default_title": "Prototype",
            "input": "Input",
            "add": "Add",
            "list": "List",
            "cards": "Cards",
        }
    return {
        "default_title": "\u041f\u0440\u043e\u0442\u043e\u0442\u0438\u043f",
        "input": "\u0412\u0432\u043e\u0434",
        "add": "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c",
        "list": "\u0421\u043f\u0438\u0441\u043e\u043a",
        "cards": "\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0438",
    }


def _field_ids(fields: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(item.get("id") or "").strip() for item in fields if str(item.get("id") or "").strip()]


def _preferred_card_preview_key(fields: Sequence[Mapping[str, Any]], *, prefer_text: bool = False) -> str:
    ids = _field_ids(fields)
    if not ids:
        return "preview"
    if prefer_text:
        for candidate in ("notes", "description", "details", "text", "comment", "summary"):
            if candidate in ids:
                return candidate
    for candidate in ids[2:] + ids[1:2]:
        if candidate:
            return candidate
    return ids[0]


def _card_key_from_template(value: Any) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"\{\{\s*([A-Za-z_][\w.-]*)\s*\}\}", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z_][\w.-]*", text):
        return text
    return ""


_CARD_TEMPLATE_FIELD_RE = re.compile(r"\{\{\s*([A-Za-z_][\w.-]*)\s*\}\}")


def _card_template_fields(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [match.group(1) for match in _CARD_TEMPLATE_FIELD_RE.finditer(text)]


def _row_path_value(row: Mapping[str, Any], path: str) -> Any:
    current: Any = row
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            return ""
    return current


def _render_card_template(value: Any, row: Mapping[str, Any]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    def _replace(match: re.Match[str]) -> str:
        raw = _row_path_value(row, match.group(1))
        if raw is None:
            return ""
        if isinstance(raw, (dict, list)):
            return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        return str(raw)

    return _CARD_TEMPLATE_FIELD_RE.sub(_replace, text).strip()


def _card_preview_derived_key(fields: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> str:
    used = set(_field_ids(fields))
    for row in rows:
        if isinstance(row, Mapping):
            used.update(str(key) for key in row.keys())
    base = "card_preview"
    if base not in used:
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    return f"{base}_{index}"


def _derive_card_preview_rows(
    template: Any,
    *,
    fields: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]]] | None:
    text = str(template or "").strip()
    if not text or _card_key_from_template(text):
        return None
    if not _card_template_fields(text):
        return None
    key = _card_preview_derived_key(fields, rows)
    derived_rows: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row) if isinstance(row, Mapping) else {}
        item[key] = _render_card_template(text, item)
        derived_rows.append(item)
    return key, derived_rows


def _preview_state(*, session: Mapping[str, Any]) -> dict[str, Any]:
    fields = [dict(item) for item in session.get("fields", []) if isinstance(item, Mapping)]
    filters = [dict(item) for item in session.get("filters", []) if isinstance(item, Mapping)]
    datasource_id = str(session.get("datasource_id") or "items")
    table_columns = [{"field": item["id"], "label": item.get("label") or item["id"]} for item in fields]
    stored_mock_rows = session.get("mock_rows")
    mock_rows = [dict(item) for item in stored_mock_rows if isinstance(item, Mapping)] if isinstance(stored_mock_rows, list) else _mock_rows(fields)
    action_position = str(session.get("form_action_position") or "").strip().lower()
    text = _ui_texts(session)
    layout_order = str(session.get("layout_order") or "").strip().lower()
    card_preview_key = str(session.get("card_preview_key") or "").strip() or _preferred_card_preview_key(fields)
    ui = {
        "schema": "adaos.declarative_ui.v1",
        "id": str(session.get("scenario_id") or "prototype"),
        "type": "page",
        "title": session.get("title") or text["default_title"],
        "children": [
            {
                "id": "editor",
                "type": "section",
                "label": text["input"],
                "children": [_component_for_field(item) for item in fields],
                "action_position": "top" if action_position == "top" else "bottom",
                "actions": [{"id": "add_item", "type": "button", "label": text["add"]}],
            },
            {
                "id": "items_table",
                "type": "table",
                "label": text["list"],
                "binding": datasource_id,
                "columns": table_columns,
                "visible": not bool(session.get("hide_table")),
            },
        ],
    }
    if session.get("card_view"):
        ui["children"].append(
            {
                "id": "items_cards",
                "type": "card_list",
                "label": text["cards"],
                "binding": datasource_id,
                "title": f"{{{{{fields[0]['id']}}}}}" if fields else "{{title}}",
                "subtitle": f"{{{{{fields[1]['id']}}}}}" if len(fields) > 1 else "",
                "preview": f"{{{{{card_preview_key}}}}}" if card_preview_key else "",
                "visible": True,
            }
        )
    result = {
        "session_id": session.get("id"),
        "title": session.get("title"),
        "current_ui": ui,
        "datasources": [
            {
                "id": datasource_id,
                "type": "internal_crud",
                "entity": "item",
                "fields": fields,
                "operations": ["create", "read", "update", "delete"],
            }
        ],
        "mock_data": {datasource_id: mock_rows},
        "filters": filters,
        "form_action_position": "top" if action_position == "top" else "bottom",
        "layout_order": layout_order or "input_first",
        "card_preview_key": card_preview_key,
        "pending_patches": [item for item in session.get("patches", []) if item.get("status") == "proposed"],
        "user_summary": session.get("user_summary") if isinstance(session.get("user_summary"), Mapping) else _draft_user_summary(session),
        "version": str(session.get("version") or "v1"),
    }
    return _repair_text_tree(result)


def _mock_rows(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for index in range(1, 4):
        row: dict[str, Any] = {}
        for field in fields:
            field_id = str(field.get("id") or "")
            field_type = str(field.get("type") or "string")
            if field_type == "number":
                row[field_id] = index
            elif _is_boolean_field_type(field_type):
                row[field_id] = index == 1
            elif field_type == "date":
                row[field_id] = f"2026-07-0{index}"
            else:
                row[field_id] = f"{field.get('label') or field_id} {index}"
        rows.append(row)
    return rows


def _default_builder_memory_text(preview_state: Mapping[str, Any]) -> str:
    title = str(preview_state.get("title") or "Builder prototype").strip() or "Builder prototype"
    summary = preview_state.get("user_summary") if isinstance(preview_state.get("user_summary"), Mapping) else {}
    lines = [
        f"# {title}",
        "",
        "## Builder memory",
        "- Keep durable product decisions, domain vocabulary, UX preferences, and constraints here.",
        "- Treat the initial scaffold as a starting point only; the current webui.json is the UI source of truth.",
        "- Future Builder turns may replace fields, widgets, mock data, layout, and copy when the user asks.",
    ]
    for heading, key in (
        ("Assumptions", "assumptions"),
        ("Expected behavior", "expected_behavior"),
        ("Preview notes", "preview"),
        ("Risks", "risks"),
    ):
        values = summary.get(key) if isinstance(summary, Mapping) else None
        if not isinstance(values, list) or not values:
            continue
        lines.extend(["", f"## {heading}"])
        lines.extend(f"- {str(item)}" for item in values[:12])
    lines.append("")
    return "\n".join(lines)


def _neutralize_legacy_builder_memory_text(text: str) -> str:
    source = _repair_mojibake_text(text)
    if "The first data model uses fields:" not in source:
        return source
    return re.sub(
        r"The first data model uses fields:\s*([^\n\r]+)",
        r"The initial scaffold started with fields: \1; this list is not a fixed product contract",
        source,
    )


def _neutralize_legacy_user_summary(summary: Mapping[str, Any] | None) -> dict[str, Any]:
    data = copy.deepcopy(dict(summary or {})) if isinstance(summary, Mapping) else {}
    assumptions = data.get("assumptions")
    if isinstance(assumptions, list):
        data["assumptions"] = [
            _neutralize_legacy_builder_memory_text(item) if isinstance(item, str) else item
            for item in assumptions
        ]
    return data


def _normalize_builder_project_memory_file(path: Path) -> None:
    if not path.exists():
        return
    source = _read_text_file(path)
    normalized = _neutralize_legacy_builder_memory_text(source)
    if normalized != source:
        _write_text_file(path, normalized)


def _legacy_default_builder_system_prompt_text() -> str:
    return (
        "# Builder project system prompt\n\n"
        "Add project-specific instructions for AdaOS Builder here.\n"
        "Prefer durable rules that should affect every future UI transform for this prototype.\n"
        "Leave this file empty when no project-specific behavior is needed.\n"
    )


def _default_builder_system_prompt_text() -> str:
    return ""


def _ensure_builder_project_files(root: Path, preview_state: Mapping[str, Any]) -> None:
    memory_path = root / BUILDER_MEMORY_FILE
    if not memory_path.exists():
        _write_text_file(memory_path, _default_builder_memory_text(preview_state))
    else:
        _normalize_builder_project_memory_file(memory_path)
    system_prompt_path = root / BUILDER_SYSTEM_PROMPT_FILE
    if not system_prompt_path.exists():
        _write_text_file(system_prompt_path, _default_builder_system_prompt_text())
    elif _read_text_file(system_prompt_path).strip() == _legacy_default_builder_system_prompt_text().strip():
        _write_text_file(system_prompt_path, _default_builder_system_prompt_text())
    tz_path = root / PROMPT_TZ_BASE_FILE
    if not tz_path.exists():
        _write_text_file(tz_path, _read_text_file(memory_path))
    else:
        _normalize_builder_project_memory_file(tz_path)
    state_path = root / "prompt_state.json"
    if state_path.exists():
        state = _load_json_file(state_path)
        if state:
            current_base_tz = str(state.get("base_tz") or "")
            normalized_base_tz = (
                _neutralize_legacy_builder_memory_text(current_base_tz)
                if current_base_tz.strip()
                else _read_text_file(tz_path)
            )
            if normalized_base_tz != current_base_tz:
                state["base_tz"] = normalized_base_tz
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if state and not str(state.get("base_tz") or "").strip():
            state["base_tz"] = _read_text_file(tz_path)
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _snapshot_prompt_files(artifact_root: str | None) -> dict[str, dict[str, Any]]:
    root = _project_artifact_root({"artifact_root": artifact_root or ""})
    if root is None:
        return {}
    snapshots: dict[str, dict[str, Any]] = {}
    for rel_path in PROMPT_REVISION_FILES:
        path = root / rel_path
        rel = rel_path.as_posix()
        if not path.exists() or not path.is_file():
            snapshots[rel] = {"exists": False, "content": ""}
            continue
        snapshots[rel] = {
            "exists": True,
            "content": _read_text_file(path),
        }
    return snapshots


def _sync_prompt_state_from_files(root: Path) -> None:
    state_path = root / "prompt_state.json"
    state = _load_json_file(state_path) if state_path.exists() else {}
    if not state:
        return
    base_tz = root / PROMPT_TZ_BASE_FILE
    if base_tz.exists():
        state["base_tz"] = _read_text_file(base_tz)
    prepare_prompt = root / "prepare" / "general_prompt.md"
    if prepare_prompt.exists():
        prepare = state.setdefault("prepare", {})
        if not isinstance(prepare, dict):
            prepare = {}
            state["prepare"] = prepare
        prepare["general_prompt"] = _read_text_file(prepare_prompt)
    generate_prompt = root / "generate" / "general_prompt.md"
    if generate_prompt.exists():
        generate = state.setdefault("generate", {})
        if not isinstance(generate, dict):
            generate = {}
            state["generate"] = generate
        generate["general_prompt"] = _read_text_file(generate_prompt)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _restore_prompt_files_from_revision(session: Mapping[str, Any], revision_payload: Mapping[str, Any]) -> dict[str, Any]:
    root = _project_artifact_root(session)
    if root is None:
        return {"ok": False, "error": "artifact_root_missing"}
    prompt_files = revision_payload.get("prompt_files")
    if not isinstance(prompt_files, Mapping):
        return {"ok": True, "restored": [], "skipped": "prompt_files_missing"}
    allowed = {path.as_posix(): path for path in PROMPT_REVISION_FILES}
    restored: list[str] = []
    for rel, payload in prompt_files.items():
        rel_token = str(rel or "").strip().replace("\\", "/")
        rel_path = allowed.get(rel_token)
        if rel_path is None or not isinstance(payload, Mapping):
            continue
        target = (root / rel_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        exists = payload.get("exists") is not False
        if not exists:
            if target.exists():
                try:
                    target.unlink()
                    restored.append(rel_token)
                except Exception:
                    pass
            continue
        _write_text_file(target, str(payload.get("content") or ""))
        restored.append(rel_token)
    if restored:
        _sync_prompt_state_from_files(root)
    return {"ok": True, "restored": restored}


def _current_scenario_manifest(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {}
    scenario_json = root / "scenario.json"
    if scenario_json.exists():
        data = _load_json_file(scenario_json)
        if data:
            data["__path"] = str(scenario_json)
            return _repair_text_tree(data)
    scenario_yaml = root / "scenario.yaml"
    if scenario_yaml.exists():
        try:
            import yaml

            data = yaml.safe_load(scenario_yaml.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        if isinstance(data, dict):
            data["__path"] = str(scenario_yaml)
            return _repair_text_tree(data)
    return {}


def _current_scenario_yaml_manifest(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {}
    for name in ("scenario.yaml", "scenario.yml"):
        path = root / name
        if not path.exists():
            continue
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        if isinstance(data, dict):
            data["__path"] = str(path)
            return _repair_text_tree(data)
    return {}


def _clean_i18n_spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    data = _repair_text_tree(copy.deepcopy(dict(value)))
    return {str(key): item for key, item in data.items() if item is not None}


def _scenario_title_i18n(
    *,
    scenario_id: str,
    title: str,
    scenario: Mapping[str, Any] | None = None,
    page_schema: Mapping[str, Any] | None = None,
    preview_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    for source in (preview_state, scenario, page_schema):
        spec = _clean_i18n_spec(source.get("title_i18n") if isinstance(source, Mapping) else None)
        if spec:
            return spec
    key = f"scenario.{scenario_id}.title" if scenario_id else "scenario.prototype.title"
    return {"key": key, "fallback": title}


def _canonical_scenario_title(
    root: Path | None,
    *,
    scenario: Mapping[str, Any] | None = None,
    preview_state: Mapping[str, Any] | None = None,
    page_schema: Mapping[str, Any] | None = None,
    prefer_preview: bool = False,
) -> tuple[str, dict[str, Any]]:
    yaml_manifest = _current_scenario_yaml_manifest(root)
    sources = [preview_state, yaml_manifest, scenario, page_schema] if prefer_preview else [yaml_manifest, scenario, preview_state, page_schema]
    scenario_id = str(
        (yaml_manifest.get("id") if isinstance(yaml_manifest, Mapping) else "")
        or (scenario.get("id") if isinstance(scenario, Mapping) else "")
        or (preview_state.get("scenario_id") if isinstance(preview_state, Mapping) else "")
        or (page_schema.get("id") if isinstance(page_schema, Mapping) else "")
        or (root.name if root is not None else "")
        or "prototype"
    ).strip()
    title = ""
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        value = str(source.get("title") or source.get("name") or "").strip()
        if value:
            title = value
            break
    if not title:
        title = scenario_id.replace("_", " ").title() if scenario_id else "Prototype"
    title_i18n = _scenario_title_i18n(
        scenario_id=scenario_id,
        title=title,
        scenario=yaml_manifest or scenario,
        page_schema=page_schema,
        preview_state=preview_state,
    )
    return title, title_i18n


def _apply_scenario_title_to_page_schema(
    page_schema: Mapping[str, Any],
    *,
    title: str,
    title_i18n: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    data = _repair_text_tree(copy.deepcopy(dict(page_schema)))
    if title:
        data["title"] = title
    spec = _clean_i18n_spec(title_i18n)
    if spec:
        data["title_i18n"] = spec
    return data


def _current_runtime_page_schema(root: Path | None) -> dict[str, Any]:
    scenario = _current_scenario_manifest(root)
    ui = scenario.get("ui") if isinstance(scenario.get("ui"), Mapping) else {}
    app = ui.get("application") if isinstance(ui.get("application"), Mapping) else {}
    desktop = app.get("desktop") if isinstance(app.get("desktop"), Mapping) else {}
    page_schema = desktop.get("pageSchema") if isinstance(desktop.get("pageSchema"), Mapping) else {}
    if not page_schema:
        return {}
    page_schema = _repair_text_tree(copy.deepcopy(dict(page_schema)))
    title, title_i18n = _canonical_scenario_title(root, scenario=scenario, page_schema=page_schema)
    return _apply_scenario_title_to_page_schema(page_schema, title=title, title_i18n=title_i18n)


def _extract_webui_page_schema(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    ui = payload.get("ui") if isinstance(payload.get("ui"), Mapping) else {}
    app = ui.get("application") if isinstance(ui.get("application"), Mapping) else {}
    desktop = app.get("desktop") if isinstance(app.get("desktop"), Mapping) else {}
    page_schema = desktop.get("pageSchema") if isinstance(desktop.get("pageSchema"), Mapping) else {}
    if page_schema:
        return _repair_text_tree(copy.deepcopy(dict(page_schema)))
    preview = payload.get("preview_state") if isinstance(payload.get("preview_state"), Mapping) else {}
    page_schema = preview.get("page_schema") if isinstance(preview.get("page_schema"), Mapping) else {}
    if page_schema:
        return _repair_text_tree(copy.deepcopy(dict(page_schema)))
    page_schema = payload.get("page_schema") if isinstance(payload.get("page_schema"), Mapping) else {}
    return _repair_text_tree(copy.deepcopy(dict(page_schema))) if page_schema else {}


def _extract_webui_application(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    ui = payload.get("ui") if isinstance(payload.get("ui"), Mapping) else {}
    app = ui.get("application") if isinstance(ui.get("application"), Mapping) else {}
    return _repair_text_tree(copy.deepcopy(dict(app))) if app else {}


def _set_webui_page_schema(payload: dict[str, Any], page_schema: Mapping[str, Any]) -> dict[str, Any]:
    payload["schema"] = "adaos.webui.v1"
    ui = payload.get("ui") if isinstance(payload.get("ui"), dict) else {}
    app = ui.get("application") if isinstance(ui.get("application"), dict) else {}
    desktop = app.get("desktop") if isinstance(app.get("desktop"), dict) else {}
    desktop["pageSchema"] = _repair_text_tree(copy.deepcopy(dict(page_schema)))
    app["desktop"] = desktop
    ui["application"] = app
    payload["ui"] = ui
    return payload


def _canonical_webui_payload(payload: Mapping[str, Any] | None, page_schema: Mapping[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(dict(payload or {}))
    for key in ("preview_state", "current_ui", "page_schema", "runtime_context"):
        data.pop(key, None)
    data.setdefault("generated_by", SKILL_ID)
    return _set_webui_page_schema(data, page_schema)


def _with_builder_page_schema_meta(page_schema: Mapping[str, Any], preview_state: Mapping[str, Any]) -> dict[str, Any]:
    data = _repair_text_tree(copy.deepcopy(dict(page_schema)))
    version = str(preview_state.get("version") or "").strip()
    if not version:
        return data
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    builder = meta.get("builder") if isinstance(meta.get("builder"), dict) else {}
    builder["proto"] = version
    builder["ui_revision"] = version
    meta["builder"] = builder
    data["meta"] = meta
    return data


def _schema_def(schema: Mapping[str, Any] | None, name: str) -> Mapping[str, Any] | None:
    defs = schema.get("$defs") if isinstance(schema, Mapping) and isinstance(schema.get("$defs"), Mapping) else {}
    value = defs.get(name) if isinstance(defs, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _webui_form_field_types(schema: Mapping[str, Any] | None = None) -> list[str]:
    schema = schema if isinstance(schema, Mapping) else _load_webui_schema()
    field_type_def = _schema_def(schema, "formInputType") or _schema_def(schema, "formFieldType")
    values = field_type_def.get("enum") if isinstance(field_type_def, Mapping) else None
    result = [str(item) for item in values if str(item or "").strip()] if isinstance(values, list) else []
    if result:
        return result
    return [
        "text",
        "textarea",
        "number",
        "integer",
        "email",
        "url",
        "phone",
        "date",
        "time",
        "dateTime",
        "dateRange",
        "timeRange",
        "toggle",
        "select",
        "combobox",
        "chips",
        "radio",
        "multiChoice",
        "linearScale",
        "rating",
        "fileUpload",
        "radioGrid",
        "checkboxGrid",
        "ratingGrid",
    ]


def _builder_runtime_component_contracts() -> dict[str, Any]:
    field_types = _webui_form_field_types()
    return {
        "ui.form": {
            "purpose": "Editable input area for a draft record.",
            "inputs": {
                "fields": {
                    "shape": "Array of field descriptors: id, type, label/title/question, optional description/helpText/placeholder/default/validation/options/rows/columns.",
                    "supported_field_types": field_types,
                    "selection_guidance": [
                        "Choose the most semantically precise supported type; use text only when no more specific type fits.",
                        "Refactor existing generic text fields into more precise types whenever the current label or user instruction implies one.",
                        "Prefer atomic inputs over broad composite fields; in forms do not leave contacts, personal data, address, schedule, or preferences as one generic field when concrete subfields are implied.",
                        "Use email/url/phone/password/pin for typed contact or secret inputs.",
                        "Use textarea/paragraph/longText for multi-line descriptions.",
                        "Use date/time/dateTime/dateRange/timeRange for temporal input.",
                        "Use toggle/boolean/switch for yes/no agreement or completion.",
                        "Use select/combobox/radio/singleChoice for one value from options.",
                        "Use multiChoice/checkboxes/chips/tags for several values from options; for plural favorites or preferred items, offer options plus an 'other' text field when appropriate.",
                        "Use linearScale/rating for numeric sentiment, importance, readiness, or priority.",
                        "Use fileUpload/file for attachments.",
                        "Use radioGrid/checkboxGrid/ratingGrid when the user asks for a matrix/table of choices across rows and columns.",
                        "For questionnaires and application forms, every requested user answer must be represented as a ui.form field; display widgets may supplement but must not replace input fields.",
                    ],
                    "semantic_examples": {
                        "email address": "email",
                        "phone number": "phone",
                        "contacts": "email plus phone or messenger fields, not one generic text field",
                        "personal data": "full name plus relevant atomic identity/contact fields",
                        "convenient dates or date interval": "dateRange",
                        "convenient time or time window": "timeRange",
                        "several interests/tags/topics": "multiChoice or chips",
                        "several favorite places/items/options": "multiChoice or chips plus optional other text",
                        "abstract, notes, comment, description": "textarea",
                        "expected duration": "number, select, or timeRange depending on wording",
                        "presentation language": "select, combobox, or chips",
                        "required equipment": "multiChoice or chips",
                        "attachment, document, presentation, upload": "fileUpload",
                        "importance, satisfaction, priority, readiness score": "linearScale or rating",
                        "rate several factors": "ratingGrid or linearScale fields",
                        "mark choices by days/sections/categories": "checkboxGrid or radioGrid",
                    },
                    "options": "Choice fields must include non-empty options/choices/items with labels and values.",
                    "grid": "Grid fields must include non-empty rows and columns/cols.",
                },
                "submitLabel": "Button label.",
                "submitPlacement": "Optional: top or bottom.",
            },
        },
        "ui.table": {
            "purpose": "Static table preview.",
            "inputs": {
                "columns": "Array of {key,label}. Boolean columns may include kind='boolean'.",
                "filters": "Optional filters by row key/stateKey.",
            },
        },
        "ui.list": {
            "purpose": "List or card preview. For cards set inputs.variant='cards'.",
            "inputs": {
                "titleKey": "Single object path used as the card title.",
                "subtitleKey": "Single object path used as the card subtitle.",
                "previewKey": "Single object path used as the small preview text; this is not a template engine.",
                "emptyText": "Empty-state text.",
            },
            "actions": "For master-detail flows attach select actions directly to the list/table item. A typical modal detail item click uses actions=[{on:'select',type:'updateState',params:{selectedId:'$event.id'}},{on:'select',type:'openModal',params:{modalId:'detail_modal'}}].",
            "notes": [
                "Do not put '{{a}} - {{b}}' into titleKey/subtitleKey/previewKey.",
                "When a card needs combined text, add a derived string property to each static dataSource.value row, then point previewKey to that property.",
                "Do not place one detached page-level 'open details' button for all master items when the natural action is selecting a concrete item.",
            ],
        },
        "ui.actions": {
            "purpose": "A compact command surface for local prototype actions.",
            "inputs": {
                "buttons": "Array of {id,label,icon?}. Use for explicit commands, mode changes, or mock workflow actions.",
                "variant": "Optional: toolbar, stack, compact, segmented.",
                "size": "Optional: small, medium, large.",
            },
            "actions": "Use local updateState for prototype-only state changes; use openModal with params.modalId for declared modal prototypes; use callSkill only for real declared skill calls.",
        },
        "application_modals": {
            "purpose": "Use ui.application.modals when the user asks for a modal, dialog, popup, drawer, sheet, or separate overlay surface.",
            "shape": "Add ui.application.modals.<modalId>={title,presentation:{kind:'modal'|'drawer'|'sheet'|'sideSheet'},schema:{id,layout,widgets}} alongside ui.application.desktop.pageSchema.",
            "open_action": "Open a declared modal from a button/action with actions=[{on:'click', type:'openModal', params:{modalId:'comment_modal'}}].",
            "rule": "Do not model an explicitly requested modal only as a hidden inline widget; use a declared modal unless the user asks for an inline panel.",
        },
        "input.commandBar": {
            "purpose": "Segmented or toolbar-like local controls for choosing a mode, filter, draft state, or preview perspective.",
            "inputs": {
                "variant": "Use 'segmented' for mutually exclusive local choices.",
                "selectedStateKey": "State key that stores the selected button id.",
                "buttons": "Array of {id,label,icon?}.",
            },
            "actions": "For local prototype interaction use actions=[{on:'click', type:'updateState', params:{selectedMode:'$event.id'}}] where selectedMode is the concrete state key. A control is incomplete unless another widget or field visibly reacts to that state.",
            "example_pattern": {
                "initialState": {"exampleMode": "empty"},
                "widgets": [
                    {
                        "id": "example-mode",
                        "type": "input.commandBar",
                        "inputs": {
                            "variant": "segmented",
                            "selectedStateKey": "exampleMode",
                            "buttons": [{"id": "empty", "label": "Empty"}, {"id": "sample", "label": "Example"}],
                        },
                        "actions": [{"on": "click", "type": "updateState", "params": {"exampleMode": "$event.id"}}],
                    },
                    {
                        "id": "sample-preview",
                        "type": "item.details",
                        "visibleIf": "$state.exampleMode === 'sample'",
                        "dataSource": {
                            "kind": "static",
                            "value": {"title": "Example values", "fields": ["Replace with realistic sample values"]},
                        },
                    },
                ],
            },
        },
        "input.selector": {
            "purpose": "Dropdown-like local selector when a compact choice control is more appropriate than buttons.",
            "inputs": {
                "options": "Array of {label,value}.",
                "stateKey": "State key that stores the selected value.",
                "placeholder": "Optional empty prompt.",
            },
        },
        "state_and_visibility": {
            "initialState": "pageSchema.initialState can seed local prototype values, selected modes, mock workflow state, and temporary examples.",
            "visibleIf": "Widgets and fields may use visibleIf expressions to show different prototype surfaces based on local state. Prefer canonical expressions like $state.activeTab === 'overview'.",
            "local_interaction": "Interactive prototype controls should update local page state and static/mock data only unless the user explicitly asks for a real integration. If the user asks to view an example, compare variants, choose a mode, or preview a state, include an explicit local input.commandBar/input.selector/ui.actions widget and seed matching initialState.",
        },
        "master_detail_and_tabs": {
            "master_detail": "For master-detail prototypes use split/focus-detail layout for side-by-side detail, or an item-triggered modal/drawer for compact detail. The master ui.list/ui.table must own the selection action; detail containers react to the selected record.",
            "modal_detail": "If detail should open in a modal/dialog, attach openModal to the master item select/click action after updating selected state. Put detail-only secondary actions such as add comment inside the detail modal/panel, not as detached global buttons.",
            "tabs": "For tabs use input.commandBar variant='segmented' plus initialState.activeTab and visibleIf expressions on tab content widgets, unless the user asks for a static tab mock.",
            "details": "Use static data examples that show the selected/default record clearly; do not leave generic placeholder rows from the scaffold.",
        },
        "layout": {
            "patterns": "Use stack for linear flows, split/sidebar-content for supporting panels, grid/dashboard for overview surfaces, and flow-like layouts for compact prototypes.",
            "areas": "Areas are flexible slots. Keep only areas that serve the requested prototype; do not preserve split main/right when it creates empty or misleading space.",
            "move_widgets": "To move visible sections, update pageSchema.widgets[*].area and keep layout.areas consistent.",
        },
    }


def _builder_prototyping_affordances() -> dict[str, Any]:
    return {
        "role": "Adaptive UI prototyping designer-programmer.",
        "safety_boundary": [
            "AdaOS handles deterministic schema validation, revisions, review, and safe apply.",
            "The LLM may freely reshape the declarative UI inside adaos.webui.v1, but must not invent unsupported component types or real side effects.",
            "Interactive controls may demonstrate behavior with local page state, static data, and mock examples.",
            "Users are expected to speak in natural product/UI language, not AdaOS ABI terminology. Infer the internal schema representation without requiring terms like pageSchema, visibleIf, openModal, modalId, updateState, or dataSource from the user.",
        ],
        "meaningful_transformation": [
            "Treat current_webui_json as the starting material, not as a constraint to preserve.",
            "Treat every explicit clause in the user's instruction as a separate requirement; broad first clauses must not cause later clauses to be dropped.",
            "When the user asks for a prototype/design/layout/workflow change, make a visible semantic change, not a rename-only, duplicate-only, or no-op patch.",
            "Change fields, grouping, order, labels, helper text, component types, layout areas, density, widgets, actions, and mock data when that better serves the request.",
            "Turn broad categories into concrete interface decisions: split composite inputs, replace vague text fields with precise supported controls, and add realistic options/examples.",
            "If creating several comparable surfaces, make each one meaningfully different across structure, field order, component types, copy, density, support widgets, or interaction model.",
        ],
        "ui_freedom_map": {
            "forms": "May split into sections and atomic fields, reorder fields, choose more precise field types, add/remove helper text, defaults, options, validation, and submit placement.",
            "layout": "May switch stack/split/grid/sidebar patterns, remove unused areas, move widgets, and change density to match the requested experience.",
            "display": "May use table/list/cards/details/metrics/json preview surfaces for examples, summaries, and comparison.",
            "interaction": "May add local selectors, command bars, buttons, and visibleIf-driven states for prototype-only flows; when the user asks to choose, compare, preview, or view an example, include an explicit local control.",
            "mock_data": "May create realistic static rows/examples in the requested domain and keep them aligned with fields and display widgets.",
            "copy": "May rewrite labels, titles, section names, placeholders, helper text, and empty states in the user's language.",
            "modals": "When the user asks for a modal/dialog/drawer/sheet, declare it in ui.application.modals and open it with an openModal action from the relevant UI element. In master-detail, the relevant element is usually the selected master row/card.",
            "natural_language_mapping": "Map ordinary words to ABI structures: modal/dialog/window -> declared modal; tab/section switch -> segmented command bar plus state-driven content; selected item/details -> master-detail; required/error/check -> field validation; compare/variants -> local switching controls.",
        },
        "self_check": [
            "Before returning JSON, compare the output to the user's request and the previous UI.",
            "If the result mostly duplicates existing widgets, preserves the same field ids in the same order, or leaves stale sample data after a design request, revise before answering.",
            "If the request asks to preview, view an example, compare alternatives, choose a mode, or switch between variants, verify the page includes a visible local control such as input.commandBar/input.selector/ui.actions plus matching updateState and initialState/visibleIf before answering.",
            "If the user asks for a modal/dialog/drawer/sheet, verify ui.application.modals contains the surface and a relevant item/control action opens it with type openModal.",
            "Verify local visibility expressions use $state.<key> comparisons, for example $state.activeTab === 'overview'.",
            "If the user explicitly asks for local elements, controls, or a way to view examples, static mock rows alone are not enough; add a dedicated control widget that changes local state.",
            "Before answering mixed requests, make an internal checklist from each user-request clause and revise if any clause is only implied rather than visibly represented.",
            "Prefer a compact, inspectable prototype over exhaustive UI, but do not omit requested data-capture behavior.",
        ],
    }


def _builder_llm_system_prompt(*, project_system_prompt: str = "", prompt_profile: Mapping[str, Any] | None = None) -> str:
    profile = prompt_profile if isinstance(prompt_profile, Mapping) else _builder_llm_prompt_profile()
    profile_id = str(profile.get("id") or "default")
    provider = str(profile.get("provider") or "root-default")
    model_hint = str(profile.get("model") or "root-default")
    system_prompt = (
        "You are AdaOS Builder, an adaptive UI prototyping designer-programmer. "
        f"Prompt profile: {profile_id}; provider hint: {provider}; model hint: {model_hint}. "
        "Transform the current prototype UI according to the user's instruction. "
        "The user should not need to know AdaOS schema terms; interpret natural UI/product language and map it to the correct internal ABI structures yourself. "
        "AdaOS, not the model, owns deterministic validation, review, revision storage, and safe apply. "
        "Return only one JSON object. Do not include markdown, code fences, or prose outside JSON. "
        "The root object must be an adaos.webui.v1 manifest with schema='adaos.webui.v1'. "
        "The renderable source of truth is ui.application.desktop.pageSchema. "
        "Return the complete updated pageSchema under ui.application.desktop.pageSchema; if the prototype needs modals, also return ui.application.modals. Do not return preview_state, current_ui, or a root-level page_schema. "
        "When the user asks to move, remove, resize, redesign, or otherwise change visible widgets, update ui.application.desktop.pageSchema.widgets and layout. "
        "Treat the current UI as starting material, not as a fixed contract: make meaningful visible changes when the request implies design, workflow, layout, or prototype evolution. "
        "Decompose the user's instruction into explicit requirements and satisfy each one; do not let a broad form/layout request hide later requirements such as examples, local controls, variant switching, translations, or sample data. "
        "Use the supplied prototyping_affordances to vary field order, grouping, labels, field types, layout, widgets, local interactions, and mock data when that better fits the user's request. "
        "Interactive prototype elements may update local page state or static/mock data; do not invent real external integrations or side effects unless explicitly requested and declared. "
        "Avoid duplicate-only, rename-only, or no-op transformations for design requests; revise the JSON before answering if the result does not visibly satisfy the request. "
        "The runtime uses ui.list inputs titleKey/subtitleKey/previewKey as single object paths, not templates. "
        "If cards need combined text like status plus date, add a derived string property to the relevant static dataSource.value rows, then point previewKey to that property. "
        "Use the supplied compact adaos.webui.v1 ABI summary as the webui.json compatibility contract. "
        "When creating or editing ui.form fields, put the most semantically precise supported input kind in each field's required 'type' property; the ABI enum for that property is named formInputType. Use generic text only as a fallback. "
        "Do not preserve an existing generic text field when the user's request or the field label clearly implies a more specific supported type. "
        "Break broad or composite user concepts into atomic fields when creating forms: contacts should normally become email plus phone/messenger fields, personal data should become name plus relevant contact fields, and preferences should become concrete choices plus optional other text when appropriate. "
        "When the user asks people to select, mark, rate, upload, schedule, or enter structured values, model that as editable ui.form fields instead of a read-only table unless the user explicitly asks for a static table. "
        "For questionnaire, survey, registration, application, and intake prototypes, treat phrases such as indicate, choose, mark, attach, rate, enter, or their localized equivalents as data-capture requirements that need ui.form fields. "
        "When the user asks to choose between variants, compare layouts, preview examples, view sample state, use local elements, add elements for viewing an example, or switch modes, add an explicit local control such as input.commandBar, input.selector, or ui.actions with local updateState and visibleIf/initialState instead of only duplicating static content or sample rows. "
        "A requested local control is not complete unless at least one widget or form field visibly reacts to the local state set by that control. "
        "Use canonical visibility expressions like $state.activeTab === 'overview'; avoid state.activeTab == 'overview' in new output. "
        "For master-detail prototypes use a split or focus-detail layout for side-by-side detail, or item-triggered modal/drawer detail for compact/mobile detail. The master ui.list/ui.table should own select/click actions that update selected state; when detail is modal, open the detail modal from that same item action, not from a detached global button. "
        "Place secondary actions that belong to the selected detail, such as add comment, inside the detail container/modal/panel. "
        "For tabbed content, use input.commandBar with variant='segmented', initialState for the active tab, and visibleIf on the tab content widgets. "
        "For modal/dialog/drawer/sheet requests, declare ui.application.modals.<modalId>.schema and open it from the page with an action {type:'openModal', params:{modalId:'...'}}; do not represent an explicitly requested modal only as a hidden inline widget. "
        "If the user says things like 'add a modal window', 'make tabs', 'show details after selecting an item', 'validate this field', or similar localized phrases, infer the corresponding internal widgets/actions without asking the user to mention schema property names. "
        "Static ui.table/ui.list widgets may preview sample data, but they must not replace the fields used to collect the user's answers. "
        "Represent emails, URLs, phones, dates, times, ranges, files, one-choice inputs, multi-choice inputs, ratings, scales, and grid/matrix questions with their dedicated field types when supported. "
        "You are responsible for all domain-specific content: sample rows, translations, labels, examples, copy, and mock data. "
        "When the user asks for sample data, realistic examples, a different domain, or translation, update the relevant widget dataSource/static values inside ui.application.desktop.pageSchema instead of leaving old rows in place. "
        "Do not rely on hidden application code to generate domain examples after your response; your JSON must be complete. "
        "For checkbox/toggle semantics use boolean fields and boolean UI/table kinds; do not represent booleans as literal strings like 'true'/'false' unless the user asks for text. "
        "If you cannot safely satisfy the request, keep the previous UI valid and set unable_reason plus a short comment."
    )
    project_prompt = str(project_system_prompt or "").strip()
    if project_prompt and project_prompt != _default_builder_system_prompt_text().strip():
        system_prompt += "\n\nProject-specific Builder system prompt:\n" + project_prompt[:8000]
    return system_prompt


def _schema_ref_name(ref: Any) -> str | None:
    token = str(ref or "").strip()
    if token.startswith("#/$defs/"):
        return token.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
    return None


def _resolve_schema_ref(schema: Mapping[str, Any], ref: Any) -> Mapping[str, Any] | None:
    token = str(ref or "").strip()
    if not token.startswith("#/"):
        return None
    node: Any = schema
    for raw_part in token[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node.get(part)
    return node if isinstance(node, Mapping) else None


def _collect_schema_refs(
    node: Any,
    schema: Mapping[str, Any],
    refs: list[str],
    seen_nodes: set[int],
    *,
    depth: int = 0,
    max_depth: int = 14,
    max_refs: int = 64,
) -> bool:
    if len(refs) >= max_refs:
        return True
    if depth > max_depth:
        return False
    if isinstance(node, Mapping):
        marker = id(node)
        if marker in seen_nodes:
            return False
        seen_nodes.add(marker)
        ref_name = _schema_ref_name(node.get("$ref"))
        if ref_name:
            if ref_name not in refs:
                refs.append(ref_name)
                if len(refs) >= max_refs:
                    return True
            resolved = _resolve_schema_ref(schema, node.get("$ref"))
            if isinstance(resolved, Mapping):
                return _collect_schema_refs(
                    resolved,
                    schema,
                    refs,
                    seen_nodes,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_refs=max_refs,
                )
        for key in ("properties", "patternProperties"):
            value = node.get(key)
            if isinstance(value, Mapping):
                for child in value.values():
                    if _collect_schema_refs(
                        child,
                        schema,
                        refs,
                        seen_nodes,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_refs=max_refs,
                    ):
                        return True
        for key in (
            "additionalProperties",
            "items",
            "prefixItems",
            "oneOf",
            "anyOf",
            "allOf",
            "then",
            "else",
            "if",
            "not",
        ):
            if _collect_schema_refs(
                node.get(key),
                schema,
                refs,
                seen_nodes,
                depth=depth + 1,
                max_depth=max_depth,
                max_refs=max_refs,
            ):
                return True
    elif isinstance(node, list):
        for item in node:
            if _collect_schema_refs(
                item,
                schema,
                refs,
                seen_nodes,
                depth=depth + 1,
                max_depth=max_depth,
                max_refs=max_refs,
            ):
                return True
    return False


def _compact_schema_node(
    node: Any,
    *,
    depth: int = 0,
    max_depth: int = 3,
    max_properties: int = 64,
    max_enum: int = 96,
) -> Any:
    if not isinstance(node, Mapping):
        return node
    ref_name = _schema_ref_name(node.get("$ref"))
    if ref_name:
        return {"$ref": ref_name}
    result: dict[str, Any] = {}
    for key in ("type", "const", "default", "minLength", "minimum", "maximum"):
        if key in node:
            result[key] = node.get(key)
    enum_values = node.get("enum")
    if isinstance(enum_values, list):
        result["enum"] = enum_values[:max_enum]
        if len(enum_values) > max_enum:
            result["enum_truncated"] = len(enum_values) - max_enum
    required = node.get("required")
    if isinstance(required, list) and required:
        result["required"] = [str(item) for item in required if str(item or "").strip()]
    additional = node.get("additionalProperties")
    if isinstance(additional, bool):
        result["additionalProperties"] = additional
    elif isinstance(additional, Mapping) and depth < max_depth:
        result["additionalProperties"] = _compact_schema_node(
            additional,
            depth=depth + 1,
            max_depth=max_depth,
            max_properties=max_properties,
            max_enum=max_enum,
        )
    properties = node.get("properties")
    if isinstance(properties, Mapping) and depth < max_depth:
        compact_props: dict[str, Any] = {}
        for index, (key, value) in enumerate(properties.items()):
            if index >= max_properties:
                result["properties_truncated"] = len(properties) - max_properties
                break
            compact_props[str(key)] = _compact_schema_node(
                value,
                depth=depth + 1,
                max_depth=max_depth,
                max_properties=max_properties,
                max_enum=max_enum,
            )
        if compact_props:
            result["properties"] = compact_props
    items = node.get("items")
    if isinstance(items, Mapping) and depth < max_depth:
        result["items"] = _compact_schema_node(
            items,
            depth=depth + 1,
            max_depth=max_depth,
            max_properties=max_properties,
            max_enum=max_enum,
        )
    for key in ("oneOf", "anyOf", "allOf"):
        options = node.get(key)
        if isinstance(options, list) and options and depth < max_depth:
            result[key] = [
                _compact_schema_node(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_properties=max_properties,
                    max_enum=max_enum,
                )
                for item in options[:8]
                if isinstance(item, Mapping)
            ]
            if len(options) > 8:
                result[f"{key}_truncated"] = len(options) - 8
    description = str(node.get("description") or "").strip()
    if description:
        result["description"] = description[:220]
    return result or {"type": "any"}


def _generated_webui_schema_summary(schema: Mapping[str, Any]) -> dict[str, Any]:
    root_contract: dict[str, Any] = {
        "type": "object",
        "required": ["schema", "ui"],
        "additionalProperties": True,
        "properties": {
            "schema": {"const": "adaos.webui.v1"},
            "ui": {"$ref": "#/$defs/uiRoot"},
        },
    }
    refs: list[str] = []
    truncated = _collect_schema_refs(root_contract, schema, refs, set())
    for name in BUILDER_WEBUI_SCHEMA_FORCED_DEFS:
        before_count = len(refs)
        truncated = (
            _collect_schema_refs({"$ref": f"#/$defs/{name}"}, schema, refs, set(), max_refs=96)
            or truncated
        )
        if len(refs) == before_count and name not in refs and _schema_def(schema, name) is not None:
            refs.append(name)
    defs = schema.get("$defs") if isinstance(schema.get("$defs"), Mapping) else {}
    compact_defs: dict[str, Any] = {}
    for name in refs:
        raw_def = defs.get(name) if isinstance(defs, Mapping) else None
        if isinstance(raw_def, Mapping):
            compact_defs[name] = _compact_schema_node(raw_def)
    try:
        schema_hash = hashlib.sha256(_compact_json(schema).encode("utf-8")).hexdigest()[:12]
    except Exception:
        schema_hash = ""
    return {
        "source": "generated_from_json_schema",
        "schema_id": str(schema.get("$id") or "adaos.webui.v1"),
        "schema_hash": schema_hash,
        "entrypoint": "ui.application.desktop.pageSchema",
        "bounded": {"max_ref_depth": 14, "max_defs": 64, "truncated": bool(truncated)},
        "root_contract": _compact_schema_node(root_contract),
        "defs": compact_defs,
    }


def _builder_webui_abi_summary() -> dict[str, Any]:
    schema = _load_webui_schema()
    schema_contract = _generated_webui_schema_summary(schema) if schema else {
        "source": "fallback_minimal_contract",
        "entrypoint": "ui.application.desktop.pageSchema",
        "root_contract": {
            "required": ["schema", "ui"],
            "properties": {"schema": {"const": "adaos.webui.v1"}, "ui": {"type": "object"}},
        },
    }
    return {
        "schema": "adaos.webui.v1",
        "validated_by": "src/adaos/abi/webui.v1.schema.json",
        "schema_contract": schema_contract,
        "supported_widget_contracts": _builder_runtime_component_contracts(),
        "notes": [
            "Return only the complete updated ui.application.desktop.pageSchema inside the root webui manifest.",
            "Keep dataSource static values in the widgets that render them.",
            "The server validates the returned object against the full ABI schema.",
        ],
    }


def _write_webui(artifact_root: str | None, preview_state: Mapping[str, Any]) -> None:
    if not artifact_root:
        return
    root = Path(artifact_root)
    if not root.exists():
        return
    preview_state = _repair_text_tree(dict(preview_state))
    _ensure_builder_project_files(root, preview_state)
    page_schema = _with_builder_page_schema_meta(_page_schema_from_preview(preview_state), preview_state)
    title, title_i18n = _canonical_scenario_title(root, preview_state=preview_state, page_schema=page_schema, prefer_preview=True)
    preview_state["title"] = title
    preview_state["title_i18n"] = title_i18n
    page_schema = _apply_scenario_title_to_page_schema(page_schema, title=title, title_i18n=title_i18n)
    payload = {
        "schema": "adaos.webui.v1",
        "generated_by": SKILL_ID,
        "ui": {"application": {"desktop": {"pageSchema": page_schema}}},
        "nlu": {
            "llm_hints": {
                "aliases": [str(preview_state.get("title") or "prototype")],
                "primary_actions": [
                    {
                        "intent": "builder.chat",
                        "notes": "Prototype UI is edited through builder_skill.chat.",
                        "supported_operations": [
                            "add_field",
                            "remove_field",
                            "update_mock_data",
                            "change_view_representation",
                            "move_form_action",
                            "set_checkbox_column",
                        ],
                    }
                ],
            }
        },
    }
    (root / "webui.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_scenario_application_value(root, payload["ui"]["application"], preview_state)


def _write_webui_payload(artifact_root: str | None, payload: Mapping[str, Any]) -> None:
    if not artifact_root:
        return
    root = Path(artifact_root)
    if not root.exists():
        return
    data = _repair_text_tree(dict(payload))
    preview_state = data.get("preview_state") if isinstance(data.get("preview_state"), Mapping) else {}
    page_schema = _extract_webui_page_schema(data)
    if not page_schema and isinstance(preview_state, Mapping):
        page_schema = _page_schema_from_preview(preview_state)
    if page_schema:
        page_schema = _with_builder_page_schema_meta(page_schema, preview_state if isinstance(preview_state, Mapping) else {})
        if isinstance(preview_state, dict) and not str(preview_state.get("title") or "").strip():
            page_title = str(page_schema.get("title") or "").strip()
            if page_title:
                preview_state["title"] = page_title
                page_title_i18n = _clean_i18n_spec(page_schema.get("title_i18n"))
                if page_title_i18n:
                    preview_state["title_i18n"] = page_title_i18n
        title, title_i18n = _canonical_scenario_title(
            root,
            preview_state=preview_state if isinstance(preview_state, Mapping) else {},
            page_schema=page_schema,
            prefer_preview=True,
        )
        if isinstance(preview_state, dict):
            preview_state["title"] = title
            preview_state["title_i18n"] = title_i18n
        page_schema = _apply_scenario_title_to_page_schema(page_schema, title=title, title_i18n=title_i18n)
        data = _canonical_webui_payload(data, page_schema)
    if isinstance(preview_state, Mapping):
        _ensure_builder_project_files(root, preview_state)
    data.setdefault("schema", "adaos.webui.v1")
    data.setdefault("generated_by", SKILL_ID)
    (root / "webui.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if page_schema:
        _write_scenario_application_value(
            root,
            _extract_webui_application(data),
            preview_state if isinstance(preview_state, Mapping) else {},
        )
    elif isinstance(preview_state, Mapping):
        _write_scenario_page_schema(root, preview_state)


def _ui_revision_dir(artifact_root: str | None) -> Path | None:
    if not artifact_root:
        return None
    root = Path(str(artifact_root))
    if not root.exists():
        return None
    return root / "ui_revisions"


def _next_ui_revision_number(revision_dir: Path) -> int:
    max_seen = 0
    if revision_dir.exists():
        for path in revision_dir.glob("*.json"):
            match = re.match(r"^(\d{3,})$", path.stem)
            if match:
                max_seen = max(max_seen, int(match.group(1)))
    return max_seen + 1


def _next_ui_revision_label(session: Mapping[str, Any]) -> str:
    revision_dir = _ui_revision_dir(str(session.get("artifact_root") or ""))
    if revision_dir is None:
        return "001"
    return f"{_next_ui_revision_number(revision_dir):03d}"


def _sync_preview_revision_version(preview_state: Mapping[str, Any], revision: str) -> dict[str, Any]:
    preview = _repair_text_tree(copy.deepcopy(dict(preview_state)))
    if revision:
        preview["version"] = revision
    return preview


def _compact_llm_result(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, Mapping):
        return None
    compact: dict[str, Any] = {}
    for key in ("ok", "error", "detail", "comment", "unable_reason", "attempts", "model", "provider", "profile_id"):
        if key in result:
            compact[key] = copy.deepcopy(result.get(key))
    if isinstance(result.get("profile"), Mapping):
        compact["profile"] = copy.deepcopy(dict(result["profile"]))
    if isinstance(result.get("timing"), Mapping):
        compact["timing"] = copy.deepcopy(dict(result["timing"]))
    if isinstance(result.get("validation"), Mapping):
        compact["validation"] = copy.deepcopy(dict(result["validation"]))
    raw = str(result.get("last_response") or result.get("raw_response") or "").strip()
    if raw:
        compact["raw_response"] = raw[:12000]
    return compact


def _write_ui_revision(
    *,
    session: dict[str, Any],
    request_text: str,
    patch: Mapping[str, Any],
    before_webui: Mapping[str, Any] | None,
    after_webui: Mapping[str, Any] | None,
    preview_state: Mapping[str, Any],
    llm_result: Mapping[str, Any] | None = None,
    llm_model: str | None = None,
    revision: str | None = None,
) -> dict[str, Any] | None:
    revision_dir = _ui_revision_dir(str(session.get("artifact_root") or ""))
    if revision_dir is None:
        return None
    revision_dir.mkdir(parents=True, exist_ok=True)
    revision = str(revision or f"{_next_ui_revision_number(revision_dir):03d}").strip()
    match = re.search(r"(\d+)", revision)
    revision = f"{int(match.group(1)):03d}" if match else f"{_next_ui_revision_number(revision_dir):03d}"
    path = revision_dir / f"{revision}.json"
    preview_for_revision = _sync_preview_revision_version(preview_state, revision)
    before_for_revision = _repair_text_tree(copy.deepcopy(dict(before_webui or {})))
    after_for_revision = _repair_text_tree(copy.deepcopy(dict(after_webui or {})))
    if isinstance(before_for_revision.get("preview_state"), dict):
        before_for_revision["preview_state"]["version"] = revision
    if isinstance(after_for_revision.get("preview_state"), dict):
        after_for_revision["preview_state"]["version"] = revision
    model_id = str(
        llm_model
        or (llm_result.get("model") if isinstance(llm_result, Mapping) else "")
        or _builder_llm_model_for_session(session, None)
        or ""
    ).strip()
    profile = _builder_llm_prompt_profile(model_id)
    inference = {
        "provider": str(profile.get("provider") or "").strip() or None,
        "model": model_id or str(profile.get("model") or "").strip() or None,
        "profile_id": str(profile.get("id") or "").strip() or None,
        "temperature": profile.get("temperature"),
    }
    payload = {
        "schema": "adaos.builder.ui_revision.v1",
        "revision": revision,
        "created_at": _now(),
        "session_id": session.get("id"),
        "scenario_id": session.get("scenario_id"),
        "draft_id": session.get("draft_id"),
        "inference": {k: v for k, v in inference.items() if v not in (None, "")},
        "request": {"text": _display_request_text(request_text, patch)},
        "patch": _repair_text_tree(copy.deepcopy(dict(patch))),
        "llm": _compact_llm_result(llm_result),
        "before_webui": before_for_revision,
        "after_webui": after_for_revision,
        "preview_state": preview_for_revision,
        "prompt_files": _snapshot_prompt_files(str(session.get("artifact_root") or "")),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (revision_dir / "current.txt").write_text(revision + "\n", encoding="utf-8")
    session["ui_revision"] = revision
    revisions = [dict(item) for item in session.get("ui_revisions", []) if isinstance(item, Mapping)]
    revisions.append(
        {
            "revision": revision,
            "path": str(path),
            "request": str(request_text or ""),
            "operation": str(patch.get("operation") or ""),
            "model": model_id,
            "created_at": payload["created_at"],
        }
    )
    session["ui_revisions"] = revisions[-20:]
    return {"revision": revision, "path": str(path)}


def _read_ui_revision(session: Mapping[str, Any], revision: str) -> dict[str, Any] | None:
    token = str(revision or "").strip()
    if not token:
        return None
    if token.lower() == "current":
        revision_dir = _ui_revision_dir(str(session.get("artifact_root") or ""))
        if revision_dir is None:
            return None
        try:
            token = (revision_dir / "current.txt").read_text(encoding="utf-8").strip()
        except Exception:
            token = str(session.get("ui_revision") or "").strip()
    match = re.search(r"(\d+)", token)
    if not match:
        return None
    normalized = f"{int(match.group(1)):03d}"
    revision_dir = _ui_revision_dir(str(session.get("artifact_root") or ""))
    if revision_dir is None:
        return None
    path = revision_dir / f"{normalized}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        if isinstance(data, dict):
            data.setdefault("revision", normalized)
            data.setdefault("path", str(path))
            return data
    except Exception:
        return None
    return None


def _revision_chat_actions(session: Mapping[str, Any], revision: str | None) -> list[dict[str, Any]]:
    rev = str(revision or session.get("ui_revision") or "").strip()
    if not rev:
        return []
    return [
        {
            "id": f"builder.revision.{rev}.current",
            "label": f"current {rev}",
            "fill": "clear",
            "disabled": True,
            "title": "This message revision is the current UI state after this Builder turn.",
        },
        {
            "id": f"builder.revision.{rev}.set_current",
            "label": "set current",
            "fill": "outline",
            "title": f"Restore UI revision {rev} as current.",
            "action": {
                "on": "click",
                "type": "callSkill",
                "target": "builder_skill.set_ui_revision_current",
                "params": {
                    "session_id": str(session.get("id") or ""),
                    "revision": rev,
                },
            },
        },
    ]


def _write_scenario_manifest(root: Path, scenario: Mapping[str, Any], preview_state: Mapping[str, Any]) -> None:
    scenario_id = str(scenario.get("id") or preview_state.get("scenario_id") or preview_state.get("id") or root.name).strip() or root.name
    title, title_i18n = _canonical_scenario_title(
        root,
        scenario=scenario,
        preview_state=preview_state,
        prefer_preview=True,
    )
    title = title or scenario_id
    depends = [
        str(item).strip()
        for item in (scenario.get("depends") if isinstance(scenario.get("depends"), list) else [])
        if isinstance(item, str) and str(item).strip() and str(item).strip() != SKILL_ID
    ]
    runtime = scenario.get("runtime") if isinstance(scenario.get("runtime"), Mapping) else {}
    skills = runtime.get("skills") if isinstance(runtime.get("skills"), Mapping) else {}
    raw_supported_locales = scenario.get("supported_locales") if isinstance(scenario.get("supported_locales"), list) else []
    supported_locales = [
        str(item).strip().lower()
        for item in raw_supported_locales
        if isinstance(item, str) and str(item).strip()
    ] or ["en", "ru"]
    required = [
        str(item).strip()
        for item in (skills.get("required") if isinstance(skills.get("required"), list) else [])
        if isinstance(item, str) and str(item).strip() and str(item).strip() != SKILL_ID
    ]
    lines = [
        f"id: {json.dumps(scenario_id, ensure_ascii=False)}",
        f"name: {json.dumps(str(scenario.get('name') or scenario_id), ensure_ascii=False)}",
        f"type: {json.dumps(str(scenario.get('type') or 'desktop'), ensure_ascii=False)}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"description: {json.dumps(str(scenario.get('description') or 'Builder rapid prototype scenario.'), ensure_ascii=False)}",
        f"version: {json.dumps(str(scenario.get('version') or '0.1.0'), ensure_ascii=False)}",
    ]
    if title_i18n:
        lines.append("title_i18n:")
        for key, value in title_i18n.items():
            lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("supported_locales:")
    lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}" for item in supported_locales)
    if depends:
        lines.append("depends:")
        lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}" for item in depends)
    else:
        lines.append("depends: []")
    lines.extend(["runtime:", "  skills:"])
    if required:
        lines.append("    required:")
        lines.extend(f"      - {json.dumps(item, ensure_ascii=False)}" for item in required)
    else:
        lines.append("    required: []")
    lines.append("")
    (root / "scenario.yaml").write_text("\n".join(lines), encoding="utf-8")


def _normalize_field_options(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    options: list[Any] = []
    for item in value:
        if isinstance(item, Mapping):
            option = copy.deepcopy(dict(item))
            label = str(
                option.get("label")
                or option.get("title")
                or option.get("name")
                or option.get("value")
                or option.get("id")
                or ""
            ).strip()
            if not label:
                continue
            option.setdefault("label", label)
            option.setdefault("value", option.get("id", label))
            options.append(option)
            continue
        if isinstance(item, (str, int, float, bool)) and str(item).strip():
            options.append(item)
    return options


def _field_options_from_any_key(field: Mapping[str, Any], keys: Sequence[str]) -> list[Any]:
    for key in keys:
        options = _normalize_field_options(field.get(key))
        if options:
            return options
    return []


def _form_field_type(field: Mapping[str, Any]) -> str:
    field_type = str(field.get("type") or "string").strip().lower()
    if field_type in {"select", "dropdown", "choice", "enum"} or _normalize_field_options(field.get("options")):
        return "select"
    if field_type in {"boolean", "bool", "toggle", "checkbox"}:
        return "toggle"
    if field_type in {"number", "integer", "float", "decimal"}:
        return "number"
    if field_type in {"date", "datetime"}:
        return "date"
    if field_type in {"textarea", "text_area", "multiline"}:
        return "textarea"
    return "text"


def _is_boolean_field_type(value: Any) -> bool:
    return str(value or "").strip().lower() in {"boolean", "bool", "toggle", "checkbox", "switch"}


def _current_ui_field_id(node: Mapping[str, Any]) -> str:
    binding = str(node.get("binding") or node.get("stateKey") or node.get("state_key") or "").strip()
    if binding.startswith("draft."):
        return binding.split(".", 1)[1].strip()
    raw = str(node.get("field_id") or node.get("field") or node.get("key") or node.get("id") or "").strip()
    for prefix in ("input_", "field_"):
        if raw.startswith(prefix) and len(raw) > len(prefix):
            return raw[len(prefix) :]
    return raw


def _current_ui_form_field_map(ui: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    supported = {
        "text_input",
        "input",
        "textarea",
        "text_area",
        "number_input",
        "date_input",
        "checkbox",
        "toggle",
        "select",
        "dropdown",
        "choice",
        "enum",
    }
    found: dict[str, dict[str, Any]] = {}

    def _walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for child in nodes:
            if not isinstance(child, Mapping):
                continue
            node_type = str(child.get("type") or "").strip().lower()
            options = _normalize_field_options(child.get("options"))
            if node_type in supported or options or child.get("binding"):
                field_id = _current_ui_field_id(child)
                if field_id:
                    field = {
                        "id": field_id,
                        "type": _form_field_type(child),
                        "label": child.get("label") or child.get("title") or field_id,
                    }
                    if options:
                        field["options"] = options
                    found[field_id] = field
            _walk(child.get("children"))

    _walk(ui.get("children"))
    return found


def _merge_current_ui_fields(
    fields: Sequence[Mapping[str, Any]],
    ui_fields: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    merged = [copy.deepcopy(dict(item)) for item in fields if isinstance(item, Mapping)]
    seen: set[str] = set()
    for item in merged:
        field_id = str(item.get("id") or "").strip()
        if not field_id:
            continue
        seen.add(field_id)
        source = ui_fields.get(field_id)
        if not isinstance(source, Mapping):
            continue
        source_type = str(source.get("type") or "").strip()
        if source_type:
            item["type"] = source_type
        if not str(item.get("label") or "").strip() and source.get("label"):
            item["label"] = source.get("label")
        options = _normalize_field_options(item.get("options")) or _normalize_field_options(source.get("options"))
        if options:
            item["options"] = options
    for field_id, source in ui_fields.items():
        if field_id in seen or not isinstance(source, Mapping):
            continue
        field = {
            "id": field_id,
            "type": str(source.get("type") or "string").strip() or "string",
            "label": source.get("label") or field_id,
            "required": False,
        }
        options = _normalize_field_options(source.get("options"))
        if options:
            field["options"] = options
        merged.append(field)
    return merged


def _page_form_field(field: Mapping[str, Any], index: int, ui_fields: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    field_id = str(field.get("id") or f"field_{index}").strip() or f"field_{index}"
    source = ui_fields.get(field_id) if isinstance(ui_fields.get(field_id), Mapping) else {}
    form_type = str(source.get("type") or "").strip() or _form_field_type(field)
    options = _normalize_field_options(field.get("options")) or _normalize_field_options(source.get("options"))
    if options:
        form_type = "select"
    item = {
        "id": field_id,
        "type": form_type,
        "label": field.get("label") or source.get("label") or field.get("id") or f"Field {index + 1}",
    }
    if options:
        item["options"] = options
    return item


def _enrich_page_schema_form_fields(data: dict[str, Any], ui_fields: Mapping[str, Mapping[str, Any]]) -> None:
    if not ui_fields:
        return
    widgets = data.get("widgets")
    if not isinstance(widgets, list):
        return
    for widget in widgets:
        if not isinstance(widget, Mapping) or str(widget.get("type") or "") != "ui.form":
            continue
        inputs = widget.get("inputs") if isinstance(widget.get("inputs"), Mapping) else {}
        fields = inputs.get("fields") if isinstance(inputs.get("fields"), list) else []
        enriched: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw_field in enumerate(fields):
            if not isinstance(raw_field, Mapping):
                continue
            field = copy.deepcopy(dict(raw_field))
            field_id = str(field.get("id") or "").strip()
            if not field_id:
                enriched.append(field)
                continue
            seen.add(field_id)
            source = ui_fields.get(field_id)
            if isinstance(source, Mapping):
                source_type = str(source.get("type") or "").strip()
                if source_type:
                    field["type"] = source_type
                if not str(field.get("label") or "").strip() and source.get("label"):
                    field["label"] = source.get("label")
                options = _normalize_field_options(field.get("options")) or _normalize_field_options(source.get("options"))
                if options:
                    field["type"] = "select"
                    field["options"] = options
            else:
                options = _normalize_field_options(field.get("options"))
                if options:
                    field["type"] = "select"
                    field["options"] = options
            enriched.append(field)
        for field_id, source in ui_fields.items():
            if field_id in seen or not isinstance(source, Mapping):
                continue
            enriched.append(_page_form_field(source, len(enriched), ui_fields))
        next_inputs = copy.deepcopy(dict(inputs))
        next_inputs["fields"] = enriched
        widget["inputs"] = next_inputs


def _enrich_page_schema_table_columns(data: dict[str, Any], ui_fields: Mapping[str, Mapping[str, Any]]) -> None:
    if not ui_fields:
        return
    widgets = data.get("widgets")
    if not isinstance(widgets, list):
        return
    for widget in widgets:
        if not isinstance(widget, Mapping) or str(widget.get("type") or "") != "ui.table":
            continue
        inputs = widget.get("inputs") if isinstance(widget.get("inputs"), Mapping) else {}
        columns = inputs.get("columns") if isinstance(inputs.get("columns"), list) else []
        enriched: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw_column in enumerate(columns):
            if not isinstance(raw_column, Mapping):
                continue
            column = copy.deepcopy(dict(raw_column))
            field_id = str(column.get("key") or column.get("field") or column.get("id") or "").strip()
            if not field_id:
                field_id = f"field_{index}"
                column["key"] = field_id
            if "key" not in column:
                column["key"] = field_id
            column.pop("field", None)
            seen.add(field_id)
            source = ui_fields.get(field_id)
            if isinstance(source, Mapping):
                if not str(column.get("label") or "").strip():
                    column["label"] = source.get("label") or field_id
                if _is_boolean_field_type(source.get("type")):
                    column["kind"] = "boolean"
                    column.setdefault("width", "72px")
            enriched.append(column)
        for field_id, source in ui_fields.items():
            if field_id in seen or not isinstance(source, Mapping):
                continue
            column = {
                "key": field_id,
                "label": source.get("label") or field_id,
            }
            if _is_boolean_field_type(source.get("type")):
                column["kind"] = "boolean"
                column["width"] = "72px"
            enriched.append(column)
        next_inputs = copy.deepcopy(dict(inputs))
        next_inputs["columns"] = enriched
        widget["inputs"] = next_inputs


def _normalise_page_schema_candidate(
    value: Any,
    *,
    title: str,
    page_id: str,
    ui_fields: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    data = copy.deepcopy(dict(value))
    widgets = data.get("widgets")
    if not isinstance(widgets, list):
        return None
    clean_widgets: list[dict[str, Any]] = []
    for index, widget in enumerate(widgets):
        if not isinstance(widget, Mapping):
            continue
        item = copy.deepcopy(dict(widget))
        item_id = str(item.get("id") or "").strip()
        item_type = str(item.get("type") or "").strip()
        if not item_id:
            item["id"] = f"widget-{index + 1}"
        if not item_type:
            continue
        item["type"] = item_type
        if item_type == "ui.list":
            inputs = item.get("inputs") if isinstance(item.get("inputs"), Mapping) else {}
            if inputs:
                clean_inputs = copy.deepcopy(dict(inputs))
                for key in ("titleKey", "subtitleKey", "previewKey"):
                    if key not in clean_inputs:
                        continue
                    clean_key = _card_key_from_template(clean_inputs.get(key))
                    if clean_key:
                        clean_inputs[key] = clean_key
                    else:
                        clean_inputs.pop(key, None)
                item["inputs"] = clean_inputs
        clean_widgets.append(item)
    if not clean_widgets:
        return None
    data["widgets"] = clean_widgets
    _enrich_page_schema_form_fields(data, ui_fields or {})
    _enrich_page_schema_table_columns(data, ui_fields or {})
    data.setdefault("id", page_id or "builder_prototype")
    data.setdefault("title", title or "Prototype")
    if not isinstance(data.get("layout"), Mapping):
        data["layout"] = {
            "type": "split",
            "pattern": "split",
            "areas": [{"id": "main", "role": "main"}, {"id": "right", "role": "aux"}],
        }
    return data


def _page_schema_from_preview(preview_state: Mapping[str, Any]) -> dict[str, Any]:
    ui = preview_state.get("current_ui") if isinstance(preview_state.get("current_ui"), Mapping) else {}
    title = str(preview_state.get("title") or ui.get("title") or "Prototype").strip() or "Prototype"
    ui_fields = _current_ui_form_field_map(ui)
    datasources = preview_state.get("datasources") if isinstance(preview_state.get("datasources"), list) else []
    datasource = datasources[0] if datasources and isinstance(datasources[0], Mapping) else {}
    fields = _merge_current_ui_fields(
        [dict(item) for item in datasource.get("fields", []) if isinstance(item, Mapping)],
        ui_fields,
    )
    schema_fields = {str(item.get("id") or ""): item for item in fields if str(item.get("id") or "").strip()}
    direct_page_schema = _normalise_page_schema_candidate(
        preview_state.get("page_schema"),
        title=title,
        page_id=str(ui.get("id") or preview_state.get("session_id") or "builder_prototype"),
        ui_fields=schema_fields or ui_fields,
    )
    if direct_page_schema is not None:
        return direct_page_schema
    datasource_id = str(datasource.get("id") or "items").strip() or "items"
    mock_data = preview_state.get("mock_data") if isinstance(preview_state.get("mock_data"), Mapping) else {}
    raw_rows = mock_data.get(datasource_id) if isinstance(mock_data.get(datasource_id), list) else []
    rows = [dict(item) for item in raw_rows if isinstance(item, Mapping)]
    filters = [dict(item) for item in preview_state.get("filters", []) if isinstance(item, Mapping)]
    layout_order = str(preview_state.get("layout_order") or ui.get("layout_order") or "").strip().lower()
    cards_first = layout_order in {"cards_first", "cards-first", "cards_left", "cards-left", "cards_main", "cards-main"}
    has_card_view = any(
        isinstance(child, Mapping) and str(child.get("type") or "") == "card_list"
        for child in (ui.get("children") if isinstance(ui.get("children"), list) else [])
    )
    table_visible = True
    for child in (ui.get("children") if isinstance(ui.get("children"), list) else []):
        if isinstance(child, Mapping) and (
            str(child.get("id") or "") == "items_table" or str(child.get("type") or "") == "table"
        ):
            table_visible = child.get("visible") is not False
            break
    editor = next(
        (
            dict(child)
            for child in (ui.get("children") if isinstance(ui.get("children"), list) else [])
            if isinstance(child, Mapping) and str(child.get("id") or "") == "editor"
        ),
        {},
    )
    submit_placement = str(editor.get("action_position") or preview_state.get("form_action_position") or "").strip().lower()
    form_area = "right" if cards_first and has_card_view else "main"
    cards_area = "main" if cards_first and has_card_view else "right"
    form_inputs = {
        "fields": [
            _page_form_field(field, index, ui_fields)
            for index, field in enumerate(fields)
        ],
        "submitLabel": "Add",
    }
    if submit_placement == "top":
        form_inputs["submitPlacement"] = "top"
    widgets: list[dict[str, Any]] = [
        {
            "id": "prototype-form",
            "type": "ui.form",
            "area": form_area,
            "title": "Input",
            "inputs": form_inputs,
            "actions": [{"on": "submit", "type": "updateState", "params": {"lastPrototypeSubmit": "$event.values"}}],
        },
    ]
    for filter_obj in filters:
        field_id = str(filter_obj.get("field_id") or "").strip()
        if not field_id:
            continue
        state_key = str(filter_obj.get("state_key") or f"builderFilter_{field_id}").strip()
        raw_options = filter_obj.get("options") if isinstance(filter_obj.get("options"), list) else []
        buttons = [{"id": "all", "label": "\u0412\u0441\u0435"}]
        if field_id == "done":
            buttons.extend(
                [
                    {"id": "true", "label": "\u041a\u0443\u043f\u043b\u0435\u043d\u043e"},
                    {"id": "false", "label": "\u041d\u0435 \u043a\u0443\u043f\u043b\u0435\u043d\u043e"},
                ]
            )
        else:
            buttons.extend({"id": str(value), "label": str(value)} for value in raw_options if str(value).strip())
        widgets.append(
            {
                "id": f"prototype-filter-{field_id}",
                "type": "input.commandBar",
                "area": form_area,
                "title": filter_obj.get("label") or field_id,
                "inputs": {
                    "variant": "segmented",
                    "size": "small",
                    "selectedStateKey": state_key,
                    "buttons": buttons,
                },
                "actions": [{"on": "click", "type": "updateState", "params": {state_key: "$event.id"}}],
            }
        )
    if table_visible:
        widgets.append(
            {
                "id": "prototype-table",
                "type": "ui.table",
                "area": form_area,
                "title": "List",
                "dataSource": {"kind": "static", "value": rows},
                "inputs": {
                    "columns": [
                        {
                            "key": str(field.get("id") or f"field_{index}"),
                            "label": field.get("label") or field.get("id") or f"Field {index + 1}",
                            **({"kind": "boolean", "width": "72px"} if _is_boolean_field_type(field.get("type")) else {}),
                        }
                        for index, field in enumerate(fields)
                    ],
                    "filters": [
                        {
                            "key": str(filter_obj.get("field_id") or ""),
                            "stateKey": str(filter_obj.get("state_key") or f"builderFilter_{filter_obj.get('field_id')}"),
                            "any": "all",
                        }
                        for filter_obj in filters
                        if str(filter_obj.get("field_id") or "").strip()
                    ],
                    "emptyText": "No items yet",
                },
            },
        )
    if has_card_view:
        first = str(fields[0].get("id") if fields else "title")
        second = str(fields[1].get("id") if len(fields) > 1 else "")
        card_child = next(
            (
                child
                for child in (ui.get("children") if isinstance(ui.get("children"), list) else [])
                if isinstance(child, Mapping) and str(child.get("type") or "") == "card_list"
            ),
            {},
        )
        preview_key = ""
        if isinstance(card_child, Mapping):
            preview_template = card_child.get("preview")
            preview_key = _card_key_from_template(preview_template)
            if not preview_key:
                derived_preview = _derive_card_preview_rows(preview_template, fields=fields, rows=rows)
                if derived_preview is not None:
                    preview_key, rows = derived_preview
        if not preview_key:
            preview_key = str(preview_state.get("card_preview_key") or "").strip()
        if not preview_key:
            preview_key = _preferred_card_preview_key(fields)
        subtitle_key = second
        if preview_key and subtitle_key == preview_key and len(fields) > 2:
            subtitle_key = str(fields[2].get("id") or "")
        widgets.append(
            {
                "id": "prototype-cards",
                "type": "ui.list",
                "area": cards_area,
                "title": "Cards",
                "dataSource": {"kind": "static", "value": rows},
                "inputs": {
                    "variant": "cards",
                    "titleKey": first,
                    "subtitleKey": subtitle_key,
                    "previewKey": preview_key,
                    "emptyText": "No cards yet",
                },
            }
        )
    else:
        widgets.append(
            {
                "id": "prototype-summary",
                "type": "item.details",
                "area": "right",
                "title": "Prototype",
                "dataSource": {"kind": "static", "value": {"title": title, "fields": [field.get("label") for field in fields]}},
            }
        )
    return {
        "id": str(ui.get("id") or preview_state.get("session_id") or "builder_prototype"),
        "title": title,
        "layout": {
            "type": "split",
            "pattern": "split",
            "areas": [
                {"id": "main", "role": "preview" if cards_first and has_card_view else "main"},
                {"id": "right", "role": "editor" if cards_first and has_card_view else "aux"},
            ],
        },
        "widgets": widgets,
    }


def _write_scenario_application_value(
    root: Path,
    application: Mapping[str, Any],
    preview_state: Mapping[str, Any],
) -> None:
    preview_state = _repair_text_tree(dict(preview_state))
    application = _repair_text_tree(copy.deepcopy(dict(application or {})))
    desktop = application.get("desktop") if isinstance(application.get("desktop"), Mapping) else {}
    page_schema = desktop.get("pageSchema") if isinstance(desktop.get("pageSchema"), Mapping) else {}
    page_schema = _with_builder_page_schema_meta(page_schema, preview_state)
    manifest = root / "scenario.json"
    if not manifest.exists() or not page_schema:
        return
    try:
        scenario = json.loads(manifest.read_text(encoding="utf-8-sig") or "{}")
    except Exception:
        return
    if not isinstance(scenario, dict):
        return
    scenario = _repair_text_tree(scenario)
    title, title_i18n = _canonical_scenario_title(
        root,
        scenario=scenario,
        preview_state=preview_state,
        page_schema=page_schema,
        prefer_preview=True,
    )
    preview_state["title"] = title
    preview_state["title_i18n"] = title_i18n
    page_schema = _apply_scenario_title_to_page_schema(page_schema, title=title, title_i18n=title_i18n)
    scenario.setdefault("id", root.name)
    scenario.setdefault("name", root.name)
    scenario.setdefault("type", "desktop")
    scenario["title"] = title or scenario.get("name") or scenario.get("id") or "Prototype"
    if title_i18n:
        scenario["title_i18n"] = title_i18n
    scenario.setdefault("supported_locales", ["en", "ru"])
    depends = scenario.get("depends")
    depends_list = [str(item) for item in depends if isinstance(item, str)] if isinstance(depends, list) else []
    depends_list = [item for item in depends_list if item != SKILL_ID]
    scenario["depends"] = depends_list
    runtime = scenario.get("runtime") if isinstance(scenario.get("runtime"), dict) else {}
    skills = runtime.get("skills") if isinstance(runtime.get("skills"), dict) else {}
    required = skills.get("required") if isinstance(skills.get("required"), list) else []
    required_list = [str(item) for item in required if isinstance(item, str)]
    required_list = [item for item in required_list if item != SKILL_ID]
    skills["required"] = required_list
    runtime["skills"] = skills
    scenario["runtime"] = runtime
    application.setdefault("version", "0.1")
    desktop = application.get("desktop") if isinstance(application.get("desktop"), dict) else {}
    desktop["pageSchema"] = page_schema
    application["desktop"] = desktop
    scenario.setdefault("ui", {})
    scenario["ui"]["application"] = application
    manifest.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _invalidate_scenario_runtime_caches(root, "builder_write_scenario_application")
    _write_scenario_manifest(root, scenario, preview_state)


def _write_scenario_page_schema_value(root: Path, page_schema: Mapping[str, Any], preview_state: Mapping[str, Any]) -> None:
    _write_scenario_application_value(root, {"desktop": {"pageSchema": page_schema}}, preview_state)


def _write_scenario_page_schema(root: Path, preview_state: Mapping[str, Any]) -> None:
    _write_scenario_page_schema_value(root, _page_schema_from_preview(preview_state), preview_state)


def _save_session(webspace_id: str, session: dict[str, Any]) -> dict[str, Any]:
    session["updated_at"] = _now()
    _normalise_pending_llm_jobs(session)
    sessions = _sessions(webspace_id)
    existing = sessions.get(str(session.get("id") or ""))
    if isinstance(existing, Mapping):
        _log_pending_llm_job_state_races(
            scenario_id=str(session.get("scenario_id") or existing.get("scenario_id") or ""),
            existing=existing.get("pending_llm_jobs") if isinstance(existing.get("pending_llm_jobs"), Mapping) else {},
            incoming=session.get("pending_llm_jobs") if isinstance(session.get("pending_llm_jobs"), Mapping) else {},
        )
        session["pending_llm_jobs"] = _merge_pending_llm_jobs(
            existing.get("pending_llm_jobs") if isinstance(existing.get("pending_llm_jobs"), Mapping) else {},
            session.get("pending_llm_jobs") if isinstance(session.get("pending_llm_jobs"), Mapping) else {},
        )
        _normalise_pending_llm_jobs(session)
    sessions[str(session["id"])] = copy.deepcopy(session)
    _mem_set_many(
        {
            _scoped_key(SESSIONS_KEY, webspace_id): _trim_sessions(sessions),
            _scoped_key(CURRENT_KEY, webspace_id): str(session["id"]),
        }
    )
    return session


def _load_session(webspace_id: str, session_id: str | None = None) -> dict[str, Any] | None:
    sessions = _sessions(webspace_id)
    sid = str(session_id or "").strip() or _current_session_id(webspace_id)
    if sid and sid in sessions:
        return copy.deepcopy(sessions[sid])
    if sessions:
        return copy.deepcopy(max(sessions.values(), key=lambda item: float(item.get("updated_at") or 0)))
    return None


def _message_created(session: Mapping[str, Any]) -> str:
    summary = session.get("user_summary") if isinstance(session.get("user_summary"), Mapping) else _draft_user_summary(session)
    assumptions = "; ".join(str(item) for item in summary.get("assumptions", [])[:2]) if isinstance(summary, Mapping) else ""
    preview = "; ".join(str(item) for item in summary.get("preview", [])[:2]) if isinstance(summary, Mapping) else ""
    risks = "; ".join(str(item) for item in summary.get("risks", [])[:2]) if isinstance(summary, Mapping) else ""
    return (
        f"{AGENT_LABEL}: \u0441\u043e\u0437\u0434\u0430\u043b dev-\u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 "
        f"{session.get('scenario_id')} \u0438 \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a webui. "
        f"Assumptions: {assumptions}. Preview: {preview}. Risks: {risks}. "
        "\u041c\u043e\u0436\u043d\u043e \u0441\u0440\u0430\u0437\u0443 \u043f\u0440\u0430\u0432\u0438\u0442\u044c: "
        "\u0434\u043e\u0431\u0430\u0432\u044c \u043f\u043e\u043b\u0435, \u0443\u0431\u0435\u0440\u0438 \u043f\u043e\u043b\u0435, \u043f\u043e\u043a\u0430\u0436\u0438 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0430\u043c\u0438."
    )


def _draft_user_summary(session: Mapping[str, Any]) -> dict[str, list[str]]:
    fields = [dict(item) for item in session.get("fields", []) if isinstance(item, Mapping)]
    labels = ", ".join(str(item.get("label") or item.get("id") or "") for item in fields[:5] if str(item.get("label") or item.get("id") or "").strip())
    scenario_id = str(session.get("scenario_id") or "prototype").strip() or "prototype"
    datasource_id = str(session.get("datasource_id") or "items").strip() or "items"
    return {
        "assumptions": [
            "This is a local dev prototype, not an activated runtime change",
            f"The initial scaffold started with fields: {labels or 'title, notes, status'}; this list is not a fixed product contract",
        ],
        "preview": [
            f"Scenario {scenario_id} has a form, table, mock data, and declarative webui.json",
            f"Data is stored in an internal CRUD datasource named {datasource_id}",
        ],
        "risks": [
            "No external network, device-control, or credential access is requested",
            "Validation and human review are still required before activation",
        ],
        "expected_behavior": [
            "The user can add records through the form and inspect them in the list",
            "Follow-up Builder turns patch the current draft and refresh the preview",
        ],
    }


def _developer_evidence(
    *,
    webspace_id: str,
    session: Mapping[str, Any] | None,
    preview_state: Mapping[str, Any] | None = None,
    workbench: Mapping[str, Any] | None = None,
    topic_ref: Mapping[str, Any] | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(session, Mapping):
        return None
    topic = dict(topic_ref or {}) if isinstance(topic_ref, Mapping) else _builder_topic_ref(webspace_id, session=session, _meta=_meta)
    artifact_root = str(session.get("artifact_root") or "").strip()
    artifact_path = Path(artifact_root) if artifact_root else None
    files: list[dict[str, Any]] = []
    if artifact_path is not None:
        for name, role in (
            ("webui.json", "runtime_preview"),
            ("scenario.json", "scenario_manifest_json"),
            ("scenario.yaml", "scenario_manifest_yaml"),
        ):
            path = artifact_path / name
            files.append({"role": role, "path": str(path), "exists": path.exists()})
    patches: list[dict[str, Any]] = []
    for patch in session.get("patches", []) if isinstance(session.get("patches"), list) else []:
        if not isinstance(patch, Mapping):
            continue
        diff = patch.get("diff") if isinstance(patch.get("diff"), Mapping) else {}
        patches.append(
            {
                "id": str(patch.get("id") or ""),
                "operation": str(patch.get("operation") or ""),
                "status": str(patch.get("status") or ""),
                "review_status": str(patch.get("review_status") or "") or None,
                "pending_action_id": str(patch.get("pending_action_id") or "") or None,
                "diff_keys": sorted(str(key) for key in diff.keys()),
                "not_implemented": list(diff.get("not_implemented") or []) if isinstance(diff.get("not_implemented"), list) else [],
            }
        )
    pending_action_ids = [
        str(value)
        for value in [session.get("pending_action_id"), *(item.get("pending_action_id") for item in patches)]
        if str(value or "").strip()
    ]
    preview = preview_state if isinstance(preview_state, Mapping) else session.get("preview_state")
    preview_payload = preview if isinstance(preview, Mapping) else {}
    workbench_payload = dict(workbench or {}) if isinstance(workbench, Mapping) else {}
    projection = workbench_payload.get("projection") if isinstance(workbench_payload.get("projection"), Mapping) else {}
    return {
        "schema": "adaos.builder.developer_evidence.v1",
        "session_id": str(session.get("id") or ""),
        "scenario_id": str(session.get("scenario_id") or "") or None,
        "draft_id": str(session.get("draft_id") or "") or None,
        "artifact_root": artifact_root or None,
        "files": files,
        "schemas": {
            "preview_state": "adaos.builder.preview_state.v1",
            "webui": "adaos.webui.v1",
            "topic_ref": "adaos.conversation.topic_ref.v1",
            "pending_action": "adaos.pending_action.v1",
        },
        "route_plan": {
            "webspace_id": webspace_id,
            "dialog_channel_id": DIALOG_CHANNEL_ID,
            "conversation_id": _conversation_id(webspace_id),
            "owner": f"skill:{SKILL_ID}",
            "default_tool": f"{SKILL_ID}.chat",
            "agent_id": AGENT_ID,
            "thread_id": str(topic.get("thread_id") or "") or None,
            "topic_id": str(topic.get("topic_id") or "") or None,
        },
        "topic": {key: value for key, value in topic.items() if key != "stored"},
        "preview_refs": {
            "current_ui_type": str(preview_payload.get("current_ui", {}).get("type") or "") if isinstance(preview_payload.get("current_ui"), Mapping) else None,
            "datasource_ids": [
                str(item.get("id") or "")
                for item in preview_payload.get("datasources", [])
                if isinstance(item, Mapping) and str(item.get("id") or "")
            ],
            "pending_patch_count": len(preview_payload.get("pending_patches") or []) if isinstance(preview_payload.get("pending_patches"), list) else 0,
        },
        "patches": patches,
        "pending_action_ids": pending_action_ids,
        "workbench": {
            "ok": bool(workbench_payload.get("ok")),
            "binding": dict(workbench_payload.get("binding") or {}) if isinstance(workbench_payload.get("binding"), Mapping) else {},
            "projection_deferred": bool(projection.get("deferred")),
        },
    }


def _extract_field_label(instruction: str) -> str | None:
    quoted = re.search(r"[\"'«](.*?)[\"'»]", instruction)
    if quoted:
        return _clean_field_label(quoted.group(1))
    match = re.search(r"(?:field|поле|column|колонк[ауи]?)\s+([A-Za-zА-Яа-я0-9 _-]{2,40})", instruction, re.IGNORECASE)
    if match:
        return _clean_field_label(match.group(1))
    return None


def _clean_field_label(label: str) -> str:
    token = str(label or "").strip(" \t\r\n:;,.!?()[]{}")
    token = re.split(r"\s+(?:в|на|к|для|со|с|to|in|as)\s+", token, maxsplit=1, flags=re.IGNORECASE)[0]
    return token.strip(" \t\r\n:;,.!?()[]{}")


def _field_id(label: str) -> str:
    lowered = str(label or "").strip().lower()
    known = {
        "\u0446\u0435\u043d\u0430": "price",
        "\u0434\u0430\u0442\u0430": "date",
        "\u043a\u0443\u043f\u043b\u0435\u043d\u043e": "done",
        "\u0442\u043e\u0432\u0430\u0440": "item",
        "\u043a\u043e\u043b-\u0432\u043e": "quantity",
        "\u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e": "quantity",
        "\u043c\u0435\u0440\u0430": "unit",
        "\u0435\u0434\u0438\u043d\u0438\u0446\u0430": "unit",
        "\u0435\u0434.": "unit",
        "\u043d\u0430\u043b\u0438\u0447\u0438\u0435": "availability",
        "\u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f": "category",
        "\u0442\u0435\u043b\u0435\u0444\u043e\u043d": "phone",
        "\u043e\u0440\u0433\u0430\u043d\u0438\u0437\u0430\u0446\u0438\u044f": "organization",
        "date": "date",
        "done": "done",
        "purchased": "done",
        "unit": "unit",
        "measure": "unit",
        "availability": "availability",
    }
    if lowered in known:
        return known[lowered]
    ascii_id = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return ascii_id or f"field_{_hash_suffix(label)}"


def _field_type_for_id(field_id: str, label: str | None = None) -> str:
    token = f"{field_id} {label or ''}".lower()
    if field_id == "done" or any(item in token for item in ("checkbox", "check box", "чекбокс", "куплено")):
        return "boolean"
    if field_id == "price" or any(item in token for item in ("price", "цена", "стоимость")):
        return "number"
    if field_id == "date" or any(item in token for item in ("date", "дата")):
        return "date"
    return "string"


def _default_label_for_field(field_id: str, fallback: str | None = None) -> str:
    fallback_text = str(fallback or "").strip().lower()
    if field_id == "done":
        if any(token in fallback_text for token in ("complete", "execution", "done", "\u0438\u0441\u043f\u043e\u043b\u043d", "\u0432\u044b\u043f\u043e\u043b\u043d")):
            return "\u0418\u0441\u043f\u043e\u043b\u043d\u0435\u043d\u043e"
        return "\u041a\u0443\u043f\u043b\u0435\u043d\u043e"
    labels = {
        "date": "\u0414\u0430\u0442\u0430",
        "price": "\u0426\u0435\u043d\u0430",
        "unit": "\u041c\u0435\u0440\u0430",
        "availability": "\u041d\u0430\u043b\u0438\u0447\u0438\u0435",
    }
    return labels.get(field_id) or _clean_field_label(fallback or field_id).title()


def _ensure_field(
    fields: list[dict[str, Any]],
    *,
    label: str,
    field_id: str | None = None,
    field_type: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    fid = str(field_id or _field_id(label)).strip()
    for item in fields:
        if str(item.get("id") or "") == fid:
            if field_type and str(item.get("type") or "") != field_type:
                item["type"] = field_type
            if not str(item.get("label") or "").strip():
                item["label"] = _default_label_for_field(fid, label)
            options = _field_options(fid)
            if options and not isinstance(item.get("options"), list):
                item["options"] = options
            return fields, item, False
    field = {
        "id": fid,
        "type": field_type or _field_type_for_id(fid, label),
        "label": _default_label_for_field(fid, label),
        "required": False,
    }
    options = _field_options(fid)
    if options:
        field["options"] = options
    fields.append(field)
    return fields, field, True


def _field_options(field_id: str) -> list[Any]:
    if field_id == "unit":
        return ["\u0448\u0442", "\u043a\u0433", "\u0433", "\u043b"]
    if field_id == "availability":
        return ["\u0432 \u043d\u0430\u043b\u0438\u0447\u0438\u0438", "\u043d\u0435\u0442"]
    if field_id == "done":
        return [True, False]
    return []


def _ensure_filter(filters: list[dict[str, Any]], field: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    field_id = str(field.get("id") or "").strip()
    if not field_id:
        return filters, {}, False
    for item in filters:
        if str(item.get("field_id") or "") == field_id:
            return filters, item, False
    filter_obj = {
        "field_id": field_id,
        "label": field.get("label") or _default_label_for_field(field_id),
        "state_key": f"builderFilter_{field_id}",
        "options": _field_options(field_id),
    }
    filters.append(filter_obj)
    return filters, filter_obj, True


def _requested_known_fields(text: str) -> list[dict[str, Any]]:
    lowered = str(text or "").lower()
    words = set(re.findall(r"[A-Za-z0-9.\u0410-\u042f\u0430-\u044f\u0401\u0451]+", lowered))
    specs: list[dict[str, Any]] = []
    if (
        any(word.startswith("\u043c\u0435\u0440") for word in words)
        or words.intersection({"\u0435\u0434\u0438\u043d\u0438\u0446\u0430", "\u0435\u0434.", "unit", "measure"})
        or "\u0435\u0434\u0438\u043d\u0438\u0446\u0430 \u0438\u0437\u043c\u0435\u0440\u0435\u043d\u0438\u044f" in lowered
    ):
        specs.append({"label": "\u041c\u0435\u0440\u0430", "field_id": "unit", "field_type": "string"})
    if any(token in lowered for token in ("\u043d\u0430\u043b\u0438\u0447", "availability", "stock")):
        specs.append({"label": "\u041d\u0430\u043b\u0438\u0447\u0438\u0435", "field_id": "availability", "field_type": "string"})
    return specs


def _requested_filter_field_ids(text: str) -> list[str]:
    lowered = str(text or "").lower()
    if not any(token in lowered for token in ("\u0444\u0438\u043b\u044c\u0442\u0440", "filter")):
        return []
    ids: list[str] = []
    if any(token in lowered for token in ("\u043a\u0443\u043f\u043b\u0435\u043d", "done", "purchased")):
        ids.append("done")
    if any(token in lowered for token in ("\u043d\u0430\u043b\u0438\u0447", "availability", "stock")):
        ids.append("availability")
    if any(token in lowered for token in ("\u043a\u0430\u0442\u0435\u0433\u043e\u0440", "category")):
        ids.append("category")
    return ids


def _move_field_first(fields: list[dict[str, Any]], field_id: str) -> list[dict[str, Any]]:
    fid = str(field_id or "").strip()
    if not fid:
        return fields
    selected = [item for item in fields if str(item.get("id") or "") == fid]
    if not selected:
        return fields
    rest = [item for item in fields if str(item.get("id") or "") != fid]
    return [selected[0], *rest]


def _date_mock_rows(fields: list[dict[str, Any]], existing_rows: Any = None) -> list[dict[str, Any]]:
    base_rows = [dict(item) for item in existing_rows if isinstance(item, Mapping)] if isinstance(existing_rows, list) else _mock_rows(fields)
    if not base_rows:
        base_rows = _mock_rows(fields)
    dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
    for index, row in enumerate(base_rows):
        row["date"] = dates[index % len(dates)]
    return base_rows


def _mentions_date(text: str) -> bool:
    return _text_contains_any(text, ("date", "\u0434\u0430\u0442"))


def _text_variants(text: str) -> list[str]:
    raw = str(text or "")
    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        lowered = str(value or "").lower()
        if lowered and lowered not in seen:
            seen.add(lowered)
            variants.append(lowered)

    add(raw)
    for encoding in ("latin1", "cp1251"):
        try:
            add(raw.encode(encoding).decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return variants


def _repair_mojibake_text(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return raw
    candidates = [raw]
    for encoding in ("cp1251", "latin1"):
        try:
            candidates.append(raw.encode(encoding).decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue

    def score(candidate: str) -> tuple[int, int, int]:
        bad_pairs = sum(candidate.count(token) for token in ("Р", "С", "Ð", "Ñ", "\ufffd"))
        bad_question_runs = len(re.findall(r"\?{2,}", candidate))
        cyrillic = sum(1 for ch in candidate if "\u0400" <= ch <= "\u04ff")
        mojibake_cyrillic_pairs = len(re.findall(r"[\u0420\u0421][\u0400-\u04ff]", candidate))
        mojibake_latin_pairs = len(re.findall(r"[\u00d0\u00d1][\x80-\xff]", candidate))
        rare_cyrillic = sum(candidate.count(token) for token in ("\u0403", "\u040a", "\u040c", "\u040b", "\u040f", "\u0453", "\u045a", "\u045c", "\u045f", "\u0491", "\u0490"))
        return (
            bad_pairs * 8 + mojibake_cyrillic_pairs * 3 + mojibake_latin_pairs * 3 + rare_cyrillic * 2 + bad_question_runs * 4,
            -cyrillic,
            len(candidate),
        )

    repaired = min(candidates, key=score)
    return "".join(
        ch
        for ch in repaired
        if ch in "\t\n\r" or (ord(ch) >= 0x20 and not 0x7F <= ord(ch) <= 0x9F)
    )


def _repair_text_tree(value: Any) -> Any:
    if isinstance(value, str):
        return _repair_mojibake_text(value)
    if isinstance(value, list):
        return [_repair_text_tree(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_repair_text_tree(item) for item in value)
    if isinstance(value, Mapping):
        return {str(key): _repair_text_tree(item) for key, item in value.items()}
    return value


def _prototype_review_notes_from_meta(_meta: Mapping[str, Any] | None) -> str:
    if not isinstance(_meta, Mapping):
        return ""
    raw = _meta.get("prototype_review_notes")
    if raw is None:
        raw = _meta.get("prototypeReviewNotes")
    if isinstance(raw, str):
        return _repair_mojibake_text(raw).strip()[:12000]
    if not isinstance(raw, Mapping):
        return ""
    notes = _repair_mojibake_text(raw.get("notes")).strip()
    if notes:
        return notes[:12000]
    comments = raw.get("comments")
    if not isinstance(comments, list):
        return ""
    lines: list[str] = []
    revision = _repair_mojibake_text(raw.get("revision_key")).strip()
    if revision:
        lines.append(f"Prototype review notes for {revision}:")
    for item in comments:
        if not isinstance(item, Mapping):
            continue
        text = _repair_mojibake_text(item.get("text")).strip()
        if not text:
            continue
        element = item.get("element")
        if isinstance(element, Mapping):
            ref = _repair_mojibake_text(element.get("ref")).strip()
            kind = _repair_mojibake_text(element.get("kind")).strip()
            label = _repair_mojibake_text(element.get("label")).strip()
            target = " ".join(part for part in (kind, ref, f'"{label}"' if label else "") if part).strip()
        else:
            target = ""
        lines.append(f"- {target}: {text}" if target else f"- {text}")
    return "\n".join(lines).strip()[:12000]


def _instruction_with_prototype_review_notes(instruction: Any, _meta: Mapping[str, Any] | None) -> str:
    text = _repair_mojibake_text(instruction).strip()
    notes = _prototype_review_notes_from_meta(_meta)
    if not notes:
        return text
    if notes in text:
        return text
    return (
        f"{text}\n\n"
        "Prototype review notes from the current dev preview. Treat these as concrete feedback for the next UI revision:\n"
        f"{notes}"
    ).strip()


def _text_contains_any(text: str, tokens: Iterable[str]) -> bool:
    token_list = [str(token or "").lower() for token in tokens if str(token or "")]
    if not token_list:
        return False
    return any(token in variant for variant in _text_variants(text) for token in token_list)


def _text_contains_all_groups(text: str, *groups: Iterable[str]) -> bool:
    normalized_groups = [
        [str(token or "").lower() for token in group if str(token or "")]
        for group in groups
    ]
    normalized_groups = [group for group in normalized_groups if group]
    if not normalized_groups:
        return False
    for variant in _text_variants(text):
        if all(any(token in variant for token in group) for group in normalized_groups):
            return True
    return False


def _has_lost_cyrillic_markers(text: str) -> bool:
    return any(len(re.findall(r"\?{2,}", variant)) >= 2 for variant in _text_variants(text))


def _display_request_text(request_text: Any, patch: Mapping[str, Any] | None = None) -> str:
    text = _repair_mojibake_text(request_text)
    if not _has_lost_cyrillic_markers(text):
        return text
    operation = str((patch or {}).get("operation") or "").strip()
    if operation == "swap_layout_areas":
        return "\u041f\u0435\u0440\u0435\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u043e\u0431\u043b\u0430\u0441\u0442\u0438 Input \u0438 Cards"
    if operation == "set_card_preview":
        return "\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u0432 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0430\u0445 \u0442\u0435\u043a\u0441\u0442\u043e\u0432\u044b\u0439 \u043f\u0440\u0438\u043c\u0435\u0440"
    if operation == "change_view_representation":
        return "\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c UI \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0430\u043c\u0438"
    return text


def _wants_add_button_above_form(text: str) -> bool:
    return _text_contains_all_groups(
        text,
        ("button", "\u043a\u043d\u043e\u043f"),
        ("add", "\u0434\u043e\u0431\u0430\u0432"),
        ("above", "top", "\u043d\u0430\u0434", "\u0432\u0435\u0440\u0445"),
        ("form", "\u0444\u043e\u0440\u043c"),
    )


def _wants_done_checkbox_first(text: str) -> bool:
    mentions_done = _text_contains_any(text, ("done", "purchased", "\u043a\u0443\u043f\u043b\u0435\u043d"))
    mentions_checkbox = _text_contains_any(text, ("checkbox", "check box", "\u0447\u0435\u043a\u0431\u043e\u043a\u0441"))
    mentions_first_column = _text_contains_all_groups(
        text,
        ("first", "\u043f\u0435\u0440\u0432"),
        ("column", "\u043a\u043e\u043b\u043e\u043d"),
    )
    return mentions_done and (mentions_checkbox or mentions_first_column)


def _wants_date_values(text: str) -> bool:
    return _mentions_date(text) and _text_contains_any(
        text,
        ("data", "value", "values", "fill", "\u0434\u0430\u043d\u043d", "\u0437\u043d\u0430\u0447\u0435\u043d", "\u0437\u0430\u043f\u043e\u043b\u043d"),
    )


def _wants_card_view(text: str) -> bool:
    return _text_contains_any(text, ("card", "cards", "\u043a\u0430\u0440\u0442\u043e\u0447", "\u043f\u043b\u0438\u0442\u043a"))


def _wants_swap_input_and_cards(text: str) -> bool:
    if _text_contains_all_groups(
        text,
        ("swap", "switch", "reorder", "change places", "\u043f\u0435\u0440\u0435\u0441\u0442\u0430\u0432", "\u043f\u043e\u043c\u0435\u043d\u044f", "\u043c\u0435\u0441\u0442\u0430\u043c\u0438"),
        ("input", "form", "\u0432\u0432\u043e\u0434", "\u0444\u043e\u0440\u043c"),
        ("card", "cards", "\u043a\u0430\u0440\u0442\u043e\u0447"),
    ):
        return True
    return _has_lost_cyrillic_markers(text) and _text_contains_all_groups(
        text,
        ("input", "form"),
        ("card", "cards"),
    )


def _wants_card_text_preview(text: str) -> bool:
    return _wants_card_view(text) and _text_contains_any(
        text,
        (
            "json",
            "not json",
            "text",
            "example",
            "preview",
            "\u0442\u0435\u043a\u0441\u0442",
            "\u043f\u0440\u0438\u043c\u0435\u0440",
            "\u043f\u0440\u0435\u0434\u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440",
            "\u0440\u0430\u0437\u043c\u0435\u0449",
        ),
    )


def _wants_hide_list_or_table(text: str) -> bool:
    mentions_remove = _text_contains_any(
        text,
        ("remove", "hide", "without", "\u0443\u0431\u0435\u0440", "\u0443\u0434\u0430\u043b", "\u0441\u043a\u0440\u043e\u0439", "\u0431\u0435\u0437"),
    )
    mentions_list = _text_contains_any(text, ("list", "table", "\u0441\u043f\u0438\u0441\u043e\u043a", "\u0442\u0430\u0431\u043b\u0438\u0446"))
    mentions_only_cards = _text_contains_any(text, ("only", "\u0442\u043e\u043b\u044c\u043a")) and _wants_card_view(text)
    return (mentions_remove and mentions_list) or mentions_only_cards


def _wants_execution_checkbox(text: str) -> bool:
    mentions_checkbox = _text_contains_any(text, ("checkbox", "check box", "\u0447\u0435\u043a\u0431\u043e\u043a\u0441", "\u0444\u043b\u0430\u0436\u043e\u043a"))
    mentions_done = _text_contains_any(
        text,
        (
            "done",
            "complete",
            "completed",
            "execution",
            "\u0438\u0441\u043f\u043e\u043b\u043d",
            "\u0432\u044b\u043f\u043e\u043b\u043d",
            "\u0433\u043e\u0442\u043e\u0432",
            "\u043a\u0443\u043f\u043b\u0435\u043d",
        )
    )
    return mentions_checkbox and mentions_done


def _wants_english_ui(text: str) -> bool:
    return _text_contains_any(text, ("english", "in english", "\u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a", "\u043d\u0430 \u0430\u043d\u0433\u043b"))


def _wants_llm_owned_content_change(text: str) -> bool:
    return _text_contains_any(
        text,
        (
            "data",
            "mock",
            "sample",
            "example",
            "record",
            "records",
            "row",
            "rows",
            "text",
            "copy",
            "content",
            "\u0434\u0430\u043d\u043d",
            "\u0437\u0430\u043f\u0438\u0441",
            "\u0441\u0442\u0440\u043e\u043a",
            "\u0442\u0435\u043a\u0441\u0442",
            "\u0441\u043e\u0434\u0435\u0440\u0436",
            "\u043f\u0440\u0438\u043c\u0435\u0440",
        ),
    )


def _has_deterministic_builder_update(text: str) -> bool:
    lowered = _repair_mojibake_text(text).lower()
    if any(
        (
            _wants_swap_input_and_cards(text),
            _wants_card_text_preview(text),
            _wants_card_view(text),
            _wants_hide_list_or_table(text),
            _wants_execution_checkbox(text),
            _wants_add_button_above_form(text),
            _wants_done_checkbox_first(text),
        )
    ):
        return True
    if _requested_known_fields(text) or _requested_filter_field_ids(text):
        return True
    if _mentions_date(text) and (
        "field" in lowered
        or "column" in lowered
        or "\u043f\u043e\u043b\u0435" in lowered
        or "\u043a\u043e\u043b\u043e\u043d" in lowered
        or _wants_date_values(text)
    ):
        return True
    return bool(_extract_field_label(text) or (_text_contains_any(text, ("\u0446\u0435\u043d", "price"))))


def _english_title(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if "\u043f\u043e\u043a\u0443\u043f" in lowered or "shopping" in lowered:
        return "Shopping List"
    if "\u0437\u0430\u0434\u0430\u0447" in lowered or "todo" in lowered:
        return "Todo List"
    if lowered:
        return str(value).replace("_", " ").title()
    return "Prototype"


def _english_label(field_id: str, label: str | None = None) -> str:
    token = f"{field_id} {label or ''}".strip().lower()
    mapping = {
        "item": "Item",
        "product": "Product",
        "title": "Title",
        "name": "Name",
        "quantity": "Quantity",
        "unit": "Unit",
        "price": "Price",
        "date": "Date",
        "category": "Category",
        "availability": "Availability",
        "done": "Done",
        "notes": "Notes",
        "status": "Status",
        "owner": "Owner",
    }
    for key, value in mapping.items():
        if key in token:
            return value
    if any(item in token for item in ("\u0442\u043e\u0432\u0430\u0440", "\u043f\u0440\u043e\u0434\u0443\u043a\u0442")):
        return "Item"
    if "\u043a\u043e\u043b" in token:
        return "Quantity"
    if "\u0446\u0435\u043d" in token:
        return "Price"
    if "\u0434\u0430\u0442" in token:
        return "Date"
    if "\u043a\u0430\u0442\u0435\u0433" in token:
        return "Category"
    if "\u043d\u0430\u043b\u0438\u0447" in token:
        return "Availability"
    if any(item in token for item in ("\u043a\u0443\u043f\u043b", "\u0438\u0441\u043f\u043e\u043b\u043d", "\u0432\u044b\u043f\u043e\u043b\u043d")):
        return "Done"
    fallback = str(label or field_id or "Field").strip()
    return fallback.replace("_", " ").title()


def _translate_session_to_english(session: dict[str, Any], fields: list[dict[str, Any]]) -> None:
    session["ui_locale"] = "en"
    session["title"] = _english_title(str(session.get("title") or session.get("scenario_id") or "Prototype"))
    for field in fields:
        field["label"] = _english_label(str(field.get("id") or ""), str(field.get("label") or ""))
    session["fields"] = fields


def _repo_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "src" / "adaos" / "abi" / "webui.v1.schema.json").exists():
        return cwd
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "adaos" / "abi" / "webui.v1.schema.json").exists():
            return parent
    return cwd


def _load_webui_schema() -> dict[str, Any]:
    path = _repo_root() / "src" / "adaos" / "abi" / "webui.v1.schema.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _builder_llm_primary_enabled(_meta: Mapping[str, Any] | None = None) -> bool:
    raw = str(os.getenv("ADAOS_BUILDER_LLM_PRIMARY") or "").strip().lower()
    if raw in {"0", "false", "no", "off", "fallback"}:
        return False
    if raw in {"1", "true", "yes", "on", "primary"}:
        return True
    if os.getenv("PYTEST_CURRENT_TEST") and str(os.getenv("ADAOS_BUILDER_LLM_IN_TESTS") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    if isinstance(_meta, Mapping) and _meta.get("disable_builder_llm") is True:
        return False
    return True


def _env_enabled(name: str, default: bool) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _builder_llm_async_enabled(_meta: Mapping[str, Any] | None = None) -> bool:
    if isinstance(_meta, Mapping) and _meta.get("builder_llm_async") is False:
        return False
    if isinstance(_meta, Mapping) and _meta.get("builder_llm_async") is True:
        return True
    if os.getenv("PYTEST_CURRENT_TEST") and not _env_enabled("ADAOS_BUILDER_LLM_ASYNC_IN_TESTS", False):
        return False
    return _env_enabled("ADAOS_BUILDER_LLM_ASYNC", True)


def _builder_llm_job_submit_timeout_s() -> float:
    raw = os.getenv("ADAOS_BUILDER_LLM_JOB_SUBMIT_TIMEOUT_S")
    try:
        value = float(raw) if raw else 15.0
    except (TypeError, ValueError):
        value = 15.0
    return max(3.0, min(value, 60.0))


def _builder_llm_job_submit_warn_ms() -> float:
    raw = os.getenv("ADAOS_BUILDER_LLM_JOB_SUBMIT_WARN_MS")
    try:
        value = float(raw) if raw else 5000.0
    except (TypeError, ValueError):
        value = 5000.0
    return max(100.0, min(value, 120000.0))


def _builder_llm_job_timeout_s() -> float:
    raw = os.getenv("ADAOS_BUILDER_LLM_JOB_TIMEOUT_S")
    try:
        value = float(raw) if raw else _builder_llm_timeout_s()
    except (TypeError, ValueError):
        value = _builder_llm_timeout_s()
    return max(30.0, min(value, 600.0))


def _builder_llm_job_poll_interval_s() -> float:
    raw = os.getenv("ADAOS_BUILDER_LLM_JOB_POLL_INTERVAL_S")
    try:
        value = float(raw) if raw else 1.0
    except (TypeError, ValueError):
        value = 1.0
    return max(0.5, min(value, 15.0))


def _project_memory(session: Mapping[str, Any]) -> dict[str, Any]:
    artifact_root = _project_artifact_root(session)
    memory_text = ""
    technical_spec_text = ""
    system_prompt_text = ""
    if artifact_root is not None:
        path = artifact_root / BUILDER_MEMORY_FILE
        if path.exists():
            memory_text = _read_text_file(path, limit=12000)
        tz_path = artifact_root / PROMPT_TZ_BASE_FILE
        if tz_path.exists():
            technical_spec_text = _read_text_file(tz_path, limit=12000)
        system_prompt_path = artifact_root / BUILDER_SYSTEM_PROMPT_FILE
        if system_prompt_path.exists():
            system_prompt_text = _read_text_file(system_prompt_path, limit=8000)
    memory_text = _neutralize_legacy_builder_memory_text(memory_text)
    technical_spec_text = _neutralize_legacy_builder_memory_text(technical_spec_text)
    return {
        "source_idea": str(session.get("source_idea") or ""),
        "user_summary": _neutralize_legacy_user_summary(session.get("user_summary") if isinstance(session.get("user_summary"), Mapping) else {}),
        "memory_text": memory_text,
        "technical_spec_text": technical_spec_text,
        "project_system_prompt_text": system_prompt_text,
        "editable_files": {
            "project_memory": BUILDER_MEMORY_FILE,
            "technical_specification": str(PROMPT_TZ_BASE_FILE).replace("\\", "/"),
            "project_system_prompt": BUILDER_SYSTEM_PROMPT_FILE,
        },
        "current_revision": str(session.get("ui_revision") or ""),
        "recent_revisions": [
            {
                "revision": str(item.get("revision") or ""),
                "operation": str(item.get("operation") or ""),
                "request": str(item.get("request") or "")[:500],
            }
            for item in session.get("ui_revisions", [])[-8:]
            if isinstance(item, Mapping)
        ],
    }


def _current_webui_payload(session: Mapping[str, Any], preview_state: Mapping[str, Any]) -> dict[str, Any]:
    artifact_root = _project_artifact_root(session)
    payload: dict[str, Any] = {}
    runtime_page_schema: dict[str, Any] = {}
    if artifact_root is not None:
        _ensure_builder_project_files(artifact_root, preview_state)
        path = artifact_root / "webui.json"
        if path.exists():
            raw = _load_json_file(path)
            if raw:
                payload = raw
        runtime_page_schema = _current_runtime_page_schema(artifact_root)
    page_schema = _extract_webui_page_schema(payload) or runtime_page_schema
    if not page_schema and isinstance(preview_state.get("page_schema"), Mapping):
        page_schema = _repair_text_tree(copy.deepcopy(dict(preview_state["page_schema"])))
    if not page_schema:
        page_schema = _page_schema_from_preview(preview_state)
    if artifact_root is not None and page_schema:
        title, title_i18n = _canonical_scenario_title(
            artifact_root,
            preview_state=preview_state,
            page_schema=page_schema,
            prefer_preview=False,
        )
        page_schema = _apply_scenario_title_to_page_schema(page_schema, title=title, title_i18n=title_i18n)
    return _canonical_webui_payload(payload, page_schema)


def _builder_runtime_context(session: Mapping[str, Any], current_payload: Mapping[str, Any]) -> dict[str, Any]:
    artifact_root = _project_artifact_root(session)
    scenario_manifest = _current_scenario_yaml_manifest(artifact_root) or (_current_scenario_manifest(artifact_root) if artifact_root is not None else {})
    current_page_schema = _extract_webui_page_schema(current_payload)
    return {
        "scenario_manifest_path": str(scenario_manifest.get("__path") or "") if scenario_manifest else "",
        "scenario_manifest_summary": {
            "id": scenario_manifest.get("id"),
            "name": scenario_manifest.get("name"),
            "title": scenario_manifest.get("title"),
            "type": scenario_manifest.get("type"),
            "depends": scenario_manifest.get("depends") if isinstance(scenario_manifest.get("depends"), list) else [],
            "runtime": scenario_manifest.get("runtime") if isinstance(scenario_manifest.get("runtime"), Mapping) else {},
        }
        if scenario_manifest
        else {},
        "current_page_schema": current_page_schema,
    }


def _builder_llm_webui_transform_request(
    *,
    session: Mapping[str, Any],
    instruction: str,
    preview_state: Mapping[str, Any],
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current_payload = _current_webui_payload(session, preview_state)
    project_memory = _project_memory(session)
    project_system_prompt = str(project_memory.get("project_system_prompt_text") or "").strip()
    history = [
        {
            "operation": str(item.get("operation") or ""),
            "summary": str(item.get("summary") or ""),
            "status": str(item.get("status") or ""),
            "revision": str(item.get("revision") or ""),
        }
        for item in (session.get("patches") if isinstance(session.get("patches"), list) else [])[-8:]
        if isinstance(item, Mapping)
    ]
    selected_model = _builder_llm_model_for_session(session, _meta)
    prompt_profile = _builder_llm_prompt_profile(selected_model)
    system_prompt = _builder_llm_system_prompt(
        project_system_prompt=project_system_prompt,
        prompt_profile=prompt_profile,
    )
    base_request = {
        "llm_prompt_profile": prompt_profile,
        "webui_v1_abi": _builder_webui_abi_summary(),
        "runtime_component_contracts": _builder_runtime_component_contracts(),
        "prototyping_affordances": _builder_prototyping_affordances(),
        "webui_v1_schema": "see webui_v1_abi.schema_contract; validated by src/adaos/abi/webui.v1.schema.json",
        "required_output_shape": {
            "schema": "adaos.webui.v1",
            "ui": {
                "application": {
                    "desktop": {
                        "pageSchema": "complete AdaOS pageSchema object with id, layout, and widgets"
                    }
                }
            },
            "comment": "short user-facing text about what changed or why it could not be changed",
            "unable_reason": "short optional diagnostic if request cannot be implemented",
        },
        "scenario_id": session.get("scenario_id"),
        "title": session.get("title"),
        "project_memory": project_memory,
        "runtime_context": _builder_runtime_context(session, current_payload),
        "recent_patch_history": history,
        "current_webui_json": current_payload,
        "instruction": instruction,
    }
    return {
        "current_payload": current_payload,
        "system_prompt": system_prompt,
        "user_prompt": _compact_json(base_request),
        "base_request": base_request,
    }


def _balanced_json_object(text: str) -> str | None:
    source = str(text or "")
    for start, char in enumerate(source):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(source)):
            current = source[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    return source[start : index + 1]
    return None


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    candidates = [raw]
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.IGNORECASE | re.DOTALL):
        candidates.insert(0, match.group(1).strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        fragment = _balanced_json_object(candidate)
        if fragment:
            try:
                parsed = json.loads(fragment)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    raise ValueError("LLM response does not contain a JSON object")


def _validate_webui_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    schema = _load_webui_schema()
    if not schema:
        return {"ok": True, "schema": "missing"}
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator(schema).validate(dict(payload))
        return {"ok": True, "schema": schema.get("$id") or "adaos.webui.v1"}
    except Exception as exc:
        return {"ok": False, "error": "webui_schema_validation_failed", "detail": f"{type(exc).__name__}: {exc}"}


def _validate_preview_state_payload(preview_state: Mapping[str, Any]) -> dict[str, Any]:
    if preview_state.get("current_ui") is not None and not isinstance(preview_state.get("current_ui"), Mapping):
        return {"ok": False, "error": "preview_state_invalid", "detail": "preview_state.current_ui must be an object when present"}
    datasources = preview_state.get("datasources")
    if datasources is not None and not isinstance(datasources, list):
        return {"ok": False, "error": "preview_state_invalid", "detail": "preview_state.datasources must be an array"}
    mock_data = preview_state.get("mock_data")
    if mock_data is not None and not isinstance(mock_data, Mapping):
        return {"ok": False, "error": "preview_state_invalid", "detail": "preview_state.mock_data must be an object"}
    page_schema = preview_state.get("page_schema")
    if page_schema is None:
        return {"ok": True}
    if not isinstance(page_schema, Mapping):
        return {"ok": False, "error": "page_schema_invalid", "detail": "preview_state.page_schema must be an object"}
    widgets = page_schema.get("widgets")
    if not isinstance(widgets, list) or not widgets:
        return {"ok": False, "error": "page_schema_invalid", "detail": "preview_state.page_schema.widgets must be a non-empty array"}
    for index, widget in enumerate(widgets):
        if not isinstance(widget, Mapping):
            return {"ok": False, "error": "page_schema_invalid", "detail": f"widgets[{index}] must be an object"}
        if not str(widget.get("id") or "").strip():
            return {"ok": False, "error": "page_schema_invalid", "detail": f"widgets[{index}].id is required"}
        if not str(widget.get("type") or "").strip():
            return {"ok": False, "error": "page_schema_invalid", "detail": f"widgets[{index}].type is required"}
    return {"ok": True}


def _iter_mapping_nodes(value: Any, path: str = "$") -> Iterable[tuple[str, Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        yield path, value
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield from _iter_mapping_nodes(child, child_path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_mapping_nodes(child, f"{path}[{index}]")


def _iter_text_nodes(value: Any, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _iter_text_nodes(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_text_nodes(child, f"{path}[{index}]")


def _validate_webui_text_integrity(payload: Mapping[str, Any]) -> dict[str, Any]:
    application = _extract_webui_application(payload)
    for path, text in _iter_text_nodes(application, "$.ui.application"):
        if re.search(r"\?{4,}", text):
            snippet = text[:80].replace("\n", "\\n")
            return {
                "ok": False,
                "error": "text_encoding_suspect",
                "detail": f"{path} contains a long run of question marks; probable encoding loss: {snippet!r}",
            }
    return {"ok": True}


def _modal_id_from_open_modal_action(action: Mapping[str, Any]) -> str:
    params = action.get("params") if isinstance(action.get("params"), Mapping) else {}
    for candidate in (params.get("modalId"), params.get("modal_id"), action.get("modalId"), action.get("openModal")):
        modal_id = str(candidate or "").strip()
        if modal_id:
            return modal_id
    return ""


def _validate_webui_modal_contracts(payload: Mapping[str, Any]) -> dict[str, Any]:
    root_modals = payload.get("modals")
    if isinstance(root_modals, Mapping) and root_modals:
        return {
            "ok": False,
            "error": "component_contract_invalid",
            "detail": "Modals must be declared under ui.application.modals, not at root modals",
        }

    application = _extract_webui_application(payload)
    desktop = application.get("desktop") if isinstance(application.get("desktop"), Mapping) else {}
    if isinstance(desktop.get("modals"), Mapping) and desktop.get("modals"):
        return {
            "ok": False,
            "error": "component_contract_invalid",
            "detail": "Modals must be declared under ui.application.modals, not ui.application.desktop.modals",
        }

    modals = application.get("modals") if isinstance(application.get("modals"), Mapping) else {}
    declared_modal_ids = {str(modal_id).strip() for modal_id in modals.keys() if str(modal_id).strip()}
    for modal_id, modal in modals.items():
        if not isinstance(modal, Mapping):
            return {
                "ok": False,
                "error": "component_contract_invalid",
                "detail": f"ui.application.modals.{modal_id} must be an object",
            }
        if not isinstance(modal.get("schema"), Mapping):
            return {
                "ok": False,
                "error": "component_contract_invalid",
                "detail": f"ui.application.modals.{modal_id}.schema must be an object",
            }

    for path, node in _iter_mapping_nodes(application, "$.ui.application"):
        if str(node.get("type") or "").strip() != "openModal":
            continue
        modal_id = _modal_id_from_open_modal_action(node)
        if not modal_id:
            return {
                "ok": False,
                "error": "component_contract_invalid",
                "detail": f"{path} opens a modal but params.modalId is missing",
            }
        if modal_id.startswith("$"):
            continue
        if modal_id not in declared_modal_ids:
            return {
                "ok": False,
                "error": "component_contract_invalid",
                "detail": f"{path} opens undeclared modal '{modal_id}'; declare it in ui.application.modals",
            }
    return {"ok": True}


def _validate_page_schema_component_contracts(page_schema: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(page_schema, Mapping):
        return {"ok": False, "error": "page_schema_invalid", "detail": "ui.application.desktop.pageSchema must be an object"}
    widgets = page_schema.get("widgets")
    if not isinstance(widgets, list) or not widgets:
        return {"ok": False, "error": "page_schema_invalid", "detail": "ui.application.desktop.pageSchema.widgets must be a non-empty array"}
    for widget_index, widget in enumerate(widgets):
        if not isinstance(widget, Mapping):
            return {"ok": False, "error": "page_schema_invalid", "detail": f"widgets[{widget_index}] must be an object"}
        if not str(widget.get("id") or "").strip():
            return {"ok": False, "error": "page_schema_invalid", "detail": f"widgets[{widget_index}].id is required"}
        widget_type = str(widget.get("type") or "").strip()
        if not widget_type:
            return {"ok": False, "error": "page_schema_invalid", "detail": f"widgets[{widget_index}].type is required"}
        if widget_type != "ui.form":
            continue
        inputs = widget.get("inputs") if isinstance(widget.get("inputs"), Mapping) else {}
        fields = inputs.get("fields") if isinstance(inputs.get("fields"), list) else []
        for field_index, field in enumerate(fields):
            if not isinstance(field, Mapping):
                return {
                    "ok": False,
                    "error": "component_contract_invalid",
                    "detail": f"widgets[{widget_index}].inputs.fields[{field_index}] must be an object",
                }
            field_type = str(field.get("type") or "").strip().lower()
            if field_type in BUILDER_FORM_CHOICE_FIELD_TYPES and not _field_options_from_any_key(
                field,
                ("options", "choices", "items"),
            ):
                field_id = str(field.get("id") or field_index)
                return {
                    "ok": False,
                    "error": "component_contract_invalid",
                    "detail": f"ui.form field '{field_id}' is {field_type} but options/choices/items are missing or empty",
                }
            if field_type in BUILDER_FORM_GRID_FIELD_TYPES:
                rows = _field_options_from_any_key(field, ("rows",))
                columns = _field_options_from_any_key(field, ("columns", "cols"))
                if not rows or not columns:
                    field_id = str(field.get("id") or field_index)
                    return {
                        "ok": False,
                        "error": "component_contract_invalid",
                        "detail": f"ui.form grid field '{field_id}' is {field_type} but rows and columns are required",
                    }
    return {"ok": True}


def _validate_builder_webui_payload(payload: Mapping[str, Any], preview_state: Mapping[str, Any]) -> dict[str, Any]:
    webui_validation = _validate_webui_payload(payload)
    if not webui_validation.get("ok"):
        return webui_validation
    page_schema = _extract_webui_page_schema(payload)
    if not page_schema:
        return {
            "ok": False,
            "error": "page_schema_missing",
            "detail": "LLM response must contain ui.application.desktop.pageSchema",
        }
    text_validation = _validate_webui_text_integrity(payload)
    if not text_validation.get("ok"):
        return text_validation
    component_validation = _validate_page_schema_component_contracts(page_schema)
    if not component_validation.get("ok"):
        return component_validation
    modal_validation = _validate_webui_modal_contracts(payload)
    if not modal_validation.get("ok"):
        return modal_validation
    preview_validation = _validate_preview_state_payload(preview_state)
    if not preview_validation.get("ok"):
        return preview_validation
    return {
        "ok": True,
        "schema": webui_validation.get("schema") or "adaos.webui.v1",
        "preview_state": "ok",
    }


def _normalise_llm_webui_payload(
    payload: Mapping[str, Any],
    *,
    previous_preview: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    data = copy.deepcopy(dict(payload))
    if len(data) == 1:
        for wrapper_key in ("adaos.webui.v1", "webui", "manifest"):
            wrapped = data.get(wrapper_key)
            if isinstance(wrapped, Mapping):
                data = copy.deepcopy(dict(wrapped))
                break
    legacy_preview = data.get("preview_state") if isinstance(data.get("preview_state"), Mapping) else {}
    page_schema = _extract_webui_page_schema(data)
    if not page_schema:
        raise ValueError("LLM payload must contain ui.application.desktop.pageSchema")
    preview_data = {
        key: copy.deepcopy(value)
        for key, value in dict(previous_preview).items()
        if key != "current_ui"
    }
    if isinstance(legacy_preview, Mapping):
        for key in ("datasources", "mock_data", "filters", "form_action_position", "layout_order", "card_preview_key"):
            if key in legacy_preview:
                preview_data[key] = copy.deepcopy(legacy_preview[key])
        if legacy_preview.get("title"):
            preview_data["title"] = copy.deepcopy(legacy_preview.get("title"))
    preview_data["page_schema"] = page_schema
    if isinstance(page_schema.get("title"), str) and page_schema.get("title").strip():
        preview_data["title"] = str(page_schema.get("title")).strip()
    if not preview_data.get("title") and previous_preview.get("title"):
        preview_data["title"] = copy.deepcopy(previous_preview.get("title"))
    if not isinstance(preview_data.get("datasources"), list):
        preview_data["datasources"] = copy.deepcopy(previous_preview.get("datasources") or [])
    if not isinstance(preview_data.get("mock_data"), Mapping):
        preview_data["mock_data"] = copy.deepcopy(previous_preview.get("mock_data") or {})
    for key in ("session_id", "title", "version"):
        if not preview_data.get(key) and previous_preview.get(key):
            preview_data[key] = copy.deepcopy(previous_preview.get(key))
    data = _canonical_webui_payload(data, page_schema)
    return data, preview_data


def _merge_session_from_preview(session: dict[str, Any], preview_state: Mapping[str, Any]) -> None:
    preview_state = _repair_text_tree(dict(preview_state))
    title = str(preview_state.get("title") or "").strip()
    if title:
        session["title"] = title
    datasources = preview_state.get("datasources") if isinstance(preview_state.get("datasources"), list) else []
    datasource = datasources[0] if datasources and isinstance(datasources[0], Mapping) else {}
    if datasource:
        datasource_id = str(datasource.get("id") or "").strip()
        if datasource_id:
            session["datasource_id"] = datasource_id
        fields = [dict(item) for item in datasource.get("fields", []) if isinstance(item, Mapping)]
        if fields:
            session["fields"] = fields
    mock_data = preview_state.get("mock_data") if isinstance(preview_state.get("mock_data"), Mapping) else {}
    datasource_id = str(session.get("datasource_id") or "items")
    rows = mock_data.get(datasource_id)
    if isinstance(rows, list):
        session["mock_rows"] = [dict(item) for item in rows if isinstance(item, Mapping)]
    filters = preview_state.get("filters") if isinstance(preview_state.get("filters"), list) else None
    if filters is not None:
        session["filters"] = [dict(item) for item in filters if isinstance(item, Mapping)]
    ui = preview_state.get("current_ui") if isinstance(preview_state.get("current_ui"), Mapping) else {}
    children = ui.get("children") if isinstance(ui.get("children"), list) else []
    session["card_view"] = any(
        isinstance(child, Mapping) and str(child.get("type") or "") == "card_list" and child.get("visible") is not False
        for child in children
    )
    card_child = next(
        (
            child
            for child in children
            if isinstance(child, Mapping) and str(child.get("type") or "") == "card_list" and child.get("visible") is not False
        ),
        {},
    )
    preview_key = str(preview_state.get("card_preview_key") or "").strip()
    if not preview_key and isinstance(card_child, Mapping):
        preview_key = _card_key_from_template(card_child.get("preview"))
    if preview_key:
        session["card_preview_key"] = preview_key
    table_children = [
        child
        for child in children
        if isinstance(child, Mapping) and (str(child.get("type") or "") == "table" or str(child.get("id") or "") == "items_table")
    ]
    session["hide_table"] = bool(
        (table_children and table_children[0].get("visible") is False)
        or (not table_children and session.get("card_view"))
    )
    editor = next(
        (
            child
            for child in children
            if isinstance(child, Mapping) and str(child.get("id") or "") == "editor"
        ),
        {},
    )
    action_position = str(editor.get("action_position") or preview_state.get("form_action_position") or "").strip().lower() if isinstance(editor, Mapping) else ""
    if action_position:
        session["form_action_position"] = "top" if action_position == "top" else "bottom"
    layout_order = str(preview_state.get("layout_order") or ui.get("layout_order") or "").strip().lower()
    page_schema = preview_state.get("page_schema") if isinstance(preview_state.get("page_schema"), Mapping) else {}
    widgets = page_schema.get("widgets") if isinstance(page_schema.get("widgets"), list) else []
    if not layout_order and widgets:
        form_widget = next((item for item in widgets if isinstance(item, Mapping) and str(item.get("id") or "") == "prototype-form"), {})
        cards_widget = next((item for item in widgets if isinstance(item, Mapping) and str(item.get("id") or "") == "prototype-cards"), {})
        if isinstance(form_widget, Mapping) and isinstance(cards_widget, Mapping):
            if str(cards_widget.get("area") or "") == "main" and str(form_widget.get("area") or "") == "right":
                layout_order = "cards_first"
            inputs = cards_widget.get("inputs") if isinstance(cards_widget.get("inputs"), Mapping) else {}
            schema_preview_key = _card_key_from_template(inputs.get("previewKey"))
            if schema_preview_key:
                session["card_preview_key"] = schema_preview_key
    if layout_order:
        session["layout_order"] = "cards_first" if layout_order in {"cards_first", "cards-first", "cards_left", "cards-left", "cards_main", "cards-main"} else "input_first"


def _apply_llm_webui_transform(
    *,
    session: Mapping[str, Any],
    instruction: str,
    preview_state: Mapping[str, Any],
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request = _builder_llm_webui_transform_request(
        session=session,
        instruction=instruction,
        preview_state=preview_state,
        _meta=_meta,
    )
    current_payload = request["current_payload"]
    system_prompt = str(request["system_prompt"])
    user_prompt = str(request["user_prompt"])
    base_request = request["base_request"]
    attempts: list[dict[str, Any]] = []
    last_response = ""
    last_error: dict[str, Any] | None = None
    try:
        from adaos.sdk.llm.llm_client import send_response

        timeout_s = _builder_llm_timeout_s()
        max_tokens = _builder_llm_max_tokens()
        temperature = _builder_llm_temperature()
        for attempt in range(1, 3):
            request_id = _builder_llm_request_id(
                session=session,
                instruction=instruction,
                current_payload=current_payload,
                attempt=attempt,
            )
            if attempt == 1:
                messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
            else:
                repair_prompt = _compact_json(
                    {
                        "task": "Repair the previous Builder JSON response. Return only corrected JSON.",
                        "validation_error": last_error or {},
                        "previous_response": last_response[:20000],
                        "original_request": base_request,
                    }
                )
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": repair_prompt},
                ]
            try:
                response = send_response(
                    messages,
                    model=_builder_llm_model_for_session(session, _meta),
                    temperature=temperature if attempt == 1 else 0,
                    max_tokens=max_tokens,
                    request_id=request_id,
                    timeout=timeout_s,
                )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                last_error = {
                    "ok": False,
                    "error": "llm_timeout" if _looks_like_timeout(detail) else "llm_request_failed",
                    "detail": detail,
                    "request_id": request_id,
                    "timeout_s": timeout_s,
                }
                attempts.append({"attempt": attempt, **last_error})
                break
            output_text = str(response.get("output_text") or "")
            last_response = output_text
            try:
                parsed = _extract_json_object(output_text)
                payload, preview = _normalise_llm_webui_payload(
                    parsed,
                    previous_preview=current_payload.get("preview_state")
                    if isinstance(current_payload.get("preview_state"), Mapping)
                    else preview_state,
                )
                validation = _validate_builder_webui_payload(payload, preview)
                attempts.append({"attempt": attempt, "ok": bool(validation.get("ok")), "request_id": request_id, "validation": validation})
                if not validation.get("ok"):
                    last_error = dict(validation)
                    last_error["request_id"] = request_id
                    continue
                return {
                    "ok": True,
                    "payload": payload,
                    "preview_state": preview,
                    "comment": str(parsed.get("comment") or parsed.get("summary") or "").strip(),
                    "unable_reason": str(parsed.get("unable_reason") or "").strip(),
                    "validation": validation,
                    "attempts": attempts,
                    "raw_response": output_text,
                }
            except Exception as exc:
                last_error = {
                    "ok": False,
                    "error": "llm_response_parse_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "request_id": request_id,
                }
                attempts.append({"attempt": attempt, **last_error})
        return {
            "ok": False,
            "error": str((last_error or {}).get("error") or "llm_webui_transform_invalid"),
            "detail": str((last_error or {}).get("detail") or "LLM response did not pass Builder validation"),
            "validation": last_error or {},
            "attempts": attempts,
            "last_response": last_response,
            "comment": "\u041d\u0435 \u0441\u043c\u043e\u0433 \u0441\u043e\u0431\u0440\u0430\u0442\u044c \u0432\u0430\u043b\u0438\u0434\u043d\u044b\u0439 UI JSON.",
        }
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        timeout = _looks_like_timeout(detail)
        return {
            "ok": False,
            "error": "llm_timeout" if timeout else "llm_webui_transform_failed",
            "detail": detail,
            "attempts": attempts,
            "last_response": last_response,
            "comment": "\u041d\u0435 \u0434\u043e\u0436\u0434\u0430\u043b\u0441\u044f \u043e\u0442\u0432\u0435\u0442\u0430 LLM." if timeout else "",
        }


def _parse_llm_webui_transform_output(
    *,
    output_text: str,
    previous_preview: Mapping[str, Any],
    request_id: str = "",
    job_id: str = "",
) -> dict[str, Any]:
    parsed = _extract_json_object(output_text)
    payload, preview = _normalise_llm_webui_payload(parsed, previous_preview=previous_preview)
    validation = _validate_builder_webui_payload(payload, preview)
    if not validation.get("ok"):
        return {
            "ok": False,
            "error": "llm_webui_transform_invalid",
            "detail": "LLM response did not pass Builder validation",
            "validation": validation,
            "attempts": [
                {
                    "attempt": 1,
                    "ok": False,
                    "request_id": request_id,
                    "job_id": job_id,
                    "validation": validation,
                }
            ],
            "last_response": output_text,
            "comment": "\u041d\u0435 \u0441\u043c\u043e\u0433 \u0441\u043e\u0431\u0440\u0430\u0442\u044c \u0432\u0430\u043b\u0438\u0434\u043d\u044b\u0439 UI JSON.",
        }
    return {
        "ok": True,
        "payload": payload,
        "preview_state": preview,
        "comment": str(parsed.get("comment") or parsed.get("summary") or "").strip(),
        "unable_reason": str(parsed.get("unable_reason") or "").strip(),
        "validation": validation,
        "attempts": [
            {
                "attempt": 1,
                "ok": True,
                "request_id": request_id,
                "job_id": job_id,
                "validation": validation,
            }
        ],
        "raw_response": output_text,
    }


def _repair_llm_webui_transform_output(
    *,
    session: Mapping[str, Any],
    instruction: str,
    previous_preview: Mapping[str, Any],
    output_text: str,
    validation_error: Mapping[str, Any],
    request_id: str = "",
    job_id: str = "",
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request = _builder_llm_webui_transform_request(
        session=session,
        instruction=instruction,
        preview_state=previous_preview,
        _meta=_meta,
    )
    repair_request_id = _builder_llm_request_id(
        session=session,
        instruction=instruction,
        current_payload=request["current_payload"],
        attempt=2,
    )
    repair_prompt = _compact_json(
        {
            "task": "Repair the previous Builder JSON response. Return only corrected JSON.",
            "validation_error": dict(validation_error),
            "previous_response": str(output_text or "")[:20000],
            "original_request": request["base_request"],
            "required_output_shape": {
                "schema": "adaos.webui.v1",
                "ui": {
                    "application": {
                        "desktop": {
                            "pageSchema": "complete AdaOS pageSchema object with id, layout, and widgets"
                        }
                    }
                },
                "comment": "short user-facing text",
                "unable_reason": "optional diagnostic",
            },
        }
    )
    try:
        from adaos.sdk.llm.llm_client import send_response

        response = send_response(
            [
                {"role": "system", "content": str(request["system_prompt"])},
                {"role": "user", "content": repair_prompt},
            ],
            model=_builder_llm_model_for_session(session, _meta),
            temperature=0,
            max_tokens=_builder_llm_max_tokens(),
            request_id=repair_request_id,
            timeout=_builder_llm_timeout_s(),
        )
        repaired_output = str(response.get("output_text") or "")
    except Exception as exc:
        return {
            "ok": False,
            "error": "llm_webui_transform_repair_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "attempts": [
                {
                    "attempt": 1,
                    "ok": False,
                    "request_id": request_id,
                    "job_id": job_id,
                    "validation": dict(validation_error),
                },
                {
                    "attempt": 2,
                    "ok": False,
                    "request_id": repair_request_id,
                    "error": "repair_request_failed",
                },
            ],
            "last_response": str(output_text or "")[:12000],
            "comment": "Не смог собрать валидный UI JSON.",
        }
    try:
        result = _parse_llm_webui_transform_output(
            output_text=repaired_output,
            previous_preview=previous_preview,
            request_id=repair_request_id,
            job_id=job_id,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "llm_webui_transform_repair_invalid",
            "detail": f"{type(exc).__name__}: {exc}",
            "attempts": [
                {
                    "attempt": 1,
                    "ok": False,
                    "request_id": request_id,
                    "job_id": job_id,
                    "validation": dict(validation_error),
                },
                {
                    "attempt": 2,
                    "ok": False,
                    "request_id": repair_request_id,
                    "job_id": job_id,
                    "validation": {"error": f"{type(exc).__name__}: {exc}"},
                },
            ],
            "last_response": repaired_output[:12000] or str(output_text or "")[:12000],
            "comment": "Не смог собрать валидный UI JSON.",
        }
    initial_attempt = {
        "attempt": 1,
        "ok": False,
        "request_id": request_id,
        "job_id": job_id,
        "validation": dict(validation_error),
    }
    attempts = [initial_attempt]
    for item in (result.get("attempts") if isinstance(result.get("attempts"), list) else []):
        if isinstance(item, Mapping):
            attempts.append(dict(item))
    result["attempts"] = attempts
    result["raw_response"] = repaired_output
    result["repair"] = {
        "schema": "adaos.builder.llm_repair.v1",
        "request_id": repair_request_id,
        "repaired": True,
    }
    return result


def _workbench_service():
    from adaos.services.builder.workbench import BuilderWorkbenchService

    return BuilderWorkbenchService.from_context()


def _request_workbench_refresh(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from adaos.sdk.data import events

        events.publish(WORKBENCH_REFRESH_TOPIC, payload, source=SKILL_ID)
        return {"ok": True, "topic": WORKBENCH_REFRESH_TOPIC}
    except Exception as exc:
        return {"ok": False, "topic": WORKBENCH_REFRESH_TOPIC, "error": f"{type(exc).__name__}: {exc}"}


def _publish_prompt_selection_async(payload: Mapping[str, Any]) -> dict[str, Any]:
    safe_payload = dict(payload)

    def _runner() -> None:
        try:
            from adaos.sdk.data import events

            for topic in PROMPT_SELECTION_ASYNC_TOPICS:
                try:
                    events.publish(topic, safe_payload, source=SKILL_ID)
                except Exception:
                    continue
        except Exception:
            return

    thread = threading.Thread(target=_runner, name="builder-prompt-selection-events", daemon=True)
    thread.start()
    return {"ok": True, "mode": "thread", "topics": list(PROMPT_SELECTION_ASYNC_TOPICS)}


def _publish_prompt_project_changed(
    webspace_id: str,
    *,
    session: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    scenario_id = str(session.get("scenario_id") or "").strip()
    if not scenario_id:
        return {"ok": False, "error": "scenario_id_missing"}
    payload = {
        "source_webspace_id": webspace_id,
        "webspace_id": webspace_id,
        "object_type": "scenario",
        "object_id": scenario_id,
        "scenario_id": scenario_id,
        "draft_id": str(session.get("draft_id") or "").strip() or None,
        "reason": reason,
    }
    try:
        from adaos.sdk.data import events

        events.publish("prompt.project.changed", payload, source=SKILL_ID)
        return {"ok": True, "topic": "prompt.project.changed", "payload": payload}
    except Exception as exc:
        return {"ok": False, "topic": "prompt.project.changed", "error": f"{type(exc).__name__}: {exc}", "payload": payload}


def _publish_prompt_project_selection(
    webspace_id: str,
    *,
    session: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    scenario_id = str(session.get("scenario_id") or "").strip()
    if not scenario_id:
        return {"ok": False, "error": "scenario_id_missing"}
    payload_base = {
        "source_webspace_id": webspace_id,
        "webspace_id": webspace_id,
        "object_type": "scenario",
        "object_id": scenario_id,
        "scenario_id": scenario_id,
        "draft_id": str(session.get("draft_id") or "").strip() or None,
        "reason": reason,
    }
    try:
        from adaos.sdk.data import events

        events.publish(
            "scenario.workflow.set_state",
            {
                "state": "tz",
                **payload_base,
                "scenario_id": PROMPT_IDE_SCENARIO_ID,
                "selected_scenario_id": scenario_id,
            },
            source=SKILL_ID,
        )
        scheduled = _publish_prompt_selection_async(payload_base)
        return {
            "ok": True,
            "published": ["scenario.workflow.set_state"],
            "scheduled": scheduled.get("topics") or [],
            "schedule": scheduled,
            "payload": payload_base,
        }
    except Exception as exc:
        return {"ok": False, "error": "prompt_project_selection_publish_failed", "detail": f"{type(exc).__name__}: {exc}"}


def _active_draft_id(session: Mapping[str, Any] | None) -> str | None:
    if not isinstance(session, Mapping):
        return None
    if not str(session.get("artifact_root") or "").strip():
        return None
    return str(session.get("draft_id") or session.get("id") or "").strip() or None


def _runtime_scenario_id(session: Mapping[str, Any] | None) -> str | None:
    if not isinstance(session, Mapping):
        return None
    if not str(session.get("artifact_root") or "").strip():
        return None
    return str(session.get("scenario_id") or "").strip() or None


def _workbench_binding(webspace_id: str) -> dict[str, Any]:
    try:
        binding = _workbench_service().get_workspace_binding(webspace_id)
        return dict(binding) if isinstance(binding, Mapping) else {}
    except Exception:
        return {}


def _existing_dir_path(value: Any) -> str | None:
    token = str(value or "").strip()
    if not token:
        return None
    try:
        path = Path(token).expanduser()
    except Exception:
        return None
    try:
        if path.exists() and path.is_dir():
            return str(path.resolve())
    except Exception:
        return None
    return None


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _artifact_root_from_draft_payload(payload: Mapping[str, Any]) -> str | None:
    artifact = payload.get("artifact") if isinstance(payload.get("artifact"), Mapping) else {}
    for raw in (
        payload.get("artifact_root"),
        payload.get("draft_root"),
        artifact.get("draft_root"),
        artifact.get("root"),
        artifact.get("artifact_root"),
    ):
        resolved = _existing_dir_path(raw)
        if resolved:
            return resolved
    return None


def _builder_draft_payloads(session: Mapping[str, Any], binding: Mapping[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    draft_ids = [
        session.get("draft_id"),
        session.get("id"),
        binding.get("active_draft_id"),
    ]
    for draft_id in draft_ids:
        token = str(draft_id or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        try:
            from adaos.services.runtime_paths import current_state_dir

            payload = _read_json_file(current_state_dir() / "builder" / "drafts" / token / "builder.draft.json")
        except Exception:
            payload = {}
        if payload:
            payloads.append(payload)
    for root in (session.get("artifact_root"), binding.get("artifact_root"), binding.get("draft_root"), binding.get("root")):
        resolved = _existing_dir_path(root)
        if not resolved:
            continue
        payload = _read_json_file(Path(resolved) / "builder.draft.json")
        if payload:
            payloads.append(payload)
    return payloads


def _scenario_artifact_root_from_id(scenario_id: str) -> str | None:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(scenario_id or "").strip()).strip("._-")
    if not token:
        return None
    try:
        from adaos.services.runtime_paths import current_repo_root

        repo_root = current_repo_root()
    except Exception:
        repo_root = Path.cwd()
    if repo_root is None:
        repo_root = Path.cwd()
    dev_root = Path(repo_root) / ".adaos" / "dev"
    try:
        for path in sorted(dev_root.glob(f"*/scenarios/{token}")):
            resolved = _existing_dir_path(path)
            if resolved:
                return resolved
    except Exception:
        return None
    return None


def _ensure_session_artifact_root(session: dict[str, Any], binding: Mapping[str, Any]) -> bool:
    current = _existing_dir_path(session.get("artifact_root"))
    if current:
        if session.get("artifact_root") != current:
            session["artifact_root"] = current
            return True
        return False
    for raw in (binding.get("artifact_root"), binding.get("draft_root"), binding.get("root")):
        resolved = _existing_dir_path(raw)
        if resolved:
            session["artifact_root"] = resolved
            return True
    for payload in _builder_draft_payloads(session, binding):
        resolved = _artifact_root_from_draft_payload(payload)
        if resolved:
            session["artifact_root"] = resolved
            artifact = payload.get("artifact") if isinstance(payload.get("artifact"), Mapping) else {}
            draft_id = str(payload.get("draft_id") or "").strip()
            scenario_id = str(artifact.get("id") or "").strip()
            if draft_id and not str(session.get("draft_id") or "").strip():
                session["draft_id"] = draft_id
            if scenario_id and not str(session.get("scenario_id") or "").strip():
                session["scenario_id"] = scenario_id
            return True
    scenario_root = _scenario_artifact_root_from_id(str(session.get("scenario_id") or binding.get("runtime_scenario_id") or ""))
    if scenario_root:
        session["artifact_root"] = scenario_root
        return True
    return False


def _sync_session_from_artifacts(session: dict[str, Any], binding: Mapping[str, Any] | None = None) -> bool:
    changed = False
    if binding is not None:
        changed = _ensure_session_artifact_root(session, binding) or changed
    root = _project_artifact_root(session)
    if root is None:
        return changed

    revision = ""
    current_revision = root / "ui_revisions" / "current.txt"
    try:
        revision = current_revision.read_text(encoding="utf-8").strip()
    except Exception:
        revision = ""
    if revision:
        if str(session.get("ui_revision") or "").strip() != revision:
            session["ui_revision"] = revision
            changed = True
        if str(session.get("version") or "").strip() != revision:
            session["version"] = revision
            changed = True
        revision_path = root / "ui_revisions" / f"{revision}.json"
        if revision_path.exists():
            revisions = [dict(item) for item in session.get("ui_revisions", []) if isinstance(item, Mapping)]
            if not any(str(item.get("revision") or "").strip() == revision for item in revisions):
                revisions.append({"revision": revision, "path": str(revision_path)})
                session["ui_revisions"] = revisions[-20:]
                changed = True

    webui = _read_json_file(root / "webui.json")
    if webui:
        if session.get("webui_payload") != webui:
            session["webui_payload"] = copy.deepcopy(webui)
            changed = True
        page_schema = _extract_webui_page_schema(webui)
        if page_schema:
            title = str(page_schema.get("title") or session.get("title") or "").strip()
            if title and str(session.get("title") or "").strip() != title:
                session["title"] = title
                changed = True
            preview = session.get("preview_state") if isinstance(session.get("preview_state"), Mapping) else {}
            next_preview = dict(preview)
            next_preview.update(
                {
                    "session_id": session.get("id"),
                    "scenario_id": session.get("scenario_id"),
                    "title": session.get("title"),
                    "page_schema": page_schema,
                    "version": str(session.get("ui_revision") or session.get("version") or ""),
                }
            )
            if preview != next_preview:
                session["preview_state"] = next_preview
                changed = True
    return changed


def _session_from_binding(webspace_id: str, binding: Mapping[str, Any]) -> dict[str, Any] | None:
    scenario_id = str(binding.get("runtime_scenario_id") or "").strip()
    draft_id = str(binding.get("active_draft_id") or "").strip()
    if not scenario_id and not draft_id:
        return None
    session: dict[str, Any] = {
        "id": draft_id or f"scenario.{scenario_id}",
        "draft_id": draft_id or None,
        "scenario_id": scenario_id or None,
        "title": scenario_id.replace("_", " ").title() if scenario_id else "Builder Prototype",
        "fields": [],
        "patches": [],
        "ui_revisions": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    if not _ensure_session_artifact_root(session, binding):
        return None
    root = _project_artifact_root(session)
    if root is not None:
        draft_payload = _read_json_file(root / "builder.draft.json")
        artifact = draft_payload.get("artifact") if isinstance(draft_payload.get("artifact"), Mapping) else {}
        metadata = draft_payload.get("metadata") if isinstance(draft_payload.get("metadata"), Mapping) else {}
        if not str(session.get("draft_id") or "").strip():
            session["draft_id"] = str(draft_payload.get("draft_id") or "").strip() or session.get("draft_id")
        if not str(session.get("scenario_id") or "").strip():
            session["scenario_id"] = str(artifact.get("id") or "").strip() or session.get("scenario_id")
        source = draft_payload.get("source") if isinstance(draft_payload.get("source"), Mapping) else {}
        session["source_idea"] = str(
            source.get("utterance")
            or metadata.get("source_idea")
            or session.get("source_idea")
            or ""
        )
        _sync_session_from_artifacts(session)
    key = str(session.get("id") or session.get("draft_id") or session.get("scenario_id") or "").strip()
    if not key:
        return None
    session["id"] = key
    _save_session(webspace_id, session)
    return session


def _session_matches_binding(session: Mapping[str, Any], binding: Mapping[str, Any]) -> bool:
    draft_id = str(binding.get("active_draft_id") or "").strip()
    scenario_id = str(binding.get("runtime_scenario_id") or "").strip()
    if draft_id and str(session.get("draft_id") or session.get("id") or "").strip() == draft_id:
        return True
    if scenario_id and str(session.get("scenario_id") or "").strip() == scenario_id:
        return True
    return not draft_id and not scenario_id


def _target_session(webspace_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    binding = _workbench_binding(webspace_id)
    draft_id = str(binding.get("active_draft_id") or "").strip()
    scenario_id = str(binding.get("runtime_scenario_id") or "").strip()
    sessions = _sessions(webspace_id)
    def resolved(session: Mapping[str, Any]) -> dict[str, Any]:
        item = copy.deepcopy(dict(session))
        changed = _sync_session_from_artifacts(item, binding)
        if changed and item.get("id"):
            sessions[str(item["id"])] = item
            _save_sessions(webspace_id, sessions)
        return item

    if draft_id or scenario_id:
        for session in sessions.values():
            if draft_id and str(session.get("draft_id") or session.get("id") or "").strip() == draft_id:
                return resolved(session), binding
            if scenario_id and str(session.get("scenario_id") or "").strip() == scenario_id:
                return resolved(session), binding
        return _session_from_binding(webspace_id, binding), binding
    session = _load_session(webspace_id)
    if session and _session_matches_binding(session, binding):
        if _sync_session_from_artifacts(session, binding):
            _save_session(webspace_id, session)
        return session, binding
    return None, binding


def _target_required_message(binding: Mapping[str, Any] | None = None) -> str:
    scenario_id = str((binding or {}).get("runtime_scenario_id") or "").strip()
    if scenario_id:
        return (
            f"{AGENT_LABEL}: \u0432 Prompt IDE \u0432\u044b\u0431\u0440\u0430\u043d \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 {scenario_id}, "
            "\u043d\u043e \u044f \u043d\u0435 \u0432\u0438\u0436\u0443 \u0434\u043b\u044f \u043d\u0435\u0433\u043e Builder-\u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a. "
            "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 Builder-\u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a \u0438\u043b\u0438 \u0441\u043e\u0437\u0434\u0430\u0439\u0442\u0435 \u043d\u043e\u0432\u044b\u0439: "
            "\u00ab\u0421\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c, \u0441\u043e\u0437\u0434\u0430\u0439 ...\u00bb."
        )
    return (
        f"{AGENT_LABEL}: \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043e\u0431\u044a\u0435\u043a\u0442 \u0434\u043b\u044f \u0434\u043e\u0440\u0430\u0431\u043e\u0442\u043a\u0438 "
        "\u0432 Prompt IDE (\u043d\u0430\u0432\u044b\u043a \u0438\u043b\u0438 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439). "
        "\u0415\u0441\u043b\u0438 \u043d\u0443\u0436\u0435\u043d \u043d\u043e\u0432\u044b\u0439 \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f, \u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435: "
        "\u00ab\u0421\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c, \u0441\u043e\u0437\u0434\u0430\u0439 ...\u00bb."
    )


def _normalized_builder_phrase(text: str) -> str:
    phrase = re.sub(r"\s+", " ", str(text or "").strip().lower()).strip(" .!?;:")
    for alias in ("builder", "\u0441\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c", "\u0431\u0438\u043b\u0434\u0435\u0440"):
        if phrase == alias:
            return ""
        for separator in (", ", ": ", " - "):
            prefix = f"{alias}{separator}"
            if phrase.startswith(prefix):
                return phrase[len(prefix) :].strip()
    return phrase


def _is_guided_clarification_request(text: str) -> bool:
    phrase = _normalized_builder_phrase(text)
    if not phrase:
        return False
    exact_vague_phrases = {
        "i have an idea",
        "i've got an idea",
        "there is an idea",
        "help me shape an idea",
        "help me build something",
        "\u0435\u0441\u0442\u044c \u0438\u0434\u0435\u044f",
        "\u0443 \u043c\u0435\u043d\u044f \u0435\u0441\u0442\u044c \u0438\u0434\u0435\u044f",
        "\u0434\u0430\u0432\u0430\u0439 \u0447\u0442\u043e-\u043d\u0438\u0431\u0443\u0434\u044c \u0441\u043e\u0431\u0435\u0440\u0435\u043c",
        "\u0434\u0430\u0432\u0430\u0439 \u0447\u0442\u043e-\u043d\u0438\u0431\u0443\u0434\u044c \u0441\u0434\u0435\u043b\u0430\u0435\u043c",
        "\u043f\u043e\u043c\u043e\u0433\u0438 \u0441\u0444\u043e\u0440\u043c\u0443\u043b\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0438\u0434\u0435\u044e",
    }
    if phrase in exact_vague_phrases:
        return True
    vague_starts = (
        "i have an idea for",
        "i want to build something",
        "\u0445\u043e\u0447\u0443 \u0441\u0434\u0435\u043b\u0430\u0442\u044c \u0447\u0442\u043e-\u0442\u043e",
        "\u043d\u0443\u0436\u043d\u043e \u0441\u043e\u0431\u0440\u0430\u0442\u044c \u0447\u0442\u043e-\u0442\u043e",
    )
    return any(phrase.startswith(item) for item in vague_starts)


def _builder_clarification_payload(
    *,
    text: str,
    webspace_id: str,
    topic: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "adaos.builder.guided_clarification.v1",
        "status": "clarification_required",
        "source_text": str(text or "").strip(),
        "webspace_id": webspace_id,
        "topic": dict(topic or {}),
        "questions": [
            {
                "id": "user_goal",
                "label": "\u0426\u0435\u043b\u044c",
                "prompt": "\u041a\u0430\u043a\u0443\u044e \u0437\u0430\u0434\u0430\u0447\u0443 \u0434\u043e\u043b\u0436\u0435\u043d \u0440\u0435\u0448\u0430\u0442\u044c \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f?",
                "required": True,
            },
            {
                "id": "primary_objects",
                "label": "\u0414\u0430\u043d\u043d\u044b\u0435",
                "prompt": "\u041a\u0430\u043a\u0438\u0435 \u043e\u0431\u044a\u0435\u043a\u0442\u044b, \u043f\u043e\u043b\u044f \u0438\u043b\u0438 \u0437\u0430\u043f\u0438\u0441\u0438 \u043d\u0443\u0436\u043d\u044b \u043d\u0430 \u043f\u0435\u0440\u0432\u043e\u043c \u044d\u043a\u0440\u0430\u043d\u0435?",
                "required": True,
            },
            {
                "id": "first_action",
                "label": "\u0414\u0435\u0439\u0441\u0442\u0432\u0438\u0435",
                "prompt": "\u041a\u0430\u043a\u043e\u0435 \u043e\u0434\u043d\u043e \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u0434\u043e\u043b\u0436\u0435\u043d \u0441\u0440\u0430\u0437\u0443 \u0441\u043c\u043e\u0447\u044c \u0441\u0434\u0435\u043b\u0430\u0442\u044c?",
                "required": True,
            },
        ],
        "suggested_replies": [
            "\u0421\u0434\u0435\u043b\u0430\u0439 \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f \u0441\u043f\u0438\u0441\u043a\u0430 \u043f\u043e\u043a\u0443\u043f\u043e\u043a: \u0442\u043e\u0432\u0430\u0440, \u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e, \u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f; \u043d\u0443\u0436\u043d\u043e \u0434\u043e\u0431\u0430\u0432\u043b\u044f\u0442\u044c \u0438 \u043e\u0442\u043c\u0435\u0447\u0430\u0442\u044c \u043a\u0443\u043f\u043b\u0435\u043d\u043d\u043e\u0435.",
            "Build a simple task tracker with title, owner, status, due date, and a quick add form.",
        ],
        "next_turn_policy": {
            "creates_draft_when_answered": True,
            "minimum_answer_fields": ["user_goal"],
            "owner": f"skill:{SKILL_ID}",
            "agent_id": AGENT_ID,
        },
    }


def _guided_clarification_message(payload: Mapping[str, Any]) -> str:
    questions = payload.get("questions") if isinstance(payload.get("questions"), list) else []
    rendered = []
    for index, item in enumerate(questions[:3], start=1):
        if isinstance(item, Mapping):
            rendered.append(f"{index}. {item.get('prompt')}")
    return (
        f"{AGENT_LABEL}: \u0438\u0434\u0435\u044e \u043b\u0443\u0447\u0448\u0435 \u0443\u0442\u043e\u0447\u043d\u0438\u0442\u044c \u0434\u043e \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a\u0430.\n\n"
        + "\n".join(rendered)
        + "\n\n\u041c\u043e\u0436\u043d\u043e \u043e\u0442\u0432\u0435\u0442\u0438\u0442\u044c \u043e\u0434\u043d\u043e\u0439 \u0444\u0440\u0430\u0437\u043e\u0439: \u0447\u0442\u043e \u0441\u0442\u0440\u043e\u0438\u043c, \u043a\u0430\u043a\u0438\u0435 \u043f\u043e\u043b\u044f \u043d\u0443\u0436\u043d\u044b, \u0438 \u043a\u0430\u043a\u043e\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u0432\u0430\u0436\u043d\u043e \u043f\u0435\u0440\u0432\u044b\u043c."
    )


def _normalise_command_text(text: str) -> str:
    lowered = str(text or "").strip().lower().replace("\u0451", "\u0435")
    lowered = re.sub(r"^\s*(?:builder|\u0441\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c)\s*[:,;\-]?\s*", "", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _strip_command_ref(value: str) -> str:
    token = str(value or "").strip(" \t\r\n:;,.!?()[]{}\"'\u00ab\u00bb")
    fillers = (
        "\u043d\u0430 ",
        "\u043a ",
        "\u043f\u0440\u043e\u0435\u043a\u0442 ",
        "\u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f ",
        "\u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 ",
        "\u0441\u0446\u0435\u043d\u0430\u0440\u0438\u044e ",
        "\u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a ",
        "\u043d\u0430\u0432\u044b\u043a ",
        "project ",
        "prototype ",
        "scenario ",
        "draft ",
        "skill ",
    )
    changed = True
    while changed:
        changed = False
        lowered = token.lower()
        for filler in fillers:
            if lowered.startswith(filler):
                token = token[len(filler) :].strip(" \t\r\n:;,.!?()[]{}\"'\u00ab\u00bb")
                changed = True
                break
    return token


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _project_words() -> tuple[str, ...]:
    return (
        "\u043f\u0440\u043e\u0435\u043a\u0442",
        "\u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f",
        "\u0441\u0446\u0435\u043d\u0430\u0440",
        "\u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a",
        "\u043d\u0430\u0432\u044b\u043a",
        "project",
        "prototype",
        "scenario",
        "draft",
        "skill",
    )


def _is_explicit_create_request(text: str) -> bool:
    lowered = _normalise_command_text(text)
    explicit_object_create = _has_any(
        lowered,
        (
            "create app",
            "create project",
            "create scenario",
            "create prototype",
            "create skill",
            "build app",
            "build project",
            "build scenario",
            "build prototype",
            "new app",
            "new scenario",
            "new prototype",
            "new skill",
            "\u0441\u043e\u0437\u0434\u0430\u0439 \u043f\u0440\u0438\u043b\u043e\u0436",
            "\u0441\u043e\u0437\u0434\u0430\u0439 \u043f\u0440\u043e\u0435\u043a\u0442",
            "\u0441\u043e\u0437\u0434\u0430\u0439 \u0441\u0446\u0435\u043d\u0430\u0440",
            "\u0441\u043e\u0437\u0434\u0430\u0439 \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f",
            "\u0441\u043e\u0437\u0434\u0430\u0439 \u043d\u0430\u0432\u044b\u043a",
            "\u0441\u0434\u0435\u043b\u0430\u0439 \u043f\u0440\u0438\u043b\u043e\u0436",
            "\u0441\u0434\u0435\u043b\u0430\u0439 \u043f\u0440\u043e\u0435\u043a\u0442",
            "\u0441\u0434\u0435\u043b\u0430\u0439 \u0441\u0446\u0435\u043d\u0430\u0440",
            "\u043d\u043e\u0432\u044b\u0439 \u043f\u0440\u043e\u0435\u043a\u0442",
            "\u043d\u043e\u0432\u044b\u0439 \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f",
            "\u043d\u043e\u0432\u043e\u0435 \u043f\u0440\u0438\u043b\u043e\u0436",
            "\u043d\u043e\u0432\u044b\u0439 \u0441\u0446\u0435\u043d\u0430\u0440",
            "\u043d\u043e\u0432\u044b\u0439 \u043d\u0430\u0432\u044b\u043a",
        ),
    )
    if explicit_object_create:
        return True
    return _has_any(
        lowered,
        (
            "lets build",
            "let's build",
            "build it",
            "\u0441\u0434\u0435\u043b\u0430\u0435\u043c",
            "\u0434\u0430\u0432\u0430\u0439 \u0441\u0434\u0435\u043b",
            "\u0441\u043e\u0431\u0435\u0440",
            "\u043f\u043e\u0441\u0442\u0440\u043e\u0438",
        ),
    )


def _is_edit_like_request(text: str) -> bool:
    lowered = _normalise_command_text(text)
    if not lowered:
        return False
    mutation_words = (
        "add",
        "change",
        "update",
        "remove",
        "delete",
        "group",
        "sample data",
        "mock data",
        "\u0434\u043e\u0431\u0430\u0432",
        "\u0438\u0437\u043c\u0435\u043d",
        "\u043e\u0431\u043d\u043e\u0432",
        "\u0443\u0431\u0435\u0440",
        "\u0443\u0434\u0430\u043b",
        "\u0441\u0433\u0440\u0443\u043f",
        "\u043f\u0440\u0438\u043c\u0435\u0440 \u0434\u0430\u043d\u043d",
    )
    target_words = (
        "field",
        "column",
        "card",
        "cards",
        "data",
        "row",
        "rows",
        "\u043f\u043e\u043b\u0435",
        "\u043a\u043e\u043b\u043e\u043d",
        "\u043a\u0430\u0440\u0442\u043e\u0447",
        "\u0434\u0430\u043d\u043d",
        "\u0441\u0442\u0440\u043e\u043a",
    )
    return _has_any(lowered, mutation_words) and _has_any(lowered, target_words)


def _parse_builder_command(text: str, *, allow_create: bool = True, has_session: bool = False) -> dict[str, Any]:
    raw = str(text or "").strip()
    lowered = _normalise_command_text(raw)
    if not lowered:
        return {"intent": "none"}

    if _has_any(
        lowered,
        (
            "\u0447\u0442\u043e \u0432 \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u043a\u0435",
            "\u043f\u043e\u043a\u0430\u0436\u0438 \u043f\u0440\u043e\u0435\u043a\u0442",
            "\u043f\u043e\u043a\u0430\u0436\u0438 \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f",
            "\u043f\u043e\u043a\u0430\u0436\u0438 \u0447\u0435\u0440\u043d\u043e\u0432",
            "\u0441\u043f\u0438\u0441\u043e\u043a \u043f\u0440\u043e\u0435\u043a\u0442",
            "\u0441\u043f\u0438\u0441\u043e\u043a \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f",
            "\u0441\u043f\u0438\u0441\u043e\u043a \u0447\u0435\u0440\u043d\u043e\u0432",
            "list projects",
            "list drafts",
            "show projects",
            "show drafts",
            "show prototypes",
        ),
    ):
        return {"intent": "project.list", "confidence": 1.0, "source": "deterministic"}

    if _has_any(
        lowered,
        (
            "\u0447\u0442\u043e \u0432\u044b\u0431\u0440\u0430\u043d",
            "\u0447\u0442\u043e \u0441\u0435\u0439\u0447\u0430\u0441 \u0432\u044b\u0431\u0440\u0430\u043d",
            "\u0442\u0435\u043a\u0443\u0449\u0438\u0439 \u043f\u0440\u043e\u0435\u043a\u0442",
            "\u0442\u0435\u043a\u0443\u0449\u0438\u0439 \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f",
            "\u043d\u0430\u0434 \u0447\u0435\u043c \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u043c",
            "current project",
            "current draft",
            "what is selected",
        ),
    ):
        return {"intent": "project.current", "confidence": 1.0, "source": "deterministic"}

    delete_verb = _has_any(lowered, ("delete", "remove project", "\u0443\u0434\u0430\u043b", "\u0441\u043e\u0442\u0440"))
    field_word = _has_any(lowered, ("field", "column", "\u043f\u043e\u043b\u0435", "\u043a\u043e\u043b\u043e\u043d"))
    if delete_verb and not field_word:
        current = _has_any(lowered, ("current", "\u0442\u0435\u043a\u0443\u0449", "\u0432\u044b\u0431\u0440\u0430\u043d"))
        if current or _has_any(lowered, _project_words()):
            ref = ""
            match = re.search(r"(?:delete|remove project|\u0443\u0434\u0430\u043b(?:\u0438|\u0438\u0442\u044c)?|\u0441\u043e\u0442\u0440(?:\u0438|\u0435\u0442\u044c)?)\s+(.+)$", lowered)
            if match:
                ref = _strip_command_ref(match.group(1))
            return {
                "intent": "project.delete",
                "project_ref": "" if current else ref,
                "target": "current" if current else "ref",
                "confidence": 1.0,
                "source": "deterministic",
            }

    for pattern in (
        r"^(?:switch to|select|open)\s+(.+)$",
        r"^(?:\u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447(?:\u0438\u0441\u044c|\u0438|\u0438\u0442\u044c\u0441\u044f)?|\u0432\u044b\u0431\u0435\u0440(?:\u0438|\u0430\u0442\u044c)?|\u043e\u0442\u043a\u0440\u043e\u0439|\u0440\u0430\u0431\u043e\u0442\u0430\u0435\u043c \u0441|\u043f\u0435\u0440\u0435\u0439\u0434\u0438 \u043a)\s+(.+)$",
    ):
        match = re.search(pattern, lowered)
        if match:
            ref = _strip_command_ref(match.group(1))
            if ref:
                return {"intent": "project.switch", "project_ref": ref, "confidence": 1.0, "source": "deterministic"}

    edit_like = _is_edit_like_request(raw)
    if allow_create and not edit_like and (_is_explicit_create_request(raw) or (not has_session and _is_create_request(raw))):
        return {"intent": "project.create", "idea": raw, "confidence": 1.0, "source": "deterministic"}

    return {"intent": "none"}


def _command_hint_message() -> str:
    return (
        f"{AGENT_LABEL}: \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043f\u0440\u043e\u0435\u043a\u0442 \u0438\u043b\u0438 \u0441\u043e\u0437\u0434\u0430\u0439\u0442\u0435 \u043d\u043e\u0432\u044b\u0439. "
        "\u041f\u0440\u0438\u043c\u0435\u0440\u044b: \u00ab\u0441\u043e\u0437\u0434\u0430\u0439 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435 \u0441\u043f\u0438\u0441\u043e\u043a \u043f\u043e\u043a\u0443\u043f\u043e\u043a\u00bb, "
        "\u00ab\u043f\u043e\u043a\u0430\u0436\u0438 \u043f\u0440\u043e\u0435\u043a\u0442\u044b\u00bb, \u00ab\u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0438\u0441\u044c \u043d\u0430 demo_scenario\u00bb."
    )


def _session_ref_values(session: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("id", "draft_id", "scenario_id", "title", "source_idea"):
        value = str(session.get(key) or "").strip()
        if value:
            values.append(value)
    return values


def _safe_ref_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _normalise_command_text(value)).strip("_")


def _session_summary(session: Mapping[str, Any]) -> dict[str, Any]:
    scenario_id = str(session.get("scenario_id") or "").strip()
    draft_id = str(session.get("draft_id") or session.get("id") or "").strip()
    return {
        "session_id": str(session.get("id") or "").strip(),
        "draft_id": draft_id or None,
        "scenario_id": scenario_id or None,
        "title": str(session.get("title") or scenario_id or draft_id or "prototype").strip(),
        "updated_at": session.get("updated_at"),
    }


def _development_sessions(webspace_id: str) -> list[dict[str, Any]]:
    sessions = [dict(item) for item in _sessions(webspace_id).values() if isinstance(item, Mapping)]
    return sorted(sessions, key=lambda item: float(item.get("updated_at") or 0), reverse=True)


def _resolve_project_session(webspace_id: str, project_ref: str, *, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    ref = _strip_command_ref(project_ref)
    if not ref and isinstance(current, Mapping):
        return {"status": "found", "session": copy.deepcopy(dict(current)), "matches": [_session_summary(current)]}
    if not ref:
        return {"status": "not_found", "matches": []}
    ref_norm = _normalise_command_text(ref)
    ref_safe = _safe_ref_token(ref)
    exact: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    for session in _development_sessions(webspace_id):
        values = _session_ref_values(session)
        value_norms = [_normalise_command_text(value) for value in values]
        value_safe = [_safe_ref_token(value) for value in values]
        if ref_norm in value_norms or ref_safe in value_safe:
            exact.append(session)
            continue
        blob_norm = " ".join(value_norms)
        blob_safe = " ".join(value_safe)
        if (ref_norm and ref_norm in blob_norm) or (ref_safe and ref_safe in blob_safe):
            partial.append(session)
    matches = exact or partial
    if len(matches) == 1:
        return {"status": "found", "session": copy.deepcopy(matches[0]), "matches": [_session_summary(matches[0])]}
    if len(matches) > 1:
        return {"status": "ambiguous", "matches": [_session_summary(item) for item in matches[:5]]}
    return {"status": "not_found", "matches": []}


def _builder_command_response(
    *,
    webspace_id: str,
    message: str,
    status: str,
    command: Mapping[str, Any],
    session: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
    topic_ref: Mapping[str, Any] | None = None,
    _meta: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    topic = dict(topic_ref or {}) if isinstance(topic_ref, Mapping) else _builder_topic_ref(webspace_id, session=session, binding=binding, _meta=_meta)
    _safe_emit_chat(message, webspace_id=webspace_id, _meta=_meta, session=session, binding=binding, topic_ref=topic)
    payload: dict[str, Any] = {
        "ok": True,
        "status": status,
        "command": dict(command),
        "message": message,
        "topic": {k: v for k, v in topic.items() if k != "stored"},
        "dialog": _dialog_state(webspace_id, topic_ref=topic),
    }
    if session is not None:
        payload["session"] = dict(session)
        payload["session_id"] = session.get("id")
        payload["scenario_id"] = session.get("scenario_id")
        payload["draft_id"] = session.get("draft_id")
    if binding is not None:
        payload["binding"] = dict(binding)
    if extra:
        payload.update(dict(extra))
    return payload


def _format_project_list(items: list[dict[str, Any]], active_session_id: str | None) -> str:
    if not items:
        return _command_hint_message()
    lines = []
    for item in items[:8]:
        mark = "* " if active_session_id and item.get("session_id") == active_session_id else "- "
        title = str(item.get("title") or item.get("scenario_id") or item.get("draft_id") or "prototype")
        scenario_id = str(item.get("scenario_id") or "")
        draft_id = str(item.get("draft_id") or "")
        ref = scenario_id or draft_id
        lines.append(f"{mark}{title} ({ref})")
    return f"{AGENT_LABEL}: \u043f\u0440\u043e\u0435\u043a\u0442\u044b \u0432 \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u043a\u0435:\n" + "\n".join(lines)


def _handle_project_list_command(
    *,
    webspace_id: str,
    session: Mapping[str, Any] | None,
    binding: Mapping[str, Any],
    topic: Mapping[str, Any],
    command: Mapping[str, Any],
    _meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    items = [_session_summary(item) for item in _development_sessions(webspace_id)]
    message = _format_project_list(items, str((session or {}).get("id") or ""))
    return _builder_command_response(
        webspace_id=webspace_id,
        message=message,
        status="project_list",
        command=command,
        session=session,
        binding=binding,
        topic_ref=topic,
        _meta=_meta,
        extra={"items": items},
    )


def _handle_project_current_command(
    *,
    webspace_id: str,
    session: Mapping[str, Any] | None,
    binding: Mapping[str, Any],
    topic: Mapping[str, Any],
    command: Mapping[str, Any],
    _meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(session, Mapping):
        return _builder_command_response(
            webspace_id=webspace_id,
            message=_command_hint_message(),
            status="target_required",
            command=command,
            binding=binding,
            topic_ref=topic,
            _meta=_meta,
            extra={"needs_selection": True},
        )
    summary = _session_summary(session)
    message = (
        f"{AGENT_LABEL}: \u0441\u0435\u0439\u0447\u0430\u0441 \u0432\u044b\u0431\u0440\u0430\u043d "
        f"{summary.get('title')} ({summary.get('scenario_id') or summary.get('draft_id')})."
    )
    return _builder_command_response(
        webspace_id=webspace_id,
        message=message,
        status="project_current",
        command=command,
        session=session,
        binding=binding,
        topic_ref=topic,
        _meta=_meta,
        extra={"project": summary},
    )


def _handle_project_switch_command(
    *,
    webspace_id: str,
    command: Mapping[str, Any],
    _meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    current, binding = _target_session(webspace_id)
    resolution = _resolve_project_session(webspace_id, str(command.get("project_ref") or ""), current=current)
    if resolution.get("status") != "found":
        topic = _builder_topic_ref(webspace_id, session=current, binding=binding, _meta=_meta)
        if resolution.get("status") == "ambiguous":
            message = f"{AGENT_LABEL}: \u043d\u0430\u0448\u0435\u043b \u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u043f\u0440\u043e\u0435\u043a\u0442\u043e\u0432. \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 id."
            status = "project_ambiguous"
        else:
            message = f"{AGENT_LABEL}: \u043d\u0435 \u043d\u0430\u0448\u0435\u043b \u043f\u0440\u043e\u0435\u043a\u0442 \u00ab{command.get('project_ref') or ''}\u00bb. \u041d\u0430\u043f\u0438\u0448\u0438\u0442\u0435: \u00ab\u043f\u043e\u043a\u0430\u0436\u0438 \u043f\u0440\u043e\u0435\u043a\u0442\u044b\u00bb."
            status = "project_not_found"
        return _builder_command_response(
            webspace_id=webspace_id,
            message=message,
            status=status,
            command=command,
            session=current,
            binding=binding,
            topic_ref=topic,
            _meta=_meta,
            extra={"matches": resolution.get("matches") or []},
        )

    selected = dict(resolution["session"])
    preview = selected.get("preview_state") if isinstance(selected.get("preview_state"), Mapping) else _preview_state(session=selected)
    workbench = _ensure_workbench(webspace_id, session=selected, preview_state=preview)
    binding = workbench.get("binding") if isinstance(workbench.get("binding"), Mapping) else _workbench_binding(webspace_id)
    topic = _builder_topic_ref(webspace_id, session=selected, binding=binding, _meta=_meta)
    selected["preview_state"] = preview
    selected["thread_id"] = str(topic.get("thread_id") or "").strip() or None
    selected["topic_id"] = str(topic.get("topic_id") or "").strip() or None
    selected["topic_ref"] = {k: v for k, v in topic.items() if k != "stored"}
    _save_session(webspace_id, selected)
    prompt_selection = _publish_prompt_project_selection(
        webspace_id,
        session=selected,
        reason="builder_project_switched",
    )
    summary = _session_summary(selected)
    message = f"{AGENT_LABEL}: \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0438\u043b\u0441\u044f \u043d\u0430 {summary.get('title')} ({summary.get('scenario_id') or summary.get('draft_id')})."
    return _builder_command_response(
        webspace_id=webspace_id,
        message=message,
        status="project_switched",
        command=command,
        session=selected,
        binding=binding,
        topic_ref=topic,
        _meta=_meta,
        extra={"project": summary, "workbench": workbench, "prompt_selection": prompt_selection},
    )


def _handle_project_delete_command(
    *,
    webspace_id: str,
    session: Mapping[str, Any] | None,
    binding: Mapping[str, Any],
    topic: Mapping[str, Any],
    command: Mapping[str, Any],
    _meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    resolution = _resolve_project_session(
        webspace_id,
        "" if command.get("target") == "current" else str(command.get("project_ref") or ""),
        current=session,
    )
    if resolution.get("status") != "found":
        message = f"{AGENT_LABEL}: \u043d\u0435 \u043f\u043e\u043d\u044f\u043b, \u043a\u0430\u043a\u043e\u0439 \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a \u0443\u0434\u0430\u043b\u0438\u0442\u044c. \u041f\u0440\u0438\u043c\u0435\u0440: \u00ab\u0443\u0434\u0430\u043b\u0438 \u0442\u0435\u043a\u0443\u0449\u0438\u0439\u00bb."
        return _builder_command_response(
            webspace_id=webspace_id,
            message=message,
            status="target_required",
            command=command,
            session=session,
            binding=binding,
            topic_ref=topic,
            _meta=_meta,
            extra={"matches": resolution.get("matches") or [], "needs_selection": True},
        )
    selected = dict(resolution["session"])
    draft_id = str(selected.get("draft_id") or "").strip()
    if not draft_id:
        message = f"{AGENT_LABEL}: \u0443 {selected.get('scenario_id') or selected.get('id')} \u043d\u0435\u0442 Builder draft id \u0434\u043b\u044f \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u044f."
        return _builder_command_response(
            webspace_id=webspace_id,
            message=message,
            status="delete_not_available",
            command=command,
            session=selected,
            binding=binding,
            topic_ref=topic,
            _meta=_meta,
        )
    delete_patch = {
        "id": f"patch_delete_{_hash_suffix(draft_id + str(_now()))}",
        "target": "builder_draft",
        "operation": "delete_draft",
        "status": "proposed",
        "summary": f"Delete Builder draft {draft_id}",
        "side_effect_class": "local_delete",
        "diff": {"draft_id": draft_id, "scenario_id": selected.get("scenario_id")},
    }
    pending_action = _publish_review_pending_action(
        webspace_id=webspace_id,
        session=selected,
        request_text=str(command.get("raw") or command.get("project_ref") or "delete current draft"),
        kind="builder.scenario_delete.review",
        summary=f"Delete Builder draft {draft_id}",
        _meta=_meta,
        patch=delete_patch,
    )
    if pending_action and pending_action.get("id"):
        selected["pending_action_id"] = pending_action.get("id")
        _save_session(webspace_id, selected)
        message = f"{AGENT_LABEL}: \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u0438\u043b \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u0435 {draft_id}. \u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 Pending Action."
        status = "delete_review_required"
    else:
        message = f"{AGENT_LABEL}: \u043d\u0435 \u0441\u043c\u043e\u0433 \u0441\u043e\u0437\u0434\u0430\u0442\u044c Pending Action \u0434\u043b\u044f \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u044f {draft_id}."
        status = "delete_review_failed"
    return _builder_command_response(
        webspace_id=webspace_id,
        message=message,
        status=status,
        command=command,
        session=selected,
        binding=binding,
        topic_ref=topic,
        _meta=_meta,
        extra={"pending_action": pending_action, "patch": delete_patch},
    )


def _is_create_request(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        token in lowered
        for token in (
            "i have an idea",
            "i've got an idea",
            "lets build",
            "let's build",
            "build it",
            "create",
            "new app",
            "new scenario",
            "app",
            "scenario",
            "skill",
            "prototype",
            "\u0441\u043e\u0437\u0434",
            "\u0441\u0434\u0435\u043b\u0430\u0435\u043c",
            "\u0434\u0430\u0432\u0430\u0439 \u0441\u0434\u0435\u043b",
            "\u0435\u0441\u0442\u044c \u0438\u0434\u0435\u044f",
            "\u0438\u0434\u0435\u044f",
            "\u0441\u043e\u0431\u0435\u0440",
            "\u043f\u043e\u0441\u0442\u0440\u043e\u0438",
            "\u043d\u043e\u0432\u044b\u0439",
            "\u043f\u0440\u0438\u043b\u043e\u0436",
            "\u0441\u0446\u0435\u043d\u0430\u0440",
            "\u043d\u0430\u0432\u044b\u043a",
        )
    )


def _ensure_workbench(
    webspace_id: str,
    *,
    session: Mapping[str, Any] | None = None,
    preview_state: Mapping[str, Any] | None = None,
    active_draft_id: str | None = None,
    runtime_scenario_id: str | None = None,
    refresh_runtime: bool = True,
    snapshot_projection: bool = True,
) -> dict[str, Any]:
    svc = _workbench_service()
    draft_id = str(active_draft_id or _active_draft_id(session) or "").strip() or None
    scenario_id = str(runtime_scenario_id or _runtime_scenario_id(session) or "").strip() or None
    try:
        binding = svc.set_active_draft(
            source_webspace_id=webspace_id,
            active_draft_id=draft_id,
            runtime_scenario_id=scenario_id,
            persist_projection=False,
        )
        if snapshot_projection:
            snapshot = svc.snapshot(webspace_id, preview_state=preview_state)
        else:
            snapshot = {
                "source_webspace_id": webspace_id,
                "preview_state": dict(preview_state or {}),
                "skipped": "snapshot_projection_deferred",
            }
        if refresh_runtime:
            direct = _ensure_workbench_runtime_direct(
                svc,
                webspace_id=webspace_id,
                active_draft_id=draft_id,
                runtime_scenario_id=scenario_id,
                preview_state=preview_state,
            )
            if isinstance(direct.get("binding"), Mapping):
                binding = dict(direct["binding"])
            event = {"ok": True, "skipped": "direct_workbench_ensure"} if direct.get("ok") else _request_workbench_refresh(
                {
                    "source_webspace_id": webspace_id,
                    "active_draft_id": draft_id,
                    "runtime_scenario_id": scenario_id,
                    "preview_state": dict(preview_state or {}),
                }
            )
        else:
            direct = {"ok": False, "skipped": "runtime_refresh_deferred_to_dev_reload"}
            event = {"ok": True, "skipped": "runtime_refresh_deferred_to_dev_reload"}
    except Exception as exc:
        return {"ok": False, "error": "workbench_unavailable", "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "binding": binding,
        "projection": {
            "ok": True,
            "snapshot": snapshot,
            "deferred": True,
            "event": event,
            "direct": direct,
            "snapshot_deferred": not snapshot_projection,
        },
    }


def _ensure_workbench_runtime_direct(
    svc: Any,
    *,
    webspace_id: str,
    active_draft_id: str | None,
    runtime_scenario_id: str | None,
    preview_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if (
        os.getenv("PYTEST_CURRENT_TEST")
        and type(svc).__module__ == "adaos.services.builder.workbench"
        and str(os.getenv("ADAOS_BUILDER_WORKBENCH_IN_TESTS") or "").strip().lower() not in {"1", "true", "yes", "on"}
    ):
        return {"ok": False, "skipped": "actual_workbench_direct_disabled_in_tests"}
    ensure = getattr(svc, "ensure_dev_webspace", None)
    if not callable(ensure):
        return {"ok": False, "skipped": "ensure_dev_webspace_unavailable"}
    try:
        value = ensure(
            webspace_id,
            active_draft_id=active_draft_id,
            runtime_scenario_id=runtime_scenario_id,
            preview_state=preview_state,
            wait_for_rebuild=False,
        )
    except TypeError:
        return {"ok": False, "skipped": "ensure_dev_webspace_signature_mismatch"}
    except Exception as exc:
        return {"ok": False, "error": "ensure_dev_webspace_failed", "detail": f"{type(exc).__name__}: {exc}"}

    if inspect.isawaitable(value):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            def _runner() -> None:
                try:
                    asyncio.run(value)
                except Exception:
                    return

            thread = threading.Thread(target=_runner, name="builder-workbench-ensure", daemon=True)
            thread.start()
            return {"ok": True, "scheduled": True, "mode": "thread"}
        else:
            try:
                loop.create_task(value)
            except Exception as exc:
                return {"ok": False, "error": "ensure_dev_webspace_schedule_failed", "detail": f"{type(exc).__name__}: {exc}"}
            return {"ok": True, "scheduled": True}

    if isinstance(value, Mapping):
        return {"ok": True, "binding": dict(value), "result": dict(value)}
    return {"ok": True, "result": value}


def _schedule_dev_runtime_reload_after_revision(
    webspace_id: str,
    *,
    session: Mapping[str, Any],
    binding: Mapping[str, Any] | None,
    revision: str | None,
    source_fingerprint: str | None = None,
    user_id: str | None = None,
    roles: Sequence[str] | None = None,
    policy_fingerprint: str | None = None,
) -> dict[str, Any]:
    scenario_id = str(session.get("scenario_id") or "").strip()
    if not scenario_id:
        return {"ok": False, "skipped": "scenario_id_missing"}
    binding = binding if isinstance(binding, Mapping) else {}
    dev_webspace_id = str(binding.get("dev_webspace_id") or _paired_dev_webspace_id(webspace_id) or "").strip()
    if not dev_webspace_id:
        return {"ok": False, "skipped": "dev_webspace_id_missing", "scenario_id": scenario_id}
    if (
        os.getenv("PYTEST_CURRENT_TEST")
        and str(os.getenv("ADAOS_BUILDER_DEV_RUNTIME_REFRESH_IN_TESTS") or "").strip().lower()
        not in {"1", "true", "yes", "on"}
    ):
        return {
            "ok": False,
            "skipped": "actual_dev_runtime_refresh_disabled_in_tests",
            "webspace_id": dev_webspace_id,
            "scenario_id": scenario_id,
        }

    rev = str(revision or session.get("ui_revision") or session.get("version") or "").strip() or "current"
    cmd_fp = _hash_suffix(f"{webspace_id}:{dev_webspace_id}:{scenario_id}:{rev}:{_now()}")
    event_payload = {
        "source": SKILL_ID,
        "reason": "builder_ui_revision_written",
        "source_webspace_id": webspace_id,
        "webspace_id": dev_webspace_id,
        "scenario_id": scenario_id,
        "draft_id": str(session.get("draft_id") or "").strip() or None,
        "ui_revision": rev,
        "_meta": {
            "cmd_id": f"builder.ui.{scenario_id}.{rev}",
            "gateway_client": SKILL_ID,
            "gateway_command_fingerprint": f"builder-ui-{cmd_fp}",
            "trace_id": f"builder-ui-refresh-{cmd_fp}",
        },
    }

    if _builder_revision_materialization_enabled():
        try:
            from adaos.services.scenario.webspace_runtime import apply_builder_revision_materialization
        except Exception as exc:
            materialization_import_error = f"{type(exc).__name__}: {exc}"
        else:
            async def _apply_materialization() -> dict[str, Any]:
                return await apply_builder_revision_materialization(
                    dev_webspace_id,
                    scenario_id=scenario_id,
                    revision=rev,
                    source_fingerprint=source_fingerprint,
                    user_id=user_id or "guest",
                    roles=list(roles or []),
                    policy_fingerprint=policy_fingerprint,
                    event_payload=event_payload,
                )

            delay_s = _builder_revision_materialization_delay_s()

            async def _delayed_apply_materialization() -> dict[str, Any]:
                if delay_s > 0:
                    await asyncio.sleep(delay_s)
                return await _apply_materialization()

            def _consume_materialization_result(done: asyncio.Task[Any]) -> None:
                try:
                    done.result()
                except Exception:
                    _LOG.warning(
                        "builder dev materialization task failed webspace=%s scenario=%s revision=%s",
                        dev_webspace_id,
                        scenario_id,
                        rev,
                        exc_info=True,
                    )

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    from adaos.sdk.data import events

                    materialize_event = dict(event_payload)
                    materialize_event["_event_type"] = "builder.ui_revision.materialize"
                    materialize_event["revision"] = rev
                    materialize_event["source_fingerprint"] = source_fingerprint
                    materialize_event["user_id"] = user_id or "guest"
                    materialize_event["roles"] = list(roles or [])
                    materialize_event["policy_fingerprint"] = policy_fingerprint
                    materialize_event["delay_s"] = delay_s
                    events.publish("builder.ui_revision.materialize", materialize_event, source=SKILL_ID)
                    return {
                        "ok": True,
                        "scheduled": True,
                        "mode": "materialization_event_bus",
                        "webspace_id": dev_webspace_id,
                        "scenario_id": scenario_id,
                        "revision": rev,
                        "source_fingerprint": source_fingerprint,
                        "user_id": user_id or "guest",
                        "roles": list(roles or []),
                        "delay_s": delay_s,
                        "event_payload": materialize_event,
                    }
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": "materialization_event_publish_failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "webspace_id": dev_webspace_id,
                        "scenario_id": scenario_id,
                        "revision": rev,
                    }
            else:
                try:
                    task = loop.create_task(
                        _delayed_apply_materialization(),
                        name=f"builder-dev-materialization:{dev_webspace_id}:{scenario_id}:{rev}",
                    )
                    task.add_done_callback(_consume_materialization_result)
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": "materialization_schedule_failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "webspace_id": dev_webspace_id,
                        "scenario_id": scenario_id,
                        "revision": rev,
                    }
                return {
                    "ok": True,
                    "scheduled": True,
                    "mode": "materialization_event_loop_task",
                    "webspace_id": dev_webspace_id,
                    "scenario_id": scenario_id,
                    "revision": rev,
                    "source_fingerprint": source_fingerprint,
                    "user_id": user_id or "guest",
                    "roles": list(roles or []),
                    "delay_s": delay_s,
                    "event_payload": event_payload,
                }
    else:
        materialization_import_error = "fast_path_disabled"

    try:
        from adaos.services.scenario.webspace_runtime import reload_webspace_from_scenario
    except Exception as exc:
        try:
            from adaos.sdk.data import events

            reload_event = dict(event_payload)
            reload_event["_event_type"] = "desktop.webspace.reload"
            reload_event["recreate_room"] = False
            events.publish("desktop.webspace.reload", reload_event, source=SKILL_ID)
            return {
                "ok": True,
                "scheduled": True,
                "mode": "event_bus_fallback",
                "webspace_id": dev_webspace_id,
                "scenario_id": scenario_id,
                "event_payload": event_payload,
                "direct_error": f"{type(exc).__name__}: {exc}",
                "materialization_error": materialization_import_error,
            }
        except Exception as bus_exc:
            bus_error = f"{type(bus_exc).__name__}: {bus_exc}"
        return {
            "ok": False,
            "error": "reload_webspace_import_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "bus_error": bus_error,
            "materialization_error": materialization_import_error,
            "webspace_id": dev_webspace_id,
            "scenario_id": scenario_id,
        }

    async def _reload() -> dict[str, Any]:
        return await reload_webspace_from_scenario(
            dev_webspace_id,
            scenario_id=scenario_id,
            action="reload",
            event_payload=event_payload,
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        def _runner() -> None:
            try:
                asyncio.run(_reload())
            except Exception:
                return

        thread = threading.Thread(target=_runner, name=f"builder-dev-runtime-reload:{dev_webspace_id}", daemon=True)
        thread.start()
        return {
            "ok": True,
            "scheduled": True,
            "mode": "thread",
            "webspace_id": dev_webspace_id,
            "scenario_id": scenario_id,
            "event_payload": event_payload,
        }

    try:
        task = loop.create_task(_reload(), name=f"builder-dev-runtime-reload:{dev_webspace_id}:{scenario_id}:{rev}")
    except Exception as exc:
        return {
            "ok": False,
            "error": "reload_schedule_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "webspace_id": dev_webspace_id,
            "scenario_id": scenario_id,
        }

    def _consume_result(done: asyncio.Task[Any]) -> None:
        try:
            done.result()
        except Exception:
            return

    task.add_done_callback(_consume_result)
    return {
        "ok": True,
        "scheduled": True,
        "mode": "event_loop_task",
        "webspace_id": dev_webspace_id,
        "scenario_id": scenario_id,
        "event_payload": event_payload,
    }


def _delete_sessions_for_draft(webspace_id: str, draft_id: str) -> None:
    token = str(draft_id or "").strip()
    if not token:
        return
    sessions = _sessions(webspace_id)
    removed = [sid for sid, session in sessions.items() if str(session.get("draft_id") or session.get("id") or "") == token]
    if not removed:
        return
    for sid in removed:
        sessions.pop(sid, None)
    _save_sessions(webspace_id, sessions)
    current = _current_session_id(webspace_id)
    if current in removed:
        latest = max(sessions.values(), key=lambda item: float(item.get("updated_at") or 0), default=None)
        _set_current_session_id(webspace_id, str(latest.get("id") if latest else ""))


@tool(summary="Start Builder rapid prototyping dialog.", side_effects="local_write")
def start(
    text: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return chat(text=text or "", webspace_id=webspace_id, _meta=_meta)


@tool(summary="Handle Builder dialog turn.", side_effects="local_write")
def chat(
    text: str | None = None,
    webspace_id: str | None = None,
    auto_apply: bool = True,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    utterance = str(text or "").strip()
    session, binding = _target_session(ws)
    topic = _builder_topic_ref(ws, session=session, binding=binding, _meta=_meta)
    if _is_guided_clarification_request(utterance):
        clarification = _builder_clarification_payload(text=utterance, webspace_id=ws, topic=topic)
        message = _guided_clarification_message(clarification)
        _safe_emit_chat(message, webspace_id=ws, _meta=_meta, binding=binding, topic_ref=topic)
        return {
            "ok": True,
            "status": "clarification_required",
            "needs_clarification": True,
            "message": message,
            "clarification": clarification,
            "binding": binding,
            "topic": topic,
            "dialog": _dialog_state(ws, topic_ref=topic),
        }
    command = _parse_builder_command(utterance, has_session=bool(session))
    command["raw"] = utterance
    intent = str(command.get("intent") or "")
    if intent == "project.list":
        return _handle_project_list_command(webspace_id=ws, session=session, binding=binding, topic=topic, command=command, _meta=_meta)
    if intent == "project.current":
        return _handle_project_current_command(webspace_id=ws, session=session, binding=binding, topic=topic, command=command, _meta=_meta)
    if intent == "project.switch":
        return _handle_project_switch_command(webspace_id=ws, command=command, _meta=_meta)
    if intent == "project.delete":
        return _handle_project_delete_command(webspace_id=ws, session=session, binding=binding, topic=topic, command=command, _meta=_meta)
    if intent == "project.create":
        result = create_scenario_draft(idea=utterance or "prototype app", webspace_id=ws, _meta=_meta)
        if result.get("ok"):
            message = str(result.get("message") or "")
            actions = result.get("message_actions") if isinstance(result.get("message_actions"), list) else None
            _safe_emit_chat(
                message,
                webspace_id=ws,
                _meta=_meta,
                topic_ref=result.get("topic") if isinstance(result.get("topic"), Mapping) else None,
                actions=actions,
            )
            return {**result, "command": command, "dialog": _dialog_state(ws, topic_ref=result.get("topic") if isinstance(result.get("topic"), Mapping) else topic)}
        return {**result, "command": command, "dialog": _dialog_state(ws, topic_ref=topic)}
    if not session:
        message = _target_required_message(binding)
        _safe_emit_chat(message, webspace_id=ws, _meta=_meta, binding=binding, topic_ref=topic)
        return {
            "ok": True,
            "status": "target_required",
            "needs_selection": True,
            "message": message,
            "binding": binding,
            "topic": topic,
            "dialog": _dialog_state(ws, topic_ref=topic),
        }
    result = update_current_scenario(instruction=utterance, webspace_id=ws, auto_apply=auto_apply, _meta=_meta)
    if result.get("ok"):
        actions = result.get("message_actions") if isinstance(result.get("message_actions"), list) else None
        emit_kwargs = {
            "webspace_id": ws,
            "_meta": _meta,
            "session": session,
            "binding": binding,
            "topic_ref": result.get("topic") if isinstance(result.get("topic"), Mapping) else topic,
            "actions": actions,
        }
        if str(result.get("status") or "") in {"llm_pending", "llm_submitting"}:
            _schedule_safe_emit_chat(str(result.get("message") or ""), delay_s=0.0, **emit_kwargs)
        else:
            _safe_emit_chat(str(result.get("message") or ""), **emit_kwargs)
    return {**result, "dialog": _dialog_state(ws, topic_ref=result.get("topic") if isinstance(result.get("topic"), Mapping) else topic)}


@tool(summary="Create scenario prototype draft.", side_effects="local_write")
def create_scenario_draft(
    idea: str,
    scenario_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    source_idea = str(idea or "").strip() or "prototype app"
    sid = re.sub(r"[^a-z0-9_.-]+", "_", str(scenario_id or "").strip().lower()).strip("._-") or _scenario_id_from_idea(source_idea)
    fields = _build_fields(source_idea)
    session_id = f"builder_session_{_hash_suffix(ws + sid + source_idea)}"
    session = {
        "id": session_id,
        "webspace_id": ws,
        "status": "drafting",
        "title": "\u0421\u043f\u0438\u0441\u043e\u043a \u043f\u043e\u043a\u0443\u043f\u043e\u043a" if "shopping" in sid else sid.replace("_", " ").title(),
        "source_idea": source_idea,
        "scenario_id": sid,
        "datasource_id": "shopping_items" if "shopping" in sid else "prototype_items",
        "fields": fields,
        "patches": [],
        "version": "001",
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        from adaos.services.builder.workspace import BuilderWorkspaceService

        draft = BuilderWorkspaceService.from_context().create_draft(
            kind="scenario",
            artifact_id=sid,
            source_idea=source_idea,
            template_id="builder_scenario",
            webspace_id=ws,
            source={
                "type": "builder_dialog",
                "utterance": source_idea,
                "side_effect_class": "local_write",
            },
        )
        draft_payload = draft.get("draft") if isinstance(draft.get("draft"), dict) else {}
        session["draft_id"] = draft_payload.get("draft_id")
        session["artifact_root"] = draft.get("artifact_root")
    except Exception as exc:
        session["status"] = "degraded"
        session["draft_error"] = f"{type(exc).__name__}: {exc}"
    session["user_summary"] = _draft_user_summary(session)
    initial_revision = _next_ui_revision_label(session)
    session["version"] = initial_revision
    preview = _preview_state(session=session)
    _write_webui(str(session.get("artifact_root") or ""), preview)
    session["preview_state"] = preview
    initial_patch = {
        "id": f"patch_initial_{_hash_suffix(session_id + source_idea)}",
        "target": "ui",
        "operation": "create_scenario_draft",
        "status": "applied",
        "created_by": "builder_skill",
        "created_at": _now(),
        "summary": source_idea,
        "diff": {"scenario_id": sid, "fields": fields},
    }
    _write_ui_revision(
        session=session,
        request_text=source_idea,
        patch=initial_patch,
        before_webui=None,
        after_webui=_current_webui_payload(session, preview),
        preview_state=preview,
        llm_result=None,
        llm_model=_builder_llm_model_for_session(session, _meta),
        revision=initial_revision,
    )
    _save_session(ws, session)
    workbench = _ensure_workbench(ws, session=session, preview_state=preview, refresh_runtime=False, snapshot_projection=False)
    binding = workbench.get("binding") if isinstance(workbench.get("binding"), Mapping) else {}
    dev_runtime_refresh = _schedule_dev_runtime_reload_after_revision(
        ws,
        session=session,
        binding=binding,
        revision=str(session.get("ui_revision") or initial_revision),
        source_fingerprint=_webui_source_fingerprint(
            session.get("webui_payload") if isinstance(session.get("webui_payload"), Mapping) else None
        ),
        user_id=_meta_user_id(_meta),
        roles=_meta_roles(_meta),
    )
    topic = _builder_topic_ref(ws, session=session, binding=binding, _meta=_meta)
    session["thread_id"] = str(topic.get("thread_id") or "").strip() or None
    session["topic_id"] = str(topic.get("topic_id") or "").strip() or None
    session["topic_ref"] = {k: v for k, v in topic.items() if k != "stored"}
    _save_session(ws, session)
    prompt_selection = _publish_prompt_project_selection(
        ws,
        session=session,
        reason="builder_project_created",
    )
    message = _message_created(session)
    if session.get("draft_error"):
        message += f" \u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u0435: dev draft \u043d\u0435 \u0441\u043e\u0437\u0434\u0430\u043d ({session['draft_error']})."
    pending_action = _publish_review_pending_action(
        webspace_id=ws,
        session=session,
        request_text=source_idea,
        kind="builder.scenario_draft.review",
        summary=f"Review Builder draft {sid}",
        _meta=_meta,
    )
    if pending_action and pending_action.get("id"):
        session["pending_action_id"] = pending_action.get("id")
        _save_session(ws, session)
    actions = _revision_chat_actions(session, str(session.get("ui_revision") or ""))
    return {
        "ok": True,
        "session_id": session_id,
        "scenario_id": sid,
        "draft_id": session.get("draft_id"),
        "artifact_root": session.get("artifact_root"),
        "preview_state": preview,
        "workbench": workbench,
        "dev_runtime_refresh": dev_runtime_refresh,
        "prompt_selection": prompt_selection,
        "topic": {k: v for k, v in topic.items() if k != "stored"},
        "pending_action": pending_action,
        "message": message,
        "message_actions": actions,
        "ui_revision": {"revision": session.get("ui_revision")} if session.get("ui_revision") else None,
        "dialog": _dialog_state(ws, topic_ref=topic),
    }


def _llm_failure_summary(llm_result: Mapping[str, Any] | None) -> str:
    if not isinstance(llm_result, Mapping):
        return ""
    detail = str(llm_result.get("detail") or llm_result.get("error") or "").strip()
    comment = str(llm_result.get("comment") or "").strip()
    request_id = str(llm_result.get("request_id") or "").strip()
    if not request_id:
        attempts = llm_result.get("attempts")
        if isinstance(attempts, list):
            for item in attempts:
                if isinstance(item, Mapping) and item.get("request_id"):
                    request_id = str(item.get("request_id") or "").strip()
                    break
    if _looks_like_timeout(detail) or str(llm_result.get("error") or "") in {"llm_timeout", "llm_webui_transform_timeout"}:
        suffix = f" request_id={request_id}" if request_id else ""
        return f"LLM timeout: {detail}{suffix}".strip()
    if comment and detail:
        return f"{comment} ({detail})"
    return comment or detail


def _llm_failure_is_timeout(llm_result: Mapping[str, Any] | None) -> bool:
    if not isinstance(llm_result, Mapping):
        return False
    if str(llm_result.get("error") or "") in {"llm_timeout", "llm_webui_transform_timeout"}:
        return True
    if _looks_like_timeout(str(llm_result.get("detail") or "")):
        return True
    attempts = llm_result.get("attempts")
    return any(
        isinstance(item, Mapping)
        and (
            str(item.get("error") or "") in {"llm_timeout", "llm_webui_transform_timeout"}
            or _looks_like_timeout(str(item.get("detail") or ""))
        )
        for item in (attempts if isinstance(attempts, list) else [])
    )


def _builder_update_message_clean(
    *,
    session: Mapping[str, Any],
    patch: Mapping[str, Any],
    revision: str | None,
    llm_comment: str = "",
    unable_reason: str = "",
    not_implemented: Sequence[Any] | None = None,
) -> str:
    revision_text = f" \u0420\u0435\u0432\u0438\u0437\u0438\u044f UI: {revision}." if revision else ""
    scenario_id = session.get("scenario_id")
    operation = patch.get("operation")
    if unable_reason:
        return (
            f"{AGENT_LABEL}: \u043e\u0431\u043d\u043e\u0432\u0438\u043b \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f {scenario_id} "
            f"\u0441 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u0435\u043c. "
            f"\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f: {operation}. {unable_reason}.{revision_text}"
        )
    if patch.get("status") == "partial" and isinstance(not_implemented, list) and not_implemented:
        return (
            f"{AGENT_LABEL}: \u0447\u0430\u0441\u0442\u0438\u0447\u043d\u043e \u043e\u0431\u043d\u043e\u0432\u0438\u043b "
            f"\u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f {scenario_id}. "
            f"\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f: {operation}. "
            f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0440\u0435\u0430\u043b\u0438\u0437\u043e\u0432\u0430\u0442\u044c: "
            f"{', '.join(str(item) for item in not_implemented)}.{revision_text}"
        )
    if llm_comment and operation == "llm_webui_transform":
        return (
            f"{AGENT_LABEL}: \u043e\u0431\u043d\u043e\u0432\u0438\u043b \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f {scenario_id}. "
            f"\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f: {operation}. {llm_comment}{revision_text}"
        )
    return (
        f"{AGENT_LABEL}: \u043e\u0431\u043d\u043e\u0432\u0438\u043b \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f {scenario_id}. "
        f"\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f: {operation}.{revision_text}"
    )


def _builder_revision_message(
    kind: str,
    *,
    revision: str | None = None,
    scenario_id: Any = None,
    detail: Any = None,
) -> str:
    if kind == "session_not_found":
        return f"{AGENT_LABEL}: \u043d\u0435 \u043d\u0430\u0448\u0435\u043b Builder-\u0441\u0435\u0441\u0441\u0438\u044e \u0434\u043b\u044f \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f \u0440\u0435\u0432\u0438\u0437\u0438\u0438."
    if kind == "revision_not_found":
        return f"{AGENT_LABEL}: \u043d\u0435 \u043d\u0430\u0448\u0435\u043b UI-\u0440\u0435\u0432\u0438\u0437\u0438\u044e {revision} \u0434\u043b\u044f {scenario_id}."
    if kind == "revision_invalid":
        return f"{AGENT_LABEL}: \u0440\u0435\u0432\u0438\u0437\u0438\u044f {revision} \u043d\u0435 \u0441\u043e\u0434\u0435\u0440\u0436\u0438\u0442 \u0432\u0430\u043b\u0438\u0434\u043d\u044b\u0439 after_webui/preview_state."
    if kind == "revision_validation_failed":
        return f"{AGENT_LABEL}: \u0440\u0435\u0432\u0438\u0437\u0438\u044f {revision} \u043d\u0435 \u043f\u0440\u043e\u0448\u043b\u0430 \u0432\u0430\u043b\u0438\u0434\u0430\u0446\u0438\u044e: {detail}."
    return f"{AGENT_LABEL}: \u0441\u0434\u0435\u043b\u0430\u043b UI-\u0440\u0435\u0432\u0438\u0437\u0438\u044e {revision} \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u0434\u043b\u044f {scenario_id}."


def _finalize_scenario_update(
    *,
    ws: str,
    session: dict[str, Any],
    binding: Mapping[str, Any],
    patch: dict[str, Any],
    request_text: str,
    before_webui: Mapping[str, Any] | None,
    llm_result: Mapping[str, Any] | None,
    auto_apply: bool,
    _meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    _ensure_session_artifact_root(session, binding)
    session.setdefault("patches", []).append(patch)
    next_revision = _next_ui_revision_label(session)
    session["version"] = next_revision
    session["user_summary"] = _draft_user_summary(session)
    if patch.get("operation") == "llm_webui_transform" and isinstance(session.get("preview_state"), Mapping):
        preview = copy.deepcopy(dict(session["preview_state"]))
        preview["version"] = next_revision
    else:
        preview = _preview_state(session=session)
    preview = _repair_text_tree(dict(preview))
    if patch.get("operation") == "llm_webui_transform" and isinstance(session.get("webui_payload"), Mapping):
        payload = copy.deepcopy(dict(session["webui_payload"]))
        session["webui_payload"] = payload
        _write_webui_payload(str(session.get("artifact_root") or ""), payload)
    else:
        _write_webui(str(session.get("artifact_root") or ""), preview)
    session["preview_state"] = preview
    after_webui = _current_webui_payload(session, preview)
    revision_info = _write_ui_revision(
        session=session,
        request_text=request_text,
        patch=patch,
        before_webui=before_webui,
        after_webui=after_webui,
        preview_state=preview,
        llm_result=llm_result,
        llm_model=_builder_llm_model_for_session(session, _meta),
        revision=next_revision,
    )
    if revision_info:
        patch["revision"] = revision_info.get("revision")
        patch["revision_path"] = revision_info.get("path")
        session["patches"][-1] = patch
    workbench = _ensure_workbench(ws, session=session, preview_state=preview, refresh_runtime=False, snapshot_projection=False)
    resolved_binding = workbench.get("binding") if isinstance(workbench.get("binding"), Mapping) else binding
    dev_runtime_refresh = _schedule_dev_runtime_reload_after_revision(
        ws,
        session=session,
        binding=resolved_binding,
        revision=str(revision_info.get("revision") if revision_info else ""),
        source_fingerprint=_webui_source_fingerprint(after_webui),
        user_id=_meta_user_id(_meta),
        roles=_meta_roles(_meta),
    )
    topic = _builder_topic_ref(ws, session=session, binding=resolved_binding, _meta=_meta)
    session["thread_id"] = str(topic.get("thread_id") or "").strip() or None
    session["topic_id"] = str(topic.get("topic_id") or "").strip() or None
    session["topic_ref"] = {k: v for k, v in topic.items() if k != "stored"}
    _save_session(ws, session)
    project_files_refresh = _publish_prompt_project_changed(
        ws,
        session=session,
        reason="builder_ui_revision_written",
    )
    prompt_selection = _publish_prompt_project_selection(
        ws,
        session=session,
        reason="builder_project_updated",
    )
    pending_action = _publish_review_pending_action(
        webspace_id=ws,
        session=session,
        request_text=request_text,
        kind="builder.scenario_patch.review",
        summary=f"Review Builder patch {patch['operation']} for {session.get('scenario_id')}",
        _meta=_meta,
        patch=patch,
    )
    if pending_action and pending_action.get("id"):
        patch["pending_action_id"] = pending_action.get("id")
        session["patches"][-1] = patch
        session["pending_action_id"] = pending_action.get("id")
        _save_session(ws, session)
    not_implemented = patch.get("diff", {}).get("not_implemented") if isinstance(patch.get("diff"), Mapping) else None
    llm_comment = str((llm_result or {}).get("comment") or "").strip() if isinstance(llm_result, Mapping) else ""
    unable_reason = str((llm_result or {}).get("unable_reason") or "").strip() if isinstance(llm_result, Mapping) else ""
    message = _builder_update_message_clean(
        session=session,
        patch=patch,
        revision=str(revision_info.get("revision") if revision_info else ""),
        llm_comment=llm_comment,
        unable_reason=unable_reason,
        not_implemented=not_implemented if isinstance(not_implemented, list) else None,
    )
    actions = _revision_chat_actions(session, str(revision_info.get("revision") if revision_info else ""))
    return {
        "ok": True,
        "session_id": session.get("id"),
        "scenario_id": session.get("scenario_id"),
        "patch": patch,
        "preview_state": preview,
        "workbench": workbench,
        "dev_runtime_refresh": dev_runtime_refresh,
        "project_files_refresh": project_files_refresh,
        "prompt_selection": prompt_selection,
        "topic": {k: v for k, v in topic.items() if k != "stored"},
        "pending_action": pending_action,
        "message": message,
        "message_actions": actions,
        "ui_revision": revision_info,
        "llm": _compact_llm_result(llm_result),
        "dialog": _dialog_state(ws, topic_ref=topic),
    }


def _finalize_llm_webui_transform_result(
    *,
    ws: str,
    session: dict[str, Any],
    binding: Mapping[str, Any],
    patch: dict[str, Any],
    request_text: str,
    before_webui: Mapping[str, Any] | None,
    llm_result: Mapping[str, Any],
    auto_apply: bool,
    _meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    preview_from_llm = llm_result.get("preview_state") if isinstance(llm_result.get("preview_state"), Mapping) else {}
    if not preview_from_llm:
        preview_from_llm = session.get("preview_state") if isinstance(session.get("preview_state"), Mapping) else _preview_state(session=session)
    payload_from_llm = llm_result.get("payload") if isinstance(llm_result.get("payload"), Mapping) else None
    patch["operation"] = "llm_webui_transform"
    patch["diff"] = {
        "schema_valid": True,
        "comment": str(llm_result.get("comment") or ""),
        "unable_reason": str(llm_result.get("unable_reason") or ""),
        "validation": dict(llm_result.get("validation") or {}) if isinstance(llm_result.get("validation"), Mapping) else {},
        "attempts": list(llm_result.get("attempts") or []) if isinstance(llm_result.get("attempts"), list) else [],
    }
    session["preview_state"] = copy.deepcopy(dict(preview_from_llm))
    if payload_from_llm is not None:
        session["webui_payload"] = copy.deepcopy(dict(payload_from_llm))
    _merge_session_from_preview(session, preview_from_llm)
    return _finalize_scenario_update(
        ws=ws,
        session=session,
        binding=binding,
        patch=patch,
        request_text=request_text,
        before_webui=before_webui,
        llm_result=llm_result,
        auto_apply=auto_apply,
        _meta=_meta,
    )


def _submit_llm_webui_transform_job(
    *,
    session: Mapping[str, Any],
    instruction: str,
    preview_state: Mapping[str, Any],
    job_nonce: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    started_at = _now()
    request = _builder_llm_webui_transform_request(
        session=session,
        instruction=instruction,
        preview_state=preview_state,
        _meta=_meta,
    )
    context_ready_at = _now()
    current_payload = request["current_payload"]
    request_id = _builder_llm_job_request_id(
        session=session,
        instruction=instruction,
        current_payload=current_payload,
        attempt=1,
        job_nonce=job_nonce,
    )
    messages = [
        {"role": "system", "content": str(request["system_prompt"])},
        {"role": "user", "content": str(request["user_prompt"])},
    ]
    system_prompt_bytes = len(str(request["system_prompt"]).encode("utf-8", errors="replace"))
    user_prompt_bytes = len(str(request["user_prompt"]).encode("utf-8", errors="replace"))
    selected_model = _builder_llm_model_for_session(session, _meta)
    _LOG.debug(
        "builder LLM job submit start scenario=%s request_id=%s model=%s context_build_ms=%d system_prompt_bytes=%d user_prompt_bytes=%d",
        str(session.get("scenario_id") or ""),
        request_id,
        selected_model or "",
        int((context_ready_at - started_at) * 1000),
        system_prompt_bytes,
        user_prompt_bytes,
    )
    try:
        from adaos.sdk.llm.llm_client import submit_response_job
    except Exception as exc:
        failed_at = _now()
        detail = f"{type(exc).__name__}: {exc}"
        _LOG.warning(
            "builder LLM job submit unavailable scenario=%s request_id=%s context_build_ms=%d total_ms=%d detail=%s",
            str(session.get("scenario_id") or ""),
            request_id,
            int((context_ready_at - started_at) * 1000),
            int((failed_at - started_at) * 1000),
            detail,
        )
        return {
            "ok": False,
            "error": "llm_job_submit_failed",
            "detail": detail,
            "request_id": request_id,
            "model": selected_model,
            "timing": {
                "context_build_ms": int((context_ready_at - started_at) * 1000),
                "submit_ms": 0,
                "total_ms": int((failed_at - started_at) * 1000),
            },
            "comment": "\u041d\u0435 \u0441\u043c\u043e\u0433 \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c LLM job.",
        }
    response: Mapping[str, Any] | None = None
    submit_done_at = context_ready_at
    submit_attempts: list[dict[str, Any]] = []
    max_submit_attempts = 2 if job_nonce else 1
    for submit_attempt in range(1, max_submit_attempts + 1):
        request_id = _builder_llm_job_request_id(
            session=session,
            instruction=instruction,
            current_payload=current_payload,
            attempt=submit_attempt,
            job_nonce=job_nonce,
        )
        attempt_started = _now()
        try:
            response = submit_response_job(
                messages,
                model=selected_model,
                temperature=_builder_llm_temperature(),
                max_tokens=_builder_llm_max_tokens(),
                request_id=request_id,
                timeout=_builder_llm_job_submit_timeout_s(),
            )
            submit_done_at = _now()
            submit_attempts.append(
                {
                    "attempt": submit_attempt,
                    "request_id": request_id,
                    "ok": True,
                    "duration_ms": int((submit_done_at - attempt_started) * 1000),
                }
            )
            break
        except Exception as exc:
            failed_at = _now()
            detail = f"{type(exc).__name__}: {exc}"
            conflict = _looks_like_llm_request_id_conflict(exc)
            retry = conflict and submit_attempt < max_submit_attempts
            submit_attempts.append(
                {
                    "attempt": submit_attempt,
                    "request_id": request_id,
                    "ok": False,
                    "duration_ms": int((failed_at - attempt_started) * 1000),
                    "error": "llm_request_id_conflict" if conflict else type(exc).__name__,
                }
            )
            _LOG.warning(
                "builder LLM job submit failed scenario=%s request_id=%s attempt=%d retry=%s context_build_ms=%d submit_ms=%d total_ms=%d detail=%s",
                str(session.get("scenario_id") or ""),
                request_id,
                submit_attempt,
                bool(retry),
                int((context_ready_at - started_at) * 1000),
                int((failed_at - context_ready_at) * 1000),
                int((failed_at - started_at) * 1000),
                detail,
            )
            if retry:
                continue
            return {
                "ok": False,
                "error": "llm_job_submit_timeout" if _looks_like_timeout(detail) else "llm_job_submit_failed",
                "detail": detail,
                "request_id": request_id,
                "model": selected_model,
                "timing": {
                    "context_build_ms": int((context_ready_at - started_at) * 1000),
                    "submit_ms": int((failed_at - context_ready_at) * 1000),
                    "total_ms": int((failed_at - started_at) * 1000),
                    "submit_attempts": submit_attempts,
                },
                "comment": "\u041d\u0435 \u0441\u043c\u043e\u0433 \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c LLM job.",
            }
    if response is None:
        failed_at = _now()
        return {
            "ok": False,
            "error": "llm_job_submit_failed",
            "detail": "submit_response_job returned no response",
            "request_id": request_id,
            "model": selected_model,
            "timing": {
                "context_build_ms": int((context_ready_at - started_at) * 1000),
                "submit_ms": int((failed_at - context_ready_at) * 1000),
                "total_ms": int((failed_at - started_at) * 1000),
                "submit_attempts": submit_attempts,
            },
            "comment": "\u041d\u0435 \u0441\u043c\u043e\u0433 \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c LLM job.",
        }
    timing = {
        "context_build_ms": int((context_ready_at - started_at) * 1000),
        "submit_ms": int((submit_done_at - context_ready_at) * 1000),
        "total_ms": int((submit_done_at - started_at) * 1000),
    }
    status = str(response.get("status") or "").strip().lower()
    job_id = str(response.get("job_id") or "").strip()
    client = response.get("_client") if isinstance(response.get("_client"), Mapping) else {}
    base_url = str(client.get("base_url") or "").strip()
    if client:
        timing["client"] = dict(client)
    if submit_attempts:
        timing["submit_attempts"] = submit_attempts
    (_LOG.warning if timing["total_ms"] >= _builder_llm_job_submit_warn_ms() else _LOG.debug)(
        "builder LLM job submit completed scenario=%s request_id=%s job_id=%s base_url=%s status=%s context_build_ms=%d submit_ms=%d total_ms=%d",
        str(session.get("scenario_id") or ""),
        request_id,
        job_id,
        base_url,
        status,
        timing["context_build_ms"],
        timing["submit_ms"],
        timing["total_ms"],
    )
    if status in {"queued", "running", "succeeded"} and job_id:
        return {
            "ok": True,
            "pending": True,
            "status": status,
            "job_id": job_id,
            "request_id": request_id,
            "model": selected_model,
            "base_url": base_url,
            "job": response,
            "timing": timing,
            "message": (
                f"{AGENT_LABEL}: \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u043b LLM-\u0437\u0430\u0434\u0430\u0447\u0443 "
                f"\u0434\u043b\u044f {session.get('scenario_id')}. Job: {job_id}."
            ),
        }
    return {
        "ok": False,
        "error": "llm_job_failed",
        "detail": str(response.get("error") or response),
        "request_id": request_id,
        "job_id": job_id,
        "model": selected_model,
        "job": response,
        "timing": timing,
    }


def _mark_llm_job_failed(
    *,
    ws: str,
    session: dict[str, Any],
    job_id: str,
    detail: str,
    binding: Mapping[str, Any] | None = None,
    topic_ref: Mapping[str, Any] | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> None:
    _update_llm_job_status(session, job_id, "failed", detail=detail)
    _save_session(ws, session)
    topic = topic_ref if isinstance(topic_ref, Mapping) else _builder_topic_ref(ws, session=session, binding=binding or {}, _meta=_meta)
    message = (
        f"{AGENT_LABEL}: LLM-\u0437\u0430\u0434\u0430\u0447\u0430 {job_id or ''} "
        f"\u0434\u043b\u044f {session.get('scenario_id')} \u043d\u0435 \u0437\u0430\u0432\u0435\u0440\u0448\u0438\u043b\u0430\u0441\u044c. {detail}"
    ).strip()
    _safe_emit_chat(message, webspace_id=ws, _meta=_meta, session=session, binding=binding or {}, topic_ref=topic)


_ACTIVE_LLM_JOB_STATUSES = frozenset({"submitting", "submitted", "queued", "running"})
_TERMINAL_LLM_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled", "canceled"})


def _llm_job_related_ids(key: str, value: Mapping[str, Any]) -> set[str]:
    return {
        item
        for item in {
            str(key or "").strip(),
            str(value.get("job_id") or "").strip(),
            str(value.get("local_job_id") or "").strip(),
            str(value.get("root_job_id") or "").strip(),
        }
        if item
    }


def _log_pending_llm_job_state_races(
    *,
    scenario_id: str,
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> None:
    existing_items = [
        (str(key), dict(value), _llm_job_related_ids(str(key), value))
        for key, value in (existing or {}).items()
        if isinstance(value, Mapping)
    ]
    for key, value in (incoming or {}).items():
        if not isinstance(value, Mapping):
            continue
        incoming_status = str(value.get("status") or "").strip().lower()
        if incoming_status not in _ACTIVE_LLM_JOB_STATUSES:
            continue
        incoming_ids = _llm_job_related_ids(str(key), value)
        if not incoming_ids:
            continue
        for existing_key, existing_value, existing_ids in existing_items:
            if not (incoming_ids & existing_ids):
                continue
            existing_status = str(existing_value.get("status") or "").strip().lower()
            if existing_status not in _TERMINAL_LLM_JOB_STATUSES:
                continue
            _LOG.warning(
                "builder pending LLM job state race ignored scenario=%s incoming_job=%s incoming_status=%s existing_job=%s existing_status=%s root_job_id=%s local_job_id=%s patch_id=%s request_id=%s",
                scenario_id,
                str(key),
                incoming_status,
                existing_key,
                existing_status,
                str(value.get("root_job_id") or existing_value.get("root_job_id") or ""),
                str(value.get("local_job_id") or existing_value.get("local_job_id") or ""),
                str(value.get("patch_id") or existing_value.get("patch_id") or ""),
                str(value.get("request_id") or existing_value.get("request_id") or ""),
            )
            break


def _merged_llm_job_status(existing: str, incoming: str) -> str:
    left = str(existing or "").strip().lower()
    right = str(incoming or "").strip().lower()
    if left == "failed" or right == "failed":
        return "failed"
    if left in _TERMINAL_LLM_JOB_STATUSES:
        return left
    if right in _TERMINAL_LLM_JOB_STATUSES:
        return right
    rank = {"": 0, "submitting": 1, "submitted": 2, "queued": 3, "running": 4}
    return right if rank.get(right, 0) >= rank.get(left, 0) else left


def _merge_pending_llm_jobs(
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {
        str(key): dict(value)
        for key, value in (existing or {}).items()
        if isinstance(value, Mapping)
    }
    for key, value in (incoming or {}).items():
        if not isinstance(value, Mapping):
            continue
        token = str(key or "").strip()
        if not token:
            continue
        current = dict(merged.get(token) or {})
        incoming_item = dict(value)
        status = _merged_llm_job_status(
            str(current.get("status") or ""),
            str(incoming_item.get("status") or ""),
        )
        current.update({k: v for k, v in incoming_item.items() if v not in (None, "")})
        if status:
            current["status"] = status
        for link_key in ("job_id", "local_job_id", "root_job_id", "request_id", "base_url", "patch_id", "request_text"):
            if not current.get(link_key) and incoming_item.get(link_key):
                current[link_key] = incoming_item.get(link_key)
        for time_key in ("created_at", "submitted_at", "started_at"):
            if current.get(time_key) is None and incoming_item.get(time_key) is not None:
                current[time_key] = incoming_item.get(time_key)
        if current.get("finished_at") is not None or incoming_item.get("finished_at") is not None:
            try:
                current["finished_at"] = max(float(current.get("finished_at") or 0.0), float(incoming_item.get("finished_at") or 0.0))
            except Exception:
                current["finished_at"] = current.get("finished_at") or incoming_item.get("finished_at")
        merged[token] = current
    holder: dict[str, Any] = {"pending_llm_jobs": merged}
    _normalise_pending_llm_jobs(holder)
    return {
        str(key): dict(value)
        for key, value in (holder.get("pending_llm_jobs") or {}).items()
        if isinstance(value, Mapping)
    }


def _ensure_llm_job_link(
    session: dict[str, Any],
    *,
    local_job_id: str | None,
    root_job_id: str | None,
    request_id: str | None = None,
    base_url: str | None = None,
    request_text: str | None = None,
    patch_id: Any = None,
    model: str | None = None,
    status: str | None = None,
) -> None:
    local_id = str(local_job_id or "").strip()
    root_id = str(root_job_id or "").strip()
    if not local_id and not root_id:
        return
    pending = session.get("pending_llm_jobs") if isinstance(session.get("pending_llm_jobs"), dict) else {}
    updated = dict(pending)
    now = _now()
    base_fields = {
        "schema": "adaos.builder.llm_job.v1",
        "request_id": str(request_id or "").strip() or None,
        "base_url": str(base_url or "").strip() or None,
        "request_text": str(request_text or ""),
        "patch_id": patch_id,
        "model": str(model or "").strip() or None,
    }
    if local_id:
        local_entry = dict(updated.get(local_id) or {})
        local_entry.setdefault("schema", "adaos.builder.llm_job.v1")
        local_entry["job_id"] = local_entry.get("job_id") or local_id
        if root_id:
            local_entry["root_job_id"] = root_id
        if request_id:
            local_entry["request_id"] = str(request_id)
        if base_url:
            local_entry["base_url"] = str(base_url)
        if request_text:
            local_entry.setdefault("request_text", str(request_text))
        if patch_id is not None:
            local_entry.setdefault("patch_id", patch_id)
        if model:
            local_entry.setdefault("model", str(model))
        local_entry.setdefault("created_at", now)
        if status:
            local_entry["status"] = _merged_llm_job_status(str(local_entry.get("status") or ""), status)
        else:
            local_entry.setdefault("status", "submitted" if root_id else "submitting")
        updated[local_id] = local_entry
    if root_id:
        root_entry = dict(updated.get(root_id) or {})
        root_entry.update({k: v for k, v in base_fields.items() if v not in (None, "")})
        root_entry["job_id"] = root_id
        if local_id:
            root_entry["local_job_id"] = local_id
        root_entry.setdefault("created_at", now)
        root_entry["status"] = _merged_llm_job_status(str(root_entry.get("status") or ""), status or "queued")
        updated[root_id] = root_entry
    session["pending_llm_jobs"] = updated
    _normalise_pending_llm_jobs(session)


def _normalise_pending_llm_jobs(session: dict[str, Any]) -> None:
    pending = session.get("pending_llm_jobs") if isinstance(session.get("pending_llm_jobs"), dict) else {}
    if not isinstance(pending, dict) or not pending:
        return
    updated = {str(key): dict(value) for key, value in pending.items() if isinstance(value, Mapping)}
    groups: dict[str, set[str]] = {}
    for key, value in updated.items():
        related = {
            key,
            str(value.get("job_id") or "").strip(),
            str(value.get("local_job_id") or "").strip(),
            str(value.get("root_job_id") or "").strip(),
        }
        related = {item for item in related if item}
        if not related:
            continue
        group_key = sorted(related)[0]
        merged = set(related)
        for existing_key, existing_ids in list(groups.items()):
            if merged & existing_ids:
                merged |= existing_ids
                groups.pop(existing_key, None)
        groups[group_key] = merged
    now = _now()
    for related_ids in groups.values():
        terminal_status = ""
        terminal_detail = ""
        finished_at = 0.0
        for related_id in related_ids:
            item = updated.get(related_id)
            if not isinstance(item, Mapping):
                continue
            status = str(item.get("status") or "").strip().lower()
            if status in _TERMINAL_LLM_JOB_STATUSES:
                if status == "failed":
                    terminal_status = "failed"
                elif not terminal_status:
                    terminal_status = status
                terminal_detail = terminal_detail or str(item.get("detail") or "")
                try:
                    finished_at = max(finished_at, float(item.get("finished_at") or 0.0))
                except Exception:
                    pass
        if not terminal_status:
            continue
        for related_id in related_ids:
            if not isinstance(updated.get(related_id), Mapping):
                continue
            item = dict(updated[related_id])
            item["status"] = terminal_status
            item.setdefault("finished_at", finished_at or now)
            if terminal_detail and not item.get("detail"):
                item["detail"] = terminal_detail
            updated[related_id] = item
    session["pending_llm_jobs"] = updated


def _reconcile_pending_llm_jobs_from_revisions(session: dict[str, Any]) -> bool:
    pending = session.get("pending_llm_jobs") if isinstance(session.get("pending_llm_jobs"), dict) else {}
    if not isinstance(pending, dict) or not pending:
        return False
    active_items = [
        (str(key), dict(value))
        for key, value in pending.items()
        if isinstance(value, Mapping)
        and str(value.get("status") or "").strip().lower() in _ACTIVE_LLM_JOB_STATUSES
    ]
    if not active_items:
        return False
    revision_dir = _ui_revision_dir(str(session.get("artifact_root") or ""))
    if revision_dir is None or not revision_dir.exists():
        return False
    try:
        revision_files = sorted(
            revision_dir.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:20]
    except Exception:
        return False
    changed = False
    for revision_file in revision_files:
        payload = _read_json_file(revision_file)
        patch = payload.get("patch") if isinstance(payload.get("patch"), Mapping) else {}
        if not patch:
            continue
        patch_id = str(patch.get("id") or "").strip()
        request = payload.get("request") if isinstance(payload.get("request"), Mapping) else {}
        request_text = str(request.get("text") or patch.get("summary") or "").strip()
        patch_diff = patch.get("diff") if isinstance(patch.get("diff"), Mapping) else {}
        attempts = patch_diff.get("attempts") if isinstance(patch_diff.get("attempts"), list) else []
        root_job_id = ""
        request_id = ""
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                continue
            root_job_id = root_job_id or str(attempt.get("job_id") or "").strip()
            request_id = request_id or str(attempt.get("request_id") or "").strip()
        for job_key, job in active_items:
            job_patch_id = str(job.get("patch_id") or "").strip()
            job_request_text = str(job.get("request_text") or "").strip()
            if patch_id and job_patch_id == patch_id:
                matched = True
            elif request_text and job_request_text == request_text:
                matched = True
            else:
                matched = False
            if not matched:
                continue
            _ensure_llm_job_link(
                session,
                local_job_id=str(job.get("local_job_id") or job_key or "").strip(),
                root_job_id=root_job_id,
                request_id=request_id,
                base_url=str(job.get("base_url") or ""),
                request_text=job_request_text or request_text,
                patch_id=job_patch_id or patch_id,
                model=str(
                    (
                        payload.get("inference", {}).get("model")
                        if isinstance(payload.get("inference"), Mapping)
                        else ""
                    )
                    or job.get("model")
                    or ""
                ).strip(),
                status="submitted",
            )
            _update_llm_job_status(session, root_job_id or job_key, "succeeded")
            _LOG.warning(
                "builder pending LLM job reconciled from revision artifact scenario=%s local_job_id=%s root_job_id=%s patch_id=%s revision=%s",
                str(session.get("scenario_id") or ""),
                str(job.get("local_job_id") or job_key or ""),
                root_job_id,
                job_patch_id or patch_id,
                str(payload.get("revision") or revision_file.stem),
            )
            changed = True
    return changed


def _update_llm_job_status(
    session: dict[str, Any],
    job_id: str,
    status: str,
    *,
    detail: str | None = None,
) -> None:
    token = str(job_id or "").strip()
    if not token:
        return
    pending = session.get("pending_llm_jobs") if isinstance(session.get("pending_llm_jobs"), dict) else {}
    if not isinstance(pending, dict):
        return
    updated = dict(pending)
    related_ids: set[str] = {token}
    current = updated.get(token)
    if isinstance(current, Mapping):
        for key in ("local_job_id", "root_job_id"):
            value = str(current.get(key) or "").strip()
            if value:
                related_ids.add(value)
    for key, value in list(updated.items()):
        if not isinstance(value, Mapping):
            continue
        value_job_id = str(value.get("job_id") or key or "").strip()
        local_job_id = str(value.get("local_job_id") or "").strip()
        root_job_id = str(value.get("root_job_id") or "").strip()
        if token in {value_job_id, local_job_id, root_job_id}:
            related_ids.add(str(key))
            if value_job_id:
                related_ids.add(value_job_id)
            if local_job_id:
                related_ids.add(local_job_id)
            if root_job_id:
                related_ids.add(root_job_id)
    now = _now()
    for related_id in related_ids:
        if not related_id or not isinstance(updated.get(related_id), Mapping):
            continue
        item = dict(updated.get(related_id) or {})
        item["status"] = status
        item["finished_at"] = now
        if detail:
            item["detail"] = detail
        updated[related_id] = item
    session["pending_llm_jobs"] = updated


def _active_llm_job(session: Mapping[str, Any]) -> dict[str, Any] | None:
    if isinstance(session, dict):
        _reconcile_pending_llm_jobs_from_revisions(session)
        _normalise_pending_llm_jobs(session)
    pending = session.get("pending_llm_jobs") if isinstance(session.get("pending_llm_jobs"), Mapping) else {}
    if not isinstance(pending, Mapping):
        return None
    try:
        stale_after_s = max(float(_builder_llm_job_timeout_s()) + 60.0, 300.0)
    except Exception:
        stale_after_s = 300.0
    now = _now()
    candidates: list[dict[str, Any]] = []
    for key, value in pending.items():
        if not isinstance(value, Mapping):
            continue
        status = str(value.get("status") or "").strip().lower()
        if status not in _ACTIVE_LLM_JOB_STATUSES:
            continue
        ts = value.get("submitted_at") or value.get("created_at") or value.get("started_at")
        try:
            age_s = max(0.0, now - float(ts))
        except Exception:
            age_s = 0.0
        if age_s > stale_after_s:
            continue
        item = dict(value)
        item.setdefault("job_id", str(key))
        item["age_s"] = age_s
        candidates.append(item)
    if not candidates:
        return None
    candidates.sort(key=lambda item: float(item.get("created_at") or item.get("submitted_at") or 0.0))
    return candidates[0]


def _complete_llm_webui_job(
    *,
    ws: str,
    session_id: str,
    binding: Mapping[str, Any],
    patch: Mapping[str, Any],
    request_text: str,
    before_webui: Mapping[str, Any] | None,
    job_id: str,
    base_url: str,
    request_id: str,
    auto_apply: bool,
    _meta: Mapping[str, Any] | None,
    local_job_id: str | None = None,
) -> None:
    session = _load_session(ws, session_id)
    if not session:
        return
    _ensure_llm_job_link(
        session,
        local_job_id=local_job_id,
        root_job_id=job_id,
        request_id=request_id,
        base_url=base_url,
        request_text=request_text,
        patch_id=patch.get("id") if isinstance(patch, Mapping) else None,
        model=_builder_llm_model_for_session(session, _meta),
        status="submitted",
    )
    topic = _builder_topic_ref(ws, session=session, binding=binding, _meta=_meta)
    started_at = _now()
    timeout_s = _builder_llm_job_timeout_s()
    poll_interval_s = _builder_llm_job_poll_interval_s()
    _LOG.debug(
        "builder LLM job wait start scenario=%s job_id=%s request_id=%s base_url=%s timeout_s=%.1f poll_interval_s=%.1f",
        str(session.get("scenario_id") or ""),
        job_id,
        request_id,
        base_url,
        timeout_s,
        poll_interval_s,
    )
    try:
        from adaos.sdk.llm.llm_client import wait_response_job

        job = wait_response_job(
            job_id,
            base_url=base_url or None,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
    except Exception as exc:
        _LOG.warning(
            "builder LLM job wait failed scenario=%s job_id=%s request_id=%s base_url=%s elapsed_ms=%d detail=%s",
            str(session.get("scenario_id") or ""),
            job_id,
            request_id,
            base_url,
            int((_now() - started_at) * 1000),
            f"{type(exc).__name__}: {exc}",
        )
        _mark_llm_job_failed(
            ws=ws,
            session=session,
            job_id=job_id,
            detail=f"{type(exc).__name__}: {exc}",
            binding=binding,
            topic_ref=topic,
            _meta=_meta,
        )
        return
    status = str(job.get("status") or "").strip().lower()
    _LOG.debug(
        "builder LLM job wait completed scenario=%s job_id=%s request_id=%s base_url=%s status=%s elapsed_ms=%d",
        str(session.get("scenario_id") or ""),
        job_id,
        request_id,
        base_url,
        status,
        int((_now() - started_at) * 1000),
    )
    if status != "succeeded":
        _LOG.warning(
            "builder LLM job returned non-success scenario=%s job_id=%s request_id=%s status=%s error=%s",
            str(session.get("scenario_id") or ""),
            job_id,
            request_id,
            status,
            str(job.get("error") or ""),
        )
        _mark_llm_job_failed(
            ws=ws,
            session=session,
            job_id=job_id,
            detail=str(job.get("error") or job),
            binding=binding,
            topic_ref=topic,
            _meta=_meta,
        )
        return
    output_text = str(job.get("output_text") or "")
    previous_preview = (
        before_webui.get("preview_state")
        if isinstance(before_webui, Mapping) and isinstance(before_webui.get("preview_state"), Mapping)
        else session.get("preview_state")
        if isinstance(session.get("preview_state"), Mapping)
        else {}
    )
    try:
        llm_result = _parse_llm_webui_transform_output(
            output_text=output_text,
            previous_preview=previous_preview,
            request_id=request_id,
            job_id=job_id,
        )
    except Exception as exc:
        llm_result = _repair_llm_webui_transform_output(
            session=session,
            instruction=request_text,
            previous_preview=previous_preview,
            output_text=output_text,
            validation_error={"error": "llm_response_parse_failed", "detail": f"{type(exc).__name__}: {exc}"},
            request_id=request_id,
            job_id=job_id,
            _meta=_meta,
        )
    if not llm_result.get("ok"):
        llm_result = _repair_llm_webui_transform_output(
            session=session,
            instruction=request_text,
            previous_preview=previous_preview,
            output_text=str(llm_result.get("last_response") or output_text),
            validation_error=dict(llm_result.get("validation") or {
                "error": llm_result.get("error") or "invalid_llm_response",
                "detail": llm_result.get("detail") or "",
            }),
            request_id=request_id,
            job_id=job_id,
            _meta=_meta,
        )
        if not llm_result.get("ok"):
            _mark_llm_job_failed(
                ws=ws,
                session=session,
                job_id=job_id,
                detail=str(llm_result.get("detail") or llm_result.get("error") or "invalid_llm_response"),
                binding=binding,
                topic_ref=topic,
                _meta=_meta,
            )
            return
    llm_result["job"] = job
    model_id = _builder_llm_model_for_session(session, _meta)
    if model_id:
        llm_result["model"] = model_id
    llm_result["profile"] = _builder_llm_prompt_profile(model_id)
    _update_llm_job_status(session, job_id, "succeeded")
    result = _finalize_llm_webui_transform_result(
        ws=ws,
        session=session,
        binding=binding,
        patch=dict(patch),
        request_text=request_text,
        before_webui=before_webui,
        llm_result=llm_result,
        auto_apply=auto_apply,
        _meta=_meta,
    )
    _LOG.debug(
        "builder LLM job applied scenario=%s job_id=%s request_id=%s elapsed_ms=%d ok=%s",
        str(session.get("scenario_id") or ""),
        job_id,
        request_id,
        int((_now() - started_at) * 1000),
        bool(result.get("ok", True)) if isinstance(result, Mapping) else True,
    )
    _safe_emit_chat(
        str(result.get("message") or ""),
        webspace_id=ws,
        _meta=_meta,
        session=result.get("session") if isinstance(result.get("session"), Mapping) else session,
        binding=binding,
        topic_ref=result.get("topic") if isinstance(result.get("topic"), Mapping) else topic,
        actions=result.get("message_actions") if isinstance(result.get("message_actions"), list) else None,
    )


def _start_llm_webui_job_worker(
    *,
    ws: str,
    session: Mapping[str, Any],
    binding: Mapping[str, Any],
    patch: Mapping[str, Any],
    request_text: str,
    before_webui: Mapping[str, Any] | None,
    job_id: str,
    base_url: str,
    request_id: str,
    auto_apply: bool,
    _meta: Mapping[str, Any] | None,
) -> None:
    thread = threading.Thread(
        target=_complete_llm_webui_job,
        kwargs={
            "ws": ws,
            "session_id": str(session.get("id") or ""),
            "binding": dict(binding),
            "patch": dict(patch),
            "request_text": request_text,
            "before_webui": copy.deepcopy(dict(before_webui or {})),
            "job_id": job_id,
            "base_url": base_url,
            "request_id": request_id,
            "local_job_id": None,
            "auto_apply": auto_apply,
            "_meta": dict(_meta or {}),
        },
        name=f"builder-llm-job:{job_id}",
        daemon=True,
    )
    thread.start()


def _local_llm_job_id(session: Mapping[str, Any], instruction: str) -> str:
    seed = f"{session.get('id') or ''}:{session.get('scenario_id') or ''}:{instruction}:{_now()}"
    return f"builder_llm_submit_{_hash_suffix(seed)}"


def _start_llm_webui_submit_worker(
    *,
    ws: str,
    session: Mapping[str, Any],
    binding: Mapping[str, Any],
    patch: Mapping[str, Any],
    request_text: str,
    before_webui: Mapping[str, Any] | None,
    local_job_id: str,
    auto_apply: bool,
    _meta: Mapping[str, Any] | None,
) -> None:
    def _runner() -> None:
        loaded = _load_session(ws, str(session.get("id") or "")) or copy.deepcopy(dict(session))
        preview_state = loaded.get("preview_state") if isinstance(loaded.get("preview_state"), Mapping) else _preview_state(session=loaded)
        submit_result = _submit_llm_webui_transform_job(
            session=loaded,
            instruction=request_text,
            preview_state=preview_state,
            job_nonce=local_job_id,
            _meta=_meta,
        )
        topic = _builder_topic_ref(ws, session=loaded, binding=binding, _meta=_meta)
        if not submit_result.get("pending"):
            _mark_llm_job_failed(
                ws=ws,
                session=loaded,
                job_id=local_job_id,
                detail=str(submit_result.get("detail") or submit_result.get("error") or "llm_job_submit_failed"),
                binding=binding,
                topic_ref=topic,
                _meta=_meta,
            )
            return
        job_id = str(submit_result.get("job_id") or "").strip()
        request_id = str(submit_result.get("request_id") or "").strip()
        base_url = str(submit_result.get("base_url") or "").strip()
        pending_jobs = dict(loaded.get("pending_llm_jobs") or {}) if isinstance(loaded.get("pending_llm_jobs"), Mapping) else {}
        local_entry = dict(pending_jobs.get(local_job_id) or {})
        local_entry.update(
            {
                "status": "submitted",
                "root_job_id": job_id,
                "request_id": request_id,
                "base_url": base_url or None,
                "submitted_at": _now(),
                "timing": dict(submit_result.get("timing") or {}) if isinstance(submit_result.get("timing"), Mapping) else {},
            }
        )
        pending_jobs[local_job_id] = local_entry
        pending_jobs[job_id] = {
            "schema": "adaos.builder.llm_job.v1",
            "job_id": job_id,
            "local_job_id": local_job_id,
            "request_id": request_id,
            "base_url": base_url,
            "status": str(submit_result.get("status") or "queued"),
            "request_text": request_text,
            "patch_id": patch.get("id"),
            "created_at": _now(),
            "timing": dict(submit_result.get("timing") or {}) if isinstance(submit_result.get("timing"), Mapping) else {},
        }
        loaded["pending_llm_jobs"] = pending_jobs
        _save_session(ws, loaded)
        _safe_emit_chat(
            str(submit_result.get("message") or (
                f"{AGENT_LABEL}: отправил LLM-задачу для {loaded.get('scenario_id')}. Job: {job_id}."
            )),
            webspace_id=ws,
            _meta=_meta,
            session=loaded,
            binding=binding,
            topic_ref=topic,
        )
        _complete_llm_webui_job(
            ws=ws,
            session_id=str(loaded.get("id") or session.get("id") or ""),
            binding=binding,
            patch=patch,
            request_text=request_text,
            before_webui=before_webui,
            job_id=job_id,
            base_url=base_url,
            request_id=request_id,
            local_job_id=local_job_id,
            auto_apply=auto_apply,
            _meta=_meta,
        )

    thread = threading.Thread(
        target=_runner,
        name=f"builder-llm-submit:{local_job_id}",
        daemon=True,
    )
    thread.start()


@tool(summary="Update current scenario prototype.", side_effects="local_write")
def update_current_scenario(
    instruction: str,
    webspace_id: str | None = None,
    auto_apply: bool = True,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    ws = _source_webspace_id(webspace_id, _meta)
    source_done_at = time.perf_counter()
    requested_binding = _align_workbench_binding_to_meta(ws, _meta)
    binding_align_done_at = time.perf_counter()
    session, binding = _target_session(ws)
    if requested_binding is not None:
        binding.update(requested_binding)
    target_done_at = time.perf_counter()
    topic = _builder_topic_ref(ws, session=session, binding=binding, _meta=_meta)
    topic_done_at = time.perf_counter()
    if not session:
        message = _target_required_message(binding)
        if _is_api_tool_call(_meta):
            request_text = str(instruction or "").strip()
            if request_text:
                _safe_emit_chat(
                    request_text,
                    webspace_id=ws,
                    _meta=_api_request_chat_meta(_meta),
                    binding=binding,
                    topic_ref=topic,
                    from_="api",
                )
            _safe_emit_chat(message, webspace_id=ws, _meta=_meta, binding=binding, topic_ref=topic)
        return {
            "ok": True,
            "status": "target_required",
            "needs_selection": True,
            "message": message,
            "binding": binding,
            "topic": topic,
            "dialog": _dialog_state(ws, topic_ref=topic),
        }
    text = _instruction_with_prototype_review_notes(instruction, _meta)
    lowered = text.lower()
    patch = {
        "id": f"patch_{_hash_suffix(session['id'] + text + str(_now()))}",
        "target": "ui",
        "operation": "noop",
        "status": "applied" if auto_apply else "proposed",
        "created_by": "llm_agent",
        "created_at": _now(),
        "summary": text,
        "diff": {},
    }
    fields = [dict(item) for item in session.get("fields", []) if isinstance(item, Mapping)]
    filters = [dict(item) for item in session.get("filters", []) if isinstance(item, Mapping)]
    base_preview = session.get("preview_state") if isinstance(session.get("preview_state"), dict) else _preview_state(session=session)
    preview_done_at = time.perf_counter()
    before_webui = _current_webui_payload(session, base_preview)
    before_webui_done_at = time.perf_counter()
    if _is_api_tool_call(_meta) and text:
        _safe_emit_chat(
            text,
            webspace_id=ws,
            _meta=_api_request_chat_meta(_meta),
            session=session,
            binding=binding,
            topic_ref=topic,
            from_="api",
        )
    llm_result: dict[str, Any] | None = None
    llm_owned_content_change = _wants_llm_owned_content_change(text)
    if text and _builder_llm_primary_enabled(_meta):
        if _builder_llm_async_enabled(_meta):
            active_job = _active_llm_job(session)
            if active_job:
                active_job_id = (
                    str(active_job.get("root_job_id") or "").strip()
                    or str(active_job.get("job_id") or "").strip()
                    or str(active_job.get("local_job_id") or "").strip()
                )
                status = str(active_job.get("status") or "").strip() or "running"
                message = (
                    f"{AGENT_LABEL}: LLM-задача для {session.get('scenario_id')} еще выполняется"
                    f"{f' ({active_job_id})' if active_job_id else ''}. "
                    "Дождитесь результата и повторите запрос."
                )
                if _is_api_tool_call(_meta):
                    _safe_emit_chat(message, webspace_id=ws, _meta=_meta, session=session, binding=binding, topic_ref=topic)
                return {
                    "ok": True,
                    "status": "llm_busy",
                    "session_id": session.get("id"),
                    "scenario_id": session.get("scenario_id"),
                    "active_llm_job": {
                        "job_id": active_job_id,
                        "status": status,
                        "age_s": active_job.get("age_s"),
                    },
                    "message": message,
                    "topic": {k: v for k, v in topic.items() if k != "stored"},
                    "dialog": _dialog_state(ws, topic_ref=topic),
                }
            local_job_id = _local_llm_job_id(session, text)
            selected_model = _builder_llm_model_for_session(session, _meta)
            pending_jobs = dict(session.get("pending_llm_jobs") or {}) if isinstance(session.get("pending_llm_jobs"), Mapping) else {}
            pending_jobs[local_job_id] = {
                "schema": "adaos.builder.llm_job.v1",
                "job_id": local_job_id,
                "status": "submitting",
                "request_text": text,
                "patch_id": patch.get("id"),
                "model": selected_model,
                "created_at": _now(),
            }
            session["pending_llm_jobs"] = pending_jobs
            _save_session(ws, session)
            save_done_at = time.perf_counter()
            _start_llm_webui_submit_worker(
                ws=ws,
                session=session,
                binding=binding,
                patch=patch,
                request_text=text,
                before_webui=before_webui,
                local_job_id=local_job_id,
                auto_apply=auto_apply,
                _meta=_meta,
            )
            worker_done_at = time.perf_counter()
            dialog = _dialog_state(ws, topic_ref=topic)
            dialog_done_at = time.perf_counter()
            total_ms = (dialog_done_at - started_at) * 1000.0
            if total_ms >= 1000:
                _LOG.warning(
                    "builder update async prepare slow scenario=%s total_ms=%.1f source_ms=%.1f binding_align_ms=%.1f target_ms=%.1f topic_ms=%.1f preview_ms=%.1f before_webui_ms=%.1f save_ms=%.1f worker_ms=%.1f dialog_ms=%.1f",
                    str(session.get("scenario_id") or ""),
                    total_ms,
                    (source_done_at - started_at) * 1000.0,
                    (binding_align_done_at - source_done_at) * 1000.0,
                    (target_done_at - binding_align_done_at) * 1000.0,
                    (topic_done_at - target_done_at) * 1000.0,
                    (preview_done_at - topic_done_at) * 1000.0,
                    (before_webui_done_at - preview_done_at) * 1000.0,
                    (save_done_at - before_webui_done_at) * 1000.0,
                    (worker_done_at - save_done_at) * 1000.0,
                    (dialog_done_at - worker_done_at) * 1000.0,
                )
            message = (
                f"{AGENT_LABEL}: \u043f\u0440\u0438\u043d\u044f\u043b \u0437\u0430\u043f\u0440\u043e\u0441 \u0434\u043b\u044f {session.get('scenario_id')} "
                f"\u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u044e LLM-\u0437\u0430\u0434\u0430\u0447\u0443. Job: {local_job_id}."
            )
            if _is_api_tool_call(_meta):
                _safe_emit_chat(message, webspace_id=ws, _meta=_meta, session=session, binding=binding, topic_ref=topic)
            return {
                "ok": True,
                "status": "llm_submitting",
                "session_id": session.get("id"),
                "scenario_id": session.get("scenario_id"),
                "patch": patch,
                "preview_state": base_preview,
                "topic": {k: v for k, v in topic.items() if k != "stored"},
                "pending_action": None,
                "llm_job": {
                    "job_id": local_job_id,
                    "status": "submitting",
                    "request_id": "",
                    "base_url": None,
                    "timing": {"submit_deferred": True},
                },
                "message": message,
                "dialog": dialog,
            }
        else:
            llm_result = _apply_llm_webui_transform(session=session, instruction=text, preview_state=base_preview, _meta=_meta)
        if llm_result.get("ok"):
            return _finalize_llm_webui_transform_result(
                ws=ws,
                session=session,
                binding=binding,
                patch=patch,
                request_text=text,
                before_webui=before_webui,
                llm_result=llm_result,
                auto_apply=auto_apply,
                _meta=_meta,
            )
    if llm_owned_content_change and patch["operation"] == "noop":
        patch["diff"] = {
            "llm_required": True,
            "llm_fallback": llm_result or {"ok": False, "error": "llm_disabled"},
        }
    if _wants_swap_input_and_cards(text):
        session["card_view"] = True
        session["hide_table"] = True
        session["layout_order"] = "cards_first"
        session["card_preview_key"] = str(session.get("card_preview_key") or "").strip() or _preferred_card_preview_key(fields)
        patch["operation"] = "swap_layout_areas"
        patch["diff"] = {
            "layout_order": "cards_first",
            "form_area": "right",
            "cards_area": "main",
            "hide_table": True,
            "card_preview_key": session["card_preview_key"],
        }
        lowered = ""
    elif _wants_card_text_preview(text):
        session["card_view"] = True
        session["card_preview_key"] = _preferred_card_preview_key(fields, prefer_text=True)
        patch["operation"] = "set_card_preview"
        patch["diff"] = {"card_preview_key": session["card_preview_key"], "card_view": True}
        lowered = ""
    elif _wants_card_view(text):
        session["card_view"] = True
        session["hide_table"] = True
        patch["operation"] = "change_view_representation"
        patch["diff"] = {"card_view": True, "hide_table": True}
        lowered = ""
    elif _wants_hide_list_or_table(text):
        session["hide_table"] = True
        if _wants_card_view(text) or session.get("card_view"):
            session["card_view"] = True
        patch["operation"] = "change_view_representation"
        patch["diff"] = {"card_view": bool(session.get("card_view")), "hide_table": True}
        lowered = ""
    elif _wants_execution_checkbox(text):
        label = "\u0418\u0441\u043f\u043e\u043b\u043d\u0435\u043d\u043e" if _text_contains_any(
            text,
            ("\u0438\u0441\u043f\u043e\u043b\u043d", "\u0432\u044b\u043f\u043e\u043b\u043d", "complete", "execution"),
        ) else "\u041a\u0443\u043f\u043b\u0435\u043d\u043e"
        fields, field, added = _ensure_field(fields, label=label, field_id="done", field_type="boolean")
        session["fields"] = fields
        patch["operation"] = "add_field" if added else "ensure_field"
        patch["diff"] = {"field": field, "added": added, "component": "checkbox"}
        lowered = ""
    elif _wants_english_ui(text) and not llm_owned_content_change:
        _translate_session_to_english(session, fields)
        patch["operation"] = "translate_ui"
        patch["diff"] = {"locale": "en", "fields": [dict(item) for item in session.get("fields", []) if isinstance(item, Mapping)]}
        lowered = ""
    if any(token in lowered for token in ("карточ", "card")):
        session["card_view"] = True
        patch["operation"] = "change_view_representation"
        session["hide_table"] = True
        patch["diff"] = {"card_view": True, "hide_table": True}
    elif any(token in lowered for token in ("убери", "удали", "remove")):
        label = _extract_field_label(text) or text.rsplit(" ", 1)[-1]
        fid = _field_id(label)
        before = len(fields)
        fields = [item for item in fields if str(item.get("id")) != fid and str(item.get("label") or "").lower() != label.lower()]
        session["fields"] = fields
        patch["operation"] = "remove_field"
        patch["diff"] = {"field_id": fid, "removed": before != len(fields), "warning": "existing records may still contain this field"}
    elif _wants_add_button_above_form(text):
        session["form_action_position"] = "top"
        patch["operation"] = "move_form_action"
        patch["diff"] = {"form_id": "prototype-form", "action_id": "add_item", "submitPlacement": "top"}
    elif _wants_done_checkbox_first(text):
        fields, field, added = _ensure_field(fields, label="\u041a\u0443\u043f\u043b\u0435\u043d\u043e", field_id="done", field_type="boolean")
        fields = _move_field_first(fields, "done")
        session["fields"] = fields
        patch["operation"] = "set_checkbox_column"
        patch["diff"] = {
            "field": field,
            "added": added,
            "field_order": [str(item.get("id") or "") for item in fields],
            "table_column": {"key": "done", "kind": "boolean", "position": 0},
        }
    elif _requested_known_fields(text) or _requested_filter_field_ids(text):
        applied: list[str] = []
        changed_fields: list[dict[str, Any]] = []
        changed_filters: list[dict[str, Any]] = []
        not_implemented: list[str] = []

        for spec in _requested_known_fields(text):
            fields, field, added = _ensure_field(
                fields,
                label=str(spec["label"]),
                field_id=str(spec["field_id"]),
                field_type=str(spec["field_type"]),
            )
            changed_fields.append(dict(field))
            applied.append("add_field" if added else "ensure_field")

        fields_by_id = {str(item.get("id") or ""): item for item in fields}
        for field_id in _requested_filter_field_ids(text):
            field = fields_by_id.get(field_id)
            if field is None and field_id in {"done", "availability"}:
                fields, field, _added = _ensure_field(
                    fields,
                    label=_default_label_for_field(field_id),
                    field_id=field_id,
                    field_type="boolean" if field_id == "done" else "string",
                )
                fields_by_id[field_id] = field
                changed_fields.append(dict(field))
            if field is None:
                not_implemented.append(f"filter:{field_id}")
                continue
            filters, filter_obj, added = _ensure_filter(filters, field)
            changed_filters.append(dict(filter_obj))
            applied.append("add_filter" if added else "ensure_filter")

        session["fields"] = fields
        session["filters"] = filters
        unique_applied = list(dict.fromkeys(applied))
        patch["operation"] = unique_applied[0] if len(unique_applied) == 1 else "multi_update"
        patch["status"] = "partial" if not_implemented else patch["status"]
        patch["diff"] = {
            "fields": changed_fields,
            "filters": changed_filters,
            "datasource_id": session.get("datasource_id") or "items",
            "applied_operations": unique_applied,
            "not_implemented": not_implemented,
        }
    elif _mentions_date(text) and ("field" in lowered or "column" in lowered or "\u043f\u043e\u043b\u0435" in lowered or "\u043a\u043e\u043b\u043e\u043d" in lowered or _wants_date_values(text)):
        fields, field, added = _ensure_field(fields, label="\u0414\u0430\u0442\u0430", field_id="date", field_type="date")
        session["fields"] = fields
        rows = _date_mock_rows(fields, session.get("mock_rows"))
        session["mock_rows"] = rows
        patch["operation"] = "add_field" if added else "update_mock_data"
        patch["diff"] = {
            "field": field,
            "added": added,
            "datasource_id": session.get("datasource_id") or "items",
            "rows": rows,
        }
    else:
        label = _extract_field_label(text) or ("\u0426\u0435\u043d\u0430" if _text_contains_any(text, ("\u0446\u0435\u043d", "price")) else None)
        if label:
            fid = _field_id(label)
            if not any(str(item.get("id")) == fid for item in fields):
                field = {"id": fid, "type": _field_type_for_id(fid, label), "label": _default_label_for_field(fid, label), "required": False}
                fields.append(field)
                session["fields"] = fields
                patch["operation"] = "add_field"
                patch["diff"] = {"field": field}
    if patch["operation"] == "noop":
        llm_patch = llm_result
        if (
            llm_patch is None
            and text
            and _builder_llm_primary_enabled(_meta)
            and not _builder_llm_async_enabled(_meta)
        ):
            llm_patch = _apply_llm_webui_transform(session=session, instruction=text, preview_state=base_preview, _meta=_meta)
        if isinstance(llm_patch, Mapping) and llm_patch.get("ok"):
            llm_result = dict(llm_patch)
            preview_from_llm = llm_patch.get("preview_state") if isinstance(llm_patch.get("preview_state"), Mapping) else base_preview
            payload_from_llm = llm_patch.get("payload") if isinstance(llm_patch.get("payload"), Mapping) else None
            patch["operation"] = "llm_webui_transform"
            patch["diff"] = {
                "schema_valid": True,
                "comment": str(llm_patch.get("comment") or ""),
                "unable_reason": str(llm_patch.get("unable_reason") or ""),
                "validation": dict(llm_patch.get("validation") or {}) if isinstance(llm_patch.get("validation"), Mapping) else {},
                "attempts": list(llm_patch.get("attempts") or []) if isinstance(llm_patch.get("attempts"), list) else [],
            }
            session["preview_state"] = copy.deepcopy(dict(preview_from_llm))
            if payload_from_llm is not None:
                session["webui_payload"] = copy.deepcopy(dict(payload_from_llm))
            _merge_session_from_preview(session, preview_from_llm)
        else:
            existing_diff = dict(patch.get("diff") or {}) if isinstance(patch.get("diff"), Mapping) else {}
            existing_diff["llm_fallback"] = llm_patch or existing_diff.get("llm_fallback") or {"ok": False, "error": "llm_disabled"}
            patch["diff"] = existing_diff
    if patch["operation"] == "noop":
        if not isinstance(session.get("user_summary"), Mapping):
            session["user_summary"] = _draft_user_summary(session)
        preview = session.get("preview_state") if isinstance(session.get("preview_state"), dict) else _preview_state(session=session)
        workbench = _ensure_workbench(ws, session=session, preview_state=preview)
        binding = workbench.get("binding") if isinstance(workbench.get("binding"), Mapping) else binding
        topic = _builder_topic_ref(ws, session=session, binding=binding, _meta=_meta)
        llm_failure = llm_result if isinstance(llm_result, Mapping) else patch.get("diff", {}).get("llm_fallback") if isinstance(patch.get("diff"), Mapping) else None
        diagnostic = _llm_failure_summary(llm_failure if isinstance(llm_failure, Mapping) else None)
        diagnostic_text = f" LLM: {diagnostic}" if diagnostic else ""
        if _llm_failure_is_timeout(llm_failure if isinstance(llm_failure, Mapping) else None):
            message = (
                f"{AGENT_LABEL}: \u043d\u0435 \u0434\u043e\u0436\u0434\u0430\u043b\u0441\u044f \u043e\u0442\u0432\u0435\u0442\u0430 LLM "
                f"\u0434\u043b\u044f {session.get('scenario_id')}. UI \u043d\u0435 \u0438\u0437\u043c\u0435\u043d\u0435\u043d. "
                f"\u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0435 \u0437\u0430\u043f\u0440\u043e\u0441 \u0438\u043b\u0438 \u0443\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u0435\u0433\u043e \u043a\u043e\u0440\u043e\u0447\u0435.{diagnostic_text}"
            )
        else:
            message = (
                f"{AGENT_LABEL}: \u043d\u0435 \u0441\u043c\u043e\u0433 \u0431\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e "
                f"\u043f\u0440\u0438\u043c\u0435\u043d\u0438\u0442\u044c \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0435 \u0434\u043b\u044f {session.get('scenario_id')}. "
                f"\u0421\u0432\u043e\u0431\u043e\u0434\u043d\u0430\u044f \u0442\u0440\u0430\u043d\u0441\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u044f UI "
                f"\u043d\u0435 \u043f\u0440\u043e\u0448\u043b\u0430 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0443 \u0438\u043b\u0438 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430.{diagnostic_text}"
            )
        return {
            "ok": True,
            "status": "noop",
            "session_id": session.get("id"),
            "scenario_id": session.get("scenario_id"),
            "patch": patch,
            "preview_state": preview,
            "workbench": workbench,
            "topic": {k: v for k, v in topic.items() if k != "stored"},
            "pending_action": None,
            "message": message,
            "dialog": _dialog_state(ws, topic_ref=topic),
        }
    if patch["operation"] != "llm_webui_transform" and isinstance(session.get("preview_state"), dict):
        preview_for_rebuild = copy.deepcopy(dict(session["preview_state"]))
        preview_for_rebuild.pop("page_schema", None)
        session["preview_state"] = preview_for_rebuild
    return _finalize_scenario_update(
        ws=ws,
        session=session,
        binding=binding,
        patch=patch,
        request_text=text,
        before_webui=before_webui,
        llm_result=llm_result,
        auto_apply=auto_apply,
        _meta=_meta,
    )


@tool(summary="Get Builder session.", side_effects="none")
def get_session(
    session_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session = _load_session(ws, session_id)
    preview = (session or {}).get("preview_state") if isinstance(session, dict) else None
    workbench = _ensure_workbench(ws, session=session, preview_state=preview, refresh_runtime=False)
    binding = workbench.get("binding") if isinstance(workbench.get("binding"), Mapping) else {}
    session_changed = False
    if isinstance(session, dict):
        session_changed = _reconcile_pending_llm_jobs_from_revisions(session)
    if isinstance(session, dict) and _sync_session_from_artifacts(session, binding):
        session_changed = True
    if isinstance(session, dict) and session_changed:
        _save_session(ws, session)
        preview = session.get("preview_state") if isinstance(session.get("preview_state"), Mapping) else preview
    topic = _builder_topic_ref(ws, session=session, binding=binding, _meta=_meta)
    return {
        "ok": bool(session),
        "session": session,
        "developer_evidence": _developer_evidence(
            webspace_id=ws,
            session=session,
            preview_state=preview if isinstance(preview, Mapping) else None,
            workbench=workbench,
            topic_ref=topic,
            _meta=_meta,
        ),
        "workbench": workbench,
        "dialog": _dialog_state(ws, topic_ref=topic),
    }


@tool(summary="Get Builder preview state.", side_effects="none")
def get_preview_state(
    session_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session = _load_session(ws, session_id)
    if not session:
        return {"ok": False, "error": "session_not_found", "preview_state": None, "dialog": _dialog_state(ws)}
    preview = session.get("preview_state") if isinstance(session.get("preview_state"), dict) else _preview_state(session=session)
    workbench = _ensure_workbench(ws, session=session, preview_state=preview)
    binding = workbench.get("binding") if isinstance(workbench.get("binding"), Mapping) else {}
    if _sync_session_from_artifacts(session, binding):
        _save_session(ws, session)
        preview = session.get("preview_state") if isinstance(session.get("preview_state"), dict) else preview
    topic = _builder_topic_ref(ws, session=session, binding=binding, _meta=_meta)
    return {
        "ok": True,
        "session_id": session.get("id"),
        "preview_state": preview,
        "developer_evidence": _developer_evidence(
            webspace_id=ws,
            session=session,
            preview_state=preview,
            workbench=workbench,
            topic_ref=topic,
            _meta=_meta,
        ),
        "workbench": workbench,
        "dialog": _dialog_state(ws, topic_ref=topic),
    }


@tool(summary="Restore a stored Builder UI revision as current.", side_effects="local_write")
def set_ui_revision_current(
    revision: str,
    session_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    timings_ms: dict[str, float] = {}
    ws = _source_webspace_id(webspace_id, _meta)
    stage_started = time.perf_counter()
    session = _load_session(ws, session_id)
    timings_ms["load_session"] = _elapsed_ms(stage_started)
    if not session:
        message = _builder_revision_message("session_not_found")
        stage_started = time.perf_counter()
        _safe_emit_chat(message, webspace_id=ws, _meta=_meta)
        timings_ms["emit_chat"] = _elapsed_ms(stage_started)
        timings_ms["total"] = _elapsed_ms(started_at)
        return {"ok": False, "error": "session_not_found", "message": message, "dialog": _dialog_state(ws), "timings_ms": timings_ms}
    stage_started = time.perf_counter()
    revision_payload = _read_ui_revision(session, revision)
    timings_ms["read_revision"] = _elapsed_ms(stage_started)
    if not revision_payload:
        message = _builder_revision_message("revision_not_found", revision=revision, scenario_id=session.get("scenario_id"))
        stage_started = time.perf_counter()
        _safe_emit_chat(message, webspace_id=ws, _meta=_meta, session=session)
        timings_ms["emit_chat"] = _elapsed_ms(stage_started)
        timings_ms["total"] = _elapsed_ms(started_at)
        return {"ok": False, "error": "revision_not_found", "message": message, "dialog": _dialog_state(ws), "timings_ms": timings_ms}
    after_webui = revision_payload.get("after_webui") if isinstance(revision_payload.get("after_webui"), Mapping) else {}
    preview = after_webui.get("preview_state") if isinstance(after_webui.get("preview_state"), Mapping) else revision_payload.get("preview_state")
    if not isinstance(after_webui, Mapping) or not isinstance(preview, Mapping):
        message = _builder_revision_message(
            "revision_invalid",
            revision=str(revision_payload.get("revision") or revision),
            scenario_id=session.get("scenario_id"),
        )
        stage_started = time.perf_counter()
        _safe_emit_chat(message, webspace_id=ws, _meta=_meta, session=session)
        timings_ms["emit_chat"] = _elapsed_ms(stage_started)
        timings_ms["total"] = _elapsed_ms(started_at)
        return {"ok": False, "error": "revision_invalid", "message": message, "dialog": _dialog_state(ws), "timings_ms": timings_ms}
    stage_started = time.perf_counter()
    validation = _validate_builder_webui_payload(after_webui, preview)
    timings_ms["validate_webui"] = _elapsed_ms(stage_started)
    if not validation.get("ok"):
        message = _builder_revision_message(
            "revision_validation_failed",
            revision=str(revision_payload.get("revision") or revision),
            scenario_id=session.get("scenario_id"),
            detail=validation.get("detail") or validation.get("error"),
        )
        stage_started = time.perf_counter()
        _safe_emit_chat(message, webspace_id=ws, _meta=_meta, session=session)
        timings_ms["emit_chat"] = _elapsed_ms(stage_started)
        timings_ms["total"] = _elapsed_ms(started_at)
        return {"ok": False, "error": "revision_validation_failed", "validation": validation, "message": message, "dialog": _dialog_state(ws), "timings_ms": timings_ms}
    stage_started = time.perf_counter()
    session["preview_state"] = copy.deepcopy(dict(preview))
    session["webui_payload"] = copy.deepcopy(dict(after_webui))
    session["ui_revision"] = str(revision_payload.get("revision") or revision)
    _merge_session_from_preview(session, preview)
    timings_ms["update_session"] = _elapsed_ms(stage_started)
    stage_started = time.perf_counter()
    _write_webui_payload(str(session.get("artifact_root") or ""), after_webui)
    timings_ms["write_webui_payload"] = _elapsed_ms(stage_started)
    stage_started = time.perf_counter()
    prompt_files_restore = _restore_prompt_files_from_revision(session, revision_payload)
    timings_ms["restore_prompt_files"] = _elapsed_ms(stage_started)
    stage_started = time.perf_counter()
    workbench = _ensure_workbench(ws, session=session, preview_state=preview, refresh_runtime=False, snapshot_projection=False)
    timings_ms["ensure_workbench"] = _elapsed_ms(stage_started)
    binding = workbench.get("binding") if isinstance(workbench.get("binding"), Mapping) else {}
    stage_started = time.perf_counter()
    dev_runtime_refresh = _schedule_dev_runtime_reload_after_revision(
        ws,
        session=session,
        binding=binding,
        revision=str(session.get("ui_revision") or revision),
        source_fingerprint=_webui_source_fingerprint(after_webui),
        user_id=_meta_user_id(_meta),
        roles=_meta_roles(_meta),
    )
    timings_ms["schedule_dev_runtime_refresh"] = _elapsed_ms(stage_started)
    stage_started = time.perf_counter()
    topic = _builder_topic_ref(ws, session=session, binding=binding, _meta=_meta)
    timings_ms["topic_ref"] = _elapsed_ms(stage_started)
    stage_started = time.perf_counter()
    session["thread_id"] = str(topic.get("thread_id") or "").strip() or None
    session["topic_id"] = str(topic.get("topic_id") or "").strip() or None
    session["topic_ref"] = {k: v for k, v in topic.items() if k != "stored"}
    _save_session(ws, session)
    timings_ms["save_session"] = _elapsed_ms(stage_started)
    message = _builder_revision_message(
        "revision_restored",
        revision=str(session.get("ui_revision") or revision),
        scenario_id=session.get("scenario_id"),
    )
    stage_started = time.perf_counter()
    actions = _revision_chat_actions(session, str(session.get("ui_revision") or ""))
    timings_ms["message_actions"] = _elapsed_ms(stage_started)
    chat_emit = {
        "scheduled": False,
        "mode": "receipt_only",
        "persisted": False,
        "reason": "revision_current_success_not_persistent",
    }
    timings_ms["emit_chat"] = 0.0
    timings_ms["emit_chat_deferred"] = 0.0
    timings_ms["total"] = _elapsed_ms(started_at)
    return {
        "ok": True,
        "session_id": session.get("id"),
        "scenario_id": session.get("scenario_id"),
        "revision": session.get("ui_revision"),
        "preview_state": preview,
        "prompt_files_restore": prompt_files_restore,
        "workbench": workbench,
        "dev_runtime_refresh": dev_runtime_refresh,
        "chat_emit": chat_emit,
        "message": message,
        "message_actions": actions,
        "dialog": _dialog_state(ws, topic_ref=topic),
        "timings_ms": timings_ms,
    }


@tool(summary="Ensure paired Builder Prompt IDE dev webspace.", side_effects="local_write")
def ensure_dev_webspace(
    webspace_id: str | None = None,
    active_draft_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session = _load_session(ws)
    explicit_draft_id = str(active_draft_id or "").strip() or None
    if active_draft_id:
        session = dict(session or {})
        session["draft_id"] = explicit_draft_id
    workbench = _ensure_workbench(ws, session=session, active_draft_id=explicit_draft_id or None)
    if not workbench.get("ok"):
        return {**workbench, "dialog": _dialog_state(ws)}
    return {"ok": True, "binding": workbench["binding"], "workbench": workbench, "dialog": _dialog_state(ws)}


@tool(summary="Return Builder workbench binding.", side_effects="none")
def get_workspace_binding(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    binding = _workbench_service().get_workspace_binding(ws)
    return {"ok": True, "binding": binding, "dialog": _dialog_state(ws)}


@tool(summary="Return URL for paired Builder Prompt IDE dev webspace.", side_effects="local_write")
def open_dev_webspace(
    webspace_id: str | None = None,
    base_url: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session, binding = _target_session(ws)
    workbench = _ensure_workbench(ws, session=session)
    if not workbench.get("ok"):
        return {**workbench, "dialog": _dialog_state(ws)}
    result = _workbench_service().open_dev_webspace(ws, base_url=base_url)
    return {**result, "binding": workbench["binding"], "workbench": workbench, "dialog": _dialog_state(ws)}


@tool(summary="Return embedded Voice Chat widget config for Builder workbench.", side_effects="none")
def attach_dialog_widget(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    binding = _workbench_service().get_workspace_binding(ws)
    widget = binding.get("dialog") if isinstance(binding.get("dialog"), Mapping) else _workbench_service().dialog_widget_config(ws)
    topic = widget.get("topic") if isinstance(widget.get("topic"), Mapping) else None
    return {"ok": True, "widget": widget, "binding": binding, "dialog": _dialog_state(ws, topic_ref=topic)}


@tool(summary="Switch active Builder development draft.", side_effects="local_write")
def set_active_draft(
    draft_id: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    session = _load_session(ws)
    if draft_id and (not session or str(session.get("draft_id") or "") != str(draft_id).strip()):
        sessions = _sessions(ws)
        for item in sessions.values():
            if str(item.get("draft_id") or item.get("id") or "").strip() == str(draft_id).strip():
                session = item
                break
    workbench = _ensure_workbench(
        ws,
        session=session,
        active_draft_id=str(draft_id or "").strip() or None,
        runtime_scenario_id=_runtime_scenario_id(session),
    )
    if not workbench.get("ok"):
        return {**workbench, "dialog": _dialog_state(ws)}
    return {"ok": True, "binding": workbench["binding"], "workbench": workbench, "dialog": _dialog_state(ws)}


@tool(summary="List Builder skills/scenarios in development.", side_effects="none")
def list_development_skills(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    return {**_workbench_service().list_development_skills(ws), "dialog": _dialog_state(ws)}


@tool(summary="Delete Builder development draft.", side_effects="local_write")
def delete_development_skill(
    draft_id: str,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _source_webspace_id(webspace_id, _meta)
    result = _workbench_service().delete_development_skill(draft_id, ws)
    if result.get("ok"):
        _delete_sessions_for_draft(ws, draft_id)
    return {**result, "dialog": _dialog_state(ws)}


def handle(topic: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = dict(payload or {})
    if topic.endswith("start"):
        return start(**data)
    if topic.endswith("create_scenario_draft"):
        return create_scenario_draft(**data)
    if topic.endswith("update_current_scenario"):
        return update_current_scenario(**data)
    if topic.endswith("get_preview_state"):
        return get_preview_state(**data)
    if topic.endswith("set_ui_revision_current"):
        return set_ui_revision_current(**data)
    if topic.endswith("get_session"):
        return get_session(**data)
    if topic.endswith("ensure_dev_webspace"):
        return ensure_dev_webspace(**data)
    if topic.endswith("get_workspace_binding"):
        return get_workspace_binding(**data)
    if topic.endswith("open_dev_webspace"):
        return open_dev_webspace(**data)
    if topic.endswith("attach_dialog_widget"):
        return attach_dialog_widget(**data)
    if topic.endswith("set_active_draft"):
        return set_active_draft(**data)
    if topic.endswith("list_development_skills"):
        return list_development_skills(**data)
    if topic.endswith("delete_development_skill"):
        return delete_development_skill(**data)
    return chat(**data)
