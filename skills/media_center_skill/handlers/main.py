from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.i18n import _


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))


_PLAYBACK_OBSERVATION_CACHE: dict[str, tuple[int, str, float]] = {}
_PLAYBACK_OBSERVATION_LIMIT = 256
_HOME_SNAPSHOT_CACHE: dict[
    tuple[str, str, bool], tuple[tuple[int, int], dict[str, Any], float]
] = {}
_HOME_SNAPSHOT_CACHE_LIMIT = 32
_HOME_SNAPSHOT_CACHE_TTL_SECONDS = 15 * 60
_home_snapshot_cache_lock = threading.Lock()
_home_snapshot_build_lock = threading.Lock()
_READY_LIBRARY_SNAPSHOT_CACHE: dict[
    tuple[str, str, bool], dict[str, Any]
] = {}
_ready_library_snapshot_cache_lock = threading.Lock()

from media_center.background import background_runtime  # noqa: E402
from media_center.catalog import (  # noqa: E402
    MediaCenterRepository,
    SCHEMA_VERSION,
    default_db_path,
    now_iso,
)
from media_center.coordinator import (  # noqa: E402
    COORDINATOR_SCHEMA,
    MediaCatalogCoordinator,
    clear_filename_evidence_cache,
)
from media_center.enrichment import (  # noqa: E402
    MediaEnrichmentWorker,
    MetadataProviderError,
    TmdbMetadataProvider,
    default_metadata_providers,
    metadata_provider_configuration,
)
from media_center.sync import MediaAgentSyncWorker  # noqa: E402


VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".ogv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".opus", ".ogg"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
LEGACY_MANAGED_COPY_RE = re.compile(r"^media-center-[0-9a-f]{24}-import\.[^.]+$", re.IGNORECASE)
_coordinator_lock = threading.Lock()
_coordinator_init_lock = threading.Lock()
_coordinator_path = ""
_coordinator_cached: MediaCatalogCoordinator | None = None
_log = logging.getLogger("adaos.skill.media_center")
_TMDB_CREDENTIAL_SECRET = "tmdb_api_credential"


class MediaRootOperationBusy(RuntimeError):
    pass


def _repository() -> MediaCenterRepository:
    return MediaCenterRepository()


def _coordinator(repository: MediaCenterRepository | None = None) -> MediaCatalogCoordinator:
    global _coordinator_cached, _coordinator_path
    if repository is None:
        path = str(default_db_path().resolve())
        with _coordinator_lock:
            if _coordinator_cached is not None and _coordinator_path == path:
                return _coordinator_cached
        with _coordinator_init_lock:
            with _coordinator_lock:
                if _coordinator_cached is not None and _coordinator_path == path:
                    return _coordinator_cached
            repo = MediaCenterRepository(path)
            coordinator = MediaCatalogCoordinator(repo)
            with _coordinator_lock:
                _coordinator_cached = coordinator
                _coordinator_path = path
                return coordinator
    else:
        repo = repository
        path = str(repo.db_path.resolve())
    with _coordinator_lock:
        if _coordinator_cached is None or _coordinator_path != path:
            _coordinator_cached = MediaCatalogCoordinator(repo)
            _coordinator_path = path
        return _coordinator_cached


def _topology() -> Any:
    # Deployment SDK imports pull in the full topology/runtime stack. Catalog
    # reads must not pay that cost when no topology tool is being called.
    from media_center.topology import MediaCenterTopology

    return MediaCenterTopology()


def _enrichment_runtime(
    catalog: MediaCatalogCoordinator | None = None,
) -> MediaEnrichmentWorker:
    coordinator = catalog or _coordinator()
    path = str(coordinator.repository.db_path.resolve())
    settings = dict(coordinator.metadata_settings()["settings"])
    credential = _read_tmdb_credential()
    configuration = metadata_provider_configuration(
        settings, tmdb_credential_configured=bool(credential)
    )
    return background_runtime().enrichment_worker(
        path,
        lambda: MediaEnrichmentWorker(
            coordinator,
            providers=default_metadata_providers(
                settings, tmdb_credential=credential
            ),
            provider_configuration=configuration,
            publish=lambda: _publish_operation_snapshot(coordinator),
            publish_settled=lambda: _publish_library_snapshot(coordinator),
        ),
    )


def _read_tmdb_credential() -> str:
    try:
        from adaos.sdk.data.secrets import get as secret_get

        return str(secret_get(_TMDB_CREDENTIAL_SECRET, "") or "").strip()
    except Exception:
        return ""


def _run_agent_sync(
    catalog: MediaCatalogCoordinator,
    *,
    max_pages: int = 8,
    limit: int = 500,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    return background_runtime().run_agent_sync(
        lambda: _sync_agents(
            catalog,
            max_pages=max_pages,
            limit=limit,
            timeout_seconds=timeout_seconds,
        )
    )


def _agent_sync_runtime(
    catalog: MediaCatalogCoordinator | None = None,
) -> MediaAgentSyncWorker:
    coordinator = catalog or _coordinator()
    path = str(coordinator.repository.db_path.resolve())
    return background_runtime().agent_sync_worker(
        path,
        lambda: MediaAgentSyncWorker(
            lambda: _run_agent_sync(
                coordinator,
                max_pages=1,
                limit=500,
                timeout_seconds=30.0,
            ),
            publish=lambda: _publish_library_snapshot(coordinator),
        ),
    )


def _agent_sync_status() -> dict[str, Any]:
    return background_runtime().agent_sync_status()


def _event_payload(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        nested = event.get("payload")
        return dict(nested) if isinstance(nested, Mapping) else dict(event)
    nested = getattr(event, "payload", None)
    return dict(nested) if isinstance(nested, Mapping) else {}


def _compact_home_artwork(value: Any) -> dict[str, Any]:
    artwork = value if isinstance(value, Mapping) else {}
    descriptor = (
        artwork.get("descriptor")
        if isinstance(artwork.get("descriptor"), Mapping)
        else {}
    )
    compact_descriptor = {
        field: descriptor[field]
        for field in (
            "resource_id",
            "name",
            "mime_type",
            "content_path",
            "routed_content_path",
        )
        if descriptor.get(field) not in (None, "")
    }
    return {
        "state": str(artwork.get("state") or "missing"),
        "url": str(artwork.get("url") or ""),
        "descriptor": compact_descriptor,
    }


def _compact_home_item(item: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        field: item[field]
        for field in (
            "id",
            "title",
            "name",
            "media_kind",
            "kind",
            "icon",
            "favorite",
            "item_count",
            "shelf_id",
            "shelf_title",
            "queue_source_type",
            "queue_source_id",
        )
        if item.get(field) not in (None, "")
    }
    quality = item.get("quality") if isinstance(item.get("quality"), Mapping) else {}
    try:
        quality_height = max(0, int(quality.get("height") or 0))
    except (TypeError, ValueError):
        quality_height = 0
    if quality_height:
        compact["quality"] = {"height": quality_height}
    artwork = _compact_home_artwork(item.get("artwork"))
    if artwork["state"] != "missing" or artwork["url"] or artwork["descriptor"]:
        compact["artwork"] = artwork
    return compact


def _compact_library_item(item: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        field: item[field]
        for field in (
            "id",
            "resource_id",
            "title",
            "name",
            "media_kind",
            "kind",
            "icon",
            "favorite",
            "size_bytes",
            "modified_at",
            "folder_path",
            "agent_id",
            "node_id",
            "root_id",
            "work_id",
            "variant_id",
            "collection_id",
            "year",
            "release_date",
            "rating",
            "critic_rating",
            "audience_rating",
            "content_rating",
            "duration_ms",
            "genres",
            "artists",
            "album",
            "series",
            "metadata_revision",
        )
        if item.get(field) not in (None, "", [], {})
    }
    quality = item.get("quality") if isinstance(item.get("quality"), Mapping) else {}
    compact_quality = {
        field: quality[field]
        for field in ("height", "width", "bitrate", "codec", "container", "language")
        if quality.get(field) not in (None, "", 0)
    }
    if compact_quality:
        compact["quality"] = compact_quality
    artwork = _compact_home_artwork(item.get("artwork"))
    if artwork["state"] != "missing" or artwork["url"] or artwork["descriptor"]:
        compact["artwork"] = artwork
    return compact


def _compact_home_snapshot(
    home: Mapping[str, Any],
    *,
    collection_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state = dict(collection_state or {})
    return {
        "ok": bool(home.get("ok")),
        "schema": str(home.get("schema") or COORDINATOR_SCHEMA),
        "profile_id": str(home.get("profile_id") or "default"),
        "profile": dict(home.get("profile") or {}),
        "shared_surface": bool(home.get("shared_surface")),
        "state": str(state.get("state") or "loading"),
        "configured": state.get("configured"),
        "root_count": int(state.get("root_count") or 0),
        "available_count": int(state.get("available_count") or 0),
        "active_operation_count": int(state.get("active_operation_count") or 0),
        "updated_at": str(state.get("updated_at") or now_iso()),
        "items": [
            _compact_home_item(item)
            for item in home.get("items") or []
            if isinstance(item, Mapping)
        ],
    }


def _cached_home_snapshot(
    catalog: MediaCatalogCoordinator,
    *,
    profile_id: str,
    shared_surface: bool,
    catalog_revision: int,
    personal_revision: int,
    collection_state: Mapping[str, Any],
) -> dict[str, Any]:
    cache_key = (
        str(catalog.repository.db_path.resolve()),
        profile_id,
        shared_surface,
    )
    signature = (catalog_revision, personal_revision)
    now = time.monotonic()
    with _home_snapshot_cache_lock:
        cached = _HOME_SNAPSHOT_CACHE.get(cache_key)
        if (
            cached is not None
            and cached[0] == signature
            and now - cached[2] <= _HOME_SNAPSHOT_CACHE_TTL_SECONDS
        ):
            home = dict(cached[1])
            _HOME_SNAPSHOT_CACHE[cache_key] = (cached[0], cached[1], now)
        else:
            home = {}
    if not home:
        with _home_snapshot_build_lock:
            with _home_snapshot_cache_lock:
                cached = _HOME_SNAPSHOT_CACHE.get(cache_key)
                if (
                    cached is not None
                    and cached[0] == signature
                    and now - cached[2] <= _HOME_SNAPSHOT_CACHE_TTL_SECONDS
                ):
                    home = dict(cached[1])
            if not home:
                home = _compact_home_snapshot(
                    catalog.home(
                        profile_id=profile_id,
                        limit=6,
                        shared_surface=shared_surface,
                    ),
                    collection_state=collection_state,
                )
                with _home_snapshot_cache_lock:
                    _HOME_SNAPSHOT_CACHE[cache_key] = (signature, dict(home), now)
                    while len(_HOME_SNAPSHOT_CACHE) > _HOME_SNAPSHOT_CACHE_LIMIT:
                        oldest = min(
                            _HOME_SNAPSHOT_CACHE,
                            key=lambda key: _HOME_SNAPSHOT_CACHE[key][2],
                        )
                        _HOME_SNAPSHOT_CACHE.pop(oldest, None)
    state = dict(collection_state)
    home.update(
        {
            "state": str(state.get("state") or "loading"),
            "configured": state.get("configured"),
            "root_count": int(state.get("root_count") or 0),
            "available_count": int(state.get("available_count") or 0),
            "active_operation_count": int(state.get("active_operation_count") or 0),
            "updated_at": str(state.get("updated_at") or now_iso()),
        }
    )
    return home


def _publish_library_snapshot(
    catalog: MediaCatalogCoordinator,
    *,
    profile_id: str = "default",
    shared_surface: bool = False,
    webspace_id: str = "",
    reuse_ready: bool = False,
) -> bool:
    try:
        from adaos.sdk.io import stream_variable_publish

        profile = str(profile_id or "default").strip() or "default"
        surface_is_shared = _bool(shared_surface, False)
        cache_key = (
            str(catalog.repository.db_path.resolve()),
            profile,
            surface_is_shared,
        )
        snapshot: dict[str, Any] | None = None
        if reuse_ready:
            with _ready_library_snapshot_cache_lock:
                cached = _READY_LIBRARY_SNAPSHOT_CACHE.get(cache_key)
                if cached is not None:
                    snapshot = dict(cached)
        if snapshot is None:
            agent_sync = _agent_sync_status()
            collection_state = catalog.collection_state(agent_sync=agent_sync)
            catalog_revision = catalog.catalog_revision()
            personal_revision = catalog.profile_revision(profile)
            home = _cached_home_snapshot(
                catalog,
                profile_id=profile,
                shared_surface=surface_is_shared,
                catalog_revision=catalog_revision,
                personal_revision=personal_revision,
                collection_state=collection_state,
            )
            snapshot = {
                "schema": "adaos.media_center.library_state.v1",
                "profile_id": profile,
                "catalog_revision": catalog_revision,
                "personal_revision": personal_revision,
                "participation": catalog.participation(),
                "collection_state": collection_state,
                "home": home,
                "agent_sync": agent_sync,
            }
            with _ready_library_snapshot_cache_lock:
                _READY_LIBRARY_SNAPSHOT_CACHE[cache_key] = dict(snapshot)
                while len(_READY_LIBRARY_SNAPSHOT_CACHE) > _HOME_SNAPSHOT_CACHE_LIMIT:
                    _READY_LIBRARY_SNAPSHOT_CACHE.pop(
                        next(iter(_READY_LIBRARY_SNAPSHOT_CACHE)), None
                    )
        stream_variable_publish(
            "media_center.library_state",
            snapshot,
            var_id=(
                f"media_center.library.{profile}."
                f"{'shared' if surface_is_shared else 'personal'}"
            ),
            # Both revisions are monotonic but advance independently. Their sum
            # advances whenever either plane changes; max() can repeat and make
            # clients reject a fresh replacement as stale.
            seq=(
                int(snapshot["catalog_revision"])
                + int(snapshot["personal_revision"])
            ),
            ttl_ms=120000,
            _meta={
                **({"webspace_id": webspace_id} if webspace_id else {}),
                "params": {
                    "profile_id": profile,
                    "shared_surface": surface_is_shared,
                },
            },
        )
        return True
    except Exception:
        _log.exception(
            "library snapshot publish failed profile=%s shared_surface=%s webspace=%s",
            str(profile_id or "default"),
            bool(shared_surface),
            str(webspace_id or "default"),
        )
        return False


def _publish_operation_snapshot(
    catalog: MediaCatalogCoordinator,
    *,
    webspace_id: str = "",
) -> bool:
    try:
        from adaos.sdk.io import stream_variable_publish

        snapshot = catalog.operation_state(limit=30)
        snapshot["runtime"] = _enrichment_runtime(catalog).status()
        snapshot["coverage"] = catalog.metadata_coverage()
        stream_variable_publish(
            "media_center.operation_state",
            snapshot,
            var_id="media_center.operations",
            ttl_ms=120000,
            _meta={
                **({"webspace_id": webspace_id} if webspace_id else {}),
            },
        )
        return True
    except Exception:
        _log.exception(
            "operation snapshot publish failed webspace=%s",
            str(webspace_id or "default"),
        )
        return False


def _invoke_agent(operation: str, arguments: Mapping[str, Any] | None = None, *, timeout: float = 15.0) -> tuple[dict[str, Any] | None, str]:
    try:
        from adaos.sdk.skills import invoke

        result = invoke("media_library_agent", operation, dict(arguments or {}), timeout=timeout)
        if isinstance(result, Mapping):
            return dict(result), ""
        return None, "media_library_agent_invalid_response"
    except Exception as exc:
        return None, str(exc)


def _invoke_skill(
    skill_name: str,
    operation: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    timeout: float = 15.0,
) -> tuple[dict[str, Any] | None, str]:
    try:
        from adaos.sdk.skills import invoke

        result = invoke(skill_name, operation, dict(arguments or {}), timeout=timeout)
        if isinstance(result, Mapping):
            return dict(result), ""
        return None, f"{skill_name}_invalid_response"
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


def _sanitize_diagnostic(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "[bounded]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            token = str(key)
            lowered = token.lower()
            if any(
                sensitive in lowered
                for sensitive in (
                    "password",
                    "secret",
                    "token",
                    "credential",
                    "source_path",
                    "root_path",
                    "content_path",
                    "routed_path",
                    "direct_url",
                )
            ):
                result[token] = "[redacted]"
            else:
                result[token] = _sanitize_diagnostic(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_diagnostic(item, depth=depth + 1)
            for item in list(value)[:100]
        ]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:500]


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
    return {
        "ok": True,
        "schema": COORDINATOR_SCHEMA,
        "catalog_revision": catalog.catalog_revision(),
        "agent_sync": {
            "ok": True,
            "deferred": True,
            "mode": "background_cursor_catchup",
            "activation": "sys.ready",
            "worker_started": False,
            "status": {"state": "deferred", "revision": 0},
        },
        "enrichment": {
            "running": False,
            "deferred": True,
            "activation": "sys.ready",
            "worker_started": False,
        },
    }


def _sync_one_agent(
    catalog: MediaCatalogCoordinator,
    *,
    instance: Mapping[str, Any] | None,
    max_pages: int,
    limit: int,
    agent_id_hint: str = "",
    node_id_hint: str = "",
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    pages = max(1, min(16, int(max_pages or 4)))
    page_limit = max(1, min(1000, int(limit or 500)))
    instance_id = str((instance or {}).get("instance_id") or "")
    node_id = str((instance or {}).get("node_id") or node_id_hint or "")
    binding = catalog.agent_binding(instance_id) if instance_id else None
    actual_agent_id = str((binding or {}).get("agent_id") or agent_id_hint or "")
    cursor = (
        str((binding or {}).get("cursor") or "")
        if instance_id
        else catalog.agent_cursor(actual_agent_id)
    )
    page_timeout = max(1.0, min(30.0, float(timeout_seconds or 30.0)))
    applied = ignored = removed = 0
    page_limit_backoffs = 0
    for _index in range(pages):
        if instance_id:
            while True:
                try:
                    page = _topology().invoke_agent(
                        instance_id,
                        "pull_deltas",
                        {"cursor": cursor, "limit": page_limit},
                        timeout_seconds=page_timeout,
                    )
                    error = ""
                    break
                except Exception as exc:
                    page, error = None, str(exc)
                    if (
                        "service_invocation_result_too_large" not in error
                        or page_limit <= 1
                    ):
                        break
                    page_limit = max(1, page_limit // 2)
                    page_limit_backoffs += 1
        else:
            page, error = _invoke_agent(
                "pull_deltas",
                {"cursor": cursor, "limit": page_limit},
                timeout=page_timeout,
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
                "effective_page_limit": page_limit,
                "page_limit_backoffs": page_limit_backoffs,
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
                "effective_page_limit": page_limit,
                "page_limit_backoffs": page_limit_backoffs,
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
        "effective_page_limit": page_limit,
        "page_limit_backoffs": page_limit_backoffs,
    }


def _sync_agents(
    catalog: MediaCatalogCoordinator,
    *,
    max_pages: int = 4,
    limit: int = 500,
    timeout_seconds: float = 30.0,
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
                timeout_seconds=timeout_seconds,
            )
            for instance in instances
        ]
        complete = all(bool(item.get("ok")) for item in results) and not any(
            bool(item.get("has_more")) for item in results
        )
        retired_compatibility = (
            catalog.retire_unbound_agent_states(
                str(item.get("agent_id") or "") for item in results
            )
            if complete
            else {
                "ok": True,
                "retired_agent_ids": [],
                "retired_agent_count": 0,
                "retired_source_count": 0,
                "deferred": True,
                "reason": "distributed_sync_incomplete",
            }
        )
        return {
            "ok": all(bool(item.get("ok")) for item in results),
            "schema": COORDINATOR_SCHEMA,
            "mode": "distributed",
            "agents": results,
            "agent_count": len(results),
            "applied_count": sum(int(item.get("applied_count") or 0) for item in results),
            "has_more": any(bool(item.get("has_more")) for item in results),
            "retired_compatibility": retired_compatibility,
            "participation": catalog.participation(),
        }
    agent_status, status_error = _invoke_agent(
        "status", {}, timeout=min(5.0, max(1.0, float(timeout_seconds)))
    )
    agent_info = (
        agent_status.get("agent")
        if isinstance(agent_status, Mapping)
        and isinstance(agent_status.get("agent"), Mapping)
        else {}
    )
    agent_id = str(agent_info.get("id") or "")
    node_id = str(agent_info.get("node_id") or "")
    if not agent_id:
        for state in catalog.participation().get("agents") or []:
            if not str(state.get("instance_id") or ""):
                catalog.mark_agent_unavailable(
                    str(state.get("agent_id") or ""),
                    node_id=str(state.get("node_id") or ""),
                    reason=str(status_error or "agent_identity_unavailable"),
                )
        return {
            "ok": False,
            "schema": COORDINATOR_SCHEMA,
            "error": "media_library_agent_unavailable",
            "detail": str(status_error or "agent_identity_unavailable")[:1000],
            "mode": "local_compatibility",
            "topology_error": topology_error[:300],
            "agents": [],
            "agent_count": 0,
            "participation": catalog.participation(),
        }
    local = _sync_one_agent(
        catalog,
        instance=None,
        max_pages=max_pages,
        limit=limit,
        agent_id_hint=agent_id,
        node_id_hint=node_id,
        timeout_seconds=timeout_seconds,
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
    background_runtime().ensure_bootstrap_started(
        str(default_db_path().resolve()),
        _start_live_runtime,
    )


def _start_live_runtime() -> None:
    catalog = _coordinator()
    _publish_library_snapshot(catalog)
    _publish_operation_snapshot(catalog)
    _agent_sync_runtime(catalog).ensure_started()
    _enrichment_runtime(catalog).ensure_started()


@subscribe(
    "webio.stream.snapshot.requested",
    receivers=("media_center.library_state", "media_center.operation_state"),
)
def on_media_center_snapshot_requested(event: Any) -> None:
    payload = _event_payload(event)
    receiver = str(payload.get("receiver") or "")
    if receiver == "media_center.operation_state":
        _publish_operation_snapshot(
            _coordinator(),
            webspace_id=str(payload.get("webspace_id") or ""),
        )
        return
    if receiver != "media_center.library_state":
        return
    params = payload.get("params")
    receiver_params = dict(params) if isinstance(params, Mapping) else {}
    _publish_library_snapshot(
        _coordinator(),
        profile_id=str(
            receiver_params.get("profile_id")
            or payload.get("profile_id")
            or "default"
        ),
        shared_surface=_bool(
            receiver_params.get(
                "shared_surface",
                payload.get("shared_surface", False),
            ),
            False,
        ),
        webspace_id=str(payload.get("webspace_id") or ""),
        reuse_ready=True,
    )


@subscribe("media_library_agent.catalog.changed")
def on_agent_catalog_changed(event: Any) -> None:
    path = str(default_db_path().resolve())
    with _coordinator_lock:
        catalog = (
            _coordinator_cached
            if _coordinator_cached is not None and _coordinator_path == path
            else None
        )
    if catalog is None:
        background_runtime().ensure_bootstrap_started(path, _start_live_runtime)
        return
    _agent_sync_runtime(catalog).ensure_started(wake=True)


@subscribe("media_control.playback.observed")
def on_playback_observed(event: Any) -> None:
    payload = _event_payload(event)
    item_id = str(payload.get("item_id") or "").strip()
    profile_id = str(payload.get("profile_id") or "default").strip() or "default"
    if not item_id:
        return
    position_ms = max(0, int(payload.get("position_ms") or 0))
    duration_ms = max(position_ms, int(payload.get("duration_ms") or 0))
    state = str(payload.get("state") or "paused").strip().lower()
    if state not in {"playing", "paused", "stopped", "ended", "error"}:
        return
    playback_confirmed = _bool(
        payload.get("playback_confirmed"),
        position_ms > 0 or state == "ended",
    )
    if not playback_confirmed:
        return
    bucket = position_ms // 15_000
    cache_key = f"{profile_id}:{item_id}"
    with _coordinator_lock:
        previous = _PLAYBACK_OBSERVATION_CACHE.get(cache_key)
        terminal = state in {"stopped", "ended", "error"}
        if previous and previous[0] == bucket and previous[1] == state:
            return
        publish_required = bool(
            previous is None
            or previous[1] != state
            or terminal
            or bucket % 4 == 0
        )
        _PLAYBACK_OBSERVATION_CACHE[cache_key] = (bucket, state, time.monotonic())
        while len(_PLAYBACK_OBSERVATION_CACHE) > _PLAYBACK_OBSERVATION_LIMIT:
            oldest = min(
                _PLAYBACK_OBSERVATION_CACHE,
                key=lambda key: _PLAYBACK_OBSERVATION_CACHE[key][2],
            )
            _PLAYBACK_OBSERVATION_CACHE.pop(oldest, None)
    catalog = _coordinator()
    result = catalog.checkpoint(
        item_id,
        profile_id=profile_id,
        position_ms=position_ms,
        duration_ms=duration_ms,
        completed=_bool(payload.get("completed"), state == "ended"),
    )
    if result.get("ok") and publish_required:
        _publish_library_snapshot(
            catalog,
            profile_id=profile_id,
            webspace_id=str(payload.get("webspace_id") or ""),
        )


@tool(summary="Pull bounded idempotent deltas from ready library agents.", side_effects="local_write")
def sync_agent(max_pages: int = 4, limit: int = 500, **_: Any) -> dict[str, Any]:
    catalog = _coordinator()
    result = _run_agent_sync(catalog, max_pages=max_pages, limit=limit)
    _agent_sync_runtime(catalog).ensure_started()
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
    repo = _repository()
    result = repo.scan_resources(resources, source=source or "all")
    _coordinator(repo).refresh_search_index(force_legacy=True)
    return result


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
    arguments = {
        "root_id": root_id,
        "mode": "incremental",
        "webspace_id": str(_.get("webspace_id") or ""),
    }


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
    if descriptors:
        _coordinator(repo).refresh_search_index(force_legacy=True)
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
        {
            "path": path,
            "label": label,
            "include_images": _bool(include_images, False),
            "webspace_id": str(_.get("webspace_id") or ""),
        },
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
    sort_direction: str = "",
    profile_id: str = "default",
    collection_id: str = "",
    genre: str = "",
    year: int | None = None,
    rating_min: float | None = None,
    content_rating: str = "",
    auto_scan: bool = True,
    projection: str = "full",
    **_: Any,
) -> dict[str, Any]:
    repo = _repository()
    catalog = _coordinator(repo)
    scan: dict[str, Any] | None = None
    agent_sync: dict[str, Any] | None = None
    if _bool(auto_scan, True):
        if catalog.catalog_revision() <= 0:
            summary = repo.compact_summary()
            agent_sync = _run_agent_sync(
                catalog, max_pages=1, limit=500, timeout_seconds=5.0
            )
            if (
                not agent_sync.get("ok")
                and int(summary.get("total_count") or 0) == 0
            ):
                scan = scan_sources(source="all", limit=5000)
        else:
            agent_sync = _agent_sync_status()
        _agent_sync_runtime(catalog).ensure_started()
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
            sort_direction=sort_direction,
            profile_id=profile_id,
            collection_id=collection_id,
            genre=genre,
            year=year,
            rating_min=rating_min,
            content_rating=content_rating,
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
    projection_token = str(projection or "full").strip().lower()
    if projection_token == "summary":
        payload["items"] = [
            _compact_library_item(item)
            for item in payload.get("items") or []
            if isinstance(item, Mapping)
        ]
        payload["projection"] = "summary"
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


@tool(summary="Return bounded profile-aware metadata navigation facets.", side_effects="none")
def metadata_facets(
    dimension: str = "genre",
    media_kind: str = "playable",
    profile_id: str = "default",
    limit: int = 50,
    include_all: bool = False,
    **_: Any,
) -> dict[str, Any]:
    return _coordinator().metadata_facets(
        dimension=dimension,
        media_kind=media_kind,
        profile_id=profile_id,
        limit=limit,
        include_all=_bool(include_all, False),
    )


@tool(summary="Return managed external metadata provider settings.", side_effects="none")
def get_metadata_settings(**_: Any) -> dict[str, Any]:
    catalog = _coordinator()
    result = catalog.metadata_settings()
    settings = dict(result["settings"])
    credential_configured = bool(_read_tmdb_credential())
    return {
        **result,
        "settings": {
            **settings,
            "tmdb_credential_configured": credential_configured,
        },
        "providers": metadata_provider_configuration(
            settings, tmdb_credential_configured=credential_configured
        ),
    }


@tool(summary="Update managed external metadata provider settings.", side_effects="local_write")
def set_metadata_settings(
    values: Mapping[str, Any] | None = None,
    tmdb_credential: str = "",
    clear_tmdb_credential: bool = False,
    **_: Any,
) -> dict[str, Any]:
    from adaos.sdk.data.secrets import delete as secret_delete
    from adaos.sdk.data.secrets import set as secret_set

    catalog = _coordinator()
    requested = dict(values or {})
    normalized: dict[str, Any] = {}
    for key in ("external_enabled", "musicbrainz_enabled", "tmdb_enabled"):
        if key in requested and requested[key] is not None:
            normalized[key] = _bool(requested[key], False)
    if "locale" in requested and requested["locale"] is not None:
        normalized["locale"] = str(requested["locale"] or "").strip()

    credential_before = _read_tmdb_credential()
    credential_value = str(tmdb_credential or "").strip()
    credential_changed = False
    validation: dict[str, Any] = {"ok": True, "skipped": True}
    if _bool(clear_tmdb_credential, False):
        if credential_before:
            secret_delete(_TMDB_CREDENTIAL_SECRET)
            credential_changed = True
    elif credential_value:
        try:
            validation = TmdbMetadataProvider(
                credential=credential_value,
                language=str(
                    normalized.get("locale")
                    or catalog.metadata_settings()["settings"].get("locale")
                    or "ru-RU"
                ),
            ).validate()
        except MetadataProviderError as exc:
            key = f"runtime.media_center.error.{exc.code}"
            fallbacks = {
                "tmdb_authentication_failed": (
                    "TMDb rejected this API key or Read Access Token."
                ),
                "tmdb_rate_limited": (
                    "TMDb is rate limiting requests. Try again later."
                ),
                "tmdb_upstream_unavailable": (
                    "TMDb is temporarily unavailable. Try again later."
                ),
                "tmdb_request_failed": (
                    "TMDb could not be reached. Check the connection and retry."
                ),
            }
            return _skill_error(
                exc.code,
                human_message=_skill_text(
                    key, fallbacks.get(exc.code, "TMDb validation failed.")
                ),
                i18n_key=key,
                retryable=exc.retryable,
            )
        secret_set(_TMDB_CREDENTIAL_SECRET, credential_value)
        credential_changed = credential_value != credential_before

    updated = catalog.set_metadata_settings(
        normalized, force_revision=credential_changed
    )
    changed = bool(updated.get("changed"))
    reset = {"stopped": True, "skipped": True}
    requeue = {"ok": True, "queued_count": 0, "skipped": True}
    if changed:
        reset = background_runtime().reset_enrichment(timeout=30.0)
        if reset.get("stopped") is not True:
            raise RuntimeError("media_center_enrichment_restart_timeout")
        requeue = catalog.requeue_metadata_enrichment()
        _enrichment_runtime(catalog).ensure_started()
        _publish_operation_snapshot(
            catalog,
            webspace_id=str(_.get("webspace_id") or ""),
        )
    current = get_metadata_settings()
    current.update(
        {
            "changed": changed,
            "worker_restarted": changed,
            "worker_reset": reset,
            "requeue": requeue,
            "credential_validation": validation,
        }
    )
    return current


@tool(summary="List media-center catalog rows.", side_effects="none")
def list_items(**payload: Any) -> dict[str, Any]:
    return library(**payload)


@tool(
    summary="Run bounded local and federated agent search stages.",
    side_effects="none",
)
def deep_search(
    query: str = "",
    profile_id: str = "default",
    media_kind: str = "playable",
    limit: int = 30,
    max_agents: int = 4,
    **_: Any,
) -> dict[str, Any]:
    token = str(query or "").strip()
    bounded = max(1, min(30, int(limit or 30)))
    if not token:
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "query": "",
            "items": [],
            "count": 0,
            "partial": False,
            "stages": [],
            "failures": [],
        }
    catalog = _coordinator()
    local = catalog.list_items(
        query=token,
        profile_id=profile_id,
        media_kind=media_kind,
        limit=bounded,
        sort="title",
    )
    items = [
        dict(item) | {"deep_match": {"stage": "coordinator_fts"}}
        for item in local.get("items") or []
    ]
    seen = {str(item.get("source_id") or "") for item in items}
    stages: list[dict[str, Any]] = [
        {"id": "coordinator_fts", "status": "completed", "count": len(items)}
    ]
    failures: list[dict[str, Any]] = []
    if len(items) < bounded:
        discovery = catalog.discovery_search(
            token,
            profile_id=profile_id,
            media_kind=media_kind,
            limit=bounded - len(items),
        )
        added = 0
        for item in discovery.get("items") or []:
            source_id = str(item.get("source_id") or "")
            if source_id and source_id in seen:
                continue
            if source_id:
                seen.add(source_id)
            items.append(dict(item))
            added += 1
            if len(items) >= bounded:
                break
        stages.append(
            {
                "id": "coordinator_local_discovery",
                "status": "completed",
                "count": added,
                "candidate_count": discovery.get("candidate_count", 0),
                "candidate_limit": discovery.get("candidate_limit", 0),
                "truncated_candidates": bool(
                    discovery.get("truncated_candidates")
                ),
            }
        )
    agent_limit = max(1, min(16, int(max_agents or 4)))
    try:
        instances = _topology().agent_instances(limit=agent_limit)
    except Exception as exc:
        instances = []
        failures.append(
            {"stage": "agent_technical_fts", "error": str(exc)[:500]}
        )
    if not instances:
        instances = [None]
    for instance in instances[:agent_limit]:
        if len(items) >= bounded:
            break
        instance_id = str((instance or {}).get("instance_id") or "")
        try:
            if instance_id:
                page = _topology().invoke_agent(
                    instance_id,
                    "search_sources",
                    {"query": token, "limit": min(30, bounded - len(items))},
                    timeout_seconds=20.0,
                )
            else:
                page, error = _invoke_agent(
                    "search_sources",
                    {"query": token, "limit": min(30, bounded - len(items))},
                    timeout=20.0,
                )
                if page is None:
                    raise RuntimeError(error or "media_library_agent_unavailable")
        except Exception as exc:
            failures.append(
                {
                    "stage": "agent_technical_fts",
                    "instance_id": instance_id,
                    "error": str(exc)[:500],
                }
            )
            continue
        agent = (
            page.get("agent") if isinstance(page.get("agent"), Mapping) else {}
        )
        agent_id = str(agent.get("id") or "")
        resolved = catalog.resolve_agent_hits(
            page.get("items") or [],
            agent_id=agent_id,
            profile_id=profile_id,
            limit=bounded - len(items),
        )
        for item in resolved:
            source_id = str(item.get("source_id") or "")
            if source_id and source_id in seen:
                continue
            if source_id:
                seen.add(source_id)
            items.append(item)
            if len(items) >= bounded:
                break
        stages.append(
            {
                "id": "agent_technical_fts",
                "status": "completed",
                "instance_id": instance_id,
                "agent_id": agent_id,
                "count": len(resolved),
                "has_more": bool(page.get("has_more")),
            }
        )
    return {
        "ok": True,
        "schema": COORDINATOR_SCHEMA,
        "query": token,
        "items": items[:bounded],
        "count": min(len(items), bounded),
        "limit": bounded,
        "partial": bool(failures) or bool(local.get("partial")),
        "stages": stages,
        "failures": failures,
        "ranking": {
            "version": "federated-discovery-v2",
            "stage_order": [
                "coordinator_fts",
                "coordinator_local_discovery",
                "agent_technical_fts",
            ],
        },
    }


@tool(summary="Read one media-center catalog item.", side_effects="none")
def get_item(
    item_id: str = "",
    profile_id: str = "default",
    **_: Any,
) -> dict[str, Any]:
    return _coordinator().item_details(item_id, profile_id=profile_id)


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
    profile_id: str = "default",
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
        profile_id=profile_id,
    )
    if result.get("error") == "playback_source_unavailable":
        legacy = repo.playback_plan(item_id)
        if legacy.get("ok"):
            legacy["compatibility_mode"] = "legacy_catalog_row"
            return legacy
    return result


@tool(
    summary="Plan and queue a compatible rendition on the source-owning agent.",
    side_effects="local_write",
)
def ensure_rendition(
    item_id: str = "",
    endpoint_capabilities: Mapping[str, Any] | None = None,
    profile_id: str = "default",
    rendition_profile: str = "browser-mp4-v1",
    preferred_language: str = "",
    priority: int = 50,
    force: bool = False,
    **_: Any,
) -> dict[str, Any]:
    catalog = _coordinator()
    plan = catalog.playback_plan(
        item_id,
        endpoint_capabilities=endpoint_capabilities,
        profile_id=profile_id,
        preferred_language=preferred_language,
    )
    if not plan.get("ok"):
        return plan
    compatibility = dict(plan.get("compatibility") or {})
    if bool(compatibility.get("ready")) and not _bool(force, False):
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "status": "ready",
            "playback_plan": plan,
            "rendition": None,
        }
    if (
        str(compatibility.get("mode") or "") == "unsupported"
        and not _bool(force, False)
    ):
        return {
            "ok": False,
            "schema": COORDINATOR_SCHEMA,
            "status": "unsupported",
            "error": "endpoint_media_unsupported",
            "playback_plan": plan,
            "rendition": None,
        }
    binding = catalog.source_binding(
        source_id=str(plan.get("source_id") or ""), item_id=item_id
    )
    arguments = {
        "source_id": str(plan.get("source_id") or ""),
        "endpoint_capabilities": dict(endpoint_capabilities or {}),
        "profile": rendition_profile,
        "preferred_audio_language": preferred_language,
        "priority": max(0, min(1000, int(priority or 50))),
        "force": bool(force),
    }
    instance_id = str((binding or {}).get("instance_id") or "")
    try:
        if instance_id:
            rendition = _topology().invoke_agent(
                instance_id,
                "plan_rendition",
                arguments,
                timeout_seconds=20.0,
            )
        else:
            rendition, error = _invoke_agent(
                "plan_rendition", arguments, timeout=20.0
            )
            if rendition is None:
                raise RuntimeError(error or "media_library_agent_unavailable")
    except Exception as exc:
        return _skill_error(
            "rendition_agent_unavailable",
            "The source agent could not prepare a compatible media version.",
            detail=str(exc),
            retryable=True,
        )
    return {
        "ok": bool(rendition.get("ok")),
        "schema": COORDINATOR_SCHEMA,
        "status": (
            "queued" if rendition.get("asynchronous") else "source_compatible"
        ),
        "source_binding": {
            "agent_id": str((binding or {}).get("agent_id") or ""),
            "node_id": str((binding or {}).get("node_id") or ""),
            "instance_id": instance_id,
        },
        "playback_plan": plan,
        "rendition": rendition,
    }


@tool(summary="Build a bounded playback queue from a catalog source.", side_effects="none")
def build_playback_queue(
    source_type: str = "item",
    source_id: str = "",
    source_context: Mapping[str, Any] | None = None,
    profile_id: str = "default",
    limit: int = 500,
    endpoint_id: str = "",
    endpoint_node_id: str = "",
    endpoint_capabilities: Mapping[str, Any] | None = None,
    preferred_quality: str = "auto",
    preferred_language: str = "",
    start_item_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    result = _coordinator().build_queue(
        source_type=source_type,
        source_id=source_id,
        source_context=source_context,
        profile_id=profile_id,
        limit=limit,
        endpoint_id=endpoint_id,
        endpoint_node_id=endpoint_node_id,
        endpoint_capabilities=endpoint_capabilities,
        preferred_quality=preferred_quality,
        preferred_language=preferred_language,
        start_item_id=start_item_id,
    )
    if not result.get("ok"):
        return result
    settings_result, _settings_error = _invoke_skill(
        "media_control_skill",
        "get_settings",
        {"profile_id": profile_id or "default", "target_id": endpoint_id},
        timeout=3.0,
    )
    settings = dict((settings_result or {}).get("settings") or {})
    playback_control = dict(result.get("playback_control") or {})
    playback_control["settings"] = {
        "autoplay": bool(settings.get("autoplay", True)),
        "auto_fullscreen": bool(settings.get("auto_fullscreen", True)),
    }
    result["playback_control"] = playback_control
    return result


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
    summary = repo.compact_summary()
    return {
        "ok": True,
        "schema": COORDINATOR_SCHEMA,
        "summary": summary,
        "facets": repo.facets(),
        "coordinator": catalog.diagnostics(summary=summary),
        "storage": catalog.storage_status(),
        "background_jobs": {
            "counts": catalog.background_job_counts(),
            "counts_by_kind": catalog.background_job_counts_by_kind(),
        },
        "agent_sync": _agent_sync_status(),
        "runtime_bootstrap": background_runtime().bootstrap_status(),
        "enrichment": _enrichment_runtime(catalog).status(),
    }


@tool(
    summary="Export bounded sanitized Media Center diagnostics and repair proposals.",
    side_effects="none",
)
def diagnostic_export(
    deployment_id: str = "media-center-home",
    browser_diagnostics: Mapping[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    catalog = _coordinator()
    coordinator_diagnostics = catalog.diagnostics()
    components: dict[str, Any] = {"coordinator": coordinator_diagnostics}
    failures: list[dict[str, Any]] = []
    probes = (
        (
            "deployment",
            lambda: _topology().deployment_status(deployment_id, limit=30),
        ),
        ("topology", lambda: _topology().distributed_status(limit=30)),
        ("library_agent", lambda: _invoke_agent("status", {}, timeout=10.0)),
        (
            "playback_control",
            lambda: _invoke_skill("media_control_skill", "status", {}, timeout=10.0),
        ),
        (
            "playback_qoe",
            lambda: _invoke_skill(
                "media_control_skill", "qoe_summary", {"limit": 30}, timeout=10.0
            ),
        ),
    )
    for name, probe in probes:
        try:
            value = probe()
            if isinstance(value, tuple):
                payload, error = value
                if payload is None:
                    raise RuntimeError(error or f"{name}_unavailable")
                value = payload
            components[name] = value
        except Exception as exc:
            components[name] = {"status": "unavailable"}
            failures.append({"component": name, "error": str(exc)[:300]})
    if browser_diagnostics:
        components["browser"] = dict(browser_diagnostics)
    sanitized = _sanitize_diagnostic(components)
    return {
        "ok": True,
        "schema": "adaos.media_center.diagnostic_export.v1",
        "generated_at": now_iso(),
        "deployment_id": str(deployment_id or "media-center-home"),
        "components": sanitized,
        "failures": _sanitize_diagnostic(failures),
        "partial": bool(failures),
        "repair_recommendations": _sanitize_diagnostic(
            coordinator_diagnostics.get("repair_recommendations") or []
        ),
        "privacy": {
            "paths": "redacted",
            "credentials": "redacted",
            "payloads": "bounded",
            "media_bytes": "not_included",
            "automatic_repair": False,
        },
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


@tool(summary="Return one durable Media Center deployment operation.", side_effects="none")
def deployment_operation_status(operation_id: str = "", **_: Any) -> dict[str, Any]:
    try:
        return _topology().deployment_operation_status(operation_id)
    except Exception as exc:
        key = "runtime.media_center.error.deployment_status_failed"
        return _skill_error(
            "deployment_status_failed",
            human_message=_skill_text(key, "Could not read the Media Center deployment operation."),
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
    deployment_id: str = "media-center-home",
    **_: Any,
) -> dict[str, Any]:
    try:
        return _topology().define_topology(
            service_definition=service_definition or {},
            service_group=service_group or {},
            datasets=datasets or [],
            expected_group_revision=expected_group_revision,
            deployment_id=deployment_id,
        )
    except Exception as exc:
        key = "runtime.media_center.error.topology_define_failed"
        return _skill_error(
            "topology_define_failed",
            human_message=_skill_text(key, "Could not define Media Center service topology."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Admit one exact deployed library-agent activation.", side_effects="local_write")
def register_agent(
    instance: Mapping[str, Any] | None = None,
    expected_revision: int = 0,
    lease_seconds: int = 300,
    **_: Any,
) -> dict[str, Any]:
    try:
        return _topology().register_agent(
            instance or {},
            expected_revision=expected_revision,
            lease_seconds=lease_seconds,
        )
    except Exception as exc:
        key = "runtime.media_center.error.agent_registration_failed"
        return _skill_error(
            "agent_registration_failed",
            human_message=_skill_text(key, "Could not admit this Media Center agent."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Renew one admitted library-agent membership.", side_effects="local_write")
def renew_agent(
    instance_id: str = "",
    expected_revision: int = 1,
    readiness: bool = True,
    status: str = "ready",
    health: Mapping[str, Any] | None = None,
    pressure: Mapping[str, Any] | None = None,
    lease_seconds: int = 300,
    **_: Any,
) -> dict[str, Any]:
    try:
        return _topology().renew_agent(
            instance_id,
            expected_revision=expected_revision,
            readiness=readiness,
            status=status,
            health=health or {"status": "ready"},
            pressure=pressure or {"level": "normal"},
            lease_seconds=lease_seconds,
        )
    except Exception as exc:
        key = "runtime.media_center.error.agent_renewal_failed"
        return _skill_error(
            "agent_renewal_failed",
            human_message=_skill_text(key, "Could not renew this Media Center agent."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Drain one admitted library agent from distributed routes.", side_effects="local_write")
def drain_agent(
    instance_id: str = "",
    expected_revision: int = 1,
    **_: Any,
) -> dict[str, Any]:
    try:
        return _topology().drain_agent(
            instance_id,
            expected_revision=expected_revision,
        )
    except Exception as exc:
        key = "runtime.media_center.error.agent_drain_failed"
        return _skill_error(
            "agent_drain_failed",
            human_message=_skill_text(key, "Could not drain this Media Center agent."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Verify a node-local replica and commit it in the authority plane.", side_effects="remote_write")
def observe_agent_topology(
    instance_id: str = "",
    partition: Mapping[str, Any] | None = None,
    replica: Mapping[str, Any] | None = None,
    timeout_seconds: float = 60.0,
    **_: Any,
) -> dict[str, Any]:
    try:
        return _topology().observe_agent_topology(
            instance_id,
            partition=partition or {},
            replica=replica or {},
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        key = "runtime.media_center.error.agent_observation_failed"
        return _skill_error(
            "agent_observation_failed",
            human_message=_skill_text(key, "Could not verify this Media Center replica."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Create a reviewed Media Center topology-change plan.", side_effects="local_write")
def plan_topology_change(
    partition_id: str = "",
    action: str = "",
    source_instance_id: str = "",
    target_instance_id: str = "",
    replica_role: str = "follower",
    **_: Any,
) -> dict[str, Any]:
    try:
        return _topology().plan_topology_change(
            partition_id,
            action=action,
            source_instance_id=source_instance_id,
            target_instance_id=target_instance_id,
            replica_role=replica_role,
        )
    except Exception as exc:
        key = "runtime.media_center.error.topology_plan_failed"
        return _skill_error(
            "topology_plan_failed",
            human_message=_skill_text(key, "Could not create the Media Center topology plan."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Apply one explicitly reviewed Media Center topology plan.", side_effects="remote_write")
def apply_topology_change(
    plan_digest: str = "",
    idempotency_key: str = "",
    **_: Any,
) -> dict[str, Any]:
    try:
        return _topology().apply_topology_change(
            plan_digest,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        key = "runtime.media_center.error.topology_apply_failed"
        return _skill_error(
            "topology_apply_failed",
            human_message=_skill_text(key, "Could not apply the Media Center topology plan."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Read one durable Media Center topology operation.", side_effects="none")
def topology_operation_status(operation_id: str = "", **_: Any) -> dict[str, Any]:
    try:
        return _topology().topology_operation_status(operation_id)
    except Exception as exc:
        key = "runtime.media_center.error.topology_status_failed"
        return _skill_error(
            "topology_status_failed",
            human_message=_skill_text(
                key, "Could not read the Media Center topology operation."
            ),
            i18n_key=key,
            detail=str(exc)[:300],
        )
@tool(summary="Perform an explicit fenced authority handoff.", side_effects="remote_write")
def handoff_authority(
    partition_id: str = "",
    target_instance_id: str = "",
    expected_partition_revision: int = 1,
    expected_epoch: int = 0,
    operation_id: str = "",
    lease_seconds: int = 120,
    **_: Any,
) -> dict[str, Any]:
    try:
        return _topology().handoff_authority(
            partition_id,
            target_instance_id,
            expected_partition_revision=expected_partition_revision,
            expected_epoch=expected_epoch,
            operation_id=operation_id,
            lease_seconds=lease_seconds,
        )
    except Exception as exc:
        key = "runtime.media_center.error.authority_handoff_failed"
        return _skill_error(
            "authority_handoff_failed",
            human_message=_skill_text(key, "Could not hand off Media Center authority."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Return bounded generic distributed topology state for Media Center.", side_effects="none")
def topology_status(limit: int = 50, **_: Any) -> dict[str, Any]:
    return _topology().distributed_status(limit=limit)


@tool(summary="Explain route eligibility and partial participation for Media Center shards.", side_effects="none")
def explain_route(
    partition_ids: list[str] | None = None,
    dataset_id: str = "media-catalog-authority",
    **_: Any,
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            **_topology().explain_route(
                partition_ids or [],
                dataset_id=dataset_id,
            ),
        }
    except Exception as exc:
        key = "runtime.media_center.error.route_explain_failed"
        return _skill_error(
            "route_explain_failed",
            human_message=_skill_text(key, "Could not explain the Media Center route."),
            i18n_key=key,
            detail=str(exc)[:300],
        )


@tool(summary="Return bounded home shelves for one media profile.", side_effects="none")
def home(
    profile_id: str = "default",
    limit: int = 12,
    shared_surface: bool = False,
    **_: Any,
) -> dict[str, Any]:
    return _coordinator().home(
        profile_id=profile_id,
        limit=limit,
        shared_surface=_bool(shared_surface, False),
    )


@tool(summary="List bounded Media Center profiles and their policies.", side_effects="none")
def list_profiles(limit: int = 20, **_: Any) -> dict[str, Any]:
    return _coordinator().list_profiles(limit=limit)


@tool(summary="Read one Media Center profile and its policy.", side_effects="none")
def get_profile(profile_id: str = "default", **_: Any) -> dict[str, Any]:
    return _coordinator().get_profile(profile_id)


@tool(summary="Revision-safely update one Media Center profile policy.", side_effects="local_write")
def set_profile_policy(
    profile_id: str = "default",
    expected_revision: int = 1,
    values: Mapping[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    catalog = _coordinator()
    result = catalog.set_profile_policy(
        profile_id,
        expected_revision=expected_revision,
        values=values or {},
    )
    if result.get("ok"):
        _publish_library_snapshot(
            catalog,
            profile_id=profile_id,
            webspace_id=str(_.get("webspace_id") or ""),
        )
    return result


@tool(summary="Set profile-scoped rating or hidden state for one media item.", side_effects="local_write")
def set_personal_state(
    item_id: str = "",
    profile_id: str = "default",
    rating: int | None = None,
    hidden: bool | None = None,
    **_: Any,
) -> dict[str, Any]:
    catalog = _coordinator()
    result = catalog.set_personal_state(
        item_id,
        profile_id=profile_id,
        rating=rating,
        hidden=hidden,
    )
    if result.get("ok"):
        _publish_library_snapshot(
            catalog,
            profile_id=profile_id,
            webspace_id=str(_.get("webspace_id") or ""),
        )
    return result


@tool(summary="Return bounded explainable profile recommendations.", side_effects="none")
def recommendations(
    profile_id: str = "default", limit: int = 12, **_: Any
) -> dict[str, Any]:
    return _coordinator().recommendations(profile_id=profile_id, limit=limit)


def _voice_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or item.get("name") or ""),
        "media_kind": str(item.get("media_kind") or ""),
        "folder_path": str(item.get("folder_path") or ""),
        "work_id": str(item.get("work_id") or ""),
        "collection_id": str(item.get("collection_id") or ""),
        "favorite": bool(item.get("favorite")),
        "quality": dict(item.get("quality") or {}),
    }


def _compound_voice_plan(
    text: str,
    *,
    profile_id: str,
    actor_ref: str,
    room_id: str,
    target_id: str,
) -> dict[str, Any] | None:
    clauses = [
        item.strip()
        for item in re.split(
            r"\s+(?:and\s+then|and|then|и\s+затем|затем|и)\s+",
            str(text or "").strip(),
            flags=re.IGNORECASE,
        )
        if item.strip()
    ][:5]
    if len(clauses) < 2:
        return None
    steps: list[dict[str, Any]] = []
    resolved_room = str(room_id or "").strip()
    for index, clause in enumerate(clauses):
        folded = clause.casefold()
        schedule: dict[str, Any] = {"kind": "immediate"}
        clock = re.search(
            r"\b(?:after|at|после|в)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
            clause,
            flags=re.IGNORECASE,
        )
        if clock:
            hour = int(clock.group(1))
            minute = int(clock.group(2) or 0)
            meridiem = str(clock.group(3) or "").lower()
            if meridiem == "pm" and hour < 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
            if hour > 23 or minute > 59:
                return None
            schedule = {
                "kind": "local_time_not_before",
                "hour": hour,
                "minute": minute,
                "timezone_source": "target",
            }
        room_match = re.search(
            r"\b(?:in|on|в)\s+(?:the\s+)?([\w\- ]+?)$",
            clause,
            flags=re.IGNORECASE,
        )
        if room_match and not resolved_room:
            resolved_room = room_match.group(1).strip()
        step: dict[str, Any] = {
            "id": f"step-{index + 1}",
            "schedule": schedule,
            "idempotency_scope": "workflow_step",
        }
        if re.search(r"\b(play|watch|listen|включ|проигр)\w*\b", folded):
            query = re.sub(
                r"^(?:play|watch|listen(?:\s+to)?|включи(?:ть)?|проиграй)\s+",
                "",
                clause,
                flags=re.IGNORECASE,
            )
            if room_match:
                query = query[: room_match.start()].strip()
            step.update(
                {
                    "action": "resolve_and_play",
                    "query": query,
                    "requires": ["catalog_policy", "target_policy", "playback_lease"],
                }
            )
        elif any(token in folded for token in ("volume", "громк")):
            percent = re.search(r"(\d{1,3})\s*%", clause)
            step.update(
                {
                    "action": "volume",
                    "arguments": (
                        {"volume": min(1.0, int(percent.group(1)) / 100)}
                        if percent
                        else {"delta": -0.2}
                        if any(token in folded for token in ("lower", "quieter", "тише"))
                        else {"delta": 0.2}
                    ),
                    "requires": ["target_policy", "playback_lease"],
                }
            )
        elif any(token in folded for token in ("pause", "stop", "next", "previous", "пауза", "стоп", "следующ", "предыдущ")):
            action = next(
                value
                for token, value in (
                    ("pause", "pause"),
                    ("пауза", "pause"),
                    ("stop", "stop"),
                    ("стоп", "stop"),
                    ("next", "next"),
                    ("следующ", "next"),
                    ("previous", "previous"),
                    ("предыдущ", "previous"),
                )
                if token in folded
            )
            step.update(
                {
                    "action": action,
                    "arguments": {},
                    "requires": ["target_policy", "playback_lease"],
                }
            )
        else:
            return None
        steps.append(step)
    digest = hashlib.sha256(
        repr((profile_id, actor_ref, resolved_room, target_id, steps)).encode("utf-8")
    ).hexdigest()
    return {
        "ok": True,
        "schema": SCHEMA_VERSION,
        "intent": "compound_control",
        "status": "approval_required",
        "workflow": {
            "schema": "adaos.workflow.request.v1",
            "workflow_type": "media.compound_control",
            "request_digest": f"sha256:{digest}",
            "profile_id": profile_id,
            "actor_ref": actor_ref or f"profile:{profile_id}",
            "target_selector": {
                "target_id": str(target_id or ""),
                "room_id": resolved_room,
            },
            "steps": steps,
            "step_count": len(steps),
            "authority": "adaos.sdk.workflow",
            "automatic_execution": False,
            "requires_confirmation": True,
            "reconcile_on_unknown": True,
        },
        "clarification": {
            "prompt": _skill_text(
                "runtime.media_center.voice.confirm_compound",
                "Please confirm this multi-step media action.",
            ),
            "options": [
                {"id": "approve", "label": "Confirm"},
                {"id": "cancel", "label": "Cancel"},
            ],
        },
        "visual_results": steps,
    }


@tool(summary="Resolve bounded Media Center voice discovery and playback intents.", side_effects="local_write")
def voice_request(
    intent: str = "",
    text: str = "",
    query: str = "",
    profile_id: str = "default",
    room_id: str = "",
    target_id: str = "",
    item_id: str = "",
    focused_item_id: str = "",
    collection_kind: str = "",
    media_kind: str = "playable",
    action: str = "",
    arguments: Mapping[str, Any] | None = None,
    dialog_context: Mapping[str, Any] | None = None,
    actor_ref: str = "",
    limit: int = 5,
    **_: Any,
) -> dict[str, Any]:
    profile = str(profile_id or "default").strip() or "default"
    bounded = max(1, min(10, int(limit or 5)))
    raw = str(text or "").strip()
    operation = str(intent or "").strip().lower()
    if operation in {"", "compound"}:
        compound = _compound_voice_plan(
            raw,
            profile_id=profile,
            actor_ref=actor_ref,
            room_id=room_id,
            target_id=target_id,
        )
        if compound is not None:
            return compound
    catalog = _coordinator()
    transport_actions = {
        "play", "pause", "seek", "volume", "mute", "next", "previous",
        "stop", "handoff", "tracks", "rate", "sleep_timer",
    }
    if not operation:
        first = raw.casefold().split(" ", 1)[0] if raw else ""
        operation = (
            "control"
            if first in transport_actions
            else "play"
            if first in {"watch", "listen"}
            else "search"
        )
        if first in transport_actions and not action:
            action = first
    context = dict(dialog_context or {})
    selected_item = str(
        item_id
        or focused_item_id
        or context.get("item_id")
        or context.get("focused_item_id")
        or ""
    ).strip()
    resolved_query = str(query or context.get("query") or raw).strip()
    resolved_query = re.sub(
        r"^(?:play|watch|listen(?: to)?|find|search(?: for)?)\s+",
        "",
        resolved_query,
        flags=re.IGNORECASE,
    ).strip()

    if operation == "control":
        command_action = str(action or context.get("action") or "").strip().lower()
        if command_action not in transport_actions:
            return _skill_error(
                "voice_intent_invalid",
                human_message=_skill_text(
                    "runtime.media_center.voice.invalid",
                    "I could not determine the playback command.",
                ),
            )
        result, error = _invoke_skill(
            "media_control_skill",
            "voice_command",
            {
                "action": command_action,
                "profile_id": profile,
                "target_id": target_id or context.get("target_id") or "",
                "session_id": context.get("session_id") or "",
                "actor_ref": actor_ref or f"profile:{profile}",
                "arguments": dict(arguments or {}),
            },
        )
        if result is not None:
            return result | {"intent": "control", "resolved_action": command_action}
        return _skill_error(
            "media_control_unavailable",
            human_message=_skill_text(
                "runtime.media_center.voice.control_unavailable",
                "Playback control is temporarily unavailable.",
            ),
            detail=error[:300],
        )

    if operation in {"status", "library_status"}:
        return status() | {
            "intent": "status",
            "speech_text": _skill_text(
                "runtime.media_center.voice.status",
                "The media library status is ready.",
            ),
        }
    if operation in {"target", "targets", "list_targets"}:
        targets, error = _invoke_skill(
            "media_control_skill", "list_targets", {"limit": bounded}
        )
        if targets is None:
            return _skill_error(
                "media_control_unavailable",
                human_message="Playback targets are temporarily unavailable.",
                detail=error[:300],
            )
        return targets | {
            "intent": "targets",
            "visual_results": [
                {
                    "id": target.get("id"),
                    "label": target.get("label"),
                    "kind": target.get("kind"),
                    "status": target.get("status"),
                }
                for target in (targets.get("items") or [])[:bounded]
            ],
        }
    if operation in {"collection", "collections"}:
        result = catalog.collections(kind=collection_kind, limit=bounded)
        return result | {
            "intent": "collections",
            "visual_results": result["items"][:bounded],
            "speech_text": f"Found {result['count']} media collections.",
        }

    candidates: list[dict[str, Any]] = []
    if selected_item:
        page = catalog.list_items(profile_id=profile, limit=30, sort="title")
        candidates = [item for item in page["items"] if item["id"] == selected_item]
        if not candidates:
            plan = catalog.playback_plan(selected_item, profile_id=profile)
            if plan.get("ok"):
                candidates = [{"id": selected_item, "title": plan["title"]}]
            elif plan.get("error") == "playback_policy_denied":
                return plan
    elif resolved_query:
        try:
            page = catalog.list_items(
                query=resolved_query,
                media_kind=media_kind,
                profile_id=profile,
                sort="title",
                limit=bounded,
            )
        except ValueError:
            return _skill_error(
                "invalid_media_catalog_cursor",
                human_message="The media results changed. Please try again.",
            )
        candidates = page["items"]
    if operation in {"search", "browse"}:
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "intent": "search",
            "query": resolved_query,
            "items": candidates,
            "visual_results": [_voice_item(item) for item in candidates[:bounded]],
            "count": len(candidates),
            "bounded": True,
            "speech_text": f"Found {len(candidates)} media items.",
        }
    if not candidates:
        return _skill_error(
            "voice_media_not_found",
            human_message=_skill_text(
                "runtime.media_center.voice.not_found",
                "I could not find matching media.",
            ),
            query=resolved_query,
            visual_results=[],
        )
    exact = [
        item
        for item in candidates
        if str(item.get("title") or "").strip().casefold()
        == resolved_query.casefold()
    ]
    if len(exact) == 1:
        candidates = exact
    if len(candidates) > 1:
        return {
            "ok": False,
            "schema": SCHEMA_VERSION,
            "error": "voice_media_ambiguous",
            "intent": operation,
            "clarification": {
                "prompt": _skill_text(
                    "runtime.media_center.voice.which_media",
                    "Which media item should I use?",
                ),
                "options": [_voice_item(item) for item in candidates[:bounded]],
            },
            "visual_results": [_voice_item(item) for item in candidates[:bounded]],
        }
    resolved_item_id = str(candidates[0]["id"])
    if operation in {"favorite", "unfavorite"}:
        favorite = operation == "favorite"
        result = catalog.set_favorite(
            resolved_item_id, profile_id=profile, favorite=favorite
        )
        if result.get("ok"):
            _publish_library_snapshot(
                catalog,
                profile_id=profile,
                webspace_id=str(_.get("webspace_id") or ""),
            )
        return result | {
            "intent": operation,
            "visual_results": [_voice_item(candidates[0])],
        }
    if operation != "play":
        return _skill_error(
            "voice_intent_invalid",
            human_message="I could not determine the media action.",
        )

    targets, target_error = _invoke_skill(
        "media_control_skill", "list_targets", {"limit": 20}
    )
    if targets is None:
        return _skill_error(
            "media_control_unavailable",
            human_message="Playback targets are temporarily unavailable.",
            detail=target_error[:300],
        )
    target_candidates = list(targets.get("items") or [])
    requested_target = str(
        target_id or context.get("target_id") or room_id or context.get("room_id") or ""
    ).strip()
    if requested_target:
        needle = requested_target.casefold()
        target_candidates = [
            target
            for target in target_candidates
            if needle
            in {
                str(target.get("id") or "").casefold(),
                str(target.get("endpoint_id") or "").casefold(),
                str(target.get("label") or "").casefold(),
                str((target.get("capabilities") or {}).get("room_id") or "").casefold(),
            }
        ]
    if len(target_candidates) != 1:
        return {
            "ok": False,
            "schema": SCHEMA_VERSION,
            "error": "playback_target_ambiguous" if target_candidates else "playback_target_unavailable",
            "intent": "play",
            "clarification": {
                "prompt": "Which playback target should I use?",
                "options": [
                    {
                        "target_id": target.get("id"),
                        "label": target.get("label"),
                        "kind": target.get("kind"),
                    }
                    for target in target_candidates[:bounded]
                ],
            },
            "visual_results": [_voice_item(candidates[0])],
        }
    queue = catalog.build_queue(
        source_type="item",
        source_id=resolved_item_id,
        profile_id=profile,
        endpoint_id=str(target_candidates[0].get("endpoint_id") or ""),
        endpoint_node_id=str(target_candidates[0].get("node_id") or ""),
        endpoint_capabilities=dict(target_candidates[0].get("capabilities") or {}),
        limit=10,
    )
    if not queue.get("ok") or not queue.get("items"):
        return _skill_error(
            "playback_queue_empty",
            human_message="No playable source is currently available.",
        )
    created, create_error = _invoke_skill(
        "media_control_skill",
        "create_session",
        {
            "target_id": target_candidates[0]["id"],
            "profile_id": profile,
            "actor_ref": actor_ref or f"profile:{profile}",
            "queue": queue["items"],
            "route": queue["items"][0].get("route") or {},
            "queue_source": queue["source"],
            "webspace_id": str(_.get("webspace_id") or ""),
        },
    )
    if created is None:
        return _skill_error(
            "playback_session_create_failed",
            human_message="Playback could not be started.",
            detail=create_error[:300],
        )
    return created | {
        "intent": "play",
        "resolved_item": _voice_item(candidates[0]),
        "resolved_target": {
            "id": target_candidates[0].get("id"),
            "label": target_candidates[0].get("label"),
        },
        "visual_results": [_voice_item(candidates[0])],
        "speech_text": f"Playing {candidates[0].get('title') or candidates[0].get('name')}.",
    }


@tool(summary="List typed media collections through an opaque cursor.", side_effects="none")
def list_collections(kind: str = "", limit: int = 30, cursor: str = "", **_: Any) -> dict[str, Any]:
    try:
        return _coordinator().collections(kind=kind, limit=limit, cursor=cursor)
    except ValueError:
        return _skill_error("invalid_media_catalog_cursor", message="The collection page changed. Refresh the list.")


@tool(
    summary="Browse one collection, its child collections, and a bounded item page.",
    side_effects="none",
)
def collection_contents(
    collection_id: str = "",
    profile_id: str = "default",
    limit: int = 30,
    cursor: str = "",
    **_: Any,
) -> dict[str, Any]:
    try:
        return _coordinator().collection_contents(
            collection_id,
            profile_id=profile_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError:
        return _skill_error(
            "invalid_media_catalog_cursor",
            message="The collection page changed. Refresh the list.",
        )


@tool(summary="Browse bounded catalog folders through an opaque cursor.", side_effects="none")
def browse_folders(
    agent_id: str = "",
    root_id: str = "",
    parent: str = "",
    profile_id: str = "default",
    limit: int = 30,
    cursor: str = "",
    **_: Any,
) -> dict[str, Any]:
    try:
        return _coordinator().folders(
            agent_id=agent_id,
            root_id=root_id,
            parent=parent,
            profile_id=profile_id,
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
        _publish_operation_snapshot(
            catalog,
            webspace_id=str(_.get("webspace_id") or ""),
        )
    return result


@tool(
    summary="Optimize Media Center catalog storage and optionally reclaim disk space.",
    side_effects="local_write",
)
def optimize_storage(reclaim: bool = False, **_: Any) -> dict[str, Any]:
    catalog = _coordinator()
    storage = catalog.storage_status()
    logical = dict(storage.get("logical_compaction") or {})
    if _bool(reclaim, False) and logical.get("phase") != "complete":
        return _skill_error(
            "media_center_logical_compaction_in_progress",
            message=(
                "Background catalog compaction must finish before physical "
                "disk space can be reclaimed."
            ),
            retryable=True,
            storage=storage,
        )
    runtime = background_runtime()
    stopped = runtime.dispose(timeout=30.0)
    if stopped.get("stopped") is not True:
        return _skill_error(
            "media_center_storage_workers_busy",
            message="Media Center workers could not pause for storage maintenance.",
            retryable=True,
            background=stopped,
        )
    try:
        return catalog.optimize_storage(reclaim=_bool(reclaim, False))
    finally:
        runtime.ensure_bootstrap_started(
            str(default_db_path().resolve()), _start_live_runtime
        )


@tool(summary="List bounded background media operations.", side_effects="none")
def operations(limit: int = 30, **_: Any) -> dict[str, Any]:
    return _coordinator().operations(limit=limit)


@tool(summary="Stop the process-local enrichment worker.", side_effects="local_write")
def dispose(**_: Any) -> dict[str, Any]:
    global _coordinator_cached, _coordinator_path
    background = background_runtime().dispose(timeout=30.0)
    if background.get("stopped") is not True:
        raise RuntimeError("media_center_background_drain_timeout")
    with _coordinator_lock:
        _PLAYBACK_OBSERVATION_CACHE.clear()
        _coordinator_cached = None
        _coordinator_path = ""
    with _home_snapshot_cache_lock:
        _HOME_SNAPSHOT_CACHE.clear()
    with _ready_library_snapshot_cache_lock:
        _READY_LIBRARY_SNAPSHOT_CACHE.clear()
    clear_filename_evidence_cache()
    return {
        "ok": True,
        "schema": COORDINATOR_SCHEMA,
        "disposed": True,
        "background": background,
    }


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
            "id": "operations",
            "label": "Review operations",
            "reason": "Inspect agent participation, background metadata jobs, playback routes, and bounded diagnostics before changing deployment.",
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
