from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.context import clear_current_skill, set_current_skill
from adaos.sdk.data.events import publish
from adaos.sdk.io import stream_publish
from adaos.services.agent_context import get_ctx

try:
    from adaos.services.yjs.webspace import default_webspace_id
except Exception:  # pragma: no cover - used by lightweight handler tests without Yjs deps.
    def default_webspace_id() -> str:
        return "desktop"

try:
    from adaos.sdk.data import ctx_subnet
except Exception:  # pragma: no cover - used by lightweight handler tests without Yjs deps.
    class _NullProjectionCtx:
        def set(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    ctx_subnet = _NullProjectionCtx()

SKILL_NAME = "cv_descriptor"
EVENT_RECEIVER = "cv_descriptor.events"

CURRENT_SLOT = "cv_descriptor.current"
DESCRIPTORS_SLOT = "cv_descriptor.descriptors"
RUNTIME_SLOT = "cv_descriptor.runtime"

_log = logging.getLogger("skills.cv_descriptor")
_projection_fingerprints: dict[str, str] = {}
_stream_fingerprints: dict[str, str] = {}


TRIAL_MOBILENET_MODEL = {
    "id": "tfjs_mobilenet_v2_100_embedding",
    "title": "MobileNetV2 browser embedding",
    "runtime": "tfjs-mobilenet",
    "task": "embed",
    "version": 2,
    "alpha": 1.0,
    "inputSize": 224,
    "inputRange": [0, 1],
    "modelUrl": "https://storage.googleapis.com/tfjs-models/savedmodel/mobilenet_v2_1.0_224/model.json",
    "model_signature": "tfjs-mobilenet-v2-1.0-224@google-storage",
    "status": "trial",
    "description": "Trial TensorFlow.js MobileNetV2 feature-vector model loaded from Google Storage in the browser.",
}

BROWSER_FRAME_MODEL = {
    "id": "browser_embedding_placeholder",
    "title": "Browser embedding placeholder",
    "runtime": "client-cv",
    "task": "embed",
    "model_signature": "browser-frame-embedding@v1",
    "status": "fallback",
    "description": "Deterministic downsampled-frame embedding fallback for browser runtime diagnostics.",
}

DEFAULT_MODEL = TRIAL_MOBILENET_MODEL

DEFAULT_MODEL_OPTIONS = [
    {
        **TRIAL_MOBILENET_MODEL,
        "label": "MobileNetV2 browser embedding",
    },
    {
        **BROWSER_FRAME_MODEL,
        "label": "Browser embedding placeholder",
    },
]

LOW_POWER_CAMERA = {
    "facingMode": "environment",
    "width": {"ideal": 320, "max": 320},
    "height": {"ideal": 240, "max": 240},
    "frameRate": {"ideal": 5, "max": 5},
    "resizeMode": "crop-and-scale",
}

LOW_POWER_TARGET_FPS = 4

_DATA_PROJECTION_ENTRIES = [
    {
        "scope": "subnet",
        "slot": CURRENT_SLOT,
        "targets": [{"backend": "yjs", "path": "data/cv_descriptor/current"}],
    },
    {
        "scope": "subnet",
        "slot": DESCRIPTORS_SLOT,
        "targets": [{"backend": "yjs", "path": "data/cv_descriptor/descriptors"}],
    },
    {
        "scope": "subnet",
        "slot": RUNTIME_SLOT,
        "targets": [{"backend": "yjs", "path": "data/cv_descriptor/runtime"}],
    },
]


def _now() -> float:
    return time.time()


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state_dir() -> Path:
    raw = str(os.getenv("CV_DESCRIPTOR_STATE_DIR") or "").strip()
    if raw:
        path = Path(raw)
        path.mkdir(parents=True, exist_ok=True)
        return path
    try:
        ctx = get_ctx()
        path = Path(ctx.paths.state_dir()) / "skills" / SKILL_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:
        path = Path(__file__).resolve().parents[1] / ".state"
        path.mkdir(parents=True, exist_ok=True)
        return path


def _state_path() -> Path:
    return _state_dir() / "state.json"


def _json_fingerprint(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        raw = repr(value)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _model_signature(model: Mapping[str, Any]) -> str:
    explicit = str(
        model.get("model_signature")
        or model.get("modelSignature")
        or model.get("signature")
        or ""
    ).strip()
    if explicit:
        return explicit
    return "sha1:" + _json_fingerprint(
        {
            key: value
            for key, value in dict(model).items()
            if key not in {"label", "title", "description", "status"}
        }
    )


def _default_state() -> dict[str, Any]:
    model = dict(DEFAULT_MODEL)
    model["model_signature"] = _model_signature(model)
    return {
        "schema": "adaos.cv_descriptor.state.v1",
        "status": "init",
        "mode": "setup",
        "model": model,
        "model_options": _merged_model_options(DEFAULT_MODEL_OPTIONS),
        "matching": {
            "metric": "cosine",
            "threshold": 0.82,
            "top_k": 3,
            "debounce_ms": 300,
            "hysteresis": 0.04,
            "max_targets": 100,
        },
        "descriptors": [],
        "runtime": {
            "desired": None,
            "diagnostics": None,
            "last_event": None,
            "current_match": None,
            "command_seq": 0,
            "client_runtime_required": True,
        },
        "created_at": _iso_now(),
        "updated_at": _iso_now(),
    }


def _merged_model_options(options: Any = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    extra_options = options if isinstance(options, list) else []
    for option in list(DEFAULT_MODEL_OPTIONS) + list(extra_options):
        if not isinstance(option, Mapping):
            continue
        item = dict(option)
        token = str(item.get("id") or "").strip()
        if not token or token in seen:
            continue
        item.setdefault("model_signature", _model_signature(item))
        item.setdefault("label", item.get("title") or token)
        seen.add(token)
        out.append(item)
    return out


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.warning("failed to read cv_descriptor state; using defaults", exc_info=True)
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    state = _default_state()
    state.update(data)
    model = state.get("model") if isinstance(state.get("model"), dict) else dict(DEFAULT_MODEL)
    if (
        str(model.get("id") or "").strip() == "browser_embedding_placeholder"
        and str(model.get("status") or "").strip() == "pending_client_runtime"
        and not state.get("descriptors")
    ):
        model = dict(DEFAULT_MODEL)
    model.setdefault("model_signature", _model_signature(model))
    state["model"] = model
    state["model_options"] = _merged_model_options(state.get("model_options"))
    if not isinstance(state.get("descriptors"), list):
        state["descriptors"] = []
    if not isinstance(state.get("runtime"), dict):
        state["runtime"] = _default_state()["runtime"]
    if not isinstance(state.get("matching"), dict):
        state["matching"] = _default_state()["matching"]
    return state


def _write_state(state: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(state)
    data["updated_at"] = _iso_now()
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return data


def _payload(evt_or_payload: Any) -> dict[str, Any]:
    payload = getattr(evt_or_payload, "payload", evt_or_payload)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _webspace_id_from_payload(payload: Mapping[str, Any] | None = None) -> str:
    if isinstance(payload, Mapping):
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
        token = str(
            payload.get("webspace_id")
            or payload.get("workspace_id")
            or meta.get("webspace_id")
            or meta.get("workspace_id")
            or ""
        ).strip()
        if token:
            return token
    return default_webspace_id()


def _public_descriptor(item: Mapping[str, Any]) -> dict[str, Any]:
    vector = item.get("vector")
    dim = len(vector) if isinstance(vector, list) else int(item.get("vector_dim") or 0)
    title = str(item.get("title") or item.get("name") or item.get("id") or "").strip()
    description = str(item.get("description") or "").strip()
    return {
        "id": str(item.get("id") or ""),
        "title": title,
        "label": title,
        "description": description,
        "preview": description,
        "thumbnail": item.get("thumbnail") or "",
        "image": item.get("thumbnail") or "",
        "enabled": item.get("enabled", True) is not False,
        "threshold": item.get("threshold"),
        "vector_dim": dim,
        "model_signature": item.get("model_signature") or "",
        "model_id": item.get("model_id") or "",
        "created_at": item.get("created_at") or "",
        "updated_at": item.get("updated_at") or "",
        "details": {
            key: value
            for key, value in dict(item).items()
            if key not in {"vector", "thumbnail"}
        },
    }


def _public_descriptors(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [_public_descriptor(item) for item in state.get("descriptors") or [] if isinstance(item, Mapping)]


def _targets(state: Mapping[str, Any], *, include_disabled: bool = False) -> list[dict[str, Any]]:
    model_signature = str((state.get("model") or {}).get("model_signature") or "").strip()
    matching = state.get("matching") if isinstance(state.get("matching"), Mapping) else {}
    default_threshold = float(matching.get("threshold") or 0.82)
    out: list[dict[str, Any]] = []
    for item in state.get("descriptors") or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("enabled", True) is False and not include_disabled:
            continue
        vector = item.get("vector")
        if not isinstance(vector, list) or not vector:
            continue
        item_signature = str(item.get("model_signature") or "").strip()
        if model_signature and item_signature and item_signature != model_signature:
            continue
        out.append(
            {
                "id": str(item.get("id") or ""),
                "label": str(item.get("title") or item.get("name") or item.get("id") or ""),
                "description": str(item.get("description") or ""),
                "vector": list(vector),
                "threshold": float(item.get("threshold") or default_threshold),
                "model_signature": item_signature or model_signature,
                "metadata": {
                    "thumbnail": item.get("thumbnail") or "",
                    "created_at": item.get("created_at") or "",
                    "updated_at": item.get("updated_at") or "",
                },
            }
        )
    return out


def _session_config(state: Mapping[str, Any], mode: str) -> dict[str, Any]:
    normalized_mode = "setup" if str(mode or "").strip().lower() == "setup" else "work"
    tasks = ["embed"] if normalized_mode == "setup" else ["embed", "identify"]
    camera = json.loads(json.dumps(LOW_POWER_CAMERA))
    pipeline = {
        "tasks": tasks,
        "targetFps": LOW_POWER_TARGET_FPS,
        "emitVectors": normalized_mode == "setup",
        "showOverlay": True,
    }
    use_case = {
        "id": f"cv_descriptor.{normalized_mode}",
        "kind": "descriptor_capture" if normalized_mode == "setup" else "descriptor_identification",
        "profile": "low_power",
        "camera": camera,
        "pipeline": dict(pipeline),
        "matching": {
            "mode": "small_list_browser_vectors",
            "metric": (state.get("matching") or {}).get("metric") or "cosine",
        },
        "ui": {
            "preview": True,
            "select": normalized_mode == "setup",
            "overlay": normalized_mode == "work",
        },
    }
    return {
        "schema": "adaos.cv.session.v1",
        "sessionId": f"cv_descriptor.{normalized_mode}",
        "mode": normalized_mode,
        "model": dict(state.get("model") or {}),
        "profile": "low_power",
        "useCase": use_case,
        "camera": camera,
        "pipeline": pipeline,
        "matching": dict(state.get("matching") or {}),
        "targets": {
            "tool": f"{SKILL_NAME}.cv_descriptor_get_targets",
            "maxItems": int((state.get("matching") or {}).get("max_targets") or 100),
        },
        "sinks": {
            "selectTool": f"{SKILL_NAME}.cv_descriptor_save_descriptor",
            "recordTool": f"{SKILL_NAME}.cv_descriptor_record_runtime_event",
            "matchTool": f"{SKILL_NAME}.cv_descriptor_record_runtime_event",
            "diagnosticsTool": f"{SKILL_NAME}.cv_descriptor_record_runtime_event",
            "eventKind": "cv_descriptor.runtime.event",
            "matchEventKind": "cv_descriptor.match",
            "diagnosticsEventKind": "cv_descriptor.runtime.diagnostics",
        },
    }


def _projection_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    public_items = _public_descriptors(state)
    runtime = dict(state.get("runtime") or {})
    sessions = {
        "setup": _session_config(state, "setup"),
        "work": _session_config(state, "work"),
    }
    runtime["sessions"] = sessions
    current = {
        "schema": "adaos.cv_descriptor.current.v1",
        "ok": True,
        "status": state.get("status") or "init",
        "mode": state.get("mode") or "setup",
        "model": dict(state.get("model") or {}),
        "model_options": list(state.get("model_options") or DEFAULT_MODEL_OPTIONS),
        "matching": dict(state.get("matching") or {}),
        "activity": {
            "label": state.get("status") or "init",
            "description": "Waiting for browser CV runtime" if runtime.get("client_runtime_required") else "",
            "color": "warning" if runtime.get("client_runtime_required") else "success",
        },
        "stats": {
            "descriptor_count": len(public_items),
            "enabled_count": len([item for item in public_items if item.get("enabled") is not False]),
            "target_count": len(_targets(state)),
        },
        "current_match": runtime.get("current_match"),
        "updated_at": state.get("updated_at"),
    }
    descriptors = {
        "schema": "adaos.cv_descriptor.descriptors.v1",
        "items": public_items,
        "updated_at": state.get("updated_at"),
    }
    return {
        "current": current,
        "descriptors": descriptors,
        "runtime": runtime,
    }


def _ensure_data_projections() -> None:
    try:
        ctx = get_ctx()
        if (
            ctx.projections.resolve("subnet", CURRENT_SLOT)
            and ctx.projections.resolve("subnet", DESCRIPTORS_SLOT)
            and ctx.projections.resolve("subnet", RUNTIME_SLOT)
        ):
            return
        ctx.projections.load_entries(_DATA_PROJECTION_ENTRIES)
    except Exception:
        _log.debug("cv_descriptor projection entries are not available yet", exc_info=True)


def _set_projection_if_changed(slot: str, value: Any, *, webspace_id: str) -> None:
    key = f"{webspace_id}:{slot}"
    fingerprint = _json_fingerprint(value)
    if _projection_fingerprints.get(key) == fingerprint:
        return
    _projection_fingerprints[key] = fingerprint
    ctx_subnet.set(slot, value, webspace_id=webspace_id)


def _project(webspace_id: str | None = None, state: Mapping[str, Any] | None = None) -> None:
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    payload = _projection_payload(state or _read_state())
    pushed = False
    try:
        _ensure_data_projections()
        try:
            pushed = bool(set_current_skill(SKILL_NAME))
        except Exception:
            pushed = False
        _set_projection_if_changed(CURRENT_SLOT, payload["current"], webspace_id=selected_webspace)
        _set_projection_if_changed(DESCRIPTORS_SLOT, payload["descriptors"], webspace_id=selected_webspace)
        _set_projection_if_changed(RUNTIME_SLOT, payload["runtime"], webspace_id=selected_webspace)
    except Exception:
        _log.debug("cv_descriptor projection failed", exc_info=True)
    finally:
        if pushed:
            try:
                clear_current_skill()
            except Exception:
                pass


def _publish_event(topic: str, payload: Mapping[str, Any]) -> None:
    try:
        publish(topic, dict(payload), source=SKILL_NAME)
    except Exception:
        _log.debug("failed to publish %s", topic, exc_info=True)


def _publish_stream_event(kind: str, data: Mapping[str, Any], *, webspace_id: str | None = None, force: bool = False) -> None:
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    item = {
        "id": f"{kind}:{int(_now() * 1000)}:{uuid.uuid4().hex[:8]}",
        "kind": kind,
        "title": str(data.get("title") or kind),
        "description": str(data.get("description") or ""),
        "payload": dict(data),
        "ts": _now(),
    }
    key = f"{selected_webspace}:{EVENT_RECEIVER}:{kind}"
    fingerprint = _json_fingerprint(item["payload"])
    if not force and _stream_fingerprints.get(key) == fingerprint:
        return
    _stream_fingerprints[key] = fingerprint
    try:
        stream_publish(
            EVENT_RECEIVER,
            item,
            _meta={
                "webspace_id": selected_webspace,
                "owner": f"skill:{SKILL_NAME}",
                "skill_name": SKILL_NAME,
            },
        )
    except Exception:
        _log.debug("failed to publish cv_descriptor stream event", exc_info=True)


def _coerce_vector(value: Any) -> list[float]:
    if isinstance(value, Mapping):
        for key in ("vector", "embedding", "latent"):
            if key in value:
                return _coerce_vector(value.get(key))
    if not isinstance(value, (list, tuple)):
        raise ValueError("vector must be an array")
    out: list[float] = []
    for raw in value:
        try:
            parsed = float(raw)
        except Exception as exc:
            raise ValueError("vector contains non-numeric values") from exc
        if not math.isfinite(parsed):
            raise ValueError("vector contains non-finite values")
        out.append(parsed)
    if not out:
        raise ValueError("vector must not be empty")
    return out


def _find_descriptor(state: Mapping[str, Any], descriptor_id: str) -> tuple[int, dict[str, Any] | None]:
    token = str(descriptor_id or "").strip()
    for idx, item in enumerate(state.get("descriptors") or []):
        if isinstance(item, Mapping) and str(item.get("id") or "").strip() == token:
            return idx, dict(item)
    return -1, None


def _result(state: Mapping[str, Any], *, webspace_id: str | None = None, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    _project(webspace_id=webspace_id, state=state)
    payload = _projection_payload(state)
    out = {
        "ok": True,
        "current": payload["current"],
        "descriptors": payload["descriptors"],
        "runtime": payload["runtime"],
    }
    if extra:
        out.update(dict(extra))
    return out


@tool("cv_descriptor_status")
def cv_descriptor_status(
    webspace_id: str | None = None,
    include_vectors: bool = False,
    **_: Any,
) -> dict[str, Any]:
    state = _read_state()
    extra: dict[str, Any] = {}
    if include_vectors:
        extra["targets"] = _targets(state)
    return _result(state, webspace_id=webspace_id, extra=extra)


@tool("cv_descriptor_configure_model")
def cv_descriptor_configure_model(
    model_id: str | None = None,
    model: Mapping[str, Any] | None = None,
    threshold: float | None = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    state = _read_state()
    chosen: dict[str, Any] = {}
    token = str(model_id or payload.get("id") or payload.get("value") or "").strip()
    if isinstance(model, Mapping):
        chosen.update(dict(model))
    if token:
        for option in state.get("model_options") or DEFAULT_MODEL_OPTIONS:
            if isinstance(option, Mapping) and str(option.get("id") or "").strip() == token:
                chosen.update(dict(option))
                break
        chosen.setdefault("id", token)
    if not chosen:
        return {"ok": False, "error": {"code": "missing_model", "message": "model_id or model is required"}}
    chosen["model_signature"] = _model_signature(chosen)
    state["model"] = chosen
    state["status"] = "configured"
    if threshold is not None:
        matching = dict(state.get("matching") or {})
        matching["threshold"] = float(threshold)
        state["matching"] = matching
    state = _write_state(state)
    _publish_event("cv_descriptor.updated", {"kind": "model.configured", "model": chosen, "ts": _now()})
    _publish_stream_event("model.configured", {"title": "Model configured", "model": chosen}, webspace_id=webspace_id, force=True)
    return _result(state, webspace_id=webspace_id, extra={"model": chosen})


@tool("cv_descriptor_save_descriptor")
def cv_descriptor_save_descriptor(
    vector: Any = None,
    embedding: Any = None,
    descriptor: Mapping[str, Any] | None = None,
    title: str | None = None,
    name: str | None = None,
    description: str | None = None,
    thumbnail: str | None = None,
    image: Any = None,
    threshold: float | None = None,
    model_signature: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    state = _read_state()
    source = dict(descriptor or {})
    source.update({k: v for k, v in payload.items() if v is not None})
    raw_vector = vector if vector is not None else embedding if embedding is not None else source.get("vector") or source.get("embedding")
    try:
        coerced = _coerce_vector(raw_vector)
    except ValueError as exc:
        return {"ok": False, "error": {"code": "invalid_vector", "message": str(exc)}}

    model = state.get("model") if isinstance(state.get("model"), Mapping) else {}
    signature = str(model_signature or source.get("model_signature") or model.get("model_signature") or "").strip()
    item_title = str(title or name or source.get("title") or source.get("label") or "").strip()
    if not item_title:
        item_title = f"Object {len(state.get('descriptors') or []) + 1}"
    item_description = str(description or source.get("description") or "").strip()
    raw_thumbnail = thumbnail or source.get("thumbnail") or source.get("preview") or ""
    if not raw_thumbnail and isinstance(image, str):
        raw_thumbnail = image
    descriptor_id = str(source.get("id") or uuid.uuid4().hex).strip()
    now = _iso_now()
    item = {
        "id": descriptor_id,
        "title": item_title,
        "description": item_description,
        "thumbnail": raw_thumbnail,
        "vector": coerced,
        "vector_dim": len(coerced),
        "model_id": str(model.get("id") or ""),
        "model_signature": signature,
        "threshold": float(threshold) if threshold is not None else None,
        "enabled": True,
        "metadata": dict(metadata or source.get("metadata") or {}),
        "capture": {
            "source": "browser",
            "bbox": source.get("bbox"),
            "score": source.get("score"),
            "ts": _now(),
        },
        "created_at": now,
        "updated_at": now,
    }
    descriptors = [item for item in state.get("descriptors") or [] if isinstance(item, Mapping) and item.get("id") != descriptor_id]
    descriptors.append(item)
    state["descriptors"] = descriptors
    state["status"] = "ready"
    state = _write_state(state)
    public_item = _public_descriptor(item)
    _publish_event("cv_descriptor.updated", {"kind": "descriptor.saved", "descriptor": public_item, "ts": _now()})
    _publish_stream_event(
        "descriptor.saved",
        {"title": "Descriptor saved", "description": item_title, "descriptor": public_item},
        webspace_id=webspace_id,
        force=True,
    )
    return _result(state, webspace_id=webspace_id, extra={"descriptor": public_item})


@tool("cv_descriptor_update_descriptor")
def cv_descriptor_update_descriptor(
    id: str | None = None,
    descriptor_id: str | None = None,
    title: str | None = None,
    name: str | None = None,
    description: str | None = None,
    thumbnail: str | None = None,
    enabled: bool | None = None,
    threshold: float | None = None,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    state = _read_state()
    token = str(descriptor_id or id or "").strip()
    idx, item = _find_descriptor(state, token)
    if item is None:
        return {"ok": False, "error": {"code": "descriptor_not_found", "message": token}}
    if title is not None or name is not None:
        item["title"] = str(title or name or "").strip()
    if description is not None:
        item["description"] = str(description or "")
    if thumbnail is not None:
        item["thumbnail"] = str(thumbnail or "")
    if enabled is not None:
        item["enabled"] = bool(enabled)
    if threshold is not None:
        item["threshold"] = float(threshold)
    item["updated_at"] = _iso_now()
    descriptors = list(state.get("descriptors") or [])
    descriptors[idx] = item
    state["descriptors"] = descriptors
    state = _write_state(state)
    public_item = _public_descriptor(item)
    _publish_event("cv_descriptor.updated", {"kind": "descriptor.updated", "descriptor": public_item, "ts": _now()})
    _publish_stream_event("descriptor.updated", {"title": "Descriptor updated", "descriptor": public_item}, webspace_id=webspace_id, force=True)
    return _result(state, webspace_id=webspace_id, extra={"descriptor": public_item})


@tool("cv_descriptor_delete_descriptor")
def cv_descriptor_delete_descriptor(
    id: str | None = None,
    descriptor_id: str | None = None,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    state = _read_state()
    token = str(descriptor_id or id or "").strip()
    before = len(state.get("descriptors") or [])
    state["descriptors"] = [
        item
        for item in state.get("descriptors") or []
        if not isinstance(item, Mapping) or str(item.get("id") or "").strip() != token
    ]
    deleted = before != len(state["descriptors"])
    state = _write_state(state)
    if deleted:
        _publish_event("cv_descriptor.updated", {"kind": "descriptor.deleted", "id": token, "ts": _now()})
        _publish_stream_event("descriptor.deleted", {"title": "Descriptor deleted", "description": token}, webspace_id=webspace_id, force=True)
    return _result(state, webspace_id=webspace_id, extra={"deleted": deleted, "id": token})


@tool("cv_descriptor_clear")
def cv_descriptor_clear(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    state = _read_state()
    state["descriptors"] = []
    state["runtime"] = {
        **dict(state.get("runtime") or {}),
        "current_match": None,
        "last_event": None,
    }
    state["status"] = "init"
    state = _write_state(state)
    _publish_event("cv_descriptor.updated", {"kind": "descriptors.cleared", "ts": _now()})
    _publish_stream_event("descriptors.cleared", {"title": "Descriptors cleared"}, webspace_id=webspace_id, force=True)
    return _result(state, webspace_id=webspace_id)


@tool("cv_descriptor_get_targets")
def cv_descriptor_get_targets(
    include_disabled: bool = False,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    state = _read_state()
    _project(webspace_id=webspace_id, state=state)
    matching = state.get("matching") if isinstance(state.get("matching"), Mapping) else {}
    return {
        "ok": True,
        "schema": "adaos.cv_descriptor.targets.v1",
        "model": dict(state.get("model") or {}),
        "matching": dict(matching),
        "targets": _targets(state, include_disabled=include_disabled),
    }


@tool("cv_descriptor_runtime_command")
def cv_descriptor_runtime_command(
    action: str | None = None,
    mode: str | None = None,
    session_id: str | None = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    state = _read_state()
    runtime = dict(state.get("runtime") or {})
    seq = int(runtime.get("command_seq") or 0) + 1
    normalized_mode = str(mode or payload.get("target_mode") or state.get("mode") or "work").strip().lower()
    if normalized_mode not in {"setup", "work"}:
        normalized_mode = "work"
    command = {
        "schema": "adaos.cv.command.v1",
        "seq": seq,
        "action": str(action or payload.get("id") or "status").strip() or "status",
        "mode": normalized_mode,
        "sessionId": str(session_id or f"cv_descriptor.{normalized_mode}"),
        "session": _session_config(state, normalized_mode),
        "issued_at": _iso_now(),
    }
    runtime["desired"] = command
    runtime["command_seq"] = seq
    state["runtime"] = runtime
    state["mode"] = normalized_mode
    state = _write_state(state)
    _publish_event("cv_descriptor.command", {"command": command, "ts": _now()})
    _publish_stream_event(
        "runtime.command",
        {"title": f"Runtime command: {command['action']}", "command": command},
        webspace_id=webspace_id,
        force=True,
    )
    return _result(state, webspace_id=webspace_id, extra={"command": command})


@tool("cv_descriptor_record_runtime_event")
def cv_descriptor_record_runtime_event(
    kind: str | None = None,
    event: Mapping[str, Any] | None = None,
    match: Mapping[str, Any] | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    state = _read_state()
    runtime = dict(state.get("runtime") or {})
    normalized_kind = str(kind or payload.get("type") or "runtime.event").strip() or "runtime.event"
    body = dict(event or payload)
    if isinstance(diagnostics, Mapping):
        runtime["diagnostics"] = dict(diagnostics)
        runtime["client_runtime_required"] = False
    if isinstance(match, Mapping):
        runtime["current_match"] = dict(match)
    elif normalized_kind.startswith("match") and body:
        runtime["current_match"] = dict(body)
    runtime["last_event"] = {
        "kind": normalized_kind,
        "payload": body,
        "ts": _now(),
    }
    state["runtime"] = runtime
    if normalized_kind in {"ready", "runtime.ready"}:
        state["status"] = "ready"
    state = _write_state(state)
    _publish_stream_event(normalized_kind, {"title": normalized_kind, "payload": body}, webspace_id=webspace_id, force=True)
    return _result(state, webspace_id=webspace_id)


@subscribe("cv_descriptor.runtime.event")
def on_cv_descriptor_runtime_event(evt: Any) -> None:
    payload = _payload(evt)
    cv_descriptor_record_runtime_event(
        kind=str(payload.get("kind") or payload.get("type") or "runtime.event"),
        event=payload,
        webspace_id=_webspace_id_from_payload(payload),
    )


@subscribe("cv_descriptor.match")
def on_cv_descriptor_match(evt: Any) -> None:
    payload = _payload(evt)
    cv_descriptor_record_runtime_event(
        kind=str(payload.get("kind") or "match"),
        match=payload,
        webspace_id=_webspace_id_from_payload(payload),
    )


@subscribe("cv_descriptor.runtime.diagnostics")
def on_cv_descriptor_runtime_diagnostics(evt: Any) -> None:
    payload = _payload(evt)
    cv_descriptor_record_runtime_event(
        kind="runtime.diagnostics",
        diagnostics=payload,
        webspace_id=_webspace_id_from_payload(payload),
    )


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = _payload(evt)
    if str(payload.get("receiver") or "").strip() != EVENT_RECEIVER:
        return
    webspace_id = _webspace_id_from_payload(payload)
    state = _read_state()
    _publish_stream_event(
        "snapshot",
        {
            "title": "CV Descriptor snapshot",
            "current": _projection_payload(state)["current"],
        },
        webspace_id=webspace_id,
        force=True,
    )


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _payload(evt)
    if str(payload.get("receiver") or "").strip() != EVENT_RECEIVER:
        return
    if str(payload.get("action") or "").strip().lower() == "unsubscribed":
        return
    on_webio_stream_snapshot_requested(payload)


def _is_cv_descriptor_yjs_projection_payload(payload: Mapping[str, Any]) -> bool:
    slot = str(payload.get("slot") or payload.get("projection") or "").strip()
    if slot in {CURRENT_SLOT, DESCRIPTORS_SLOT, RUNTIME_SLOT}:
        return True
    topic = str(payload.get("topic") or "").strip()
    if any(topic.endswith(f".{slot_name}") for slot_name in (CURRENT_SLOT, DESCRIPTORS_SLOT, RUNTIME_SLOT)):
        return True
    path = str(payload.get("path") or payload.get("projection_path") or "").strip()
    return path.startswith("data/cv_descriptor/")


def _project_on_yjs_demand(evt: Any) -> None:
    payload = _payload(evt)
    if not _is_cv_descriptor_yjs_projection_payload(payload):
        return
    if str(payload.get("action") or "").strip().lower() == "unsubscribed":
        return
    _project(webspace_id=_webspace_id_from_payload(payload))


@subscribe("webio.yjs.snapshot.requested")
def on_yjs_snapshot_requested(evt: Any) -> None:
    _project_on_yjs_demand(evt)


@subscribe("webio.yjs.subscription.changed")
def on_yjs_subscription_changed(evt: Any) -> None:
    _project_on_yjs_demand(evt)
