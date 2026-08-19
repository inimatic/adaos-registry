from __future__ import annotations

import fnmatch
import hashlib
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
    RENDITION_JOB_SCHEMA,
    VIDEO_EXTENSIONS,
    fingerprint,
    folder_segments,
    media_kind,
    now_iso,
    text,
)
from .rendition import (
    current_disk_usage,
    output_path,
    publish_derived_resource,
    rendition_limits,
    transcode_with_ffmpeg,
)
from .repository import MediaLibraryAgentRepository


RegisterCallback = Callable[[Path, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
PublishCallback = Callable[[Mapping[str, Any], str], None]
TranscodeCallback = Callable[..., Mapping[str, Any]]
PublishDerivedCallback = Callable[[Path, Mapping[str, Any]], Mapping[str, Any]]


class MediaLibraryAgentWorker:
    """One bounded scanner per agent process with durable queue ownership."""

    def __init__(
        self,
        repository: MediaLibraryAgentRepository,
        *,
        register: RegisterCallback | None = None,
        publish: PublishCallback | None = None,
        transcode: TranscodeCallback | None = None,
        publish_derived: PublishDerivedCallback | None = None,
        poll_seconds: float = 1.0,
    ):
        self.repository = repository
        self._register = register or register_media_reference
        self._publish_callback = publish
        self._transcode = transcode or transcode_with_ffmpeg
        self._publish_derived = publish_derived or publish_derived_resource
        self._poll_seconds = max(0.05, min(10.0, float(poll_seconds)))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._resource_pressure = "normal"
        self._last_publish_monotonic = 0.0
        self._job_started: dict[str, float] = {}
        self._wait_reason = ""
        self._watch_state: dict[str, tuple[str, float, bool]] = {}
        self._watch_pending: dict[str, tuple[float, bool]] = {}

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
        self._poll_watch_schedules()
        self._cleanup_invalidated_renditions()
        rendition = self.repository.next_queued_rendition_job()
        if rendition is not None:
            claimed_rendition = self.repository.claim_rendition_job(rendition["id"])
            if claimed_rendition is not None:
                return self._run_rendition_job(claimed_rendition)
        queued = self.repository.next_queued_job()
        if queued is None:
            return None
        claimed = self.repository.claim_job(queued["id"])
        if claimed is None:
            return None
        return self._run_job(claimed)

    def _run_rendition_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        job_id = text(job.get("id"))
        source = self.repository.get_source(text(job.get("source_id")))
        if source is None or not source.get("present"):
            return self._finish_rendition_failed(job_id, "source_not_found")
        if (
            int(source.get("revision") or 0) != int(job.get("source_revision") or 0)
            or text(source.get("fingerprint")) != text(job.get("source_fingerprint"))
        ):
            return self._finish_rendition_failed(
                job_id, "source_changed", status="invalidated"
            )
        try:
            source_path = self._contained_source_path(source)
        except (OSError, ValueError) as exc:
            return self._finish_rendition_failed(job_id, "source_path_invalid", str(exc))
        target_path = output_path(self.repository.db_path, job)
        limits = rendition_limits()
        estimated = min(
            int(limits["max_output_bytes"]), max(1, int(source.get("size_bytes") or 0))
        )
        if current_disk_usage(self.repository.db_path) + estimated > int(
            limits["disk_quota_bytes"]
        ):
            return self._finish_rendition_failed(job_id, "rendition_disk_quota_exceeded")
        self._publish_rendition(job_id)
        try:
            self._wait_for_rendition_resources(job_id)
            if self._rendition_cancelled(job_id):
                return self._finish_rendition_failed(
                    job_id, "rendition_canceled", status="canceled"
                )
            result = self._transcode(
                source_path,
                target_path,
                job,
                cancelled=lambda: self._stop.is_set()
                or self._rendition_cancelled(job_id),
            )
            current = self.repository.get_source(text(job.get("source_id")))
            if (
                current is None
                or int(current.get("revision") or 0)
                != int(job.get("source_revision") or 0)
                or text(current.get("fingerprint"))
                != text(job.get("source_fingerprint"))
            ):
                return self._finish_rendition_failed(
                    job_id, "source_changed", status="invalidated"
                )
            descriptor = dict(self._publish_derived(target_path, job))
            descriptor_metadata = dict(descriptor.get("metadata") or {})
            descriptor_metadata.update(
                {
                    "storage_mode": "derived_copy",
                    "derived_from_source_id": text(job.get("source_id")),
                    "derived_from_source_revision": int(
                        job.get("source_revision") or 0
                    ),
                    "derived_from_source_fingerprint": text(
                        job.get("source_fingerprint")
                    ),
                    "rendition_profile": text(job.get("profile")),
                }
            )
            descriptor["metadata"] = descriptor_metadata
            completed = self.repository.complete_rendition_job(
                job_id,
                descriptor=descriptor,
                output_bytes=int(
                    result.get("size_bytes")
                    or descriptor.get("size_bytes")
                    or target_path.stat().st_size
                ),
            )
            if not completed.get("advertised"):
                self._cleanup_published_descriptor(descriptor)
            self._publish_rendition(job_id)
            return dict(
                completed.get("job")
                or self.repository.get_rendition_job(job_id)
                or completed
            )
        except Exception as exc:
            code, _, detail = text(exc).partition(":")
            return self._finish_rendition_failed(
                job_id, code or "rendition_failed", detail
            )
        finally:
            target_path.unlink(missing_ok=True)
            target_path.with_suffix(target_path.suffix + ".partial").unlink(
                missing_ok=True
            )

    def _wait_for_rendition_resources(self, job_id: str) -> None:
        waiting = False
        while self._resource_pressure in {"playback", "critical"} and not self._stop.is_set():
            if self._rendition_cancelled(job_id):
                return
            if not waiting:
                self.repository.update_rendition_job(
                    job_id, status="waiting_resources"
                )
                self._publish_rendition(job_id)
                waiting = True
            self._wake.wait(0.5)
            self._wake.clear()
        if waiting:
            self.repository.update_rendition_job(job_id, status="running")

    def _rendition_cancelled(self, job_id: str) -> bool:
        job = self.repository.get_rendition_job(job_id)
        return bool(job and job.get("cancel_requested"))

    def _finish_rendition_failed(
        self,
        job_id: str,
        code: str,
        detail: str = "",
        *,
        status: str = "failed",
    ) -> dict[str, Any]:
        result = self.repository.update_rendition_job(
            job_id,
            status=status,
            finished_at=now_iso(),
            error_code=text(code),
            error_detail=text(detail)[:2000],
        )
        self._publish_rendition(job_id)
        return result or {}

    def _publish_rendition(self, job_id: str) -> None:
        if not self._publish_callback:
            return
        job = self.repository.get_rendition_job(job_id)
        if job is None:
            return
        try:
            self._publish_callback(
                {
                    **job,
                    "schema": RENDITION_JOB_SCHEMA,
                    "job_type": "rendition",
                    "agent_id": self.repository.agent_id,
                    "node_id": self.repository.node_id,
                    "updated_at": now_iso(),
                },
                "",
            )
        except Exception:
            return

    def _cleanup_invalidated_renditions(self) -> None:
        for job in self.repository.invalidated_rendition_outputs(limit=10):
            self._cleanup_published_descriptor(job.get("output") or {})
            self.repository.update_rendition_job(
                job["id"], cleaned_at=now_iso()
            )

    @staticmethod
    def _cleanup_published_descriptor(descriptor: Mapping[str, Any]) -> None:
        metadata = dict(descriptor.get("metadata") or {})
        filename = text(descriptor.get("filename") or descriptor.get("resource_id"))
        if (
            text(metadata.get("namespace")) != "media-library-rendition"
            and not filename.startswith("media-library-rendition-")
        ):
            return
        path = Path(text(descriptor.get("path")))
        if not path.name or path.name != filename:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    def _contained_source_path(self, source: Mapping[str, Any]) -> Path:
        descriptor = dict(source.get("descriptor") or {})
        path = Path(
            text(descriptor.get("source_path") or descriptor.get("path"))
        ).resolve(strict=True)
        root = self.repository.get_root(text(source.get("root_id")))
        if root is None:
            raise ValueError("root_not_found")
        root_path = Path(text(root.get("path"))).resolve(strict=True)
        try:
            path.relative_to(root_path)
        except ValueError as exc:
            raise ValueError("source_outside_root") from exc
        if not path.is_file():
            raise ValueError("source_not_file")
        return path

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

    def _poll_watch_schedules(self, *, force: bool = False) -> None:
        now = time.monotonic()
        active_ids = set()
        for schedule in self.repository.watch_schedules():
            root_id = text(schedule.get("root_id"))
            active_ids.add(root_id)
            previous = self._watch_state.get(root_id)
            poll_seconds = max(5, int(schedule.get("watch_poll_seconds") or 30))
            if not force and previous and now - previous[1] < poll_seconds:
                self._enqueue_debounced_watch(schedule, now)
                continue
            witness, overflow = self._filesystem_witness(schedule)
            self._watch_state[root_id] = (witness, now, overflow)
            if previous is None:
                if overflow:
                    self._watch_pending[root_id] = (now, True)
            elif previous[0] != witness or overflow != previous[2]:
                self._watch_pending[root_id] = (now, overflow)
            self._enqueue_debounced_watch(schedule, now)
        for root_id in set(self._watch_state).difference(active_ids):
            self._watch_state.pop(root_id, None)
            self._watch_pending.pop(root_id, None)

    def _enqueue_debounced_watch(
        self, schedule: Mapping[str, Any], now: float
    ) -> None:
        root_id = text(schedule.get("root_id"))
        pending = self._watch_pending.get(root_id)
        if pending is None:
            return
        detected_at, overflow = pending
        debounce = max(1, int(schedule.get("debounce_seconds") or 30))
        if now - detected_at < debounce:
            return
        self.repository.create_job(
            root_id,
            mode="reconcile" if overflow else "incremental",
        )
        self._watch_pending.pop(root_id, None)
        self._wake.set()

    @staticmethod
    def _filesystem_witness(schedule: Mapping[str, Any]) -> tuple[str, bool]:
        root_path = Path(text(schedule.get("path")))
        exclusions = [
            text(item) for item in schedule.get("exclusions") or [] if text(item)
        ]
        follow_symlinks = bool(schedule.get("follow_symlinks"))
        try:
            maximum = max(
                100,
                min(
                    500_000,
                    int(os.environ.get("MEDIA_LIBRARY_AGENT_WATCH_MAX_ENTRIES") or 50_000),
                ),
            )
        except ValueError:
            maximum = 50_000
        digest = hashlib.sha256()
        count = 0
        overflow = False
        stack = [root_path]
        visited: set[tuple[int, int]] = set()
        while stack and not overflow:
            directory = stack.pop()
            try:
                directory_stat = directory.stat(follow_symlinks=follow_symlinks)
                identity = (int(directory_stat.st_dev), int(directory_stat.st_ino))
                if identity in visited:
                    continue
                visited.add(identity)
                entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except (OSError, PermissionError):
                digest.update(f"unavailable:{directory}".encode("utf-8", errors="replace"))
                continue
            for entry in entries:
                path = Path(entry.path)
                try:
                    relative = path.relative_to(root_path).as_posix()
                except ValueError:
                    continue
                if MediaLibraryAgentWorker._excluded(relative, exclusions):
                    continue
                try:
                    stat = entry.stat(follow_symlinks=follow_symlinks)
                    is_directory = entry.is_dir(follow_symlinks=follow_symlinks)
                except (OSError, PermissionError):
                    continue
                digest.update(
                    f"{relative}\0{int(is_directory)}\0{int(stat.st_size)}\0{int(stat.st_mtime_ns)}\n".encode(
                        "utf-8", errors="replace"
                    )
                )
                count += 1
                if count >= maximum:
                    overflow = True
                    break
                if is_directory and (follow_symlinks or not entry.is_symlink()):
                    stack.append(path)
        digest.update(f"count:{count}:overflow:{int(overflow)}".encode("ascii"))
        return digest.hexdigest(), overflow

    def watch_status(self) -> dict[str, Any]:
        schedules = self.repository.watch_schedules()
        return {
            "enabled_root_count": len(schedules),
            "observed_root_count": len(self._watch_state),
            "pending_root_ids": sorted(self._watch_pending),
            "mode": "bounded_polling_with_periodic_reconcile",
        }

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
        perceptual_hash = MediaLibraryAgentWorker._perceptual_hash(
            path, mime_type=mime_type
        )
        if perceptual_hash:
            result.update(
                {
                    "perceptual_hash": perceptual_hash,
                    "perceptual_hash_algorithm": "ffmpeg_sample_sha256_v1",
                }
            )
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

    @staticmethod
    def _perceptual_hash(path: Path, *, mime_type: str) -> str:
        mode = text(
            os.environ.get("MEDIA_LIBRARY_AGENT_PERCEPTUAL_HASH_MODE") or "off"
        ).lower()
        executable = shutil.which("ffmpeg") if mode == "ffmpeg" else None
        if not executable:
            return ""
        if mime_type.startswith("video/"):
            transform = [
                "-an",
                "-vf",
                "fps=1/30,scale=16:16,format=gray",
                "-frames:v",
                "8",
                "-f",
                "rawvideo",
            ]
        elif mime_type.startswith("audio/"):
            transform = [
                "-vn",
                "-t",
                "30",
                "-ac",
                "1",
                "-ar",
                "2000",
                "-f",
                "s16le",
            ]
        else:
            return ""
        try:
            timeout = max(
                2.0,
                min(
                    30.0,
                    float(
                        os.environ.get(
                            "MEDIA_LIBRARY_AGENT_PERCEPTUAL_HASH_TIMEOUT_SECONDS"
                        )
                        or 10
                    ),
                ),
            )
            completed = subprocess.run(
                [
                    executable,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-threads",
                    "1",
                    "-i",
                    str(path),
                    *transform,
                    "pipe:1",
                ],
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""
        if (
            completed.returncode != 0
            or not completed.stdout
            or len(completed.stdout) > 512 * 1024
        ):
            return ""
        return hashlib.sha256(completed.stdout).hexdigest()


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
