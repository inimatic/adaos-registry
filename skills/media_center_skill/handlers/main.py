from __future__ import annotations

import mimetypes
import re
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.i18n import _


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from media_center.catalog import MediaCenterRepository, SCHEMA_VERSION  # noqa: E402
from media_center.coordinator import COORDINATOR_SCHEMA, MediaCatalogCoordinator  # noqa: E402
from media_center.enrichment import MediaEnrichmentWorker  # noqa: E402
from media_center.topology import MediaCenterTopology  # noqa: E402


VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".ogv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".opus", ".ogg"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
LEGACY_MANAGED_COPY_RE = re.compile(r"^media-center-[0-9a-f]{24}-import\.[^.]+$", re.IGNORECASE)
_enrichment_lock = threading.Lock()
_enrichment_path = ""
_enrichment_worker: MediaEnrichmentWorker | None = None


class MediaRootOperationBusy(RuntimeError):
    pass


def _repository() -> MediaCenterRepository:
    return MediaCenterRepository()


def _coordinator(repository: MediaCenterRepository | None = None) -> MediaCatalogCoordinator:
    return MediaCatalogCoordinator(repository or _repository())


def _topology() -> MediaCenterTopology:
    return MediaCenterTopology()


def _enrichment_runtime(
    catalog: MediaCatalogCoordinator | None = None,
) -> MediaEnrichmentWorker:
    global _enrichment_path, _enrichment_worker
    coordinator = catalog or _coordinator()
    path = str(coordinator.repository.db_path.resolve())
    with _enrichment_lock:
        if _enrichment_worker is None or _enrichment_path != path:
            if _enrichment_worker is not None:
                _enrichment_worker.dispose(timeout=0.2)
            _enrichment_worker = MediaEnrichmentWorker(
                coordinator,
                publish=lambda: _publish_library_snapshot(coordinator),
            )
            _enrichment_path = path
        return _enrichment_worker


def _event_payload(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        nested = event.get("payload")
        return dict(nested) if isinstance(nested, Mapping) else dict(event)
    nested = getattr(event, "payload", None)
    return dict(nested) if isinstance(nested, Mapping) else {}


def _publish_library_snapshot(
    catalog: MediaCatalogCoordinator,
    *,
    profile_id: str = "default",
    webspace_id: str = "",
) -> None:
    try:
        from adaos.sdk.io import stream_variable_publish

        profile = str(profile_id or "default").strip() or "default"
        snapshot = {
            "schema": "adaos.media_center.library_state.v1",
            "profile_id": profile,
            "catalog_revision": catalog.catalog_revision(),
            "personal_revision": catalog.profile_revision(profile),
            "participation": catalog.participation(),
            "home": catalog.home(profile_id=profile, limit=8),
            "operations": catalog.operations(limit=10),
        }
        stream_variable_publish(
            "media_center.library_state",
            snapshot,
            var_id=f"media_center.library.{profile}",
            seq=max(
                int(snapshot["catalog_revision"]),
                int(snapshot["personal_revision"]),
            ),
            ttl_ms=120000,
            _meta={"webspace_id": webspace_id} if webspace_id else None,
        )
    except Exception:
        return


def _invoke_agent(operation: str, arguments: Mapping[str, Any] | None = None, *, timeout: float = 15.0) -> tuple[dict[str, Any] | None, str]:
    try:
        from adaos.sdk.skills import invoke

        result = invoke("media_library_agent", operation, dict(arguments or {}), timeout=timeout)
        if isinstance(result, Mapping):
            return dict(result), ""
        return None, "media_library_agent_invalid_response"
    except Exception as exc:
        return None, str(exc)


@contextmanager
def _root_mutation_lease(repo: MediaCenterRepository) -> Iterator[None]:
    lock_path = repo.db_path.with_suffix(".root-mutation.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (OSError, BlockingIOError) as exc:
            raise MediaRootOperationBusy("media_root_operation_busy") from exc
        yield
    finally:
        if acquired:
            handle.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _skill_error(
    code: str,
    *,
    message: str = "",
    human_message: str = "",
    i18n_key: str = "",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "schema": SCHEMA_VERSION,
        "error": str(code or "").strip() or "skill_error",
    }
    if message:
        payload["message"] = message
    if human_message:
        payload["human_message"] = human_message
    if i18n_key:
        payload["human_message_i18n"] = {"key": i18n_key}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def _skill_text(key: str, fallback: str) -> str:
    try:
        translated = str(_(key) or "").strip()
    except Exception:
        translated = ""
    return translated if translated and translated != key else fallback


def _discover_resources(source: str = "all", limit: int | None = 5000) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        from adaos.sdk.io.media import list_media_resources
    except Exception as exc:
        return [], {"ok": False, "error": "sdk_media_discovery_unavailable", "detail": str(exc)}

    try:
        resources = list_media_resources(source=source, limit=limit)
        return [item for item in resources if not _is_legacy_managed_copy(item)], {"ok": True}
    except ValueError as exc:
        return [], {"ok": False, "error": str(exc)}
    except Exception as exc:
        return [], {"ok": False, "error": "media_discovery_failed", "detail": str(exc)}


def _is_legacy_managed_copy(descriptor: Mapping[str, Any]) -> bool:
    metadata = descriptor.get("metadata") if isinstance(descriptor.get("metadata"), Mapping) else {}
    if metadata.get("namespace") == "media-center" and metadata.get("variant") == "import":
        return True
    return bool(LEGACY_MANAGED_COPY_RE.fullmatch(str(descriptor.get("name") or "")))


def _register_media_file_descriptor(path: Path, *, root: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        from adaos.sdk.io.media import register_media_file
    except Exception as exc:
        return None, {"error": "sdk_media_registration_unavailable", "detail": str(exc), "path": str(path)}

    try:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        resolved_path = path.resolve(strict=True)
        root_path = Path(str(root.get("path") or "")).expanduser().resolve(strict=True)
        descriptor = register_media_file(
            path,
            root=root_path,
            content_ref=f"{root.get('id')}:{resolved_path}",
            namespace="media-center",
            mime=mime_type,
            metadata={
                "media_center_root_id": str(root.get("id") or ""),
                "media_center_root_path": str(root_path),
            },
        )
        descriptor = dict(descriptor)
        descriptor.setdefault("source_path", str(path))
        descriptor.setdefault("path", str(path))
        descriptor.setdefault("name", path.name)
        descriptor.setdefault("title", path.stem)
        descriptor.setdefault("mime_type", mime_type)
        return descriptor, None
    except Exception as exc:
        return None, {"error": "media_file_registration_failed", "detail": str(exc), "path": str(path)}


def _iter_root_media_files(root: Mapping[str, Any]) -> Iterator[Path]:
    root_path = Path(str(root.get("path") or "")).expanduser()
    include_images = _bool(root.get("include_images"), False)
    suffixes = set(VIDEO_EXTENSIONS) | set(AUDIO_EXTENSIONS)
    if include_images:
        suffixes |= IMAGE_EXTENSIONS
    if not root_path.exists() or not root_path.is_dir():
        return
    for path in root_path.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in suffixes:
            continue
        yield path


def _root_media_files(root: Mapping[str, Any]) -> list[Path]:
    files = list(_iter_root_media_files(root))
    return sorted(files, key=lambda item: str(item).lower())


def _int_limit(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except Exception:
        parsed = default
    return max(1, min(maximum, parsed))


@tool(summary="Ensure the durable media-center catalog schema.", side_effects="local_write")
def ensure_schema(**_: Any) -> dict[str, Any]:
    repo = _repository()
    legacy = repo.ensure_schema()
    coordinator = _coordinator(repo).ensure_schema()
    return {**legacy, "coordinator": coordinator}


@tool(summary="Rehydrate the durable media-center catalog after activation.", side_effects="local_write")
def rehydrate(**_: Any) -> dict[str, Any]:
    repo = _repository()
    catalog = _coordinator(repo)
    sync = _sync_agents(catalog, max_pages=4)
    enrichment = _enrichment_runtime(catalog)
    enrichment.ensure_started()
    _publish_library_snapshot(
        catalog,
        profile_id=str(_.get("profile_id") or "default"),
        webspace_id=str(_.get("webspace_id") or ""),
    )
    return {
        "ok": True,
        "schema": COORDINATOR_SCHEMA,
        "summary": repo.summary(),
        "facets": repo.facets(),
        "catalog_revision": catalog.catalog_revision(),
        "agent_sync": sync,
        "enrichment": {"running": True},
    }


def _sync_one_agent(
    catalog: MediaCatalogCoordinator,
    *,
    instance: Mapping[str, Any] | None,
    max_pages: int,
    limit: int,
) -> dict[str, Any]:
    pages = max(1, min(16, int(max_pages or 4)))
    page_limit = max(1, min(1000, int(limit or 500)))
    instance_id = str((instance or {}).get("instance_id") or "")
    node_id = str((instance or {}).get("node_id") or "")
    binding = catalog.agent_binding(instance_id) if instance_id else None
    actual_agent_id = str((binding or {}).get("agent_id") or "")
    cursor = str((binding or {}).get("cursor") or "")
    applied = ignored = removed = 0
    for _index in range(pages):
        if instance_id:
            try:
                page = _topology().invoke_agent(
                    instance_id,
                    "pull_deltas",
                    {"cursor": cursor, "limit": page_limit},
                    timeout_seconds=30.0,
                )
                error = ""
            except Exception as exc:
                page, error = None, str(exc)
        else:
            page, error = _invoke_agent(
                "pull_deltas",
                {"cursor": cursor, "limit": page_limit},
                timeout=30.0,
            )
        if page is None:
            if actual_agent_id:
                catalog.mark_agent_unavailable(
                    actual_agent_id,
                    node_id=node_id,
                    reason=error or "agent_unavailable",
                )
            return {
                "ok": False,
                "error": "media_library_agent_unavailable",
                "detail": error[:1000],
                "applied_count": applied,
                "retryable": True,
                "instance_id": instance_id,
                "node_id": node_id,
            }
        if not page.get("ok"):
            return {**page, "applied_count": applied}
        agent = page.get("agent") if isinstance(page.get("agent"), Mapping) else {}
        actual_agent_id = str(agent.get("id") or "")
        result = catalog.apply_agent_page(page, instance_id=instance_id)
        if not result.get("ok"):
            return result
        applied += int(result.get("applied_count") or 0)
        ignored += int(result.get("ignored_count") or 0)
        removed += int(result.get("removed_count") or 0)
        cursor = str(page.get("next_cursor") or cursor)
        if not page.get("has_more"):
            return {
                "ok": True,
                "schema": COORDINATOR_SCHEMA,
                "agent_id": actual_agent_id,
                "applied_count": applied,
                "ignored_count": ignored,
                "removed_count": removed,
                "has_more": False,
                "next_cursor": cursor,
                "instance_id": instance_id,
                "node_id": node_id or str(agent.get("node_id") or ""),
            }
    return {
        "ok": True,
        "schema": COORDINATOR_SCHEMA,
        "agent_id": actual_agent_id,
        "applied_count": applied,
        "ignored_count": ignored,
        "removed_count": removed,
        "has_more": True,
        "next_cursor": cursor,
        "bounded": True,
        "instance_id": instance_id,
        "node_id": node_id,
    }


def _sync_agents(
    catalog: MediaCatalogCoordinator, *, max_pages: int = 4, limit: int = 500
) -> dict[str, Any]:
    topology_error = ""
    try:
        instances = _topology().agent_instances(limit=100)
    except Exception as exc:
        instances = []
        topology_error = str(exc)
    if instances:
        catalog.reconcile_agent_instances(
            str(item.get("instance_id") or "") for item in instances
        )
        results = [
            _sync_one_agent(
                catalog,
                instance=instance,
                max_pages=max_pages,
                limit=limit,
            )
            for instance in instances
        ]
        return {
            "ok": all(bool(item.get("ok")) for item in results),
            "schema": COORDINATOR_SCHEMA,
            "mode": "distributed",
            "agents": results,
            "agent_count": len(results),
            "applied_count": sum(int(item.get("applied_count") or 0) for item in results),
            "has_more": any(bool(item.get("has_more")) for item in results),
            "participation": catalog.participation(),
        }
    local = _sync_one_agent(
        catalog,
        instance=None,
        max_pages=max_pages,
        limit=limit,
    )
    return {
        **local,
        "mode": "local_compatibility",
        "topology_error": topology_error[:300],
        "agents": [local],
        "agent_count": int(bool(local.get("agent_id"))),
        "participation": catalog.participation(),
    }


@subscribe("sys.ready")
def on_sys_ready(_: Any) -> None:
    catalog = _coordinator()
    _sync_agents(catalog, max_pages=4, limit=1000)
    _enrichment_runtime(catalog).ensure_started()
    _publish_library_snapshot(catalog)


@subscribe(
    "webio.stream.snapshot.requested",
    receivers=("media_center.library_state",),
)
def on_library_snapshot_requested(event: Any) -> None:
    payload = _event_payload(event)
    if str(payload.get("receiver") or "") != "media_center.library_state":
        return
    _publish_library_snapshot(
        _coordinator(),
        profile_id=str(payload.get("profile_id") or "default"),
        webspace_id=str(payload.get("webspace_id") or ""),
    )


@subscribe("media_library_agent.catalog.changed")
def on_agent_catalog_changed(event: Any) -> None:
    payload = _event_payload(event)
    catalog = _coordinator()
    _sync_agents(catalog, max_pages=8, limit=1000)
    _publish_library_snapshot(
        catalog,
        profile_id=str(payload.get("profile_id") or "default"),
        webspace_id=str(payload.get("webspace_id") or ""),
    )


@tool(summary="Pull bounded idempotent deltas from ready library agents.", side_effects="local_write")
def sync_agent(max_pages: int = 4, limit: int = 500, **_: Any) -> dict[str, Any]:
    catalog = _coordinator()
    result = _sync_agents(catalog, max_pages=max_pages, limit=limit)
    _publish_library_snapshot(
        catalog,
        profile_id=str(_.get("profile_id") or "default"),
        webspace_id=str(_.get("webspace_id") or ""),
    )
    return result


@tool(summary="Scan core-backed media resources into the media-center catalog.", side_effects="local_write")
def scan_sources(source: str = "all", limit: int = 5000, **_: Any) -> dict[str, Any]:
    resources, discovery = _discover_resources(source=source or "all", limit=limit)
    if not discovery.get("ok"):
        repo = _repository()
        return {**discovery, "schema": SCHEMA_VERSION, "summary": repo.summary(), "facets": repo.facets()}
    return _repository().scan_resources(resources, source=source or "all")


@tool(summary="List configured media-center library folders.", side_effects="none")
def list_roots(include_disabled: bool = False, **_: Any) -> dict[str, Any]:
    agent, _error = _invoke_agent("list_roots", {"include_disabled": _bool(include_disabled, False)})
    if agent is not None:
        agent["owner"] = "media_library_agent"
        return agent
    return _repository().list_roots(include_disabled=_bool(include_disabled, False))


@tool(summary="Add a local library folder to the media-center import set.", side_effects="local_write")
def add_root(path: str = "", label: str = "", include_images: bool = False, **_: Any) -> dict[str, Any]:
    agent, _error = _invoke_agent(
        "add_root",
        {"path": path, "label": label, "include_images": _bool(include_images, False)},
    )
    if agent is not None:
        agent["owner"] = "media_library_agent"
        return agent
    repo = _repository()
    try:
        with _root_mutation_lease(repo):
            return repo.add_root(path, label=label, include_images=_bool(include_images, False))
    except MediaRootOperationBusy:
        return _skill_error(
            "media_root_operation_busy",
            message="Another media folder import or deletion is still running.",
            retryable=True,
        )


@tool(summary="Disable a configured media-center library folder.", side_effects="local_write")
def remove_root(root_id: str = "", path: str = "", **_: Any) -> dict[str, Any]:
    agent, _error = _invoke_agent("remove_root", {"root_id": root_id})
    if agent is not None:
        agent["owner"] = "media_library_agent"
        return agent
    repo = _repository()
    try:
        with _root_mutation_lease(repo):
            return repo.remove_root(root_id=root_id, path=path)
    except MediaRootOperationBusy:
        return _skill_error(
            "media_root_operation_busy",
            message="Another media folder import or deletion is still running.",
            retryable=True,
        )


@tool(summary="Delete a media folder, its catalog rows, and core resource links.", side_effects="local_write")
def delete_root(root_id: str = "", path: str = "", **_: Any) -> dict[str, Any]:
    agent, _error = _invoke_agent("remove_root", {"root_id": root_id})
    if agent is not None:
        return {
            **agent,
            "owner": "media_library_agent",
            "disabled": bool(agent.get("ok")),
            "source_files_deleted": False,
            "retention": "external_media_and_catalog_evidence_retained",
        }
    repo = _repository()
    try:
        with _root_mutation_lease(repo):
            plan = repo.root_delete_plan(root_id=root_id, path=path)
            if not plan.get("ok"):
                return plan
            resource_ids = [str(item) for item in plan.get("resource_ids") or []]
            try:
                from adaos.sdk.io.media import unregister_media_references

                resource_cleanup = unregister_media_references(resource_ids)
            except Exception as exc:
                return _skill_error(
                    "media_reference_cleanup_failed",
                    message="The folder was retained because its media resource links could not be removed.",
                    detail=str(exc),
                    root=plan.get("root"),
                    resource_ids=resource_ids,
                )
            deleted = repo.delete_root(root_id=str(plan["root"]["id"]))
            return {**deleted, "resource_cleanup": resource_cleanup}
    except MediaRootOperationBusy:
        return _skill_error(
            "media_root_operation_busy",
            message="Another media folder import or deletion is still running.",
            retryable=True,
        )


@tool(summary="Register playable files from configured folders without copying media bytes.", side_effects="local_write")
def scan_roots(root_id: str = "", path: str = "", limit: int = 1000, **_: Any) -> dict[str, Any]:
    arguments = {"root_id": root_id, "mode": "incremental"}
    agent, _error = _invoke_agent("start_scan", arguments)
    if agent is not None:
        agent["owner"] = "media_library_agent"
        agent["legacy_limit_ignored"] = int(limit or 0)
        return agent
    repo = _repository()
    try:
        with _root_mutation_lease(repo):
            return _scan_roots(repo, root_id=root_id, path=path, limit=limit)
    except MediaRootOperationBusy:
        return _skill_error(
            "media_root_operation_busy",
            message="Another media folder import or deletion is still running.",
            retryable=True,
        )


def _scan_roots(repo: MediaCenterRepository, *, root_id: str = "", path: str = "", limit: int = 1000) -> dict[str, Any]:
    limit_value = _int_limit(limit, 1000, 5000)
    roots = repo.list_roots()["items"]
    root_token = str(root_id or "").strip()
    path_token = str(path or "").strip()
    if root_token:
        roots = [root for root in roots if str(root.get("id") or "") == root_token]
    elif path_token:
        roots = [root for root in roots if str(root.get("path") or "") == str(Path(path_token).expanduser().resolve(strict=False))]

    if not roots:
        return _skill_error(
            "no_active_media_roots",
            message="No active media folders are configured.",
            human_message=_skill_text(
                "runtime.media_center.error.no_active_media_roots",
                "Add a media folder first, then run import.",
            ),
            i18n_key="runtime.media_center.error.no_active_media_roots",
            roots=repo.list_roots()["items"],
        )

    descriptors: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped = 0
    visited = 0
    for root in roots:
        if len(descriptors) >= limit_value:
            skipped += 1
            repo.mark_root_scanned(str(root.get("id") or ""), status="limit_reached")
            continue
        root_registered = 0
        root_errors = 0
        root_skipped = 0
        found = False
        for file_path in _iter_root_media_files(root):
            found = True
            if len(descriptors) >= limit_value:
                skipped += 1
                root_skipped += 1
                break
            visited += 1
            descriptor, error = _register_media_file_descriptor(file_path, root=root)
            if descriptor:
                descriptors.append(descriptor)
                root_registered += 1
            elif error:
                errors.append(error)
                root_errors += 1
        if not found:
            repo.mark_root_scanned(str(root.get("id") or ""), status="no_playable_files")
            continue
        status = "ok" if root_registered else ("error" if root_errors else "limit_reached" if root_skipped else "empty")
        repo.mark_root_scanned(str(root.get("id") or ""), status=status)

    scan = repo.scan_resources(descriptors, source="media_server", mark_missing=False) if descriptors else {
        "ok": True,
        "schema": SCHEMA_VERSION,
        "source": "media_server",
        "discovered_count": 0,
        "updated_count": 0,
        "missing_count": 0,
        "summary": repo.summary(),
    }
    return {
        **scan,
        "roots": repo.list_roots()["items"],
        "visited_count": visited,
        "registered_count": len(descriptors),
        "skipped_count": skipped,
        "error_count": len(errors),
        "errors": errors[:20],
    }


@tool(summary="Add a folder and register its playable files in place.", side_effects="local_write")
def import_folder(path: str = "", label: str = "", include_images: bool = False, limit: int = 1000, **_: Any) -> dict[str, Any]:
    agent, _error = _invoke_agent(
        "import_folder",
        {"path": path, "label": label, "include_images": _bool(include_images, False)},
    )
    if agent is not None:
        agent["owner"] = "media_library_agent"
        agent["legacy_limit_ignored"] = int(limit or 0)
        return agent
    repo = _repository()
    try:
        with _root_mutation_lease(repo):
            added = repo.add_root(path, label=label, include_images=_bool(include_images, False))
            if not added.get("ok"):
                return added
            root = added.get("root") if isinstance(added.get("root"), Mapping) else {}
            scan = _scan_roots(repo, root_id=str(root.get("id") or ""), limit=limit)
            return {**scan, "root": root, "add": added}
    except MediaRootOperationBusy:
        return _skill_error(
            "media_root_operation_busy",
            message="Another media folder import or deletion is still running.",
            retryable=True,
        )


@tool(summary="Return the media-center library projection for widgets and playback.", side_effects="none")
def library(
    query: str = "",
    media_kind: str = "playable",
    source: str = "",
    limit: int = 30,
    offset: int = 0,
    cursor: str = "",
    include_missing: bool = False,
    favorites_only: bool = False,
    sort: str = "recent",
    profile_id: str = "default",
    collection_id: str = "",
    auto_scan: bool = True,
    **_: Any,
) -> dict[str, Any]:
    repo = _repository()
    catalog = _coordinator(repo)
    scan: dict[str, Any] | None = None
    agent_sync: dict[str, Any] | None = None
    summary = repo.summary()
    if _bool(auto_scan, True):
        agent_sync = _sync_agents(catalog, max_pages=1, limit=500)
        if not agent_sync.get("ok") and int(summary.get("total_count") or 0) == 0:
            scan = scan_sources(source="all", limit=5000)
    try:
        payload = catalog.list_items(
            query=query,
            media_kind=media_kind,
            source=source,
            limit=limit,
            offset=offset,
            cursor=cursor,
            include_missing=_bool(include_missing, False),
            favorites_only=_bool(favorites_only, False),
            sort=sort,
            profile_id=profile_id,
            collection_id=collection_id,
        )
    except ValueError:
        return _skill_error(
            "invalid_media_catalog_cursor",
            human_message=_skill_text(
                "runtime.media_center.error.invalid_media_catalog_cursor",
                "The catalog page changed. Refresh the list.",
            ),
            i18n_key="runtime.media_center.error.invalid_media_catalog_cursor",
        )
    if scan is not None:
        payload["scan"] = scan
    if agent_sync is not None:
        payload["agent_sync"] = agent_sync
    payload["runtime"] = {
        "catalog_owner": "media_center_skill",
        "discovery_owner": "media_library_agent",
        "resource_boundary": "adaos.sdk.io.media.list_media_resources",
        "agent_delta_boundary": "media_library_agent.pull_deltas",
        "publication_boundary": "adaos.sdk.io.media.register_media_file via media_library_agent",
        "storage_mode": "reference",
        "playback_contract": "adaos.media.resource.v1",
    }
    payload["capabilities"] = {
        "catalog": {"status": "distributed_coordinator", "durable": True, "max_page_size": 30},
        "playback": {"status": "delegated_to_core_media_resource"},
        "enrichment": {"status": "background_jobs"},
    }
    return payload


@tool(summary="List media-center catalog rows.", side_effects="none")
def list_items(**payload: Any) -> dict[str, Any]:
    return library(**payload)


@tool(summary="Read one media-center catalog item.", side_effects="none")
def get_item(item_id: str = "", **_: Any) -> dict[str, Any]:
    return _repository().get_item(item_id)


@tool(summary="Return the selected media item and a bounded playback queue.", side_effects="none")
def playback_queue(
    item_id: str = "",
    query: str = "",
    media_kind: str = "playable",
    source: str = "",
    favorites_only: bool = False,
    sort: str = "recent",
    limit: int = 10,
    profile_id: str = "default",
    **_: Any,
) -> dict[str, Any]:
    repo = _repository()
    catalog = _coordinator(repo)
    selected_result = repo.get_item(item_id)
    if not selected_result.get("ok"):
        return {**selected_result, "items": [], "count": 0, "total_count": 0}
    selected = dict(selected_result["item"])
    queue_limit = _int_limit(limit, 10, 10)
    listing = catalog.list_items(
        query=query,
        media_kind=media_kind or "playable",
        source=source,
        favorites_only=_bool(favorites_only, False),
        limit=queue_limit,
        offset=0,
        sort=sort,
        profile_id=profile_id,
    )
    items = [selected]
    items.extend(item for item in listing["items"] if item.get("id") != selected.get("id"))
    items = items[:queue_limit]
    return {
        **listing,
        "items": items,
        "count": len(items),
        "selected_item_id": selected.get("id"),
        "pagination": {
            "limit": queue_limit,
            "offset": 0,
            "next_offset": None,
            "has_more": False,
        },
        "runtime": {
            "catalog_owner": "media_center_skill",
            "playback_contract": "adaos.media.resource.v1",
            "storage_mode": "reference",
        },
        "capabilities": {
            "playback": {"status": "delegated_to_core_media_resource"},
            "playlist": {"status": "bounded", "max_items": 10},
        },
    }


@tool(summary="Select a media variant and route for one playback endpoint.", side_effects="none")
def playback_plan(
    item_id: str = "",
    endpoint_id: str = "",
    endpoint_node_id: str = "",
    endpoint_capabilities: Mapping[str, Any] | None = None,
    preferred_quality: str = "auto",
    preferred_language: str = "",
    variant_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    repo = _repository()
    result = _coordinator(repo).playback_plan(
        item_id,
        endpoint_id=endpoint_id,
        endpoint_node_id=endpoint_node_id,
        endpoint_capabilities=endpoint_capabilities,
        preferred_quality=preferred_quality,
        preferred_language=preferred_language,
        variant_id=variant_id,
    )
    if result.get("error") == "playback_source_unavailable":
        legacy = repo.playback_plan(item_id)
        if legacy.get("ok"):
            legacy["compatibility_mode"] = "legacy_catalog_row"
            return legacy
    return result


@tool(summary="Build a bounded playback queue from a catalog source.", side_effects="none")
def build_playback_queue(
    source_type: str = "item",
    source_id: str = "",
    profile_id: str = "default",
    limit: int = 500,
    endpoint_id: str = "",
    endpoint_node_id: str = "",
    endpoint_capabilities: Mapping[str, Any] | None = None,
    preferred_quality: str = "auto",
    preferred_language: str = "",
    **_: Any,
) -> dict[str, Any]:
    return _coordinator().build_queue(
        source_type=source_type,
        source_id=source_id,
        profile_id=profile_id,
        limit=limit,
        endpoint_id=endpoint_id,
        endpoint_node_id=endpoint_node_id,
        endpoint_capabilities=endpoint_capabilities,
        preferred_quality=preferred_quality,
        preferred_language=preferred_language,
    )


@tool(summary="Mark or unmark one media-center item as favorite.", side_effects="local_write")
def set_favorite(item_id: str = "", favorite: bool = True, **_: Any) -> dict[str, Any]:
    profile_id = str(_.get("profile_id") or "default")
    catalog = _coordinator()
    result = catalog.set_favorite(
        item_id, profile_id=profile_id, favorite=_bool(favorite, True)
    )
    if result.get("ok"):
        _publish_library_snapshot(
            catalog,
            profile_id=profile_id,
            webspace_id=str(_.get("webspace_id") or ""),
        )
    return result


@tool(summary="Return compact media-center catalog status.", side_effects="none")
def status(**_: Any) -> dict[str, Any]:
    repo = _repository()
    catalog = _coordinator(repo)
    return {
        "ok": True,
        "schema": COORDINATOR_SCHEMA,
        "summary": repo.summary(),
        "facets": repo.facets(),
        "coordinator": catalog.diagnostics(),
    }


@tool(summary="Return Media Center deployment and node administration state.", side_effects="none")
def deployment_status(deployment_id: str = "media-center-home", limit: int = 50, **_: Any) -> dict[str, Any]:
    return _topology().deployment_status(deployment_id, limit=limit)


@tool(summary="Create a reviewed dry-run Media Center deployment plan.", side_effects="local_write")
def configure_deployment(
    release_digest: str = "",
    subnet_id: str = "",
    coordinator_node_id: str = "",
    agent_node_ids: list[str] | None = None,
    all_matching_agents: bool = False,
    expected_revision: int = 0,
    allow_release_skew: bool = False,
    reason: str = "Media Center placement update",
    deployment_id: str = "media-center-home",
    **_: Any,
) -> dict[str, Any]:
    try:
        return _topology().configure_deployment(
            release_digest=release_digest,
            subnet_id=subnet_id,
            coordinator_node_id=coordinator_node_id,
            agent_node_ids=agent_node_ids or [],
            all_matching_agents=all_matching_agents,
            expected_revision=expected_revision,
            allow_release_skew=allow_release_skew,
            reason=reason,
            deployment_id=deployment_id,
        )
    except Exception as exc:
        key = "runtime.media_center.error.deployment_plan_failed"
        return _skill_error(
            "deployment_plan_failed",
            human_message=_skill_text(key, "Could not create the Media Center deployment plan."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Apply one explicitly reviewed Media Center deployment plan.", side_effects="remote_write")
def apply_deployment(plan_digest: str = "", idempotency_key: str = "", **_: Any) -> dict[str, Any]:
    try:
        return _topology().apply_deployment(plan_digest, idempotency_key=idempotency_key)
    except Exception as exc:
        key = "runtime.media_center.error.deployment_apply_failed"
        return _skill_error(
            "deployment_apply_failed",
            human_message=_skill_text(key, "Could not apply the Media Center deployment plan."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Cordon and drain one Media Center component activation.", side_effects="remote_write")
def drain_activation(activation_id: str = "", idempotency_key: str = "", **_: Any) -> dict[str, Any]:
    try:
        return _topology().drain_activation(activation_id, idempotency_key=idempotency_key)
    except Exception as exc:
        key = "runtime.media_center.error.deployment_drain_failed"
        return _skill_error(
            "deployment_drain_failed",
            human_message=_skill_text(key, "Could not drain the Media Center agent."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Remove one drained Media Center activation while retaining media and derived data.", side_effects="remote_write")
def remove_activation(activation_id: str = "", idempotency_key: str = "", **_: Any) -> dict[str, Any]:
    try:
        return _topology().remove_activation(activation_id, idempotency_key=idempotency_key)
    except Exception as exc:
        key = "runtime.media_center.error.deployment_remove_failed"
        return _skill_error(
            "deployment_remove_failed",
            human_message=_skill_text(key, "Could not remove the Media Center agent."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Define Media Center logical service and datasets through the public SDK.", side_effects="local_write")
def define_topology(
    service_definition: Mapping[str, Any] | None = None,
    service_group: Mapping[str, Any] | None = None,
    datasets: list[Mapping[str, Any]] | None = None,
    expected_group_revision: int = 0,
    **_: Any,
) -> dict[str, Any]:
    try:
        return _topology().define_topology(
            service_definition=service_definition or {},
            service_group=service_group or {},
            datasets=datasets or [],
            expected_group_revision=expected_group_revision,
        )
    except Exception as exc:
        key = "runtime.media_center.error.topology_define_failed"
        return _skill_error(
            "topology_define_failed",
            human_message=_skill_text(key, "Could not define Media Center service topology."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Return bounded generic distributed topology state for Media Center.", side_effects="none")
def topology_status(limit: int = 50, **_: Any) -> dict[str, Any]:
    return _topology().distributed_status(limit=limit)


@tool(summary="Explain route eligibility and partial participation for Media Center shards.", side_effects="none")
def explain_route(partition_ids: list[str] | None = None, **_: Any) -> dict[str, Any]:
    try:
        return {"ok": True, **_topology().explain_route(partition_ids or [])}
    except Exception as exc:
        key = "runtime.media_center.error.route_explain_failed"
        return _skill_error(
            "route_explain_failed",
            human_message=_skill_text(key, "Could not explain the Media Center route."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Return bounded home shelves for one media profile.", side_effects="none")
def home(profile_id: str = "default", limit: int = 12, **_: Any) -> dict[str, Any]:
    return _coordinator().home(profile_id=profile_id, limit=limit)


@tool(summary="List typed media collections through an opaque cursor.", side_effects="none")
def list_collections(kind: str = "", limit: int = 30, cursor: str = "", **_: Any) -> dict[str, Any]:
    try:
        return _coordinator().collections(kind=kind, limit=limit, cursor=cursor)
    except ValueError:
        return _skill_error("invalid_media_catalog_cursor", message="The collection page changed. Refresh the list.")


@tool(summary="Browse bounded catalog folders through an opaque cursor.", side_effects="none")
def browse_folders(
    agent_id: str = "",
    root_id: str = "",
    parent: str = "",
    limit: int = 30,
    cursor: str = "",
    **_: Any,
) -> dict[str, Any]:
    try:
        return _coordinator().folders(
            agent_id=agent_id,
            root_id=root_id,
            parent=parent,
            limit=limit,
            cursor=cursor,
        )
    except ValueError:
        return _skill_error(
            "invalid_media_catalog_cursor",
            message="The folder page changed. Refresh the list.",
        )


@tool(summary="Create a profile-owned media playlist.", side_effects="local_write")
def create_playlist(
    title: str = "",
    profile_id: str = "default",
    visibility: str = "private",
    item_ids: list[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    catalog = _coordinator()
    result = catalog.create_playlist(
        profile_id=profile_id,
        title=title,
        visibility=visibility,
        item_ids=item_ids or [],
    )
    if result.get("ok"):
        _publish_library_snapshot(
            catalog,
            profile_id=profile_id,
            webspace_id=str(_.get("webspace_id") or ""),
        )
    return result


@tool(summary="List playlists visible to one profile.", side_effects="none")
def list_playlists(
    profile_id: str = "default",
    limit: int = 30,
    cursor: str = "",
    **_: Any,
) -> dict[str, Any]:
    try:
        return _coordinator().playlists(
            profile_id=profile_id, limit=limit, cursor=cursor
        )
    except ValueError:
        return _skill_error("invalid_media_catalog_cursor")


@tool(summary="Read one bounded playlist page.", side_effects="none")
def playlist_items(
    playlist_id: str = "",
    profile_id: str = "default",
    limit: int = 30,
    cursor: str = "",
    **_: Any,
) -> dict[str, Any]:
    try:
        return _coordinator().playlist_items(
            playlist_id,
            profile_id=profile_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError:
        return _skill_error("invalid_media_catalog_cursor")


@tool(summary="Revision-safely replace playlist metadata and order.", side_effects="local_write")
def update_playlist(
    playlist_id: str = "",
    profile_id: str = "default",
    expected_revision: int = 1,
    title: str | None = None,
    visibility: str | None = None,
    item_ids: list[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    catalog = _coordinator()
    result = catalog.update_playlist(
        playlist_id,
        profile_id=profile_id,
        expected_revision=expected_revision,
        title=title,
        visibility=visibility,
        item_ids=item_ids,
    )
    if result.get("ok"):
        _publish_library_snapshot(
            catalog,
            profile_id=profile_id,
            webspace_id=str(_.get("webspace_id") or ""),
        )
    return result


@tool(summary="Revision-safely delete a playlist without deleting media.", side_effects="local_write")
def delete_playlist(
    playlist_id: str = "",
    profile_id: str = "default",
    expected_revision: int = 1,
    **_: Any,
) -> dict[str, Any]:
    catalog = _coordinator()
    result = catalog.delete_playlist(
        playlist_id,
        profile_id=profile_id,
        expected_revision=expected_revision,
    )
    if result.get("ok"):
        _publish_library_snapshot(
            catalog,
            profile_id=profile_id,
            webspace_id=str(_.get("webspace_id") or ""),
        )
    return result


@tool(summary="Apply an audited reversible catalog correction.", side_effects="local_write")
def apply_correction(
    operation: str = "",
    subject_ref: str = "",
    values: Mapping[str, Any] | None = None,
    actor_ref: str = "profile:default",
    **_: Any,
) -> dict[str, Any]:
    return _coordinator().apply_correction(
        operation=operation,
        subject_ref=subject_ref,
        values=values or {},
        actor_ref=actor_ref,
    )


@tool(summary="Reverse one audited catalog correction.", side_effects="local_write")
def reverse_correction(
    correction_id: str = "", actor_ref: str = "profile:default", **_: Any
) -> dict[str, Any]:
    return _coordinator().reverse_correction(
        correction_id, actor_ref=actor_ref
    )


@tool(summary="List bounded metadata claims and provenance.", side_effects="none")
def metadata_claims(
    subject_ref: str = "", limit: int = 30, **_: Any
) -> dict[str, Any]:
    return _coordinator().metadata_claims(subject_ref, limit=limit)


@tool(summary="Return cheap non-destructive duplicate and variant candidates.", side_effects="none")
def duplicate_candidates(limit: int = 30, **_: Any) -> dict[str, Any]:
    return _coordinator().duplicate_candidates(limit=limit)


@tool(summary="Persist a bounded profile resume checkpoint.", side_effects="local_write")
def save_checkpoint(
    item_id: str = "",
    profile_id: str = "default",
    position_ms: int = 0,
    duration_ms: int = 0,
    completed: bool = False,
    **_: Any,
) -> dict[str, Any]:
    catalog = _coordinator()
    result = catalog.checkpoint(
        item_id,
        profile_id=profile_id,
        position_ms=position_ms,
        duration_ms=duration_ms,
        completed=_bool(completed, False),
    )
    if result.get("ok"):
        _publish_library_snapshot(
            catalog,
            profile_id=profile_id,
            webspace_id=str(_.get("webspace_id") or ""),
        )
    return result


@tool(summary="Queue background media enrichment or technical analysis.", side_effects="local_write")
def queue_background_job(kind: str = "", subject_ref: str = "", priority: int = 100, **_: Any) -> dict[str, Any]:
    catalog = _coordinator()
    result = catalog.queue_background_job(kind, subject_ref, priority=priority)
    if result.get("ok"):
        _enrichment_runtime(catalog).ensure_started()
        _publish_library_snapshot(
            catalog,
            profile_id=str(_.get("profile_id") or "default"),
            webspace_id=str(_.get("webspace_id") or ""),
        )
    return result


@tool(summary="List bounded background media operations.", side_effects="none")
def operations(limit: int = 30, **_: Any) -> dict[str, Any]:
    return _coordinator().operations(limit=limit)


@tool(summary="Stop the process-local enrichment worker.", side_effects="local_write")
def dispose(**_: Any) -> dict[str, Any]:
    global _enrichment_worker
    with _enrichment_lock:
        worker = _enrichment_worker
        _enrichment_worker = None
    if worker is not None:
        worker.dispose()
    return {"ok": True, "schema": COORDINATOR_SCHEMA, "disposed": True}


@tool(summary="Explain the MVP media-center workflow and admitted next steps.", side_effects="none")
def next_steps(**_: Any) -> dict[str, Any]:
    steps = [
        {
            "id": "scan",
            "label": "Refresh catalog",
            "reason": "Reconcile Media Server and legacy media-indexer resource descriptors into the durable catalog.",
        },
        {
            "id": "folders",
            "label": "Import folders",
            "reason": "Store folder roots in the skill, register playable files in place, and index the resulting core descriptors.",
        },
        {
            "id": "play",
            "label": "Preview available media",
            "reason": "Use the core media resource content paths; the catalog does not stream files directly.",
        },
        {
            "id": "production_media_center",
            "label": "Add production semantics later",
            "reason": "Metadata enrichment, people/scenes, queues, remote players, recommendations, and library sources belong above this MVP.",
        },
    ]
    message = " ".join(f"{idx + 1}. {item['label']}: {item['reason']}" for idx, item in enumerate(steps))
    return {"ok": True, "schema": SCHEMA_VERSION, "steps": steps, "message": message, "speech_text": message}


@tool(summary="Small conversational entrypoint for media-center control.", side_effects="local_write")
def chat(text: str = "", **payload: Any) -> dict[str, Any]:
    raw = str(text or "").strip()
    folded = raw.casefold()
    if any(token in folded for token in ("folder", "folders", "root", "roots", "папк")):
        return list_roots()
    if any(token in folded for token in ("scan", "refresh", "rescan", "index", "catalog")):
        result = scan_sources(source=str(payload.get("source") or "all"), limit=int(payload.get("limit") or 5000))
        result["message"] = (
            f"Catalog refreshed: {result.get('discovered_count', 0)} resources, "
            f"{result.get('updated_count', 0)} updated, {result.get('missing_count', 0)} missing."
        )
        return result
    if any(token in folded for token in ("next", "help", "roadmap")):
        return next_steps()
    result = library(query=raw, limit=int(payload.get("limit") or 20), auto_scan=True)
    result["message"] = f"Found {result.get('total_count', 0)} media items."
    return result
