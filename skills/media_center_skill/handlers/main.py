from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from media_center.catalog import MediaCenterRepository, SCHEMA_VERSION


def _repository() -> MediaCenterRepository:
    return MediaCenterRepository()


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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

