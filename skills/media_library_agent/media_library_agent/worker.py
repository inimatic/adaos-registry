from __future__ import annotations

import fnmatch
import json
import mimetypes
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .contracts import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    PROGRESS_SCHEMA,
    VIDEO_EXTENSIONS,
    fingerprint,
    folder_segments,
    media_kind,
    now_iso,
    text,
)
from .repository import MediaLibraryAgentRepository


RegisterCallback = Callable[[Path, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
PublishCallback = Callable[[Mapping[str, Any], str], None]


class MediaLibraryAgentWorker:
    """One bounded scanner per agent process with durable queue ownership."""

    def __init__(
        self,
        repository: MediaLibraryAgentRepository,
        *,
        register: RegisterCallback | None = None,
        publish: PublishCallback | None = None,
        poll_seconds: float = 1.0,
    ):
        self.repository = repository
        self._register = register or register_media_reference
        self._publish_callback = publish
        self._poll_seconds = max(0.05, min(10.0, float(poll_seconds)))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._resource_pressure = "normal"
        self._last_publish_monotonic = 0.0
        self._job_started: dict[str, float] = {}
        self._wait_reason = ""

    def ensure_started(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return False
            self._stop.clear()
            thread = threading.Thread(target=self._loop, name="media-library-agent", daemon=True)
            self._thread = thread
            thread.start()
        return True

    def dispose(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def set_resource_pressure(self, level: str) -> str:
        token = text(level).lower()
        if token not in {"normal", "playback", "critical"}:
            raise ValueError("invalid_resource_pressure")
        self._resource_pressure = token
        self._wake.set()
        return token

    @property
    def resource_pressure(self) -> str:
        return self._resource_pressure

    def run_once(self) -> dict[str, Any] | None:
        self._enqueue_due_schedules()
        queued = self.repository.next_queued_job()
        if queued is None:
            return None
        claimed = self.repository.claim_job(queued["id"])
        if claimed is None:
            return None
        return self._run_job(claimed)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.run_once()
            except Exception:
                result = None
            if result is None:
                self._wake.wait(self._poll_seconds)
                self._wake.clear()

    def _enqueue_due_schedules(self) -> None:
        for schedule in self.repository.due_schedules():
            root_id = text(schedule.get("root_id"))
            if not root_id:
                continue
            self.repository.create_job(root_id, mode="incremental")
            next_run = datetime.now(tz=timezone.utc) + timedelta(seconds=int(schedule.get("interval_seconds") or 21600))
            self.repository.advance_schedule(root_id, next_run.isoformat())

    def _run_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        job_id = text(job.get("id"))
        root = self.repository.get_root(text(job.get("root_id")))
        if root is None:
            return self._finish_failed(job_id, "root_not_found", "The configured media root no longer exists.")
        root_path = Path(root["path"])
        if not root_path.exists() or not root_path.is_dir():
            return self._finish_failed(job_id, "root_unavailable", f"Media root is unavailable: {root_path}")

        counters = {
            "discovered_count": 0,
            "processed_count": 0,
            "added_count": 0,
            "updated_count": 0,
            "removed_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "processed_bytes": 0,
        }
        seen: set[str] = set()
        current_path = ""
        self._job_started[job_id] = time.monotonic()
        self._publish_progress(job_id, root, counters, current_path, force=True)
        try:
            for path, relative_path, stat in self._walk(root_path, root):
                current = self.repository.get_job(job_id)
                if self._stop.is_set() or (current and current["cancel_requested"]):
                    finished = self.repository.update_job(job_id, status="canceled", finished_at=now_iso(), current_path=current_path)
                    self.repository.mark_root_scan(root["id"], "canceled")
                    self._publish_progress(job_id, root, counters, current_path, status="canceled", force=True)
                    return finished or {}

                self._wait_for_resources(job_id, root, counters, current_path)
                current_path = relative_path
                counters["discovered_count"] += 1
                seen.add(relative_path)
                try:
                    source_fingerprint = fingerprint(path, relative_path=relative_path, stat=stat)
                    previous = self.repository.source_by_path(root["id"], relative_path)
                    descriptor: Mapping[str, Any]
                    if previous and previous["fingerprint"] == source_fingerprint and previous["present"]:
                        descriptor = previous.get("descriptor") or {}
                    else:
                        metadata = {
                            "media_library_agent_id": self.repository.agent_id,
                            "media_library_root_id": root["id"],
                            "media_library_root_path": root["path"],
                            "relative_path": relative_path,
                            "folder_path": str(Path(relative_path).parent).replace("\\", "/").strip("."),
                            "folder_segments": folder_segments(relative_path),
                            "storage_mode": "reference",
                            "technical": self._technical_metadata(
                                path, stat=stat
                            ),
                        }
                        descriptor = self._register(path, root, metadata)
                    mime_type = text(descriptor.get("mime_type") or descriptor.get("mime")) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    source = {
                        "root_id": root["id"],
                        "relative_path": relative_path,
                        "folder_path": str(Path(relative_path).parent).replace("\\", "/").strip("."),
                        "name": path.name,
                        "media_kind": media_kind(path, mime_type),
                        "mime_type": mime_type,
                        "size_bytes": int(stat.st_size),
                        "modified_ns": int(stat.st_mtime_ns),
                        "inode": int(getattr(stat, "st_ino", 0) or 0),
                        "fingerprint": source_fingerprint,
                        "resource_id": text(descriptor.get("resource_id") or descriptor.get("id")),
                        "descriptor": dict(descriptor),
                        "metadata": dict(descriptor.get("metadata") or {}),
                    }
                    operation, _ = self.repository.upsert_source(source, job_id=job_id)
                    if operation == "added":
                        counters["added_count"] += 1
                    elif operation in {"updated", "restored"}:
                        counters["updated_count"] += 1
                    else:
                        counters["skipped_count"] += 1
                    counters["processed_bytes"] += int(stat.st_size)
                except Exception:
                    counters["error_count"] += 1
                counters["processed_count"] += 1
                self._checkpoint(job_id, root, counters, current_path)

            removed = self.repository.mark_missing(root["id"], seen_relative_paths=seen, job_id=job_id)
            counters["removed_count"] = len(removed)
            finished = self.repository.update_job(
                job_id,
                status="completed",
                finished_at=now_iso(),
                current_path="",
                **counters,
            )
            self.repository.mark_root_scan(root["id"], "completed")
            self._publish_progress(job_id, root, counters, "", status="completed", force=True)
            self._job_started.pop(job_id, None)
            return finished or {}
        except Exception as exc:
            return self._finish_failed(job_id, "scan_failed", str(exc), root=root, counters=counters, current_path=current_path)

    def _walk(self, root_path: Path, root: Mapping[str, Any]) -> Iterator[tuple[Path, str, os.stat_result]]:
        include_images = bool(root.get("include_images"))
        suffixes = set(VIDEO_EXTENSIONS) | set(AUDIO_EXTENSIONS)
        if include_images:
            suffixes.update(IMAGE_EXTENSIONS)
        exclusions = [text(item) for item in root.get("exclusions") or [] if text(item)]
        follow_symlinks = bool(root.get("follow_symlinks"))
        stack = [root_path]
        visited_directories: set[tuple[int, int]] = set()
        max_files = max(1, min(5_000_000, int(os.environ.get("MEDIA_LIBRARY_AGENT_MAX_FILES") or 1_000_000)))
        yielded = 0
        while stack and yielded < max_files and not self._stop.is_set():
            directory = stack.pop()
            try:
                directory_stat = directory.stat(follow_symlinks=follow_symlinks)
                identity = (int(directory_stat.st_dev), int(directory_stat.st_ino))
                if identity in visited_directories:
                    continue
                visited_directories.add(identity)
                entries = list(os.scandir(directory))
            except (OSError, PermissionError):
                continue
            entries.sort(key=lambda item: item.name.casefold(), reverse=True)
            for entry in entries:
                path = Path(entry.path)
                try:
                    relative_path = path.relative_to(root_path).as_posix()
                except ValueError:
                    continue
                if self._excluded(relative_path, exclusions):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=follow_symlinks):
                        if entry.is_symlink() and not follow_symlinks:
                            continue
                        stack.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=follow_symlinks):
                        continue
                    if path.suffix.lower() not in suffixes:
                        continue
                    stat = entry.stat(follow_symlinks=follow_symlinks)
                except (OSError, PermissionError):
                    continue
                yielded += 1
                yield path, relative_path, stat
                if yielded >= max_files:
                    break

    @staticmethod
    def _excluded(relative_path: str, patterns: list[str]) -> bool:
        normalized = relative_path.replace("\\", "/")
        return any(fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(Path(normalized).name, pattern) for pattern in patterns)

    def _wait_for_resources(
        self,
        job_id: str,
        root: Mapping[str, Any],
        counters: Mapping[str, int],
        current_path: str,
    ) -> None:
        waiting = False
        while (
            self._resource_pressure in {"playback", "critical"}
            or not self._scan_window_open(root)
        ) and not self._stop.is_set():
            current = self.repository.get_job(job_id)
            if current and current["cancel_requested"]:
                return
            if not waiting:
                self._wait_reason = (
                    "resource_pressure"
                    if self._resource_pressure in {"playback", "critical"}
                    else "scan_window"
                )
                self.repository.update_job(job_id, status="waiting_resources")
                self._publish_progress(job_id, root, counters, current_path, status="waiting_resources", force=True)
                waiting = True
            self._wake.wait(0.5)
            self._wake.clear()
        if waiting:
            self._wait_reason = ""
            self.repository.update_job(job_id, status="running")

    def _checkpoint(self, job_id: str, root: Mapping[str, Any], counters: Mapping[str, int], current_path: str) -> None:
        self._throttle_io(job_id, counters)
        now = time.monotonic()
        if int(counters["processed_count"]) % 100 != 0 and now - self._last_publish_monotonic < 0.5:
            return
        self.repository.update_job(job_id, current_path=current_path, **dict(counters))
        self._publish_progress(job_id, root, counters, current_path)

    def _finish_failed(
        self,
        job_id: str,
        code: str,
        detail: str,
        *,
        root: Mapping[str, Any] | None = None,
        counters: Mapping[str, int] | None = None,
        current_path: str = "",
    ) -> dict[str, Any]:
        fields = dict(counters or {})
        finished = self.repository.update_job(
            job_id,
            status="failed",
            finished_at=now_iso(),
            error_code=text(code),
            error_detail=text(detail)[:2000],
            current_path=current_path,
            **fields,
        )
        if root:
            self.repository.mark_root_scan(root["id"], "failed")
            self._publish_progress(job_id, root, counters or {}, current_path, status="failed", force=True)
        self._job_started.pop(job_id, None)
        return finished or {}

    def _publish_progress(
        self,
        job_id: str,
        root: Mapping[str, Any],
        counters: Mapping[str, int],
        current_path: str,
        *,
        status: str = "running",
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and now - self._last_publish_monotonic < 0.5:
            return
        self._last_publish_monotonic = now
        started = self._job_started.get(job_id, now)
        elapsed = max(0.001, now - started)
        progress = dict(counters)
        progress.update(
            {
                "elapsed_seconds": round(elapsed, 3),
                "throughput_bytes_per_second": round(
                    int(counters.get("processed_bytes") or 0) / elapsed, 3
                ),
                "phase": (
                    "waiting"
                    if status == "waiting_resources"
                    else "terminal"
                    if status in {"completed", "failed", "canceled"}
                    else "enumerating"
                ),
                "checkpoint_age_seconds": 0,
            }
        )
        payload = {
            "schema": PROGRESS_SCHEMA,
            "job_id": job_id,
            "agent_id": self.repository.agent_id,
            "node_id": self.repository.node_id,
            "root_id": root["id"],
            "root_label": root["label"],
            "status": status,
            "progress": progress,
            "current_path": current_path[-500:],
            "resource_pressure": self._resource_pressure,
            "wait_reason": self._wait_reason,
            "updated_at": now_iso(),
        }
        if self._publish_callback:
            try:
                self._publish_callback(payload, text((self.repository.get_job(job_id) or {}).get("webspace_id")))
            except Exception:
                pass

    @staticmethod
    def _scan_window_open(root: Mapping[str, Any]) -> bool:
        window = root.get("scan_window")
        if not isinstance(window, Mapping) or not window:
            return True
        local = datetime.now().astimezone()
        days = window.get("days")
        if isinstance(days, list) and days and local.weekday() not in {
            int(item) for item in days if str(item).isdigit()
        }:
            return False
        start = text(window.get("start"))
        end = text(window.get("end"))
        if not start or not end:
            return True
        try:
            start_minutes = int(start[:2]) * 60 + int(start[3:5])
            end_minutes = int(end[:2]) * 60 + int(end[3:5])
        except (TypeError, ValueError):
            return True
        current = local.hour * 60 + local.minute
        if start_minutes <= end_minutes:
            return start_minutes <= current < end_minutes
        return current >= start_minutes or current < end_minutes

    def _throttle_io(self, job_id: str, counters: Mapping[str, int]) -> None:
        try:
            maximum = max(
                0,
                int(os.environ.get("MEDIA_LIBRARY_AGENT_MAX_BYTES_PER_SECOND") or 0),
            )
        except ValueError:
            maximum = 0
        if maximum <= 0:
            return
        started = self._job_started.get(job_id, time.monotonic())
        expected = int(counters.get("processed_bytes") or 0) / maximum
        delay = expected - (time.monotonic() - started)
        if delay > 0:
            self._stop.wait(min(delay, 0.5))

    @staticmethod
    def _technical_metadata(path: Path, *, stat: os.stat_result) -> dict[str, Any]:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        result: dict[str, Any] = {
            "probe": "basic",
            "container": path.suffix.lower().lstrip("."),
            "mime_type": mime_type,
            "size_bytes": int(stat.st_size),
        }
        mode = text(os.environ.get("MEDIA_LIBRARY_AGENT_PROBE_MODE") or "basic").lower()
        executable = shutil.which("ffprobe") if mode == "ffprobe" else None
        if not executable:
            return result
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration,bit_rate,format_name:stream=codec_type,codec_name,width,height,sample_rate,channels",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                check=False,
                timeout=max(
                    1.0,
                    min(
                        float(
                            os.environ.get("MEDIA_LIBRARY_AGENT_PROBE_TIMEOUT_SECONDS")
                            or 5
                        ),
                        30.0,
                    ),
                ),
            )
            if completed.returncode != 0 or len(completed.stdout) > 256 * 1024:
                return result | {"probe_status": "failed"}
            payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
            streams = [item for item in payload.get("streams") or [] if isinstance(item, Mapping)]
            primary = next(
                (item for item in streams if item.get("codec_type") == "video"),
                streams[0] if streams else {},
            )
            format_value = payload.get("format") if isinstance(payload.get("format"), Mapping) else {}
            return result | {
                "probe": "ffprobe",
                "probe_status": "complete",
                "codec": text(primary.get("codec_name")),
                "width": int(primary.get("width") or 0),
                "height": int(primary.get("height") or 0),
                "sample_rate": int(primary.get("sample_rate") or 0),
                "channels": int(primary.get("channels") or 0),
                "duration_seconds": float(format_value.get("duration") or 0),
                "bitrate": int(format_value.get("bit_rate") or 0),
                "format": text(format_value.get("format_name")),
            }
        except Exception:
            return result | {"probe_status": "failed"}


def register_media_reference(path: Path, root: Mapping[str, Any], metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    from adaos.sdk.io.media import register_media_file

    return register_media_file(
        path,
        root=Path(str(root["path"])),
        content_ref=f"{root['id']}:{path.resolve(strict=True)}",
        namespace="media-library",
        mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        metadata=dict(metadata),
    )
