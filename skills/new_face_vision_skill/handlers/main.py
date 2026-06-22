from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet
from adaos.sdk.data.context import clear_current_skill, set_current_skill
from adaos.sdk.data.events import publish
from adaos.sdk.io import stream_publish
from adaos.services.agent_context import get_ctx
from adaos.services.yjs.webspace import default_webspace_id

def _get_engine_class():
    from service.engine import NewFaceVisionEngine
    return NewFaceVisionEngine


SKILL_NAME = "new_face_vision_skill"
FRAME_RECEIVER = "newface_vision_frame"
METRICS_RECEIVER = "newface_vision_metrics"
PROGRESS_RECEIVER = "newface_vision_progress"
REQUIRES_DATA_PROJECTIONS = ["new_face_vision.current", "new_face_vision.history"]
_DATA_PROJECTION_ENTRIES = [
    {
        "scope": "subnet",
        "slot": "new_face_vision.current",
        "targets": [{"backend": "yjs", "path": "data/new_face_vision/current"}],
    },
    {
        "scope": "subnet",
        "slot": "new_face_vision.history",
        "targets": [{"backend": "yjs", "path": "data/new_face_vision/history"}],
    },
]
_log = logging.getLogger("skills.new_face_vision_skill")
_engine: Any = None
_ENGINE_LOCK = threading.RLock()
_projection_fingerprints: dict[str, str] = {}
_stream_fingerprints: dict[str, str] = {}
_last_frame_payload_by_webspace: dict[str, dict[str, Any]] = {}
_metrics_points_by_webspace: dict[str, list[dict[str, Any]]] = {}
_last_progress_payload_by_webspace: dict[str, dict[str, Any]] = {}
_last_playback_project_at_by_webspace: dict[str, float] = {}
_METRICS_HISTORY_MAX = 120
_PLAYBACK_PROJECT_INTERVAL_S = 1.0
_playback_stop: threading.Event | None = None
_playback_thread: threading.Thread | None = None


def _state_dir() -> Path:
    try:
        ctx = get_ctx()
        return Path(ctx.paths.state_dir()) / "skills" / SKILL_NAME
    except Exception:
        return Path(__file__).resolve().parents[1] / ".state"


def _uploads_dir() -> Path | None:
    for key in ("ADAOS_SKILL_ENV_PATH", "ADAOS_SKILL_MEMORY_PATH"):
        raw = str(os.getenv(key) or "").strip()
        if not raw:
            continue
        path = Path(raw)
        data_root = path.parent.parent if path.parent.name == "db" else path.parent
        candidate = (data_root / "files" / "uploads").resolve()
        if candidate.exists():
            return candidate
    try:
        ctx = get_ctx()
        runtime_root = Path(ctx.paths.workspace_dir()) / "skills" / ".runtime" / SKILL_NAME
        current_version_path = runtime_root / "current_version"
        if current_version_path.exists():
            current_version = current_version_path.read_text(encoding="utf-8").strip()
            if current_version:
                major_minor = ".".join(current_version.lstrip("vV").split(".")[:2]) or current_version
                candidate = runtime_root / f"v{major_minor}" / "data" / "files" / "uploads"
                if candidate.exists():
                    return candidate.resolve()
        candidates = [path for path in runtime_root.glob("*/data/files/uploads") if path.exists()]
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime).resolve()
    except Exception:
        _log.debug("failed to discover new_face_vision upload directory", exc_info=True)
    return None


def _models_dir() -> Path:
    for key in ("ADAOS_SKILL_ENV_PATH", "ADAOS_SKILL_MEMORY_PATH"):
        raw = str(os.getenv(key) or "").strip()
        if not raw:
            continue
        path = Path(raw)
        data_root = path.parent.parent if path.parent.name == "db" else path.parent
        candidate = (data_root / "files" / "models").resolve()
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    try:
        ctx = get_ctx()
        runtime_root = Path(ctx.paths.workspace_dir()) / "skills" / ".runtime" / SKILL_NAME
        current_version_path = runtime_root / "current_version"
        if current_version_path.exists():
            current_version = current_version_path.read_text(encoding="utf-8").strip()
            if current_version:
                major_minor = ".".join(current_version.lstrip("vV").split(".")[:2]) or current_version
                candidate = runtime_root / f"v{major_minor}" / "data" / "files" / "models"
                candidate.mkdir(parents=True, exist_ok=True)
                return candidate.resolve()
    except Exception:
        _log.debug("failed to discover new_face_vision model directory", exc_info=True)
    candidate = _SKILL_ROOT / ".state" / "data" / "files" / "models"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _engine_instance() -> Any:
    global _engine
    if _engine is None:
        engine_class = _get_engine_class()
        _engine = engine_class(_state_dir(), upload_root=_uploads_dir())
    return _engine


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


def _ensure_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        if ctx.projections.resolve("subnet", "new_face_vision.current") and ctx.projections.resolve(
            "subnet", "new_face_vision.history"
        ):
            return
        ctx.projections.load_entries(_DATA_PROJECTION_ENTRIES)
    except Exception:
        _log.debug("projection entries are not available yet", exc_info=True)


def _fingerprint(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except Exception:
        return repr(value)


def _set_projection_if_changed(slot: str, value: Any, *, webspace_id: str) -> None:
    key = f"{webspace_id}:{slot}"
    fingerprint = _fingerprint(value)
    if _projection_fingerprints.get(key) == fingerprint:
        return
    _projection_fingerprints[key] = fingerprint
    ctx_subnet.set(slot, value, webspace_id=webspace_id)


def _project(webspace_id: str | None = None) -> None:
    _ensure_skill_data_projections()
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    with _ENGINE_LOCK:
        snapshot = _engine_instance().snapshot()
    history = list(snapshot.get("history") or [])
    current_snapshot = dict(snapshot)
    current_snapshot["history"] = []
    pushed = False
    try:
        try:
            pushed = bool(set_current_skill(SKILL_NAME))
        except Exception:
            pushed = False
        _set_projection_if_changed("new_face_vision.current", current_snapshot, webspace_id=selected_webspace)
        _set_projection_if_changed(
            "new_face_vision.history",
            history,
            webspace_id=selected_webspace,
        )
    except Exception:
        _log.debug("new_face_vision projection failed", exc_info=True)
    finally:
        if pushed:
            try:
                clear_current_skill()
            except Exception:
                pass


def _publish_event(topic: str, payload: dict[str, Any]) -> None:
    try:
        publish(topic, payload, source=SKILL_NAME)
    except Exception:
        _log.debug("failed to publish %s", topic, exc_info=True)


def _publish_stream(receiver: str, data: Any, *, webspace_id: str | None = None, force: bool = False) -> None:
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    key = f"{selected_webspace}:{receiver}"
    fingerprint = _fingerprint(data)
    if not force and _stream_fingerprints.get(key) == fingerprint:
        return
    _stream_fingerprints[key] = fingerprint
    try:
        stream_publish(
            receiver,
            data,
            _meta={
                "webspace_id": selected_webspace,
                "owner": f"skill:{SKILL_NAME}",
                "skill_name": SKILL_NAME,
            },
        )
    except Exception:
        _log.debug("failed to publish stream receiver=%s", receiver, exc_info=True)


def _remember_frame_payload(webspace_id: str | None, payload: dict[str, Any]) -> None:
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    _last_frame_payload_by_webspace[selected_webspace] = dict(payload)


def _republish_last_frame(webspace_id: str | None) -> bool:
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    payload = _last_frame_payload_by_webspace.get(selected_webspace)
    if not payload:
        return False
    _publish_stream(FRAME_RECEIVER, payload, webspace_id=selected_webspace, force=True)
    return True


def _mark_calculation_if_uncached(frame_idx: int | None, webspace_id: str | None) -> bool:
    with _ENGINE_LOCK:
        engine = _engine_instance()
        if engine.is_frame_cached(frame_idx):
            return False
        engine.begin_calculation_status(frame_idx)
    _project(webspace_id=webspace_id)
    _republish_last_frame(webspace_id)
    return True


def _remember_metrics_payload(webspace_id: str | None, payload: dict[str, Any]) -> None:
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    points = _metrics_points_by_webspace.setdefault(selected_webspace, [])
    points.append(dict(payload))
    if len(points) > _METRICS_HISTORY_MAX:
        del points[:-_METRICS_HISTORY_MAX]


def _remember_progress_payload(webspace_id: str | None, payload: dict[str, Any]) -> None:
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    _last_progress_payload_by_webspace[selected_webspace] = dict(payload)


def _publish_progress(snapshot: Mapping[str, Any], *, ok: bool, webspace_id: str | None, error: Mapping[str, Any] | None = None) -> None:
    payload = _progress_payload(snapshot, ok=ok, error=error)
    _remember_progress_payload(webspace_id, payload)
    _publish_stream(PROGRESS_RECEIVER, payload, webspace_id=webspace_id)


def _compact_frame_event(result: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in dict(result).items() if k != "preview_base64"}


def _publish_frame_result(result: Mapping[str, Any], *, webspace_id: str | None = None) -> None:
    engine = _engine_instance()
    frame_payload = engine.frame_stream_payload(result)
    metrics_payload = engine.metrics_stream_payload(result)
    _remember_frame_payload(webspace_id, frame_payload)
    _remember_metrics_payload(webspace_id, metrics_payload)
    _publish_stream(FRAME_RECEIVER, frame_payload, webspace_id=webspace_id)
    _publish_stream(METRICS_RECEIVER, metrics_payload, webspace_id=webspace_id)
    _publish_event("new_face_vision.frame", _compact_frame_event(result))


def _playback_fps(value: Any) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = 5.0
    if parsed < 0.5:
        parsed = 0.5
    if parsed > 30:
        parsed = 30
    return round(parsed, 2)


def _should_project_playback(webspace_id: str) -> bool:
    now = time.monotonic()
    last = _last_playback_project_at_by_webspace.get(webspace_id, 0.0)
    if now - last < _PLAYBACK_PROJECT_INTERVAL_S:
        return False
    _last_playback_project_at_by_webspace[webspace_id] = now
    return True


def _stop_playback_thread(*, wait: bool = False) -> None:
    global _playback_stop, _playback_thread
    stop = _playback_stop
    thread = _playback_thread
    if stop is not None:
        stop.set()
    if wait and thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    if thread is None or not thread.is_alive():
        _playback_thread = None
        _playback_stop = None


def _start_playback_thread(*, webspace_id: str | None, fps: float) -> None:
    global _playback_stop, _playback_thread
    if _playback_thread is not None and _playback_thread.is_alive():
        return
    stop = threading.Event()
    _playback_stop = stop
    _playback_thread = threading.Thread(
        target=_playback_loop,
        args=(str(webspace_id or default_webspace_id()).strip() or default_webspace_id(), fps, stop),
        daemon=True,
        name="new-face-vision-playback",
    )
    _playback_thread.start()


def _playback_loop(webspace_id: str, fps: float, stop: threading.Event) -> None:
    interval = max(0.03, 1.0 / max(0.5, fps))
    while not stop.is_set():
        try:
            _republish_last_frame(webspace_id)
            with _ENGINE_LOCK:
                engine = _engine_instance()
                snapshot = engine.snapshot()
                playback = snapshot.get("playback") if isinstance(snapshot.get("playback"), Mapping) else {}
                if str(playback.get("mode") or "").lower() != "playing":
                    break
                needs_calculation = not engine.is_frame_cached(None)
                if needs_calculation:
                    engine.begin_calculation_status(None)
            if needs_calculation:
                _project(webspace_id=webspace_id)
                _republish_last_frame(webspace_id)
            with _ENGINE_LOCK:
                engine = _engine_instance()
                result = engine.process_frame(None)
            if result.get("ok"):
                _publish_frame_result(result, webspace_id=webspace_id)
                if _should_project_playback(webspace_id):
                    _project(webspace_id=webspace_id)
            with _ENGINE_LOCK:
                snapshot = _engine_instance().snapshot()
            if not result.get("ok"):
                error = _normalize_error_payload(result.get("error"), code="playback_failed")
                _publish_progress(snapshot, ok=False, error=error, webspace_id=webspace_id)
                _publish_event("new_face_vision.error", {"ok": False, "error": error, "ts": time.time()})
                with _ENGINE_LOCK:
                    _engine_instance().set_playback("paused")
                _project(webspace_id=webspace_id)
                break
            stop.wait(interval)
        except Exception as exc:
            _handle_error(exc, webspace_id=webspace_id)
            break


def _normalize_error_payload(
    error: Any,
    *,
    code: str = "skill_error",
    retryable: bool = False,
) -> dict[str, Any]:
    if isinstance(error, Mapping):
        message = str(error.get("message") or error.get("error") or error.get("code") or code)
        out: dict[str, Any] = {
            "code": str(error.get("code") or code),
            "message": message,
            "retryable": bool(error.get("retryable", retryable)),
            "ts": float(error.get("ts")) if isinstance(error.get("ts"), (int, float)) else time.time(),
        }
        if "details" in error:
            out["details"] = error.get("details")
        return out
    return {
        "code": code,
        "message": str(error or code),
        "retryable": retryable,
        "ts": time.time(),
    }


def _set_engine_error(error: Mapping[str, Any]) -> None:
    with _ENGINE_LOCK:
        engine = _engine_instance()
        normalized = dict(error)
        engine.last_error = normalized
        operation = getattr(engine, "_operation", None)
        if isinstance(operation, dict):
            engine._operation = {**operation, "error": normalized}


def _progress_payload(
    snapshot: Mapping[str, Any],
    *,
    ok: bool,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    operation = snapshot.get("operation") if isinstance(snapshot.get("operation"), Mapping) else {}
    return {
        "ok": ok,
        "status": snapshot.get("status"),
        "operation": dict(operation),
        "error": dict(error) if isinstance(error, Mapping) else snapshot.get("error"),
        "ts": time.time(),
    }


def _artifact_path(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in (
            "path",
            "local_path",
            "file_path",
            "stored_path",
            "abs_path",
            "absolute_path",
        ):
            nested = value.get(key)
            if nested:
                return _artifact_path(nested)
        nested_ref = value.get("artifact_ref") or value.get("file") or value.get("value")
        if nested_ref:
            return _artifact_path(nested_ref)
        uri = str(value.get("uri") or value.get("url") or "").strip()
        if uri.startswith("file://"):
            return uri[len("file://") :]
        return ""
    text = str(value or "").strip()
    if text.startswith("file://"):
        return text[len("file://") :]
    return text


def _resolve_path(path: Any = None, artifact_ref: Any = None, file: Any = None, **payload: Any) -> str:
    candidates = [
        path,
        artifact_ref,
        file,
        payload.get("artifact"),
        payload.get("ref"),
        payload.get("value"),
    ]
    for candidate in candidates:
        resolved = _artifact_path(candidate)
        if resolved:
            return resolved
    return ""


def _source_ref(path: Any = None, artifact_ref: Any = None, file: Any = None, **payload: Any) -> dict[str, Any] | None:
    for candidate in (artifact_ref, file, payload.get("artifact"), payload.get("ref"), payload.get("value"), path):
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return None


def _publish_model_to_root(path: str, *, result: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from adaos.sdk.data.models import update_model_if_changed

        file_path = Path(path)
        metadata = {
            "source": "new_face_vision_load_model",
            "device": result.get("device"),
            "size_mb": result.get("size_mb"),
        }
        return update_model_if_changed(
            file_path,
            skill_id=SKILL_NAME,
            artifact=file_path.name,
            metadata={key: value for key, value in metadata.items() if value is not None},
        )
    except Exception as exc:
        _log.warning("failed to publish new_face_vision model to Root: %s", exc)
        return {"ok": False, "error": str(exc)}


def _model_storage_info(label: str = "current") -> dict[str, Any]:
    from adaos.sdk.data.models import get_model_manifest

    return get_model_manifest(SKILL_NAME, label=label)


def _result_with_snapshot(result: dict[str, Any], *, webspace_id: str | None = None) -> dict[str, Any]:
    ok = bool(result.get("ok", True))
    if not ok:
        result = {
            **result,
            "error": _normalize_error_payload(result.get("error"), code="operation_failed"),
        }
        _set_engine_error(result["error"])
    _project(webspace_id=webspace_id)
    with _ENGINE_LOCK:
        snapshot = _engine_instance().snapshot()
    operation = snapshot.get("operation") if isinstance(snapshot.get("operation"), Mapping) else {}
    if operation.get("id") or not ok:
        _publish_progress(snapshot, ok=ok, error=result.get("error") if not ok else None, webspace_id=webspace_id)
    if not ok:
        _publish_event(
            "new_face_vision.error",
            {"ok": False, "error": result["error"], "ts": time.time()},
        )
    return {"ok": ok, **result, "current": snapshot}


def _handle_error(exc: Exception, *, webspace_id: str | None = None) -> dict[str, Any]:
    error = _normalize_error_payload(exc, code="handler_exception")
    _set_engine_error(error)
    _project(webspace_id=webspace_id)
    with _ENGINE_LOCK:
        snapshot = _engine_instance().snapshot()
    payload = {"ok": False, "error": error, "current": snapshot, "ts": time.time()}
    _publish_event("new_face_vision.error", payload)
    _publish_progress(snapshot, ok=False, error=error, webspace_id=webspace_id)
    return payload


@tool("new_face_vision_persist_state")
def new_face_vision_persist_state(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        with _ENGINE_LOCK:
            result = _engine_instance().persist_state()
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_rehydrate")
def new_face_vision_rehydrate(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        with _ENGINE_LOCK:
            result = _engine_instance().rehydrate(force=True)
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_status")
def new_face_vision_status(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    _project(webspace_id=webspace_id)
    with _ENGINE_LOCK:
        snapshot = _engine_instance().snapshot()
    return {"ok": True, "current": snapshot}


@tool("new_face_vision_configure")
def new_face_vision_configure(
    model_path: str | None = None,
    frames_path: str | None = None,
    masks_path: str | None = None,
    metadata_path: str | None = None,
    threshold: float | None = None,
    warning_threshold: float | None = None,
    alarm_threshold: float | None = None,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    try:
        with _ENGINE_LOCK:
            result = _engine_instance().configure(
                model_path=model_path,
                frames_path=frames_path,
                masks_path=masks_path,
                metadata_path=metadata_path,
                threshold=threshold,
                warning_threshold=warning_threshold,
                alarm_threshold=alarm_threshold,
            )
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_load_model")
def new_face_vision_load_model(
    path: Any = None,
    artifact_ref: Any = None,
    file: Any = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    try:
        resolved_path = _resolve_path(path, artifact_ref, file, **payload)
        with _ENGINE_LOCK:
            result = _engine_instance().load_model(resolved_path, source_ref=_source_ref(path, artifact_ref, file, **payload))
        if result.get("ok") and resolved_path:
            result = {**result, "model_storage": _publish_model_to_root(resolved_path, result=result)}
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_model_storage_status")
def new_face_vision_model_storage_status(
    label: str | None = None,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    try:
        current = _model_storage_info("current")
        previous = _model_storage_info("previous") if str(label or "").strip().lower() in {"", "all", "previous"} else None
        payload = {"ok": True, "current_model": current}
        if previous is not None:
            payload["previous_model"] = previous
        return _result_with_snapshot(payload, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_restore_previous_model")
def new_face_vision_restore_previous_model(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        from adaos.sdk.data.models import download_previous_model

        download = download_previous_model(_models_dir(), skill_id=SKILL_NAME)
        with _ENGINE_LOCK:
            result = _engine_instance().load_model(
                str(download.get("path") or ""),
                source_ref={
                    "purpose": "models",
                    "source": "root_previous",
                    "root": download.get("manifest"),
                },
            )
        return _result_with_snapshot({**result, "model_storage": download}, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_load_frames")
def new_face_vision_load_frames(
    path: Any = None,
    artifact_ref: Any = None,
    file: Any = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    try:
        resolved_path = _resolve_path(path, artifact_ref, file, **payload)
        selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
        _metrics_points_by_webspace.pop(selected_webspace, None)
        _last_frame_payload_by_webspace.pop(selected_webspace, None)
        with _ENGINE_LOCK:
            engine = _engine_instance()
            result = engine.load_frames(resolved_path, source_ref=_source_ref(path, artifact_ref, file, **payload))
            empty_frame = engine.empty_frame_stream_payload(label="No frame", clear_image=True) if result.get("ok") else None
        if empty_frame:
            _remember_frame_payload(selected_webspace, empty_frame)
            _publish_stream(FRAME_RECEIVER, empty_frame, webspace_id=selected_webspace, force=True)
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_load_masks")
def new_face_vision_load_masks(
    path: Any = None,
    artifact_ref: Any = None,
    file: Any = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    try:
        resolved_path = _resolve_path(path, artifact_ref, file, **payload)
        with _ENGINE_LOCK:
            result = _engine_instance().load_masks(resolved_path, source_ref=_source_ref(path, artifact_ref, file, **payload))
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_load_metadata")
def new_face_vision_load_metadata(
    path: Any = None,
    artifact_ref: Any = None,
    file: Any = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    try:
        resolved_path = _resolve_path(path, artifact_ref, file, **payload)
        with _ENGINE_LOCK:
            result = _engine_instance().load_metadata(resolved_path, source_ref=_source_ref(path, artifact_ref, file, **payload))
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_process_frame")
def new_face_vision_process_frame(
    frame_idx: int | None = None,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    try:
        _republish_last_frame(webspace_id)
        _mark_calculation_if_uncached(frame_idx, webspace_id)
        with _ENGINE_LOCK:
            engine = _engine_instance()
            result = engine.process_frame(frame_idx)
        if result.get("ok"):
            _publish_frame_result(result, webspace_id=webspace_id)
        else:
            _publish_event("new_face_vision.frame", _compact_frame_event(result))
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_play_step")
def new_face_vision_play_step(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    return new_face_vision_process_frame(frame_idx=None, webspace_id=webspace_id)


@tool("new_face_vision_step_forward")
def new_face_vision_step_forward(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    return new_face_vision_play_step(webspace_id=webspace_id)


@tool("new_face_vision_step_back")
def new_face_vision_step_back(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        _republish_last_frame(webspace_id)
        with _ENGINE_LOCK:
            target_idx = _engine_instance().resolve_relative_frame_index(-1)
        _mark_calculation_if_uncached(target_idx, webspace_id)
        with _ENGINE_LOCK:
            engine = _engine_instance()
            result = engine.process_relative_frame(-1)
        if result.get("ok"):
            _publish_frame_result(result, webspace_id=webspace_id)
        else:
            _publish_event("new_face_vision.frame", _compact_frame_event(result))
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_seek_frame")
def new_face_vision_seek_frame(
    frame_idx: int | None = None,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    try:
        _republish_last_frame(webspace_id)
        _mark_calculation_if_uncached(frame_idx, webspace_id)
        with _ENGINE_LOCK:
            result = _engine_instance().seek_frame(frame_idx)
        if result.get("ok"):
            _publish_frame_result(result, webspace_id=webspace_id)
        else:
            _publish_event("new_face_vision.frame", _compact_frame_event(result))
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_play")
def new_face_vision_play(fps: float | None = None, webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        selected_fps = _playback_fps(fps)
        selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
        with _ENGINE_LOCK:
            result = _engine_instance().set_playback("playing", fps=selected_fps)
        _last_playback_project_at_by_webspace.pop(selected_webspace, None)
        response = _result_with_snapshot(result, webspace_id=selected_webspace)
        _start_playback_thread(webspace_id=selected_webspace, fps=selected_fps)
        return response
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_pause")
def new_face_vision_pause(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        _stop_playback_thread(wait=False)
        with _ENGINE_LOCK:
            result = _engine_instance().set_playback("paused")
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_stop")
def new_face_vision_stop(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        _stop_playback_thread(wait=True)
        with _ENGINE_LOCK:
            engine = _engine_instance()
            result = engine.stop()
            empty_frame = engine.empty_frame_stream_payload(label="Stopped")
        if not _republish_last_frame(webspace_id):
            _remember_frame_payload(webspace_id, empty_frame)
            _publish_stream(FRAME_RECEIVER, empty_frame, webspace_id=webspace_id, force=True)
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_replay")
def new_face_vision_replay(fps: float | None = None, webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        selected_fps = _playback_fps(fps)
        _stop_playback_thread(wait=True)
        selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
        _metrics_points_by_webspace.pop(selected_webspace, None)
        _last_playback_project_at_by_webspace.pop(selected_webspace, None)
        with _ENGINE_LOCK:
            result = _engine_instance().replay(fps=selected_fps)
        response = _result_with_snapshot(result, webspace_id=selected_webspace)
        _start_playback_thread(webspace_id=selected_webspace, fps=selected_fps)
        return response
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_reset")
def new_face_vision_reset(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        _stop_playback_thread(wait=True)
        selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
        _metrics_points_by_webspace.pop(selected_webspace, None)
        _last_playback_project_at_by_webspace.pop(selected_webspace, None)
        with _ENGINE_LOCK:
            result = _engine_instance().reset()
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_clear")
def new_face_vision_clear(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        _stop_playback_thread(wait=True)
        selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
        _metrics_points_by_webspace.pop(selected_webspace, None)
        _last_frame_payload_by_webspace.pop(selected_webspace, None)
        _last_playback_project_at_by_webspace.pop(selected_webspace, None)
        with _ENGINE_LOCK:
            engine = _engine_instance()
            result = engine.clear()
            empty_frame = engine.empty_frame_stream_payload(label="No frame", clear_image=True)
        _publish_stream(FRAME_RECEIVER, empty_frame, webspace_id=webspace_id, force=True)
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


def _action_id(payload: Mapping[str, Any]) -> str:
    return str(payload.get("id") or payload.get("action") or "").strip()


def _action_value(payload: Mapping[str, Any]) -> str:
    value = payload.get("value")
    if value is None:
        value = payload.get("path")
    if isinstance(value, Mapping):
        for key in ("value", "path", "text"):
            nested = value.get(key)
            if nested:
                return str(nested).strip()
        return ""
    return str(value or "").strip()


@tool("new_face_vision_action")
def new_face_vision_action(id: str | None = None, value: Any = None, webspace_id: str | None = None, **payload: Any) -> dict[str, Any]:
    merged: dict[str, Any] = dict(payload)
    if id is not None:
        merged["id"] = id
    if value is not None:
        merged["value"] = value
    selected_webspace = webspace_id or _webspace_id_from_payload(merged)
    action = _action_id(merged)
    try:
        if action in {"", "refresh", "status"}:
            return new_face_vision_status(webspace_id=selected_webspace)
        if action in {"process_next", "step", "next"}:
            return new_face_vision_step_forward(webspace_id=selected_webspace)
        if action in {"step_forward", "forward"}:
            return new_face_vision_step_forward(webspace_id=selected_webspace)
        if action in {"step_back", "back"}:
            return new_face_vision_step_back(webspace_id=selected_webspace)
        if action in {"seek", "seek_frame", "goto_frame"}:
            try:
                frame_idx = int(_action_value(merged))
            except Exception:
                frame_idx = None
            return new_face_vision_seek_frame(frame_idx=frame_idx, webspace_id=selected_webspace)
        if action == "play":
            return new_face_vision_play(webspace_id=selected_webspace)
        if action == "pause":
            return new_face_vision_pause(webspace_id=selected_webspace)
        if action == "stop":
            return new_face_vision_stop(webspace_id=selected_webspace)
        if action == "replay":
            return new_face_vision_replay(webspace_id=selected_webspace)
        if action == "reset":
            return new_face_vision_reset(webspace_id=selected_webspace)
        if action == "clear":
            return new_face_vision_clear(webspace_id=selected_webspace)
        if action == "load_model":
            return new_face_vision_load_model(_action_value(merged), webspace_id=selected_webspace)
        if action == "load_frames":
            return new_face_vision_load_frames(_action_value(merged), webspace_id=selected_webspace)
        if action == "load_masks":
            return new_face_vision_load_masks(_action_value(merged), webspace_id=selected_webspace)
        if action == "load_metadata":
            return new_face_vision_load_metadata(_action_value(merged), webspace_id=selected_webspace)
        if action == "set_threshold":
            return new_face_vision_configure(threshold=float(_action_value(merged)), webspace_id=selected_webspace)
        return _result_with_snapshot(
            {
                "ok": False,
                "error": {
                    "code": "unknown_action",
                    "message": f"Unknown action: {action}",
                    "details": {"action": action},
                },
            },
            webspace_id=selected_webspace,
        )
    except Exception as exc:
        return _handle_error(exc, webspace_id=selected_webspace)


@subscribe("new_face_vision.action")
def on_new_face_vision_action(evt: Any) -> None:
    payload = _payload(evt)
    new_face_vision_action(**payload)


@subscribe("new_face_vision.status.refresh")
def on_new_face_vision_status_refresh(evt: Any) -> None:
    payload = _payload(evt)
    new_face_vision_status(webspace_id=_webspace_id_from_payload(payload))


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = _payload(evt)
    receiver = str(payload.get("receiver") or "").strip()
    if receiver not in {FRAME_RECEIVER, METRICS_RECEIVER, PROGRESS_RECEIVER}:
        return
    webspace_id = _webspace_id_from_payload(payload)
    if receiver == FRAME_RECEIVER:
        selected = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
        with _ENGINE_LOCK:
            fallback = _engine_instance().empty_frame_stream_payload()
        _publish_stream(receiver, _last_frame_payload_by_webspace.get(selected) or fallback, webspace_id=webspace_id, force=True)
        return
    if receiver == METRICS_RECEIVER:
        selected = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
        for point in list(_metrics_points_by_webspace.get(selected) or []):
            _publish_stream(receiver, point, webspace_id=webspace_id, force=True)
        return
    with _ENGINE_LOCK:
        snapshot = _engine_instance().snapshot()
    selected = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    payload = _last_progress_payload_by_webspace.get(selected) or _progress_payload(snapshot, ok=True)
    _publish_stream(receiver, payload, webspace_id=webspace_id, force=True)


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _payload(evt)
    action = str(payload.get("action") or "").strip().lower()
    if action == "unsubscribed":
        return
    receiver = str(payload.get("receiver") or "").strip()
    if receiver in {FRAME_RECEIVER, METRICS_RECEIVER, PROGRESS_RECEIVER}:
        _log.debug("new_face_vision subscription changed receiver=%s action=%s", receiver, action or "subscribed")


@subscribe("sys.ready")
def on_sys_ready(evt: Any) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id_from_payload(payload)
    _project(webspace_id=webspace_id)
    _publish_event("new_face_vision.ready", {"ok": True, "webspace_id": webspace_id, "ts": time.time()})
