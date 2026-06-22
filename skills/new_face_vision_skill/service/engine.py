from __future__ import annotations

import os
import json
import hashlib
import zipfile
import shutil
import io
import base64
import tempfile
import logging
import time
from pathlib import Path
from typing import Any, Mapping

try:
    import numpy as np
    _numpy_import_error = None
except Exception as exc:
    np = None
    _numpy_import_error = exc

try:
    from PIL import Image
    _pillow_import_error = None
except Exception as exc:
    Image = None
    _pillow_import_error = exc

try:
    import torch
    import torch.nn as nn
    import torchvision
    from torchvision.transforms import functional as TF
    _torch_import_error = None
except Exception as exc:
    torch = None
    nn = None
    torchvision = None
    TF = None
    _torch_import_error = exc

_log = logging.getLogger("new_face_vision.engine")

_PREVIEW_MAX_WIDTH = 640
_PREVIEW_MAX_HEIGHT = 180
_PREVIEW_MIN_WIDTH = 320
_PREVIEW_MIN_HEIGHT = 90
_PREVIEW_JPEG_MAX_BYTES = 12_000
_PREVIEW_JPEG_QUALITIES = (62, 54, 46, 38, 32)
_STATE_MANIFEST = "state_manifest.json"
_UPLOAD_EXTENSIONS = {
    "model": {".pt", ".pth", ".bin", ".ckpt"},
    "frames": {".zip"},
    "masks": {".zip"},
    "metadata": {".jsonl", ".json", ".ndjson"},
}
_UPLOAD_PURPOSES = {
    "model": ("models", "model"),
    "frames": ("frames",),
    "masks": ("masks",),
    "metadata": ("metadata",),
}


class NewFaceVisionEngine:
    def __init__(self, state_dir: Path, upload_root: Path | None = None):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.frames_dir = self.state_dir / "frames"
        self.masks_dir = self.state_dir / "masks"
        self.cache_dir = self.state_dir / "prediction_cache"
        self.frames_dir.mkdir(exist_ok=True)
        self.masks_dir.mkdir(exist_ok=True)
        self.cache_dir.mkdir(exist_ok=True)
        self.manifest_path = self.state_dir / _STATE_MANIFEST
        self.upload_root = Path(upload_root).resolve() if upload_root else self._infer_upload_root()

        self._device = "cuda" if (torch is not None and torch.cuda.is_available()) else "cpu"
        self._model = None
        self._frames: dict[str, Path] = {}
        self._masks: dict[str, Path] = {}
        self._metadata = {}
        self._current_frame_idx = 0
        self._threshold = 0.35
        self._warning_threshold = 0.05
        self._alarm_threshold = 0.15
        self._prediction_cache = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._result_rows: dict[str, dict[str, Any]] = {}
        self._run_id = 1
        self._seq = 0
        self._processed_frames = 0
        self._dice_sum = 0.0
        self._iou_sum = 0.0
        self._target_fps = 5.0
        self._actual_fps: float | None = None
        self._recent_frame_ts: list[float] = []
        self._timeline_scan_at = 0.0
        self._timeline_scan_signature = ""
        self._timeline_cached_indices: list[int] = []
        self._playback: dict[str, Any] = {
            "mode": "idle",
            "fps": self._target_fps,
            "run_id": self._run_id,
            "updated_at": None,
        }
        self._model_path = None
        self._files: dict[str, dict[str, Any] | None] = {
            "model": None,
            "frames": None,
            "masks": None,
            "metadata": None,
        }
        self._operation: dict[str, Any] = {
            "id": None,
            "label": "",
            "progress": None,
            "error": None,
        }
        self._latest: dict[str, Any] | None = None
        self.last_error: dict[str, Any] | None = None

        self.rehydrate()
        _log.info(f"NewFaceVisionEngine initialized. Device: {self._device}")

    def configure(
        self,
        model_path: str | None = None,
        frames_path: str | None = None,
        masks_path: str | None = None,
        metadata_path: str | None = None,
        threshold: float | None = None,
        warning_threshold: float | None = None,
        alarm_threshold: float | None = None,
    ) -> dict[str, Any]:
        result = {"ok": True, "actions": []}

        if threshold is not None:
            self._threshold = self._normalize_threshold(threshold, fallback=self._threshold)
            self._prediction_cache = {}
            result["actions"].append(f"threshold={self._threshold}")
        if warning_threshold is not None:
            self._warning_threshold = self._normalize_threshold(warning_threshold, fallback=self._warning_threshold)
            result["actions"].append(f"warning_threshold={self._warning_threshold}")
        if alarm_threshold is not None:
            self._alarm_threshold = self._normalize_threshold(alarm_threshold, fallback=self._alarm_threshold)
            result["actions"].append(f"alarm_threshold={self._alarm_threshold}")

        if model_path:
            load_result = self.load_model(model_path)
            result.update(load_result)
            if not load_result.get("ok", True):
                result["ok"] = False
                return result

        if frames_path:
            load_result = self.load_frames(frames_path)
            result.update(load_result)
            if not load_result.get("ok", True):
                result["ok"] = False
                return result

        if masks_path:
            load_result = self.load_masks(masks_path)
            result.update(load_result)
            if not load_result.get("ok", True):
                result["ok"] = False
                return result

        if metadata_path:
            load_result = self.load_metadata(metadata_path)
            result.update(load_result)
            if not load_result.get("ok", True):
                result["ok"] = False
                return result

        self.persist_state()
        return result

    def persist_state(self, *, cleared: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "new_face_vision.state.v1",
            "updated_at": time.time(),
            "files": dict(self._files),
            "thresholds": {
                "prediction": self._threshold,
                "warning": self._warning_threshold,
                "alarm": self._alarm_threshold,
            },
        }
        if cleared:
            payload["cleared_at"] = time.time()
        try:
            tmp = self.manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.manifest_path)
            return {"ok": True, "path": str(self.manifest_path)}
        except Exception as exc:
            _log.warning("failed to persist new_face_vision state: %s", exc)
            return {"ok": False, "error": str(exc), "path": str(self.manifest_path)}

    def rehydrate(self, *, force: bool = False) -> dict[str, Any]:
        if force:
            self._model = None
            self._model_path = None
            self._frames = {}
            self._masks = {}
            self._metadata = {}
            self._files = {
                "model": None,
                "frames": None,
                "masks": None,
                "metadata": None,
            }
            self._prediction_cache = {}
            self._result_rows = {}
            self._latest = None

        manifest = self._read_state_manifest()
        if manifest.get("cleared_at"):
            return {
                "ok": True,
                "source": "manifest",
                "cleared": True,
                "restored": {"model": False, "frames": 0, "masks": 0, "metadata": 0},
            }

        source = "manifest" if manifest else "legacy"
        files = manifest.get("files") if isinstance(manifest.get("files"), Mapping) else None
        discovered_files: dict[str, dict[str, Any]] = {}
        if files is None:
            files = self._discover_latest_upload_refs()
        else:
            merged_files = dict(files)
            for kind in ("model", "frames", "masks", "metadata"):
                if self._normalize_file_ref(merged_files.get(kind)):
                    continue
                if not discovered_files:
                    discovered_files = self._discover_latest_upload_refs()
                if kind in discovered_files:
                    merged_files[kind] = discovered_files[kind]
            files = merged_files

        thresholds = manifest.get("thresholds") if isinstance(manifest.get("thresholds"), Mapping) else {}
        self._restore_thresholds(thresholds)

        restored = {
            "model": False,
            "frames": 0,
            "masks": 0,
            "metadata": 0,
        }

        model_ref = self._normalize_file_ref(files.get("model") if isinstance(files, Mapping) else None)
        if model_ref:
            self._files["model"] = model_ref
            model_path = self._path_from_ref(model_ref)
            if model_path and Path(model_path).exists():
                self._model_path = model_path
                restored["model"] = True

        frames_ref = self._normalize_file_ref(files.get("frames") if isinstance(files, Mapping) else None)
        restored["frames"] = self._restore_image_set("frames", frames_ref, self.frames_dir)

        masks_ref = self._normalize_file_ref(files.get("masks") if isinstance(files, Mapping) else None)
        restored["masks"] = self._restore_image_set("masks", masks_ref, self.masks_dir)

        metadata_ref = self._normalize_file_ref(files.get("metadata") if isinstance(files, Mapping) else None)
        restored["metadata"] = self._restore_metadata(metadata_ref)

        if any(restored.values()) and not manifest:
            self.persist_state()
        return {"ok": True, "source": source, "restored": restored}

    def load_model(self, path: str, *, source_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._begin_operation("load_model", "Load model")
            _log.info(f"Loading model from {path}")

            model_result = self._load_model_weights(path)
            if not model_result.get("ok"):
                return self._fail_operation(model_result, code=str(model_result.get("code") or "load_model_failed"))
            self._files["model"] = self._file_ref(path, source_ref=source_ref)

            size_mb = os.path.getsize(path) / 1024 / 1024
            _log.info(f"Model loaded: {size_mb:.1f} MB on {self._device}")

            self._prediction_cache = {}
            self._result_rows = {}
            cleanup = self._cleanup_previous_uploads(Path(path))
            if cleanup:
                self._files["model"]["cleanup"] = cleanup
            self.persist_state()
            self._end_operation()
            return {"ok": True, "model_loaded": True, "device": self._device, "size_mb": round(size_mb, 1)}

        except Exception as e:
            _log.error(f"Failed to load model: {e}")
            return self._fail_operation(str(e), code="load_model_failed")

    def load_frames(self, path: str, *, source_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._begin_operation("load_frames", "Load frames")
            _log.info(f"Loading frames from {path}")

            deps_ok, deps_details = self._ensure_image_dependencies()
            if not deps_ok:
                return self._fail_operation(
                    {
                        "message": "Pillow/numpy are not installed",
                        "details": deps_details,
                    },
                    code="dependency_missing",
                )

            if not os.path.exists(path):
                return self._fail_operation(f"Frames path not found: {path}", code="file_not_found")

            source_path = Path(path)
            if source_path.is_file() and source_path.suffix.lower() == '.zip':
                if self.frames_dir.exists():
                    shutil.rmtree(self.frames_dir)
                self.frames_dir.mkdir(exist_ok=True)

                self._extract_zip_safely(source_path, self.frames_dir)
                images_dir = self.frames_dir
            elif source_path.is_dir():
                images_dir = source_path
            else:
                images_dir = self.frames_dir

            self._frames = self._load_images_from_folder(str(images_dir))
            self._current_frame_idx = 0
            self._prediction_cache = {}
            self._result_rows = {}
            self._latest = None
            self._begin_run(mode="idle")

            if len(self._frames) == 0:
                return self._fail_operation("No images found", code="empty_dataset")

            _log.info(f"Loaded {len(self._frames)} frames")
            self._files["frames"] = self._file_ref(path, source_ref=source_ref)
            cleanup = self._cleanup_previous_uploads(Path(path))
            if cleanup:
                self._files["frames"]["cleanup"] = cleanup
            self.persist_state()
            self._end_operation()
            return {"ok": True, "total_frames": len(self._frames)}

        except Exception as e:
            _log.error(f"Failed to load frames: {e}")
            return self._fail_operation(str(e), code="load_frames_failed")

    def load_masks(self, path: str, *, source_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._begin_operation("load_masks", "Load masks")
            _log.info(f"Loading masks from {path}")

            deps_ok, deps_details = self._ensure_image_dependencies()
            if not deps_ok:
                return self._fail_operation(
                    {
                        "message": "Pillow/numpy are not installed",
                        "details": deps_details,
                    },
                    code="dependency_missing",
                )

            if not os.path.exists(path):
                return self._fail_operation(f"Masks path not found: {path}", code="file_not_found")

            source_path = Path(path)
            if source_path.is_file() and source_path.suffix.lower() == '.zip':
                if self.masks_dir.exists():
                    shutil.rmtree(self.masks_dir)
                self.masks_dir.mkdir(exist_ok=True)

                self._extract_zip_safely(source_path, self.masks_dir)
                masks_dir = self.masks_dir
            elif source_path.is_dir():
                masks_dir = source_path
            else:
                masks_dir = self.masks_dir

            self._masks = self._load_images_from_folder(str(masks_dir))
            self._prediction_cache = {}
            self._result_rows = {}

            _log.info(f"Loaded {len(self._masks)} masks")
            self._files["masks"] = self._file_ref(path, source_ref=source_ref)
            cleanup = self._cleanup_previous_uploads(Path(path))
            if cleanup:
                self._files["masks"]["cleanup"] = cleanup
            self.persist_state()
            self._end_operation()
            return {"ok": True, "loaded_masks": len(self._masks)}

        except Exception as e:
            _log.error(f"Failed to load masks: {e}")
            return self._fail_operation(str(e), code="load_masks_failed")

    def load_metadata(self, path: str, *, source_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._begin_operation("load_metadata", "Load metadata")
            _log.info(f"Loading metadata from {path}")

            if not os.path.exists(path):
                return self._fail_operation(f"Metadata file not found: {path}", code="file_not_found")

            self._metadata = {}
            with open(path, 'r', encoding='utf-8') as f:
                for i, line in enumerate(f):
                    line = line.strip()
                    if line:
                        try:
                            data = json.loads(line)
                            frame_idx = data.get('frame_idx', i)
                            self._metadata[int(frame_idx)] = data
                        except json.JSONDecodeError:
                            continue

            _log.info(f"Loaded {len(self._metadata)} metadata entries")
            self._prediction_cache = {}
            self._result_rows = {}
            self._files["metadata"] = self._file_ref(path, source_ref=source_ref)
            cleanup = self._cleanup_previous_uploads(Path(path))
            if cleanup:
                self._files["metadata"]["cleanup"] = cleanup
            self.persist_state()
            self._end_operation()
            return {"ok": True, "loaded_metadata": len(self._metadata)}

        except Exception as e:
            _log.error(f"Failed to load metadata: {e}")
            return self._fail_operation(str(e), code="load_metadata_failed")

    def process_frame(self, frame_idx: int | None = None, *, count_metrics: bool = True) -> dict[str, Any]:
        try:
            self._begin_operation("process_frame", "Calculate")
            deps_ok, deps_details = self._ensure_image_dependencies()
            if not deps_ok:
                return self._fail_operation(
                    {
                        "message": "Pillow/numpy are not installed",
                        "details": deps_details,
                    },
                    code="dependency_missing",
                )

            if not self._frames:
                return self._fail_operation("No frames loaded", code="frames_missing")

            frame_ctx = self._frame_context(frame_idx)
            if not frame_ctx:
                return self._fail_operation("No frames loaded", code="frames_missing")
            frame_idx = int(frame_ctx["frame_idx"])
            frame_key = str(frame_ctx["frame_key"])
            frame_path = frame_ctx["frame_path"]
            mask_path = frame_ctx["mask_path"]
            true_ratio = frame_ctx["true_ratio"]
            cache_key = str(frame_ctx["cache_key"])
            frame_keys = list(frame_ctx["frame_keys"])
            if cache_key in self._prediction_cache:
                self._cache_hits += 1
                result = self._record_frame_result(
                    dict(self._prediction_cache[cache_key]),
                    total_frames=len(frame_keys),
                    count_metrics=count_metrics,
                    cached=True,
                )
                self._end_operation()
                return result
            cached_result = self._load_cached_result(cache_key)
            if cached_result:
                self._cache_hits += 1
                cached_result["frame_idx"] = frame_idx
                cached_result["frame_key"] = frame_key
                cached_result["total_frames"] = len(frame_keys)
                self._remember_prediction(cache_key, cached_result)
                result = self._record_frame_result(
                    cached_result,
                    total_frames=len(frame_keys),
                    count_metrics=count_metrics,
                    cached=True,
                )
                self._end_operation()
                return result

            self._cache_misses += 1
            frame = self._load_image_ref(frame_path)

            gt_mask = None
            if mask_path is not None:
                gt_mask = self._load_image_ref(mask_path)

            if self._model is None and self._model_path:
                model_result = self._load_model_weights(self._model_path)
                if not model_result.get("ok"):
                    return self._fail_operation(
                        model_result,
                        code=str(model_result.get("code") or "restore_model_failed"),
                    )

            if self._model is not None:
                predicted_mask, _ = self._predict_with_model(frame)
                predicted_mask = Image.fromarray(predicted_mask)
            else:
                predicted_mask = self._create_dummy_prediction(frame)

            side_by_side = self._create_side_by_side_image(frame, gt_mask, predicted_mask)
            preview_base64 = self._encode_preview_jpeg(side_by_side)

            pred_ratio = float(np.mean(np.array(predicted_mask) > 0))

            if pred_ratio >= self._alarm_threshold:
                status, status_color = "Alarm", "red"
            elif pred_ratio >= self._warning_threshold:
                status, status_color = "Warning", "yellow"
            else:
                status, status_color = "OK", "green"

            metrics = {"dice": 0, "iou": 0}
            if gt_mask is not None:
                dice_val, iou_val = self._calculate_metrics(predicted_mask, gt_mask)
                metrics = {"dice": round(dice_val, 4), "iou": round(iou_val, 4)}

            result = {
                "ok": True,
                "frame_idx": frame_idx,
                "frame_key": frame_key,
                "total_frames": len(frame_keys),
                "preview_base64": preview_base64,
                "pred_ratio": round(pred_ratio, 4),
                "true_ratio": round(true_ratio, 4) if true_ratio is not None else None,
                "status": status,
                "status_color": status_color,
                "metrics": metrics,
                "cached": False,
                "cache_key": cache_key,
            }

            cache_stored = self._store_cached_result(cache_key, result)
            result["cache_stored"] = cache_stored
            self._remember_prediction(cache_key, result)
            result = self._record_frame_result(
                result,
                total_frames=len(frame_keys),
                count_metrics=count_metrics,
                cached=False,
            )
            self._end_operation()

            return result

        except Exception as e:
            _log.error(f"Failed to process frame: {e}")
            return self._fail_operation(str(e), code="frame_processing_failed")

    def process_relative_frame(self, delta: int) -> dict[str, Any]:
        if not self._frames:
            self._begin_operation("process_frame", "Calculate")
            return self._fail_operation("No frames loaded", code="frames_missing")

        target_idx = self.resolve_relative_frame_index(delta)
        return self.process_frame(target_idx, count_metrics=False)

    def seek_frame(self, frame_idx: int | None) -> dict[str, Any]:
        return self.process_frame(frame_idx, count_metrics=False)

    def resolve_relative_frame_index(self, delta: int) -> int | None:
        if not self._frames:
            return None
        total_frames = len(self._frames)
        try:
            step = int(delta)
        except Exception:
            step = 1

        latest_idx = self._latest.get("frame_idx") if isinstance(self._latest, Mapping) else None
        try:
            base_idx = int(latest_idx) if latest_idx is not None else int(self._current_frame_idx)
        except Exception:
            base_idx = 0
        if latest_idx is None and step < 0:
            base_idx = int(self._current_frame_idx) - 1
        return (base_idx + step) % total_frames

    def is_frame_cached(self, frame_idx: int | None = None) -> bool:
        frame_ctx = self._frame_context(frame_idx)
        if not frame_ctx:
            return False
        cache_key = str(frame_ctx["cache_key"])
        return cache_key in self._prediction_cache or self._cache_path(cache_key).exists()

    def begin_calculation_status(self, frame_idx: int | None = None) -> dict[str, Any]:
        frame_ctx = self._frame_context(frame_idx)
        target_idx = frame_ctx.get("frame_idx") if frame_ctx else frame_idx
        self._begin_operation("process_frame", "Calculate")
        self._playback = {
            **self._playback,
            "phase": "calculate",
            "calculating_frame_idx": target_idx,
            "updated_at": time.time(),
        }
        return {"ok": True, "frame_idx": target_idx}

    def reset(self) -> dict[str, Any]:
        self._begin_operation("reset", "Reset")
        self._current_frame_idx = 0
        self._prediction_cache = {}
        self._latest = None
        self._begin_run(mode="idle")
        self._end_operation()
        return {"ok": True, "message": "Reset completed"}

    def set_playback(self, mode: str, *, fps: float | None = None) -> dict[str, Any]:
        normalized = str(mode or "idle").strip().lower()
        if normalized not in {"idle", "playing", "paused", "stopped"}:
            normalized = "idle"
        if fps is not None:
            self._target_fps = self._normalize_fps(fps)
        self._playback = {
            **self._playback,
            "mode": normalized,
            "fps": self._target_fps,
            "run_id": self._run_id,
            "updated_at": time.time(),
        }
        return {"ok": True, "playback": dict(self._playback)}

    def replay(self, *, fps: float | None = None) -> dict[str, Any]:
        self._current_frame_idx = 0
        self._prediction_cache = {}
        self._latest = None
        self._begin_run(mode="playing", fps=fps)
        return {"ok": True, "message": "Replay started", "playback": dict(self._playback)}

    def stop(self) -> dict[str, Any]:
        self._current_frame_idx = 0
        self.set_playback("stopped")
        return {"ok": True, "message": "Playback stopped", "playback": dict(self._playback)}

    def clear(self) -> dict[str, Any]:
        self._model = None
        self._model_path = None
        self._frames = {}
        self._masks = {}
        self._metadata = {}
        self._current_frame_idx = 0
        self._prediction_cache = {}
        self._result_rows = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._latest = None
        self._begin_run(mode="idle", bump=False)
        self._files = {
            "model": None,
            "frames": None,
            "masks": None,
            "metadata": None,
        }
        self._operation = {
            "id": None,
            "label": "",
            "progress": None,
            "error": None,
        }
        self.last_error = None

        for dir_path in [self.frames_dir, self.masks_dir]:
            if dir_path.exists():
                shutil.rmtree(dir_path)
                dir_path.mkdir(exist_ok=True)
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)
            self.cache_dir.mkdir(exist_ok=True)

        self.persist_state(cleared=True)
        _log.info("Engine cleared")
        return {"ok": True, "message": "All data cleared"}

    def snapshot(self) -> dict[str, Any]:
        status = "error" if self.last_error else ("ready" if self._frames else "init")
        compute = self._compute_info()
        quality = self._quality_info()
        activity = self._activity_info(status)
        timeline = self._timeline_info()
        return {
            "ok": True,
            "status": status,
            "activity": activity,
            "quality": quality,
            "timeline": timeline,
            "operation": dict(self._operation),
            "files": self._public_files(),
            "file_items": self._file_items(),
            "model": {
                "loaded": self._model is not None or bool(self._model_path),
                "materialized": self._model is not None,
                "available": bool(self._model_path),
                "name": (self._files.get("model") or {}).get("name") if isinstance(self._files.get("model"), Mapping) else "",
                "device": self._device,
            },
            "compute": compute,
            "stats": {
                "total_frames": len(self._frames),
                "loaded_masks": len(self._masks),
                "loaded_metadata": len(self._metadata),
                "model_loaded": self._model is not None or bool(self._model_path),
                "model_materialized": self._model is not None,
                "current_frame": self._latest.get("frame_idx") if self._latest else None,
                "next_frame": self._current_frame_idx,
                "processed_frames": self._processed_frames,
                "avg_dice": self._round_optional(
                    self._dice_sum / self._processed_frames if self._processed_frames else None
                ),
                "avg_iou": self._round_optional(
                    self._iou_sum / self._processed_frames if self._processed_frames else None
                ),
                "fps": self._round_optional(self._actual_fps) if self._actual_fps is not None else self._target_fps,
                "actual_fps": self._round_optional(self._actual_fps),
                "target_fps": self._target_fps,
                "run_id": self._run_id,
                "cached_results": len(self._result_rows),
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
            },
            "cache": {
                "dir": str(self.cache_dir),
                "memory_entries": len(self._prediction_cache),
                "disk_entries": self._count_cache_files(),
                "hits": self._cache_hits,
                "misses": self._cache_misses,
            },
            "playback": dict(self._playback),
            "thresholds": {
                "warning": self._warning_threshold,
                "alarm": self._alarm_threshold,
                "prediction": self._threshold,
            },
            "latest": self._latest or self._empty_latest(),
            "error": self.last_error,
            "history": self._history_rows(),
        }

    def frame_stream_payload(self, result: Mapping[str, Any]) -> dict[str, Any]:
        preview = str(result.get("preview_base64") or "")
        frame_idx = result.get("frame_idx")
        total_frames = result.get("total_frames") or len(self._frames)
        return {
            "ok": bool(result.get("ok", True)),
            "id": result.get("id"),
            "seq": result.get("seq"),
            "run_id": result.get("run_id"),
            "frame_idx": frame_idx,
            "frame_key": result.get("frame_key"),
            "frame_label": self._frame_label(frame_idx, total_frames),
            "total_frames": total_frames,
            "image": {
                "mime": "image/jpeg",
                "encoding": "base64",
                "data": "",
                "src": f"data:image/jpeg;base64,{preview}" if preview else "",
            },
            "prediction": {
                "pred_ratio": result.get("pred_ratio"),
                "true_ratio": result.get("true_ratio"),
            },
            "status": {
                "label": result.get("status"),
                "color": result.get("status_color"),
            },
            "metrics": dict(result.get("metrics") or {}),
            "ts": time.time(),
        }

    def empty_frame_stream_payload(self, *, label: str = "No frame", clear_image: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "clear_image": bool(clear_image),
            "id": f"{self._run_id}:empty:{self._seq}",
            "seq": self._seq,
            "run_id": self._run_id,
            "frame_idx": None,
            "frame_key": None,
            "frame_label": "",
            "total_frames": len(self._frames),
            "image": {
                "mime": "image/jpeg",
                "encoding": "base64",
                "data": "",
                "src": "",
            },
            "prediction": {
                "pred_ratio": None,
                "true_ratio": None,
            },
            "status": {
                "label": label,
                "color": "medium",
            },
            "metrics": {"dice": 0, "iou": 0},
            "ts": time.time(),
        }

    def metrics_stream_payload(self, result: Mapping[str, Any]) -> dict[str, Any]:
        metrics = result.get("metrics") if isinstance(result.get("metrics"), Mapping) else {}
        frame_idx = result.get("frame_idx")
        total_frames = result.get("total_frames") or len(self._frames)
        return {
            "id": result.get("id"),
            "seq": result.get("seq"),
            "run_id": result.get("run_id"),
            "frame_idx": frame_idx,
            "frame_label": self._frame_label(frame_idx, total_frames),
            "total_frames": total_frames,
            "ts": time.time(),
            "series": {
                "pred_ratio": result.get("pred_ratio"),
                "true_ratio": result.get("true_ratio"),
                "dice": metrics.get("dice"),
                "iou": metrics.get("iou"),
            },
        }

    def _record_frame_result(
        self,
        result: Mapping[str, Any],
        *,
        total_frames: int,
        count_metrics: bool = True,
        cached: bool | None = None,
    ) -> dict[str, Any]:
        if not result.get("ok"):
            return dict(result)
        frame_idx = int(result.get("frame_idx") or 0)
        metrics = result.get("metrics") if isinstance(result.get("metrics"), Mapping) else {}
        recorded = dict(result)
        if cached is not None:
            recorded["cached"] = bool(cached)
        self._seq += 1
        recorded["seq"] = self._seq
        recorded["run_id"] = self._run_id
        recorded["id"] = f"{self._run_id}:{self._seq}"
        pred_ratio = result.get("pred_ratio")
        true_ratio = result.get("true_ratio")
        description_parts = []
        if pred_ratio is not None:
            description_parts.append(f"pred={pred_ratio}")
        if true_ratio is not None:
            description_parts.append(f"true={true_ratio}")
        if metrics:
            description_parts.append(f"dice={metrics.get('dice', 0)}")
            description_parts.append(f"iou={metrics.get('iou', 0)}")
        now = time.time()
        self._latest = {
            "value": recorded.get("status") or "ok",
            "label": f"frame {frame_idx + 1}/{total_frames}" if total_frames else f"frame {frame_idx}",
            "description": " ".join(description_parts),
            "id": recorded["id"],
            "seq": self._seq,
            "run_id": self._run_id,
            "frame_idx": frame_idx,
            "frame_key": recorded.get("frame_key"),
            "total_frames": total_frames,
            "pred_ratio": pred_ratio,
            "true_ratio": true_ratio,
            "metrics": dict(metrics),
            "status": {
                "label": recorded.get("status"),
                "color": recorded.get("status_color"),
            },
            "ts": now,
        }
        if not count_metrics:
            recorded["navigation"] = True
            self._latest["navigation"] = True
        else:
            self._processed_frames += 1
            self._dice_sum += self._numeric(metrics.get("dice"))
            self._iou_sum += self._numeric(metrics.get("iou"))
            self._record_frame_rate(now)
        self.last_error = None
        self._current_frame_idx = (frame_idx + 1) % total_frames if total_frames > 0 else 0
        self._playback = {
            **self._playback,
            "run_id": self._run_id,
            "last_frame_idx": frame_idx,
            "updated_at": self._latest["ts"],
        }
        self._record_result_row(recorded)
        return recorded

    def _empty_latest(self) -> dict[str, Any]:
        return {
            "value": "--",
            "label": "",
            "description": "",
            "id": None,
            "seq": self._seq,
            "run_id": self._run_id,
            "frame_idx": None,
            "frame_key": None,
            "total_frames": len(self._frames),
            "pred_ratio": None,
            "true_ratio": None,
            "metrics": {"dice": 0, "iou": 0},
            "status": {"label": "", "color": ""},
            "ts": None,
        }

    def _begin_run(self, *, mode: str, fps: float | None = None, bump: bool = True) -> None:
        if bump:
            self._run_id += 1
        if fps is not None:
            self._target_fps = self._normalize_fps(fps)
        self._seq = 0
        self._processed_frames = 0
        self._dice_sum = 0.0
        self._iou_sum = 0.0
        self._actual_fps = None
        self._recent_frame_ts = []
        self._timeline_scan_at = 0.0
        self._timeline_scan_signature = ""
        self._timeline_cached_indices = []
        self._playback = {
            "mode": mode,
            "fps": self._target_fps,
            "run_id": self._run_id,
            "updated_at": time.time(),
        }

    def _begin_operation(self, operation_id: str, label: str) -> None:
        self._operation = {
            "id": operation_id,
            "label": label,
            "progress": 0.0,
            "error": None,
        }

    def _end_operation(self) -> None:
        self._operation = {
            **self._operation,
            "progress": 1.0,
            "error": None,
        }
        if self._playback.get("phase") == "calculate":
            self._playback = {
                **self._playback,
                "phase": "ready",
                "updated_at": time.time(),
            }
        self.last_error = None

    def _fail_operation(
        self,
        error: Any,
        *,
        code: str = "operation_failed",
        retryable: bool = False,
    ) -> dict[str, Any]:
        normalized = self._normalize_error(error, code=code, retryable=retryable)
        self.last_error = normalized
        self._operation = {
            **self._operation,
            "error": normalized,
        }
        return {"ok": False, "error": normalized}

    def _normalize_error(
        self,
        error: Any,
        *,
        code: str = "operation_failed",
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

    def _normalize_threshold(self, value: Any, *, fallback: float) -> float:
        try:
            parsed = float(value)
        except Exception:
            return fallback
        if not 0 <= parsed <= 1:
            return fallback
        return round(parsed, 4)

    def _normalize_fps(self, value: Any) -> float:
        try:
            parsed = float(value)
        except Exception:
            parsed = self._target_fps
        if parsed < 0.5:
            parsed = 0.5
        if parsed > 30:
            parsed = 30
        return round(parsed, 2)

    def _numeric(self, value: Any) -> float:
        try:
            parsed = float(value)
        except Exception:
            return 0.0
        return parsed if parsed == parsed else 0.0

    def _round_optional(self, value: Any) -> float | None:
        try:
            parsed = float(value)
        except Exception:
            return None
        if parsed != parsed:
            return None
        return round(parsed, 4)

    def _frame_context(self, frame_idx: int | None = None) -> dict[str, Any]:
        if not self._frames:
            return {}
        frame_keys = sorted(self._frames.keys())
        if frame_idx is None:
            frame_idx = self._current_frame_idx
        try:
            normalized_idx = int(frame_idx)
        except Exception:
            normalized_idx = 0
        if normalized_idx >= len(frame_keys):
            normalized_idx = 0
        if normalized_idx < 0:
            normalized_idx = len(frame_keys) - 1

        frame_key = frame_keys[normalized_idx]
        frame_path = self._frames[frame_key]
        mask_path = self._mask_path_for_frame(frame_key)
        true_ratio = None
        if normalized_idx in self._metadata:
            true_ratio = self._metadata[normalized_idx].get("ratio_bad_true")
        cache_key = self._result_cache_key(
            frame_idx=normalized_idx,
            frame_key=frame_key,
            frame_path=frame_path,
            mask_path=mask_path,
            true_ratio=true_ratio,
        )
        return {
            "frame_idx": normalized_idx,
            "frame_key": frame_key,
            "frame_path": frame_path,
            "mask_path": mask_path,
            "true_ratio": true_ratio,
            "cache_key": cache_key,
            "frame_keys": frame_keys,
        }

    def _quality_info(self) -> dict[str, Any]:
        latest = self._latest if isinstance(self._latest, Mapping) else {}
        ratio = self._round_optional(latest.get("pred_ratio")) if latest else None
        threshold = self._warning_threshold
        threshold_label = self._format_percent(threshold)
        frame_label = str(latest.get("label") or "").strip()
        if ratio is None:
            return {
                "value": None,
                "bad_ratio": None,
                "threshold": threshold,
                "threshold_label": threshold_label,
                "label": "--",
                "state": "unknown",
                "color": "medium",
                "description": f"Threshold {threshold_label}",
            }
        defect = ratio >= threshold
        label = "Брак" if defect else "Норма"
        color = "danger" if defect else "success"
        state = "defect" if defect else "normal"
        description = f"{label}; threshold {threshold_label}"
        if frame_label:
            description = f"{description}; {frame_label}"
        return {
            "value": ratio,
            "bad_ratio": ratio,
            "threshold": threshold,
            "threshold_label": threshold_label,
            "label": label,
            "state": state,
            "color": color,
            "description": description,
        }

    def _activity_info(self, status: str) -> dict[str, Any]:
        operation = self._operation if isinstance(self._operation, Mapping) else {}
        playback = self._playback if isinstance(self._playback, Mapping) else {}
        progress = operation.get("progress")
        mode = str(playback.get("mode") or "idle").strip().lower()
        if self.last_error:
            return {
                "value": "error",
                "label": "Error",
                "description": str(self.last_error.get("message") or self.last_error.get("code") or ""),
                "color": "danger",
            }
        if operation.get("id") == "process_frame" and progress != 1.0:
            frame_idx = playback.get("calculating_frame_idx")
            description = ""
            if frame_idx is not None:
                description = f"frame {int(frame_idx) + 1}/{len(self._frames)}" if self._frames else f"frame {frame_idx}"
            return {
                "value": "calculate",
                "label": "Calculate",
                "description": description,
                "color": "warning",
            }
        if mode == "playing":
            return {
                "value": "playing",
                "label": "Playing",
                "description": f"FPS {self._format_number(self._target_fps)}",
                "color": "primary",
            }
        if mode == "paused":
            return {"value": "paused", "label": "Paused", "description": "", "color": "medium"}
        if mode == "stopped":
            return {"value": "stopped", "label": "Stopped", "description": "", "color": "medium"}
        if status == "ready":
            return {"value": "ready", "label": "ready", "description": "", "color": "success"}
        return {"value": "init", "label": "init", "description": "", "color": "medium"}

    def _record_frame_rate(self, timestamp: float) -> None:
        self._recent_frame_ts.append(float(timestamp))
        if len(self._recent_frame_ts) > 20:
            del self._recent_frame_ts[:-20]
        if len(self._recent_frame_ts) < 2:
            self._actual_fps = None
            return
        elapsed = self._recent_frame_ts[-1] - self._recent_frame_ts[0]
        if elapsed > 0:
            self._actual_fps = (len(self._recent_frame_ts) - 1) / elapsed

    def _format_percent(self, value: Any) -> str:
        rounded = self._round_optional(value)
        if rounded is None:
            return "--%"
        return f"{(rounded * 100):.1f}%".replace(".0%", "%")

    def _format_number(self, value: Any) -> str:
        rounded = self._round_optional(value)
        if rounded is None:
            return "--"
        return f"{rounded:.2f}".rstrip("0").rstrip(".")

    def _timeline_info(self) -> dict[str, Any]:
        total_frames = len(self._frames)
        current_frame = self._latest.get("frame_idx") if isinstance(self._latest, Mapping) else None
        disk_cached_indices: set[int] = set(self._disk_cached_frame_indices())
        memory_result_indices: set[int] = set()
        calculated_indices: set[int] = set(disk_cached_indices)
        for key, row in self._result_rows.items():
            try:
                frame_idx = int(row.get("frame_idx", key))
            except Exception:
                continue
            if 0 <= frame_idx < total_frames:
                memory_result_indices.add(frame_idx)
                calculated_indices.add(frame_idx)
        compact_indices = sorted(calculated_indices)
        return {
            "total_frames": total_frames,
            "current_frame": current_frame,
            "next_frame": self._current_frame_idx,
            "calculated_count": len(compact_indices),
            "disk_cached_count": len(disk_cached_indices),
            "memory_result_count": len(memory_result_indices),
            "calculated_ranges": self._compact_ranges(compact_indices),
        }

    def _cached_frame_indices(self) -> list[int]:
        return sorted(set(self._disk_cached_frame_indices()) | set(self._memory_cached_frame_indices()))

    def _memory_cached_frame_indices(self) -> list[int]:
        total_frames = len(self._frames)
        indices: list[int] = []
        for frame_idx in range(total_frames):
            frame_ctx = self._frame_context(frame_idx)
            cache_key = str(frame_ctx.get("cache_key") or "")
            if cache_key and cache_key in self._prediction_cache:
                indices.append(frame_idx)
        return indices

    def _disk_cached_frame_indices(self) -> list[int]:
        total_frames = len(self._frames)
        if total_frames <= 0:
            return []
        signature_payload = {
            "frames": total_frames,
            "threshold": self._threshold,
            "model": self._model_signature(),
            "cache_files": self._count_cache_files(),
        }
        signature = json.dumps(signature_payload, sort_keys=True, separators=(",", ":"), default=str)
        now = time.monotonic()
        if signature == self._timeline_scan_signature and now - self._timeline_scan_at < 3.0:
            return list(self._timeline_cached_indices)

        indices: list[int] = []
        for frame_idx in range(total_frames):
            frame_ctx = self._frame_context(frame_idx)
            cache_key = str(frame_ctx.get("cache_key") or "")
            if not cache_key:
                continue
            if self._cache_path(cache_key).exists():
                indices.append(frame_idx)
        self._timeline_scan_signature = signature
        self._timeline_scan_at = now
        self._timeline_cached_indices = indices
        return list(indices)

    def _compact_ranges(self, values: list[int]) -> list[dict[str, int]]:
        ranges: list[dict[str, int]] = []
        if not values:
            return ranges
        start = values[0]
        prev = values[0]
        for value in values[1:]:
            if value == prev + 1:
                prev = value
                continue
            ranges.append({"start": start, "end": prev})
            start = value
            prev = value
        ranges.append({"start": start, "end": prev})
        return ranges

    def _compute_info(self) -> dict[str, Any]:
        deps_ok, deps_details = self._ensure_model_dependencies()
        torch_version = getattr(torch, "__version__", None) if torch is not None else None
        cuda_version = getattr(getattr(torch, "version", None), "cuda", None) if torch is not None else None
        cuda_available = bool(torch is not None and torch.cuda.is_available())
        device_count = 0
        device_name = ""
        if cuda_available:
            try:
                device_count = int(torch.cuda.device_count())
                if device_count:
                    device_name = str(torch.cuda.get_device_name(0))
            except Exception:
                device_count = 0
                device_name = ""
        mode = "GPU" if cuda_available else "CPU"
        if device_name:
            description = device_name
        elif torch_version:
            description = f"torch {torch_version}, CUDA unavailable"
        else:
            description = "torch unavailable" if not deps_ok else "CUDA unavailable"
        return {
            "mode": mode,
            "device": self._device,
            "description": description,
            "cuda_available": cuda_available,
            "device_count": device_count,
            "device_name": device_name,
            "torch": torch_version,
            "cuda": cuda_version,
            "details": deps_details,
        }

    def _count_cache_files(self) -> int:
        try:
            return sum(1 for path in self.cache_dir.glob("*.json") if path.is_file())
        except Exception:
            return 0

    def _remember_prediction(self, cache_key: str, result: Mapping[str, Any]) -> None:
        if len(self._prediction_cache) >= 100:
            self._prediction_cache.pop(next(iter(self._prediction_cache)), None)
        self._prediction_cache[cache_key] = dict(result)

    def _load_cached_result(self, cache_key: str) -> dict[str, Any] | None:
        path = self._cache_path(cache_key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _log.warning("failed to read prediction cache %s: %s", path, exc)
            return None
        if not isinstance(payload, Mapping):
            return None
        result = payload.get("result") if isinstance(payload.get("result"), Mapping) else payload
        if not isinstance(result, Mapping) or not result.get("ok"):
            return None
        return dict(result)

    def _store_cached_result(self, cache_key: str, result: Mapping[str, Any]) -> bool:
        if not result.get("ok"):
            return False
        path = self._cache_path(cache_key)
        tmp = path.with_suffix(".tmp")
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": "new_face_vision.prediction_cache.v1",
                "cache_key": cache_key,
                "created_at": time.time(),
                "result": dict(result),
            }
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
            tmp.write_text(text, encoding="utf-8")
            try:
                os.replace(tmp, path)
            except Exception as replace_exc:
                _log.warning("atomic prediction cache replace failed %s -> %s: %s", tmp, path, replace_exc)
                path.write_text(text, encoding="utf-8")
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            self._timeline_scan_signature = ""
            _log.info(
                "stored prediction cache frame=%s path=%s",
                result.get("frame_idx"),
                path,
            )
            return True
        except Exception as exc:
            _log.warning("failed to store prediction cache key=%s path=%s: %s", cache_key, path, exc)
            return False

    def _cache_path(self, cache_key: str) -> Path:
        safe_key = "".join(ch for ch in cache_key if ch.isalnum() or ch in {"-", "_"})[:96]
        return self.cache_dir / f"{safe_key}.json"

    def _result_cache_key(
        self,
        *,
        frame_idx: int,
        frame_key: str,
        frame_path: Path,
        mask_path: Path | None,
        true_ratio: Any,
    ) -> str:
        payload = {
            "schema": "new_face_vision.prediction_cache.v1",
            "threshold": self._threshold,
            "model": self._model_signature(),
            "frame": {
                "idx": frame_idx,
                "key": frame_key,
                "file": self._file_signature(frame_path),
            },
            "mask": self._file_signature(mask_path) if mask_path else None,
            "true_ratio": self._round_optional(true_ratio),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _model_signature(self) -> dict[str, Any]:
        if self._model_path:
            return {
                "kind": "torch",
                "file": self._file_signature(Path(self._model_path)),
            }
        return {"kind": "dummy", "file": None}

    def _file_signature(self, path: Path | None) -> dict[str, Any] | None:
        if path is None:
            return None
        file_path = Path(path)
        try:
            stat = file_path.stat()
        except Exception:
            return {"path": str(file_path), "exists": False}
        return {
            "path": str(file_path),
            "name": file_path.name,
            "size_bytes": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
        }

    def _mask_path_for_frame(self, frame_key: str) -> Path | None:
        if frame_key in self._masks:
            return self._masks[frame_key]
        for key, path in self._masks.items():
            if frame_key in key or key in frame_key:
                return path
        return None

    def _record_result_row(self, recorded: Mapping[str, Any]) -> None:
        if not recorded.get("ok"):
            return
        metrics = recorded.get("metrics") if isinstance(recorded.get("metrics"), Mapping) else {}
        frame_idx = int(recorded.get("frame_idx") or 0)
        total_frames = int(recorded.get("total_frames") or len(self._frames) or 0)
        self._result_rows[str(frame_idx)] = {
            "id": recorded.get("id"),
            "frame_idx": frame_idx,
            "frame_label": self._frame_label(frame_idx, total_frames),
            "frame_key": recorded.get("frame_key"),
            "pred_ratio": recorded.get("pred_ratio"),
            "true_ratio": recorded.get("true_ratio"),
            "dice": metrics.get("dice"),
            "iou": metrics.get("iou"),
            "status": recorded.get("status"),
            "cached": bool(recorded.get("cached")),
            "navigation": bool(recorded.get("navigation")),
            "seq": recorded.get("seq"),
            "run_id": recorded.get("run_id"),
            "ts": time.time(),
        }

    def _history_rows(self) -> list[dict[str, Any]]:
        def sort_key(item: tuple[str, dict[str, Any]]) -> int:
            try:
                return int(item[1].get("frame_idx"))
            except Exception:
                return 0

        return [dict(row) for _, row in sorted(self._result_rows.items(), key=sort_key)]

    def _infer_upload_root(self) -> Path | None:
        for key in ("ADAOS_SKILL_ENV_PATH", "ADAOS_SKILL_MEMORY_PATH"):
            raw = str(os.getenv(key) or "").strip()
            if not raw:
                continue
            path = Path(raw)
            data_root = path.parent.parent if path.parent.name == "db" else path.parent
            candidate = (data_root / "files" / "uploads").resolve()
            if candidate.exists():
                return candidate
        return None

    def _read_state_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {}
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _restore_thresholds(self, thresholds: Mapping[str, Any]) -> None:
        if not isinstance(thresholds, Mapping):
            return
        prediction = thresholds.get("prediction", thresholds.get("threshold"))
        warning = thresholds.get("warning", thresholds.get("warning_threshold"))
        alarm = thresholds.get("alarm", thresholds.get("alarm_threshold"))
        if prediction is not None:
            self._threshold = self._normalize_threshold(prediction, fallback=self._threshold)
        if warning is not None:
            self._warning_threshold = self._normalize_threshold(warning, fallback=self._warning_threshold)
        if alarm is not None:
            self._alarm_threshold = self._normalize_threshold(alarm, fallback=self._alarm_threshold)

    def _discover_latest_upload_refs(self) -> dict[str, dict[str, Any]]:
        if self.upload_root is None or not self.upload_root.exists():
            return {}
        refs: dict[str, dict[str, Any]] = {}
        for kind, extensions in _UPLOAD_EXTENSIONS.items():
            purpose_dirs = [self.upload_root / purpose for purpose in _UPLOAD_PURPOSES.get(kind, (kind,))]
            candidates = [
                path
                for purpose_dir in purpose_dirs
                if purpose_dir.exists()
                for path in purpose_dir.rglob("*")
                if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in extensions
            ]
            if not candidates:
                candidates = [
                    path
                    for purpose_dir in purpose_dirs
                    if purpose_dir.exists()
                    for path in purpose_dir.rglob("*")
                    if path.is_file() and not path.name.startswith(".")
                ]
            if not candidates:
                continue
            latest = max(candidates, key=lambda path: path.stat().st_mtime)
            refs[kind] = self._file_ref(
                str(latest),
                source_ref={
                    "purpose": kind,
                    "path": str(latest),
                    "source": "legacy_upload_scan",
                },
            )
        return refs

    def _path_from_ref(self, value: Any) -> str:
        if isinstance(value, Mapping):
            for key in ("path", "local_path", "stored_path", "file_path", "abs_path", "absolute_path"):
                raw = value.get(key)
                if raw:
                    return self._path_from_ref(raw)
            nested = value.get("source")
            if isinstance(nested, Mapping):
                resolved = self._path_from_ref(nested)
                if resolved:
                    return resolved
            uri = str(value.get("uri") or value.get("url") or "").strip()
            if uri.startswith("file://"):
                return uri[len("file://") :]
            return ""
        text = str(value or "").strip()
        if text.startswith("file://"):
            return text[len("file://") :]
        return text

    def _normalize_file_ref(self, value: Any) -> dict[str, Any] | None:
        if not value:
            return None
        if isinstance(value, Mapping):
            path = self._path_from_ref(value)
            if not path:
                return dict(value)
            source = value.get("source") if isinstance(value.get("source"), Mapping) else None
            refreshed = self._file_ref(path, source_ref=source)
            for key in (
                "id",
                "artifact_id",
                "purpose",
                "sha256",
                "mime",
                "relative_path",
                "uri",
                "local_path",
                "stored_path",
                "cleanup",
            ):
                if key in value and key not in refreshed:
                    refreshed[key] = value.get(key)
            return refreshed
        path = self._path_from_ref(value)
        return self._file_ref(path) if path else None

    def _restore_image_set(self, kind: str, ref: Mapping[str, Any] | None, target_dir: Path) -> int:
        if ref:
            self._files[kind] = dict(ref)

        source_path_text = self._path_from_ref(ref)
        images_dir = target_dir
        try:
            source_path = Path(source_path_text) if source_path_text else None
            if source_path and source_path.exists():
                if source_path.is_file() and source_path.suffix.lower() == ".zip":
                    if not self._load_images_from_folder(str(target_dir)):
                        if target_dir.exists():
                            shutil.rmtree(target_dir)
                        target_dir.mkdir(parents=True, exist_ok=True)
                        self._extract_zip_safely(source_path, target_dir)
                    images_dir = target_dir
                elif source_path.is_dir():
                    images_dir = source_path
        except Exception as exc:
            self.last_error = self._normalize_error(
                {
                    "code": f"{kind}_rehydrate_failed",
                    "message": str(exc),
                    "retryable": False,
                },
                code=f"{kind}_rehydrate_failed",
            )
            return 0

        images = self._load_images_from_folder(str(images_dir))
        if images and not ref:
            self._files[kind] = self._file_ref(str(images_dir))
        if kind == "frames":
            self._frames = images
            if images:
                self._current_frame_idx = 0
                self._prediction_cache = {}
                self._latest = None
                self._begin_run(mode="idle", bump=False)
        elif kind == "masks":
            self._masks = images
        return len(images)

    def _restore_metadata(self, ref: Mapping[str, Any] | None) -> int:
        if ref:
            self._files["metadata"] = dict(ref)
        path_text = self._path_from_ref(ref)
        if not path_text:
            return 0
        path = Path(path_text)
        if not path.exists() or not path.is_file():
            return 0

        restored: dict[int, Any] = {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, Mapping):
                        frame_idx = data.get("frame_idx", index)
                        try:
                            restored[int(frame_idx)] = dict(data)
                        except Exception:
                            restored[index] = dict(data)
        except Exception as exc:
            self.last_error = self._normalize_error(
                {
                    "code": "metadata_rehydrate_failed",
                    "message": str(exc),
                    "retryable": False,
                },
                code="metadata_rehydrate_failed",
            )
            return 0
        self._metadata = restored
        if restored and not ref:
            self._files["metadata"] = self._file_ref(str(path))
        return len(restored)

    def _load_model_weights(self, path: str) -> dict[str, Any]:
        if not os.path.exists(path):
            return {
                "ok": False,
                "code": "file_not_found",
                "message": f"Model file not found: {path}",
            }
        validation_error = self._validate_model_file(path)
        if validation_error is not None:
            return validation_error

        deps_ok, deps_details = self._ensure_model_dependencies()
        if not deps_ok:
            return {
                "ok": False,
                "code": "dependency_missing",
                "message": "torch/torchvision are not installed",
                "details": deps_details,
            }

        try:
            checkpoint = self._load_torch_checkpoint(path)

            model = torchvision.models.segmentation.deeplabv3_resnet50(
                weights=None,
                weights_backbone=None,
            )
            model.classifier[-1] = nn.Conv2d(256, 1, kernel_size=1)

            if "model_state" in checkpoint:
                model.load_state_dict(checkpoint["model_state"], strict=False)
                _log.info(f"Loaded checkpoint epoch: {checkpoint.get('epoch', '?')}")
            else:
                model.load_state_dict(checkpoint, strict=False)

            model.to(self._device)
            model.eval()
            self._model = model
            self._model_path = path
            return {"ok": True}
        except Exception as exc:
            return {
                "ok": False,
                "code": "load_model_failed",
                "message": str(exc),
            }

    def _validate_model_file(self, path: str) -> dict[str, Any] | None:
        model_path = Path(path)
        try:
            size = model_path.stat().st_size
            with model_path.open("rb") as handle:
                prefix = handle.read(64).lstrip()
        except OSError as exc:
            return {
                "ok": False,
                "code": "file_not_readable",
                "message": f"Model file is not readable: {exc}",
            }
        if size < 1024:
            return {
                "ok": False,
                "code": "invalid_model_file",
                "message": f"Model file is too small to be a PyTorch checkpoint: {size} bytes",
                "details": {"path": str(model_path), "size_bytes": size},
            }
        if prefix.startswith((b"{", b"[", b"<", b"<!")):
            return {
                "ok": False,
                "code": "invalid_model_file",
                "message": "Model file looks like text/JSON/HTML, not a PyTorch checkpoint",
                "details": {
                    "path": str(model_path),
                    "size_bytes": size,
                    "prefix": prefix[:32].decode("utf-8", errors="replace"),
                },
            }
        return None

    def _load_torch_checkpoint(self, path: str) -> Any:
        try:
            return torch.load(path, map_location=self._device, weights_only=True)
        except TypeError:
            return torch.load(path, map_location=self._device)
        except Exception as exc:
            message = str(exc)
            if "Weights only load failed" not in message and "Unsupported operand" not in message:
                raise
            _log.warning(
                "Retrying trusted PyTorch checkpoint load with weights_only=False after weights-only failure: %s",
                message,
            )
            return torch.load(path, map_location=self._device, weights_only=False)

    def _ensure_image_dependencies(self) -> tuple[bool, dict[str, str]]:
        global Image, np, _numpy_import_error, _pillow_import_error

        details: dict[str, str] = {}
        if np is None:
            try:
                import numpy as imported_np

                np = imported_np
                _numpy_import_error = None
            except Exception as exc:
                _numpy_import_error = exc
        if Image is None:
            try:
                from PIL import Image as imported_image

                Image = imported_image
                _pillow_import_error = None
            except Exception as exc:
                _pillow_import_error = exc

        if np is None and _numpy_import_error is not None:
            details["numpy"] = repr(_numpy_import_error)
        if Image is None and _pillow_import_error is not None:
            details["pillow"] = repr(_pillow_import_error)
        return Image is not None and np is not None, details

    def _ensure_model_dependencies(self) -> tuple[bool, dict[str, str]]:
        global TF, nn, torch, torchvision, _torch_import_error

        if torch is None or nn is None or torchvision is None or TF is None:
            try:
                import torch as imported_torch
                import torch.nn as imported_nn
                import torchvision as imported_torchvision
                from torchvision.transforms import functional as imported_tf

                torch = imported_torch
                nn = imported_nn
                torchvision = imported_torchvision
                TF = imported_tf
                _torch_import_error = None
            except Exception as exc:
                _torch_import_error = exc

        if torch is not None:
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        details: dict[str, str] = {}
        if (torch is None or nn is None or torchvision is None or TF is None) and _torch_import_error is not None:
            details["torch"] = repr(_torch_import_error)
        return torch is not None and nn is not None and torchvision is not None and TF is not None, details

    def _frame_label(self, frame_idx: Any, total_frames: Any) -> str:
        if frame_idx is None:
            return ""
        try:
            idx = int(frame_idx)
            total = int(total_frames or 0)
        except Exception:
            return str(frame_idx)
        return f"{idx + 1}/{total}" if total else str(idx)

    def _file_ref(self, path: str, *, source_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
        file_path = Path(path)
        stat = None
        if file_path.exists():
            stat = file_path.stat()
        out = {
            "path": str(file_path),
            "name": file_path.name,
            "exists": file_path.exists(),
            "size_bytes": stat.st_size if stat is not None and file_path.is_file() else None,
            "modified_at": stat.st_mtime if stat is not None else None,
            "updated_at": stat.st_mtime if stat is not None else None,
        }
        if source_ref:
            out["source"] = dict(source_ref)
        return out

    def _public_files(self) -> dict[str, dict[str, Any] | None]:
        return {
            kind: self._public_file_ref(ref)
            for kind, ref in self._files.items()
        }

    def _public_file_ref(self, ref: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not ref:
            return None
        name = str(ref.get("name") or Path(str(ref.get("path") or "")).name or "")
        out: dict[str, Any] = {
            "name": name,
            "exists": ref.get("exists"),
            "size_bytes": ref.get("size_bytes"),
            "size_label": self._format_bytes(ref.get("size_bytes")),
            "modified_at": ref.get("modified_at"),
            "updated_at": ref.get("updated_at"),
        }
        cleanup = ref.get("cleanup") if isinstance(ref, Mapping) else None
        if isinstance(cleanup, Mapping):
            out["cleanup"] = {
                "deleted_count": cleanup.get("deleted_count"),
                "deleted_names": cleanup.get("deleted_names"),
                "deleted_bytes": cleanup.get("deleted_bytes"),
            }
        return out

    def _file_items(self) -> list[dict[str, Any]]:
        labels = {
            "model": "Model",
            "frames": "Frames",
            "masks": "Masks",
            "metadata": "Metadata",
        }
        icons = {
            "model": "cube-outline",
            "frames": "images-outline",
            "masks": "layers-outline",
            "metadata": "document-text-outline",
        }
        counters = {
            "model": "loaded" if self._model is not None else "available" if self._model_path else "",
            "frames": f"{len(self._frames)} frames" if self._frames else "",
            "masks": f"{len(self._masks)} masks" if self._masks else "",
            "metadata": f"{len(self._metadata)} rows" if self._metadata else "",
        }
        items: list[dict[str, Any]] = []
        for kind in ("model", "frames", "masks", "metadata"):
            ref = self._files.get(kind)
            if not ref:
                continue
            name = str(ref.get("name") or Path(str(ref.get("path") or "")).name or kind)
            size_label = self._format_bytes(ref.get("size_bytes"))
            counter = counters.get(kind) or ""
            title_suffix = f" ({counter})" if counter else ""
            details = {
                "kind": kind,
                "size": size_label,
                "size_bytes": ref.get("size_bytes"),
                "modified_at": ref.get("modified_at"),
                "exists": ref.get("exists"),
            }
            cleanup = ref.get("cleanup") if isinstance(ref, Mapping) else None
            if cleanup:
                details["cleanup"] = cleanup
            items.append(
                {
                    "id": kind,
                    "kind": kind,
                    "icon": icons.get(kind),
                    "label": f"{labels[kind]}: {name}{title_suffix}",
                    "name": name,
                    "updated_at": ref.get("updated_at"),
                    "modified_at": ref.get("modified_at"),
                    "size_bytes": ref.get("size_bytes"),
                    "size_label": size_label,
                    "details": details,
                }
            )
        return items

    def _format_bytes(self, value: Any) -> str:
        try:
            size = int(value)
        except Exception:
            return ""
        if size >= 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024 * 1024):.1f} GB"
        if size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        if size >= 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size} B"

    def _cleanup_previous_uploads(self, current_path: Path) -> dict[str, Any] | None:
        try:
            current = current_path.resolve()
        except Exception:
            return None
        if not current.exists() or not current.is_file():
            return None

        purpose_dir = self._upload_purpose_dir(current)
        if purpose_dir is None:
            return None

        deleted_names: list[str] = []
        deleted_bytes = 0
        for candidate in sorted(purpose_dir.rglob("*")):
            if not candidate.is_file():
                continue
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            if resolved == current or candidate.name.startswith("."):
                continue
            try:
                stat = candidate.stat()
                candidate.unlink()
                deleted_names.append(candidate.name)
                deleted_bytes += int(stat.st_size)
            except Exception as exc:
                _log.warning("failed to remove stale upload %s: %s", candidate, exc)

        for directory in sorted(
            [item for item in purpose_dir.rglob("*") if item.is_dir()],
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

        if not deleted_names:
            return None
        return {
            "deleted_count": len(deleted_names),
            "deleted_names": deleted_names[:20],
            "deleted_bytes": deleted_bytes,
        }

    def _upload_purpose_dir(self, current: Path) -> Path | None:
        for parent in current.parents:
            if parent.name and parent.parent.name == "uploads":
                return parent
        return None

    def _load_images_from_folder(self, folder_path: str) -> dict[str, Path]:
        images: dict[str, Path] = {}
        folder = Path(folder_path)

        if not folder.exists():
            return images

        image_extensions = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif'}

        for img_path in sorted(folder.rglob('*')):
            if img_path.suffix.lower() in image_extensions:
                images[img_path.stem] = img_path

        return images

    def _extract_zip_safely(self, zip_path: Path, dest_dir: Path) -> None:
        dest_root = dest_dir.resolve()
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.infolist():
                if member.is_dir():
                    continue
                member_name = str(member.filename or "").replace("\\", "/")
                if not member_name or member_name.startswith("/") or ".." in Path(member_name).parts:
                    raise ValueError(f"Unsafe archive member: {member.filename}")
                target = (dest_root / member_name).resolve()
                if dest_root not in target.parents and target != dest_root:
                    raise ValueError(f"Unsafe archive member: {member.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_ref.open(member) as source, open(target, "wb") as sink:
                    shutil.copyfileobj(source, sink)

    def _load_image_ref(self, img_path: Path) -> Image.Image:
        with Image.open(img_path) as img:
            return img.copy()

    def _create_dummy_prediction(self, frame: Image.Image) -> Image.Image:
        img_array = np.array(frame.convert('L'))
        threshold = np.mean(img_array) * 0.8
        pred_mask = (img_array < threshold).astype(np.uint8) * 255
        return Image.fromarray(pred_mask)

    def _predict_with_model(self, frame: Image.Image):
        img_tensor = TF.to_tensor(frame).unsqueeze(0).to(self._device)

        with torch.no_grad():
            if self._device == 'cuda':
                with torch.amp.autocast("cuda"):
                    logits = self._model(img_tensor)["out"]
            else:
                logits = self._model(img_tensor)["out"]

            prob = torch.sigmoid(logits)[0, 0].cpu().numpy()
            pred = (prob > self._threshold).astype(np.uint8) * 255

        return pred, prob

    def _encode_preview_jpeg(self, image: Image.Image) -> str:
        preview = image.convert("RGB")
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", getattr(Image, "LANCZOS", 1))
        max_width = _PREVIEW_MAX_WIDTH
        max_height = _PREVIEW_MAX_HEIGHT
        best_bytes = b""

        while True:
            resized = preview.copy()
            resized.thumbnail((max_width, max_height), resampling)

            for quality in _PREVIEW_JPEG_QUALITIES:
                buffered = io.BytesIO()
                resized.save(buffered, format="JPEG", quality=quality, optimize=True)
                jpeg_bytes = buffered.getvalue()
                if not best_bytes or len(jpeg_bytes) < len(best_bytes):
                    best_bytes = jpeg_bytes
                if len(jpeg_bytes) <= _PREVIEW_JPEG_MAX_BYTES:
                    return base64.b64encode(jpeg_bytes).decode()

            if max_width <= _PREVIEW_MIN_WIDTH and max_height <= _PREVIEW_MIN_HEIGHT:
                return base64.b64encode(best_bytes).decode()
            max_width = max(_PREVIEW_MIN_WIDTH, int(max_width * 0.75))
            max_height = max(_PREVIEW_MIN_HEIGHT, int(max_height * 0.75))

    def _create_side_by_side_image(self, original: Image.Image, gt_mask: Image.Image | None = None, pred_mask: Image.Image | None = None) -> Image.Image:
        if original.mode != 'RGB':
            original = original.convert('RGB')
        original_arr = np.array(original)

        h, w = original_arr.shape[:2]

        panel1 = original_arr.copy()

        panel2 = np.zeros((h, w, 3), dtype=np.uint8)
        if gt_mask is not None:
            gt_arr = np.array(gt_mask)
            if len(gt_arr.shape) == 3:
                gt_arr = gt_arr[:, :, 0]
            if gt_arr.max() > 0:
                gt_arr = (gt_arr > 30).astype(np.uint8) * 255
            panel2[gt_arr > 128] = [255, 255, 255]

        panel3 = np.zeros((h, w, 3), dtype=np.uint8)
        if pred_mask is not None:
            pred_arr = np.array(pred_mask)
            if len(pred_arr.shape) == 3:
                pred_arr = pred_arr[:, :, 0]
            if pred_arr.max() > 0:
                pred_arr = (pred_arr > 30).astype(np.uint8) * 255
            panel3[pred_arr > 128] = [255, 255, 255]

        panel4 = original_arr.copy()
        if pred_mask is not None:
            pred_arr = np.array(pred_mask)
            if len(pred_arr.shape) == 3:
                pred_arr = pred_arr[:, :, 0]
            if pred_arr.max() > 0:
                mask = pred_arr > 30
                if mask.any():
                    panel4[mask] = [255, 0, 0]
                    alpha = 0.6
                    panel4[mask] = (alpha * panel4[mask] + (1 - alpha) * original_arr[mask]).astype(np.uint8)

        combined = np.concatenate([panel1, panel2, panel3, panel4], axis=1)
        return Image.fromarray(combined)

    def _calculate_metrics(self, pred_mask: Image.Image, gt_mask: Image.Image) -> tuple[float, float]:
        pred = (np.array(pred_mask) > 128).astype(np.uint8)
        gt = (np.array(gt_mask) > 128).astype(np.uint8)

        if len(pred.shape) == 3:
            pred = pred[:, :, 0]
        if len(gt.shape) == 3:
            gt = gt[:, :, 0]

        intersection = (pred & gt).sum()
        pred_sum = pred.sum()
        gt_sum = gt.sum()

        eps = 1e-6
        dice = (2.0 * intersection + eps) / (pred_sum + gt_sum + eps)
        iou = (intersection + eps) / (pred_sum + gt_sum - intersection + eps)

        return float(dice), float(iou)
