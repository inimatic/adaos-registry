from __future__ import annotations

import mimetypes
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from adaos.sdk.core.decorators import tool
from adaos.sdk.data.i18n import _


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from media_center.catalog import MediaCenterRepository, SCHEMA_VERSION


VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".ogv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".opus", ".ogg"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
LEGACY_MANAGED_COPY_RE = re.compile(r"^media-center-[0-9a-f]{24}-import\.[^.]+$", re.IGNORECASE)


class MediaRootOperationBusy(RuntimeError):
    pass


def _repository() -> MediaCenterRepository:
    return MediaCenterRepository()


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
    return _repository().ensure_schema()


@tool(summary="Rehydrate the durable media-center catalog after activation.", side_effects="local_write")
def rehydrate(**_: Any) -> dict[str, Any]:
    repo = _repository()
    return {"ok": True, "schema": SCHEMA_VERSION, "summary": repo.summary(), "facets": repo.facets()}


@tool(summary="Scan core-backed media resources into the media-center catalog.", side_effects="local_write")
def scan_sources(source: str = "all", limit: int = 5000, **_: Any) -> dict[str, Any]:
    resources, discovery = _discover_resources(source=source or "all", limit=limit)
    if not discovery.get("ok"):
        repo = _repository()
        return {**discovery, "schema": SCHEMA_VERSION, "summary": repo.summary(), "facets": repo.facets()}
    return _repository().scan_resources(resources, source=source or "all")


@tool(summary="List configured media-center library folders.", side_effects="none")
def list_roots(include_disabled: bool = False, **_: Any) -> dict[str, Any]:
    return _repository().list_roots(include_disabled=_bool(include_disabled, False))


@tool(summary="Add a local library folder to the media-center import set.", side_effects="local_write")
def add_root(path: str = "", label: str = "", include_images: bool = False, **_: Any) -> dict[str, Any]:
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
    limit: int = 100,
    offset: int = 0,
    include_missing: bool = False,
    favorites_only: bool = False,
    sort: str = "recent",
    auto_scan: bool = True,
    **_: Any,
) -> dict[str, Any]:
    repo = _repository()
    scan: dict[str, Any] | None = None
    summary = repo.summary()
    if _bool(auto_scan, True) and int(summary.get("total_count") or 0) == 0:
        scan = scan_sources(source="all", limit=5000)
    payload = repo.list_items(
        query=query,
        media_kind=media_kind,
        source=source,
        limit=limit,
        offset=offset,
        include_missing=_bool(include_missing, False),
        favorites_only=_bool(favorites_only, False),
        sort=sort,
    )
    if scan is not None:
        payload["scan"] = scan
    payload["runtime"] = {
        "catalog_owner": "media_center_skill",
        "resource_boundary": "adaos.sdk.io.media.list_media_resources",
        "publication_boundary": "adaos.sdk.io.media.register_media_file",
        "storage_mode": "reference",
        "playback_contract": "adaos.media.resource.v1",
    }
    payload["capabilities"] = {
        "catalog": {"status": "mvp", "durable": True},
        "playback": {"status": "delegated_to_core_media_resource"},
        "enrichment": {"status": "planned"},
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
    **_: Any,
) -> dict[str, Any]:
    repo = _repository()
    selected_result = repo.get_item(item_id)
    if not selected_result.get("ok"):
        return {**selected_result, "items": [], "count": 0, "total_count": 0}
    selected = dict(selected_result["item"])
    queue_limit = _int_limit(limit, 10, 10)
    listing = repo.list_items(
        query=query,
        media_kind=media_kind or "playable",
        source=source,
        favorites_only=_bool(favorites_only, False),
        limit=queue_limit,
        offset=0,
        sort=sort,
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


@tool(summary="Return a core-media playback plan for one catalog item.", side_effects="local_write")
def playback_plan(item_id: str = "", **_: Any) -> dict[str, Any]:
    return _repository().playback_plan(item_id)


@tool(summary="Mark or unmark one media-center item as favorite.", side_effects="local_write")
def set_favorite(item_id: str = "", favorite: bool = True, **_: Any) -> dict[str, Any]:
    return _repository().set_favorite(item_id, favorite=_bool(favorite, True))


@tool(summary="Return compact media-center catalog status.", side_effects="none")
def status(**_: Any) -> dict[str, Any]:
    repo = _repository()
    return {"ok": True, "schema": SCHEMA_VERSION, "summary": repo.summary(), "facets": repo.facets()}


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
