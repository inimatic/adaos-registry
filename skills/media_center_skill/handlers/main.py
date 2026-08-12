from __future__ import annotations

import hashlib
import mimetypes
import sys
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


def _repository() -> MediaCenterRepository:
    return MediaCenterRepository()


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
        return list_media_resources(source=source, limit=limit), {"ok": True}
    except ValueError as exc:
        return [], {"ok": False, "error": str(exc)}
    except Exception as exc:
        return [], {"ok": False, "error": "media_discovery_failed", "detail": str(exc)}


def _publish_media_file_descriptor(path: Path, *, root: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        from adaos.sdk.io.media import publish_media_file
    except Exception as exc:
        return None, {"error": "sdk_media_publication_unavailable", "detail": str(exc), "path": str(path)}

    try:
        stat = path.stat()
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        identity = f"{root.get('id')}:{path.resolve(strict=False)}:{stat.st_mtime_ns}:{stat.st_size}"
        content_ref = "media_center:" + hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
        descriptor = publish_media_file(
            path,
            content_ref=content_ref,
            namespace="media-center",
            variant="import",
            mime=mime_type,
        )
        descriptor = dict(descriptor)
        descriptor.setdefault("source_path", str(path))
        descriptor.setdefault("path", str(path))
        descriptor.setdefault("name", path.name)
        descriptor.setdefault("title", path.stem)
        descriptor.setdefault("mime_type", mime_type)
        metadata = descriptor.get("metadata") if isinstance(descriptor.get("metadata"), Mapping) else {}
        descriptor["metadata"] = {
            **dict(metadata),
            "media_center_root_id": str(root.get("id") or ""),
            "media_center_root_path": str(root.get("path") or ""),
        }
        return descriptor, None
    except Exception as exc:
        return None, {"error": "media_file_publication_failed", "detail": str(exc), "path": str(path)}


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
    return _repository().add_root(path, label=label, include_images=_bool(include_images, False))


@tool(summary="Disable a configured media-center library folder.", side_effects="local_write")
def remove_root(root_id: str = "", path: str = "", **_: Any) -> dict[str, Any]:
    return _repository().remove_root(root_id=root_id, path=path)


@tool(summary="Import playable files from configured folders into Media Server-backed resources.", side_effects="local_write")
def scan_roots(root_id: str = "", path: str = "", limit: int = 1000, **_: Any) -> dict[str, Any]:
    repo = _repository()
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
        root_published = 0
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
            descriptor, error = _publish_media_file_descriptor(file_path, root=root)
            if descriptor:
                descriptors.append(descriptor)
                root_published += 1
            elif error:
                errors.append(error)
                root_errors += 1
        if not found:
            repo.mark_root_scanned(str(root.get("id") or ""), status="no_playable_files")
            continue
        status = "ok" if root_published else ("error" if root_errors else "limit_reached" if root_skipped else "empty")
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
        "published_count": len(descriptors),
        "skipped_count": skipped,
        "error_count": len(errors),
        "errors": errors[:20],
    }


@tool(summary="Add a folder and import its playable files through Media Server.", side_effects="local_write")
def import_folder(path: str = "", label: str = "", include_images: bool = False, limit: int = 1000, **_: Any) -> dict[str, Any]:
    repo = _repository()
    added = repo.add_root(path, label=label, include_images=_bool(include_images, False))
    if not added.get("ok"):
        return added
    root = added.get("root") if isinstance(added.get("root"), Mapping) else {}
    scan = scan_roots(root_id=str(root.get("id") or ""), limit=limit)
    return {**scan, "root": root, "add": added}


@tool(summary="Return the media-center library projection for widgets and playback.", side_effects="none")
def library(
    query: str = "",
    media_kind: str = "",
    source: str = "",
    limit: int = 100,
    offset: int = 0,
    include_missing: bool = False,
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
        sort=sort,
    )
    if scan is not None:
        payload["scan"] = scan
    payload["runtime"] = {
        "catalog_owner": "media_center_skill",
        "resource_boundary": "adaos.sdk.io.media.list_media_resources",
        "publication_boundary": "adaos.sdk.io.media.publish_media_file",
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
            "reason": "Store folder roots in the skill, publish playable files into Media Server, and index the resulting core descriptors.",
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
