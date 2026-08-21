from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core import runtime_identity
from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.i18n import _


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from media_library_agent.contracts import (  # noqa: E402
    PROGRESS_SCHEMA,
    RENDITION_JOB_SCHEMA,
    SCHEMA_VERSION,
    compact_error,
    now_iso,
    text,
)
from media_library_agent.rendition import (  # noqa: E402
    ARTWORK_PROFILE,
    artwork_plan,
    rendition_limits,
    rendition_plan,
)
from media_library_agent.repository import MediaLibraryAgentRepository, default_db_path  # noqa: E402
from media_library_agent.worker import MediaLibraryAgentWorker  # noqa: E402
from media_library_agent.topology import LibraryAgentTopology  # noqa: E402


_runtime_lock = threading.Lock()
_runtime_path = ""
_runtime_node_id = ""
_repository_instance: MediaLibraryAgentRepository | None = None
_worker_instance: MediaLibraryAgentWorker | None = None
_progress_condition = threading.Condition()
_progress_pending: dict[str, tuple[dict[str, Any], str]] = {}
_progress_thread: threading.Thread | None = None
_progress_stopping = False
_progress_inflight_since = 0.0
_progress_delivered_count = 0
_progress_failure_count = 0
_progress_last_error = ""
_MAX_PENDING_PROGRESS = 64


def _human_error(code: str, fallback: str, **extra: Any) -> dict[str, Any]:
    key = f"runtime.media_library_agent.error.{code}"
    try:
        translated = text(_(key))
    except Exception:
        translated = ""
    message = translated if translated and translated != key else fallback
    return compact_error(
        code,
        detail=text(extra.pop("detail", "")),
        human_message=message,
        human_message_i18n={"key": key},
        **extra,
    )


def _publish_progress(payload: Mapping[str, Any], webspace_id: str) -> None:
    global _progress_stopping, _progress_thread
    snapshot = dict(payload)
    job_type = "rendition" if text(snapshot.get("job_type")) == "rendition" else "scan"
    key = f"{job_type}:{text(snapshot.get('job_id')) or 'snapshot'}"
    with _progress_condition:
        _progress_stopping = False
        _progress_pending.pop(key, None)
        _progress_pending[key] = (snapshot, text(webspace_id))
        while len(_progress_pending) > _MAX_PENDING_PROGRESS:
            _progress_pending.pop(next(iter(_progress_pending)))
        if _progress_thread is None or not _progress_thread.is_alive():
            _progress_thread = threading.Thread(
                target=_progress_publisher_loop,
                name="media-library-progress-publisher",
                daemon=True,
            )
            _progress_thread.start()
        _progress_condition.notify()


def _progress_publisher_loop() -> None:
    global _progress_delivered_count, _progress_failure_count
    global _progress_inflight_since, _progress_last_error
    while True:
        with _progress_condition:
            while not _progress_pending and not _progress_stopping:
                _progress_condition.wait(1.0)
            if _progress_stopping and not _progress_pending:
                return
            key = next(iter(_progress_pending))
            payload, webspace_id = _progress_pending.pop(key)
            _progress_inflight_since = time.monotonic()
        try:
            _deliver_progress(payload, webspace_id)
        except Exception as exc:
            with _progress_condition:
                _progress_failure_count += 1
                _progress_last_error = f"{type(exc).__name__}: {exc}"[:300]
        else:
            with _progress_condition:
                _progress_delivered_count += 1
                _progress_last_error = ""
        finally:
            with _progress_condition:
                _progress_inflight_since = 0.0


def _deliver_progress(payload: Mapping[str, Any], webspace_id: str) -> None:
    from adaos.sdk.io import stream_variable_publish

    meta = {"webspace_id": webspace_id} if webspace_id else None
    is_rendition = text(payload.get("job_type")) == "rendition"
    stream_variable_publish(
        (
            "media_library_agent.rendition_progress"
            if is_rendition
            else "media_library_agent.progress"
        ),
        dict(payload),
        var_id=(
            "media_library_agent.current_rendition"
            if is_rendition
            else "media_library_agent.current_scan"
        ),
        seq=(
            int(payload.get("revision") or 0)
            if is_rendition
            else int((payload.get("progress") or {}).get("processed_count") or 0)
        ),
        ttl_ms=120000,
        _meta=meta,
    )
    if text(payload.get("status")) in {"completed", "canceled", "failed"}:
        from adaos.sdk.data.events import publish as publish_event

        publish_event(
            "media_library_agent.catalog.changed",
            {
                "schema": "adaos.media_library.catalog_changed.v1",
                "agent_id": text(payload.get("agent_id")),
                "node_id": text(payload.get("node_id")),
                "root_id": text(payload.get("root_id")),
                "job_id": text(payload.get("job_id")),
                "status": text(payload.get("status")),
            },
            source="media_library_agent",
        )


def _progress_publisher_status() -> dict[str, Any]:
    with _progress_condition:
        inflight_seconds = (
            max(0.0, time.monotonic() - _progress_inflight_since)
            if _progress_inflight_since
            else 0.0
        )
        return {
            "mode": "bounded_coalescing",
            "max_pending": _MAX_PENDING_PROGRESS,
            "pending_count": len(_progress_pending),
            "inflight": bool(_progress_inflight_since),
            "inflight_seconds": round(inflight_seconds, 3),
            "delivered_count": _progress_delivered_count,
            "failure_count": _progress_failure_count,
            "last_error": _progress_last_error,
        }


def _stop_progress_publisher(*, timeout: float = 0.2) -> None:
    global _progress_stopping
    with _progress_condition:
        _progress_stopping = True
        _progress_pending.clear()
        thread = _progress_thread
        _progress_condition.notify_all()
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=max(0.0, timeout))


def _event_payload(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        nested = event.get("payload")
        return dict(nested) if isinstance(nested, Mapping) else dict(event)
    nested = getattr(event, "payload", None)
    return dict(nested) if isinstance(nested, Mapping) else {}


def _runtime() -> tuple[MediaLibraryAgentRepository, MediaLibraryAgentWorker]:
    global _repository_instance, _runtime_node_id, _runtime_path, _worker_instance
    path = str(default_db_path().resolve())
    node_id = _node_identity()
    with _runtime_lock:
        if (
            _repository_instance is None
            or _worker_instance is None
            or _runtime_path != path
            or _runtime_node_id != node_id
        ):
            if _worker_instance is not None:
                _worker_instance.dispose(timeout=0.2)
            _repository_instance = MediaLibraryAgentRepository(path, node_id=node_id)
            _worker_instance = MediaLibraryAgentWorker(
                _repository_instance, publish=_publish_progress
            )
            _runtime_path = path
            _runtime_node_id = node_id
        return _repository_instance, _worker_instance


def _node_identity() -> str:
    try:
        identity = runtime_identity()
        node = identity.get("node") if isinstance(identity, Mapping) else None
        node_id = text(node.get("node_id")) if isinstance(node, Mapping) else ""
        if node_id:
            return node_id
    except Exception:
        pass
    return (
        text(os.environ.get("ADAOS_NODE_ID") or os.environ.get("ADAOS_HUB_ID"))
        or "local"
    )


def _owns_background_worker() -> bool:
    service_skill = text(os.environ.get("ADAOS_SERVICE_SKILL"))
    if service_skill:
        return service_skill == "media_library_agent"
    embedded = text(os.environ.get("MEDIA_LIBRARY_AGENT_EMBEDDED_WORKER")).lower()
    if embedded in {"1", "true", "yes", "on"}:
        return True
    return not bool(text(os.environ.get("ADAOS_RUNTIME_PORT")))


def _ensure_worker_if_owned(worker: MediaLibraryAgentWorker) -> bool:
    return worker.ensure_started() if _owns_background_worker() else False


def _topology() -> LibraryAgentTopology:
    return LibraryAgentTopology()


@tool(
    summary="Ensure the durable media-library agent schema.", side_effects="local_write"
)
def ensure_schema(**_: Any) -> dict[str, Any]:
    repository, _worker = _runtime()
    return repository.ensure_schema()


@tool(
    summary="Resume queued media-library work after activation.",
    side_effects="local_write",
)
def rehydrate(**_: Any) -> dict[str, Any]:
    if not _owns_background_worker():
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "deferred": True,
            "deferred_to": "service_process",
            "worker": {"running": False, "owner": "service_process"},
        }
    repository, worker = _runtime()
    _ensure_worker_if_owned(worker)
    return {
        **repository.summary(),
        "worker": {
            "running": worker.running if _owns_background_worker() else False,
            "owner": (
                "service_process"
                if not _owns_background_worker()
                else "current_process"
            ),
            "resource_pressure": worker.resource_pressure,
        },
    }


def recover_interrupted_runtime() -> dict[str, Any]:
    repository, worker = _runtime()
    if not _owns_background_worker():
        return {
            **repository.summary(),
            "requeued_job_count": 0,
            "worker": {"running": False, "owner": "service_process"},
        }
    requeued = repository.requeue_interrupted_jobs()
    worker.ensure_started()
    return {
        **repository.summary(),
        "requeued_job_count": requeued,
        "worker": {"running": True, "owner": "current_process"},
    }


@subscribe("sys.ready")
def on_sys_ready(_: Any) -> None:
    recover_interrupted_runtime()


@subscribe(
    "webio.stream.snapshot.requested",
    receivers=(
        "media_library_agent.progress",
        "media_library_agent.rendition_progress",
    ),
)
def on_progress_snapshot_requested(event: Any) -> None:
    payload = _event_payload(event)
    receiver = text(payload.get("receiver"))
    if receiver not in {
        "media_library_agent.progress",
        "media_library_agent.rendition_progress",
    }:
        return
    repository, worker = _runtime()
    if receiver == "media_library_agent.rendition_progress":
        jobs = repository.list_rendition_jobs(limit=1).get("items") or []
        job = (
            dict(jobs[0])
            if jobs
            else {
                "schema": RENDITION_JOB_SCHEMA,
                "id": "",
                "status": "idle",
                "revision": 0,
            }
        )
        _publish_progress(
            {
                **job,
                "job_type": "rendition",
                "agent_id": repository.agent_id,
                "node_id": repository.node_id,
                "updated_at": now_iso(),
            },
            text(payload.get("webspace_id")),
        )
        return
    jobs = repository.list_jobs(limit=1).get("items") or []
    job = dict(jobs[0]) if jobs else {}
    progress = dict(job.get("progress") or {})
    progress.update(
        {
            "elapsed_seconds": 0,
            "throughput_bytes_per_second": 0,
            "phase": "idle" if not job else "enumerating",
            "checkpoint_age_seconds": 0,
        }
    )
    snapshot = {
        "schema": PROGRESS_SCHEMA,
        "job_id": text(job.get("id")),
        "agent_id": repository.agent_id,
        "node_id": repository.node_id,
        "root_id": text(job.get("root_id")),
        "root_label": "",
        "status": text(job.get("status")) or "idle",
        "progress": progress,
        "current_path": text((job.get("progress") or {}).get("current_path"))[-500:],
        "resource_pressure": worker.resource_pressure,
        "wait_reason": "",
        "updated_at": now_iso(),
    }
    _publish_progress(snapshot, text(payload.get("webspace_id")))


@tool(
    summary="Add a local media root without copying its files.",
    side_effects="local_write",
)
def add_root(
    path: str = "",
    label: str = "",
    include_images: bool = False,
    follow_symlinks: bool = False,
    exclusions: list[str] | None = None,
    scan_window: Mapping[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    repository, _worker = _runtime()
    result = repository.add_root(
        path,
        label=label,
        include_images=bool(include_images),
        follow_symlinks=bool(follow_symlinks),
        exclusions=exclusions or (),
        scan_window=scan_window,
    )
    if result.get("ok"):
        return result
    code = text(result.get("error"))
    fallbacks = {
        "root_path_required": "Enter a media folder path.",
        "root_path_not_found": "The media folder does not exist on this node.",
        "root_path_not_directory": "The selected path is not a folder.",
        "root_exclusion_invalid": "One of the exclusion patterns is invalid.",
        "root_path_overlap": "This folder overlaps another active media folder on this node.",
        "root_scan_window_invalid": "The scan window must contain valid local start/end times and weekdays.",
    }
    return {
        **result,
        **_human_error(
            code, fallbacks.get(code, "The media folder could not be added.")
        ),
    }


@tool(
    summary="Disable a media root while retaining external files and indexed evidence.",
    side_effects="local_write",
)
def remove_root(root_id: str = "", **_: Any) -> dict[str, Any]:
    repository, _worker = _runtime()
    result = repository.disable_root(root_id)
    if result.get("ok"):
        return result
    return {
        **result,
        **_human_error("root_not_found", "The media folder is no longer configured."),
    }


@tool(summary="List media roots owned by this node agent.", side_effects="none")
def list_roots(include_disabled: bool = False, **_: Any) -> dict[str, Any]:
    repository, _worker = _runtime()
    return repository.list_roots(include_disabled=bool(include_disabled))


@tool(
    summary="Queue bounded incremental scans and return immediately.",
    side_effects="local_write",
)
def start_scan(
    root_id: str = "",
    mode: str = "incremental",
    webspace_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    repository, worker = _runtime()
    root_ids = (
        [text(root_id)]
        if text(root_id)
        else [item["id"] for item in repository.list_roots()["items"]]
    )
    if not root_ids:
        return _human_error(
            "no_active_media_roots", "No active media folders are configured.", roots=[]
        )
    jobs: list[dict[str, Any]] = []
    accepted = 0
    for candidate in root_ids:
        result = repository.create_job(candidate, mode=mode, webspace_id=webspace_id)
        if not result.get("ok"):
            return {
                **result,
                **_human_error(
                    text(result.get("error")), "The media scan could not be queued."
                ),
            }
        jobs.append(dict(result["job"]))
        accepted += int(bool(result.get("accepted")))
    _ensure_worker_if_owned(worker)
    return {
        "ok": True,
        "schema": SCHEMA_VERSION,
        "accepted": bool(accepted),
        "accepted_count": accepted,
        "jobs": jobs,
        "job": jobs[0] if len(jobs) == 1 else None,
        "asynchronous": True,
    }


@tool(
    summary="Add a media root and queue its first asynchronous scan.",
    side_effects="local_write",
)
def import_folder(
    path: str = "",
    label: str = "",
    include_images: bool = False,
    follow_symlinks: bool = False,
    exclusions: list[str] | None = None,
    scan_window: Mapping[str, Any] | None = None,
    webspace_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    added = add_root(
        path=path,
        label=label,
        include_images=include_images,
        follow_symlinks=follow_symlinks,
        exclusions=exclusions,
        scan_window=scan_window,
    )
    if not added.get("ok"):
        return added
    queued = start_scan(
        root_id=added["root"]["id"], mode="full", webspace_id=webspace_id
    )
    return {
        **queued,
        "root": added["root"],
        "roots": added["roots"],
        "storage": {"mode": "external_reference", "media_bytes_copied": False},
    }


@tool(summary="Read one durable scan job and compact progress.", side_effects="none")
def scan_status(job_id: str = "", **_: Any) -> dict[str, Any]:
    repository, worker = _runtime()
    job = repository.get_job(job_id)
    if job is None:
        return _human_error(
            "scan_job_not_found",
            "The media scan is no longer available.",
            job_id=text(job_id),
        )
    return {
        "ok": True,
        "schema": SCHEMA_VERSION,
        "job": job,
        "resource_pressure": worker.resource_pressure,
    }


@tool(summary="List recent bounded scan jobs.", side_effects="none")
def list_jobs(root_id: str = "", limit: int = 20, **_: Any) -> dict[str, Any]:
    repository, _worker = _runtime()
    return repository.list_jobs(root_id=root_id, limit=limit)


@tool(
    summary="Request cooperative cancellation of one scan.", side_effects="local_write"
)
def cancel_scan(job_id: str = "", **_: Any) -> dict[str, Any]:
    repository, worker = _runtime()
    result = repository.request_cancel(job_id)
    _ensure_worker_if_owned(worker)
    if result.get("ok"):
        return result
    return {
        **result,
        **_human_error("scan_job_not_found", "The media scan is no longer available."),
    }


@tool(
    summary="Pull ordered idempotent source deltas through an opaque cursor.",
    side_effects="none",
)
def pull_deltas(
    cursor: str = "", limit: int = 250, root_id: str = "", **_: Any
) -> dict[str, Any]:
    repository, _worker = _runtime()
    try:
        return repository.pull_deltas(cursor=cursor, limit=limit, root_id=root_id)
    except ValueError:
        return _human_error(
            "invalid_cursor", "The catalog cursor has expired or is invalid."
        )


@tool(
    summary="Browse indexed folders as an alternative library navigation.",
    side_effects="none",
)
def browse_folders(
    root_id: str = "",
    parent: str = "",
    limit: int = 100,
    cursor: str = "",
    **_: Any,
) -> dict[str, Any]:
    repository, _worker = _runtime()
    try:
        return repository.browse_folders(
            root_id=root_id, parent=parent, limit=limit, cursor=cursor
        )
    except ValueError:
        return _human_error(
            "invalid_cursor", "The folder cursor has expired or is invalid."
        )


@tool(
    summary="Configure periodic reconciliation for one media root.",
    side_effects="local_write",
)
def configure_schedule(
    root_id: str = "",
    enabled: bool = True,
    interval_seconds: int = 21600,
    debounce_seconds: int = 30,
    watch_enabled: bool = False,
    watch_poll_seconds: int = 30,
    **_: Any,
) -> dict[str, Any]:
    repository, worker = _runtime()
    result = repository.configure_schedule(
        root_id,
        enabled=bool(enabled),
        interval_seconds=interval_seconds,
        debounce_seconds=debounce_seconds,
        watch_enabled=bool(watch_enabled),
        watch_poll_seconds=watch_poll_seconds,
    )
    _ensure_worker_if_owned(worker)
    return result


@tool(
    summary="Pause or resume background scans according to playback pressure.",
    side_effects="local_write",
)
def set_resource_pressure(level: str = "normal", **_: Any) -> dict[str, Any]:
    repository, worker = _runtime()
    try:
        current = repository.set_resource_pressure(level)
        if _owns_background_worker():
            worker.set_resource_pressure(current)
    except ValueError:
        return _human_error(
            "invalid_resource_pressure", "The resource pressure level is invalid."
        )
    return {"ok": True, "schema": SCHEMA_VERSION, "resource_pressure": current}


@tool(
    summary="Inspect one indexed source without reading media bytes.",
    side_effects="none",
)
def inspect_source(source_id: str = "", **_: Any) -> dict[str, Any]:
    repository, _worker = _runtime()
    source = repository.get_source(source_id)
    if source is None:
        return _human_error(
            "source_not_found",
            "The media source is no longer available.",
            source_id=text(source_id),
        )
    return {"ok": True, "schema": SCHEMA_VERSION, "source": source}


@tool(
    summary="Search node-local filenames, folders, tags, and technical metadata.",
    side_effects="none",
)
def search_sources(
    query: str = "", limit: int = 30, cursor: str = "", **_: Any
) -> dict[str, Any]:
    repository, _worker = _runtime()
    try:
        return repository.search_sources(query=query, limit=limit, cursor=cursor)
    except ValueError:
        return _human_error(
            "invalid_cursor", "The catalog cursor has expired or is invalid."
        )


@tool(
    summary="Resolve and queue one bounded source artwork rendition.",
    side_effects="local_write",
)
def plan_artwork(
    source_id: str = "",
    priority: int = 350,
    force: bool = False,
    **_: Any,
) -> dict[str, Any]:
    repository, worker = _runtime()
    source = repository.get_source(source_id)
    if source is None:
        return _human_error(
            "source_not_found",
            "The media source is no longer available.",
            source_id=text(source_id),
        )
    plan = artwork_plan(source, force=bool(force))
    if not plan["required"]:
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "asynchronous": False,
            "plan": plan,
            "job": None,
            "artwork": plan.get("artwork"),
        }
    queued = repository.create_rendition_job(
        source_id,
        profile=ARTWORK_PROFILE,
        target=plan["target"],
        priority=priority,
        force=bool(force),
    )
    if not queued.get("ok"):
        return _human_error(
            text(queued.get("error")) or "artwork_queue_failed",
            "Artwork extraction could not be queued.",
            source_id=text(source_id),
        )
    _ensure_worker_if_owned(worker)
    return {
        "ok": True,
        "schema": SCHEMA_VERSION,
        "asynchronous": True,
        "plan": plan,
        "job": queued.get("job"),
        "created": bool(queued.get("created")),
    }


@tool(
    summary="Plan and optionally queue one browser-compatible derived rendition.",
    side_effects="local_write",
)
def plan_rendition(
    source_id: str = "",
    endpoint_capabilities: Mapping[str, Any] | None = None,
    profile: str = "browser-mp4-v1",
    priority: int = 50,
    force: bool = False,
    **_: Any,
) -> dict[str, Any]:
    repository, worker = _runtime()
    source = repository.get_source(source_id)
    if source is None:
        return _human_error(
            "source_not_found",
            "The media source is no longer available.",
            source_id=text(source_id),
        )
    plan = rendition_plan(
        source,
        endpoint_capabilities=endpoint_capabilities,
        profile=profile,
    )
    if not plan["required"]:
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "asynchronous": False,
            "plan": plan,
            "job": None,
        }
    queued = repository.create_rendition_job(
        source_id,
        profile=text(profile) or "browser-mp4-v1",
        target=plan["target"],
        priority=priority,
        force=bool(force),
    )
    if not queued.get("ok"):
        return _human_error(
            text(queued.get("error")) or "rendition_queue_failed",
            "The compatible media rendition could not be queued.",
            source_id=text(source_id),
        )
    _ensure_worker_if_owned(worker)
    return {
        "ok": True,
        "schema": SCHEMA_VERSION,
        "asynchronous": True,
        "plan": plan,
        "job": queued.get("job"),
        "created": bool(queued.get("created")),
    }


@tool(summary="Inspect one durable rendition job.", side_effects="none")
def rendition_status(job_id: str = "", **_: Any) -> dict[str, Any]:
    repository, _worker = _runtime()
    job = repository.get_rendition_job(job_id)
    if job is None:
        return _human_error(
            "rendition_job_not_found",
            "The rendition job is no longer available.",
            job_id=text(job_id),
        )
    return {"ok": True, "schema": SCHEMA_VERSION, "job": job}


@tool(summary="List recent bounded rendition jobs.", side_effects="none")
def list_rendition_jobs(
    source_id: str = "", limit: int = 20, **_: Any
) -> dict[str, Any]:
    repository, _worker = _runtime()
    return repository.list_rendition_jobs(source_id=source_id, limit=limit)


@tool(summary="Cooperatively cancel one rendition job.", side_effects="local_write")
def cancel_rendition(job_id: str = "", **_: Any) -> dict[str, Any]:
    repository, worker = _runtime()
    result = repository.request_rendition_cancel(job_id)
    _ensure_worker_if_owned(worker)
    if result.get("ok"):
        return result
    return _human_error(
        text(result.get("error")) or "rendition_cancel_failed",
        "The rendition job could not be canceled.",
        job_id=text(job_id),
    )


@tool(summary="Return compact agent health and capacity state.", side_effects="none")
def status(**_: Any) -> dict[str, Any]:
    repository, worker = _runtime()
    summary = repository.summary()
    pressure = worker.resource_pressure
    active_jobs = int(summary.get("active_job_count") or 0)
    failed_jobs = int(summary.get("failed_job_count") or 0)
    return {
        **summary,
        "distributed": {
            "health": {
                "status": "passing",
                "ready": True,
                "active_jobs": active_jobs,
                "failed_jobs": failed_jobs,
            },
            "pressure": {
                "state": pressure,
                "active_jobs": active_jobs,
            },
        },
        "worker": {
            "owner": (
                "current_process" if _owns_background_worker() else "service_process"
            ),
            "resource_pressure": pressure,
            "max_concurrent_scans": 1,
            "watch": worker.watch_status(),
            "progress_publisher": _progress_publisher_status(),
        },
        "limits": {
            "max_files_per_scan": int(
                os.environ.get("MEDIA_LIBRARY_AGENT_MAX_FILES") or 1_000_000
            ),
            "max_delta_page": 1000,
            "progress_publish_hz": 2,
            "watch_max_entries": int(
                os.environ.get("MEDIA_LIBRARY_AGENT_WATCH_MAX_ENTRIES") or 50_000
            ),
            "rendition": rendition_limits(),
        },
    }


@tool(
    summary="Validate and report one node-local partition replica.", side_effects="none"
)
def observe_topology(
    partition: Mapping[str, Any] | None = None,
    replica: Mapping[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    try:
        repository, _worker = _runtime()
        return _topology().observe(repository, partition or {}, replica or {})
    except Exception as exc:
        return _human_error(
            "topology_observe_failed",
            "This library agent could not report its shard state.",
            detail=str(exc)[:300],
        )


@tool(
    summary="Execute one reviewed distributed topology phase for an agent shard.",
    side_effects="local_write",
)
def distributed_topology_phase(**payload: Any) -> dict[str, Any]:
    repository, worker = _runtime()
    try:
        return _topology().execute_phase(
            repository,
            payload,
            resource_pressure=worker.resource_pressure,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "media_agent_topology_phase_failed",
            "detail": str(exc)[:300],
        }


@tool(
    summary="Transfer one bounded reviewed topology snapshot chunk.",
    side_effects="local_write",
)
def distributed_topology_transfer(**payload: Any) -> dict[str, Any]:
    repository, _worker = _runtime()
    try:
        return _topology().execute_transfer(repository, payload)
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "media_agent_topology_transfer_failed",
            "detail": str(exc)[:300],
        }


@tool(summary="Stop process-local media-library workers.", side_effects="local_write")
def dispose(**_: Any) -> dict[str, Any]:
    owner = "current_process" if _owns_background_worker() else "service_process"
    if owner == "current_process":
        _repository, worker = _runtime()
        worker.dispose()
    _stop_progress_publisher()
    return {
        "ok": True,
        "schema": SCHEMA_VERSION,
        "disposed": owner == "current_process",
        "owner": owner,
        "deferred": owner == "service_process",
    }
