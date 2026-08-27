from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests


RegisterCallback = Callable[..., Mapping[str, Any]]
_ALLOWED_HOSTS = frozenset(
    {
        "image.tmdb.org",
        "coverartarchive.org",
        "covers.openlibrary.org",
        "archive.org",
    }
)
_MIME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class ArtworkCacheError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _allowed_host(value: str) -> bool:
    host = str(urlparse(value).hostname or "").lower()
    return bool(
        host in _ALLOWED_HOSTS
        or host.endswith(".archive.org")
        or host.endswith(".us.archive.org")
    )


def _detected_mime(payload: bytes, declared: str) -> str:
    token = str(declared or "").split(";", 1)[0].strip().lower()
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    if token in _MIME_SUFFIXES:
        return token
    raise ArtworkCacheError("artwork_cache_invalid_image")


class ExternalArtworkCache:
    """Bounded external artwork cache published through the AdaOS media plane."""

    def __init__(
        self,
        root: str | Path,
        *,
        register: RegisterCallback | None = None,
        session: requests.Session | None = None,
        timeout_seconds: float = 20.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._register = register or self._register_media_file
        self._session = session or requests.Session()
        self.timeout_seconds = max(2.0, min(float(timeout_seconds), 60.0))
        self.max_bytes = max(64 * 1024, min(int(max_bytes), 32 * 1024 * 1024))
        self._lock = threading.Lock()
        self._attempt_count = 0
        self._cache_hit_count = 0
        self._ready_count = 0
        self._failure_count = 0
        self._stored_bytes = 0
        self._last_error = ""

    @staticmethod
    def _register_media_file(path: Path, **kwargs: Any) -> Mapping[str, Any]:
        from adaos.sdk.io.media import register_media_file

        return register_media_file(path, **kwargs)

    def cache(
        self,
        subject: Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        provider_id: str,
    ) -> dict[str, Any]:
        url = str(candidate.get("url") or "").strip()
        if not url or not _allowed_host(url):
            raise ArtworkCacheError("artwork_cache_source_not_allowed")
        kind = str(candidate.get("kind") or "poster").strip().lower() or "poster"
        url_digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        with self._lock:
            self._attempt_count += 1
        existing = next(
            (
                path
                for path in self.root.glob(f"{url_digest}.*")
                if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ),
            None,
        )
        mime_type = ""
        if existing is not None and existing.is_file():
            mime_type = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
            }.get(existing.suffix.lower(), "")
            if mime_type:
                with self._lock:
                    self._cache_hit_count += 1
        if not mime_type:
            existing, mime_type = self._download(url, url_digest)
        descriptor = dict(
            self._register(
                existing,
                root=self.root,
                content_ref=f"media-center-artwork:{url_digest}",
                namespace="media-center-artwork-cache",
                mime=mime_type,
                metadata={
                    "namespace": "media-center-artwork-cache",
                    "provider_id": str(provider_id),
                    "artwork_kind": kind,
                    "source_fingerprint": str(subject.get("fingerprint") or "")[:128],
                    "storage_mode": "node_cache",
                },
            )
        )
        with self._lock:
            self._ready_count += 1
            self._last_error = ""
        return {
            "schema": "adaos.media.artwork.v1",
            "state": "ready",
            "descriptor": descriptor,
            "provider_id": str(provider_id),
            "source_kind": "external_cached",
            "exact_source_revision": int(
                (subject.get("metadata") or {}).get("source_revision") or 0
            )
            if isinstance(subject.get("metadata"), Mapping)
            else 0,
            "exact_source_fingerprint": str(subject.get("fingerprint") or "")[:128],
            "kind": kind,
            "width": max(0, int(candidate.get("width") or 0)),
            "height": max(0, int(candidate.get("height") or 0)),
            "storage_mode": "node_cache",
        }

    def _download(self, url: str, digest: str) -> tuple[Path, str]:
        partial = self.root / f"{digest}.partial"
        try:
            with self._session.get(
                url,
                headers={
                    "Accept": "image/avif,image/webp,image/png,image/jpeg;q=0.9,*/*;q=0.1",
                    "User-Agent": "AdaOS-MediaCenter/1.0 (https://inimatic.com)",
                },
                timeout=self.timeout_seconds,
                stream=True,
                allow_redirects=True,
            ) as response:
                if not _allowed_host(str(response.url or url)):
                    raise ArtworkCacheError("artwork_cache_redirect_not_allowed")
                response.raise_for_status()
                declared_length = int(response.headers.get("Content-Length") or 0)
                if declared_length > self.max_bytes:
                    raise ArtworkCacheError("artwork_cache_too_large")
                payload = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    payload.extend(chunk)
                    if len(payload) > self.max_bytes:
                        raise ArtworkCacheError("artwork_cache_too_large")
            mime_type = _detected_mime(
                bytes(payload[:32]), str(response.headers.get("Content-Type") or "")
            )
            if len(payload) < 128:
                raise ArtworkCacheError("artwork_cache_invalid_image")
            target = self.root / f"{digest}{_MIME_SUFFIXES[mime_type]}"
            partial.write_bytes(payload)
            os.replace(partial, target)
            with self._lock:
                self._stored_bytes += len(payload)
            return target, mime_type
        except ArtworkCacheError as exc:
            self._failed(exc.code)
            raise
        except requests.RequestException as exc:
            self._failed("artwork_cache_request_failed")
            raise ArtworkCacheError("artwork_cache_request_failed") from exc
        except OSError as exc:
            self._failed("artwork_cache_storage_failed")
            raise ArtworkCacheError("artwork_cache_storage_failed") from exc
        finally:
            partial.unlink(missing_ok=True)

    def _failed(self, code: str) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_error = str(code)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "adaos.media_center.artwork_cache_status.v1",
                "state": "degraded" if self._last_error else "ready",
                "attempt_count": self._attempt_count,
                "cache_hit_count": self._cache_hit_count,
                "ready_count": self._ready_count,
                "failure_count": self._failure_count,
                "stored_bytes": self._stored_bytes,
                "last_error": self._last_error,
                "max_object_bytes": self.max_bytes,
            }


__all__ = ["ArtworkCacheError", "ExternalArtworkCache"]
