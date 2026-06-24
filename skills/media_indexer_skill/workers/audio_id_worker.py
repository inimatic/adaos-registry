from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("media_indexer.audio_id_worker")


def _cache_key(path: Path) -> str:
    stat = path.stat()
    return f"{path.resolve()}|{int(stat.st_size)}|{int(stat.st_mtime)}"


def _load_cache(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(path: Path, cache: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _track_payload(response: Dict[str, Any]) -> Dict[str, Any]:
    track = response.get("track") if isinstance(response.get("track"), dict) else {}
    if not track:
        return {}
    payload = {
        "shazam_title": track.get("title"),
        "shazam_subtitle": track.get("subtitle"),
        "shazam_genre": (track.get("genres") or {}).get("primary") if isinstance(track.get("genres"), dict) else None,
        "shazam_url": track.get("url"),
    }
    return {key: value for key, value in payload.items() if value}


async def _recognize_one(shazam: Any, path: Path, *, timeout: float) -> Dict[str, Any]:
    async def _call() -> Dict[str, Any]:
        if hasattr(shazam, "recognize"):
            return await shazam.recognize(str(path))
        if hasattr(shazam, "recognize_song"):
            return await shazam.recognize_song(str(path))
        raise RuntimeError("unsupported_shazamio_api")

    response = await asyncio.wait_for(_call(), timeout=timeout)
    return _track_payload(response if isinstance(response, dict) else {})


async def _run(request: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import static_ffmpeg

        add_paths = getattr(static_ffmpeg, "add_paths", None)
        if callable(add_paths):
            add_paths()
    except Exception:
        pass

    from shazamio import Shazam

    raw_files = request.get("files") if isinstance(request.get("files"), list) else []
    cache_path = Path(str(request.get("cache_path") or "audio_id_cache.json"))
    per_file_timeout = max(5.0, float(request.get("per_file_timeout_sec") or 30))
    max_files = max(1, int(request.get("max_files") or 20))
    cache = _load_cache(cache_path)
    shazam = Shazam()
    results: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for raw in raw_files[:max_files]:
        path = Path(str(raw or ""))
        key = str(path)
        try:
            if not path.exists() or not path.is_file():
                errors[key] = "missing_file"
                continue
            cache_key = _cache_key(path)
            cached = cache.get(cache_key)
            if isinstance(cached, dict):
                results[key] = {**cached, "cached": True}
                continue
            payload = await _recognize_one(shazam, path, timeout=per_file_timeout)
            cache[cache_key] = payload
            results[key] = payload
        except Exception as exc:  # pragma: no cover - network/tool dependent
            logger.warning("Audio ID failed for %s: %s", path, exc)
            errors[key] = f"{type(exc).__name__}: {exc}"
            cache_key = ""
            try:
                cache_key = _cache_key(path)
            except Exception:
                pass
            if cache_key:
                cache[cache_key] = {}
    _save_cache(cache_path, cache)
    return {"ok": True, "results": results, "errors": errors, "processed": len(results) + len(errors)}


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    total_timeout = max(10.0, float(request.get("total_timeout_sec") or os.getenv("MEDIA_INDEXER_AUDIO_ID_TOTAL_TIMEOUT_SEC") or 240))
    try:
        result = asyncio.run(asyncio.wait_for(_run(request), timeout=total_timeout))
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "results": {}, "errors": {}}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
