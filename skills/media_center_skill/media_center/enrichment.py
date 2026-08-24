from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

import requests

from .coordinator import MediaCatalogCoordinator
from .discovery import text_embedding


class MetadataProvider(Protocol):
    provider_id: str
    supported_jobs: frozenset[str]

    def claims(
        self, subject: Mapping[str, Any], *, job_kind: str
    ) -> list[dict[str, Any]]: ...

    def status(self) -> dict[str, Any]: ...


class MetadataProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DeterministicLocalProvider:
    """Cheap offline claims derived from indexed evidence, never media bytes."""

    provider_id: str = "media_center.deterministic_local.v1"
    supported_jobs: frozenset[str] = frozenset(
        {"metadata_enrichment", "technical_probe", "fingerprint", "embedding"}
    )

    def claims(
        self, subject: Mapping[str, Any], *, job_kind: str
    ) -> list[dict[str, Any]]:
        if job_kind not in self.supported_jobs:
            raise LookupError("enrichment_provider_unavailable")
        claims: list[dict[str, Any]] = []
        subject_ref = str(subject.get("subject_ref") or "")
        if job_kind == "metadata_enrichment":
            title = str(subject.get("title") or "").strip()
            if title:
                claims.append(
                    {
                        "subject_ref": subject_ref,
                        "field_name": "title",
                        "value": title,
                        "confidence": 0.6,
                    }
                )
            folder = str(subject.get("folder_path") or "").strip("/")
            segments = [part for part in folder.split("/") if part]
            if segments:
                claims.append(
                    {
                        "subject_ref": subject_ref,
                        "field_name": "folder_keywords",
                        "value": segments,
                        "confidence": 0.8,
                    }
                )
            metadata = subject.get("metadata")
            if isinstance(metadata, Mapping):
                for field in ("album", "artist", "artists", "series", "language"):
                    value = metadata.get(field)
                    if value not in (None, "", []):
                        claims.append(
                            {
                                "subject_ref": subject_ref,
                                "field_name": field,
                                "value": value,
                                "confidence": 0.9,
                            }
                        )
        elif job_kind == "technical_probe":
            metadata = subject.get("metadata")
            technical = metadata.get("technical") if isinstance(metadata, Mapping) else None
            if isinstance(technical, Mapping) and technical:
                claims.append(
                    {
                        "subject_ref": subject_ref,
                        "field_name": "technical",
                        "value": dict(technical),
                        "confidence": 1.0,
                    }
                )
        elif job_kind == "embedding":
            metadata = subject.get("metadata")
            search_parts = [
                str(subject.get("title") or ""),
                str(subject.get("name") or ""),
                str(subject.get("folder_path") or ""),
            ]
            if isinstance(metadata, Mapping):
                for field in (
                    "album",
                    "artist",
                    "artists",
                    "series",
                    "language",
                    "folder_segments",
                    "aliases",
                ):
                    value = metadata.get(field)
                    if isinstance(value, (list, tuple, set)):
                        search_parts.extend(str(item) for item in value)
                    elif value:
                        search_parts.append(str(value))
            claims.append(
                {
                    "subject_ref": subject_ref,
                    "field_name": "text_embedding_v1",
                    "value": text_embedding(" ".join(search_parts)),
                    "confidence": 1.0,
                }
            )
        else:
            descriptor = subject.get("descriptor")
            metadata = subject.get("metadata")
            technical = (
                metadata.get("technical")
                if isinstance(metadata, Mapping)
                and isinstance(metadata.get("technical"), Mapping)
                else {}
            )
            perceptual_hash = str(technical.get("perceptual_hash") or "").strip()
            if perceptual_hash:
                claims.append(
                    {
                        "subject_ref": subject_ref,
                        "field_name": "perceptual_hash_v1",
                        "value": perceptual_hash,
                        "confidence": 0.95,
                    }
                )
            fingerprint = (
                descriptor.get("fingerprint")
                if isinstance(descriptor, Mapping)
                else None
            )
            fingerprint = fingerprint or subject.get("fingerprint")
            if fingerprint:
                claims.append(
                    {
                        "subject_ref": subject_ref,
                        "field_name": "fingerprint",
                        "value": fingerprint,
                        "confidence": 1.0,
                    }
                )
        return claims

    def status(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "kind": "local",
            "enabled": True,
            "state": "ready",
            "privacy": "indexed_evidence_only",
        }


_TITLE_NOISE_RE = re.compile(
    r"\b(?:2160p|1080p|720p|480p|uhd|hdr10?|bluray|bdrip|brrip|webrip|web[ ._-]?dl|"
    r"dvdrip|hdtv|x26[45]|h\.?(?:264|265)|hevc|avc|remux)\b.*$",
    re.IGNORECASE,
)
_EPISODE_RE = re.compile(r"\bS\d{1,2}E\d{1,3}\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")


def _enabled(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _external_subject(subject: Mapping[str, Any]) -> dict[str, Any]:
    metadata = subject.get("metadata")
    evidence = dict(metadata) if isinstance(metadata, Mapping) else {}
    raw_title = str(subject.get("title") or subject.get("name") or "").strip()
    title = re.sub(r"\.(?:mkv|mp4|m4v|avi|mov|webm)$", "", raw_title, flags=re.I)
    title = _EPISODE_RE.sub(" ", title)
    title = _TITLE_NOISE_RE.sub("", title)
    title = re.sub(r"[._]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -[]()")
    year_value = evidence.get("year") or evidence.get("release_year")
    year_match = _YEAR_RE.search(str(year_value or raw_title))
    year = int(year_match.group(1)) if year_match else None
    if year:
        title = _YEAR_RE.sub(" ", title)
        title = re.sub(r"\s+", " ", title).strip(" -[]()")
    series = bool(
        evidence.get("series")
        or evidence.get("season")
        or _EPISODE_RE.search(raw_title)
    )
    return {
        "subject_ref": str(subject.get("subject_ref") or ""),
        "title": title[:300],
        "year": year,
        "media_kind": str(subject.get("media_kind") or ""),
        "tmdb_kind": "tv" if series else "movie",
    }


class TmdbMetadataProvider:
    provider_id = "media_center.tmdb.v1"
    supported_jobs = frozenset({"metadata_enrichment"})

    def __init__(
        self,
        *,
        read_access_token: str,
        language: str = "en-US",
        api_base: str = "https://api.themoviedb.org/3",
        timeout_seconds: float = 10.0,
        minimum_interval_seconds: float = 0.25,
        cache_ttl_seconds: float = 86400.0,
        cache_limit: int = 1000,
        session: requests.Session | None = None,
    ) -> None:
        token = str(read_access_token or "").strip()
        if not token:
            raise ValueError("tmdb_read_access_token_required")
        self._token = token
        self.language = str(language or "en-US")[:20]
        self.api_base = str(api_base or "https://api.themoviedb.org/3").rstrip("/")
        self.timeout_seconds = max(2.0, min(float(timeout_seconds), 30.0))
        self.minimum_interval_seconds = max(
            0.1, min(float(minimum_interval_seconds), 5.0)
        )
        self.cache_ttl_seconds = max(60.0, min(float(cache_ttl_seconds), 604800.0))
        self.cache_limit = max(16, min(int(cache_limit), 5000))
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._last_request_monotonic = 0.0
        self._cache: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = (
            OrderedDict()
        )
        self._requests = 0
        self._cache_hits = 0
        self._failures = 0
        self._last_error = ""
        self._last_success_at = 0.0

    def _search(self, evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = str(evidence.get("tmdb_kind") or "movie")
        title = str(evidence.get("title") or "").strip()
        if not title:
            return []
        year = evidence.get("year")
        cache_key = f"{kind}|{self.language}|{year or ''}|{title.casefold()}"
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] <= self.cache_ttl_seconds:
                self._cache.move_to_end(cache_key)
                self._cache_hits += 1
                return list(cached[1])
            wait = self.minimum_interval_seconds - (
                now - self._last_request_monotonic
            )
            if wait > 0:
                time.sleep(wait)
            self._last_request_monotonic = time.monotonic()
        params: dict[str, Any] = {
            "query": title,
            "language": self.language,
            "include_adult": "false",
            "page": 1,
        }
        if year:
            params["first_air_date_year" if kind == "tv" else "year"] = int(year)
        try:
            response = self._session.get(
                f"{self.api_base}/search/{kind}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=self.timeout_seconds,
            )
            self._requests += 1
            if response.status_code in {401, 403}:
                raise MetadataProviderError("tmdb_authentication_failed", retryable=False)
            if response.status_code == 429:
                raise MetadataProviderError("tmdb_rate_limited", retryable=True)
            if response.status_code >= 500:
                raise MetadataProviderError("tmdb_upstream_unavailable", retryable=True)
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") if isinstance(payload, Mapping) else []
            bounded = [dict(item) for item in list(results or [])[:3] if isinstance(item, Mapping)]
        except MetadataProviderError as exc:
            self._failures += 1
            self._last_error = exc.code
            raise
        except (requests.RequestException, ValueError, TypeError) as exc:
            self._failures += 1
            self._last_error = type(exc).__name__
            raise MetadataProviderError("tmdb_request_failed", retryable=True) from exc
        with self._lock:
            self._cache[cache_key] = (time.monotonic(), bounded)
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self.cache_limit:
                self._cache.popitem(last=False)
            self._last_error = ""
            self._last_success_at = time.time()
        return bounded

    def claims(
        self, subject: Mapping[str, Any], *, job_kind: str
    ) -> list[dict[str, Any]]:
        if job_kind not in self.supported_jobs:
            raise LookupError("enrichment_provider_unavailable")
        evidence = _external_subject(subject)
        if evidence["media_kind"] != "video":
            return []
        results = self._search(evidence)
        if not results:
            return []
        result = results[0]
        subject_ref = str(evidence["subject_ref"])
        fields = {
            "tmdb_id": result.get("id"),
            "title": result.get("title") or result.get("name"),
            "original_title": result.get("original_title")
            or result.get("original_name"),
            "overview": result.get("overview"),
            "release_date": result.get("release_date")
            or result.get("first_air_date"),
            "poster_path": result.get("poster_path"),
            "backdrop_path": result.get("backdrop_path"),
            "popularity": result.get("popularity"),
            "vote_average": result.get("vote_average"),
            "external_media_kind": str(evidence["tmdb_kind"]),
        }
        return [
            {
                "subject_ref": subject_ref,
                "field_name": field_name,
                "value": value,
                "confidence": 0.85 if field_name == "title" else 0.8,
            }
            for field_name, value in fields.items()
            if value not in (None, "", [])
        ][:20]

    def status(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "kind": "external",
            "enabled": True,
            "state": "degraded" if self._last_error else "ready",
            "language": self.language,
            "privacy": "normalized_title_year_kind_only",
            "request_count": self._requests,
            "cache_hit_count": self._cache_hits,
            "failure_count": self._failures,
            "cache_entries": len(self._cache),
            "last_error": self._last_error,
            "last_success_at": self._last_success_at or None,
        }


def default_metadata_providers() -> tuple[MetadataProvider, ...]:
    providers: list[MetadataProvider] = [DeterministicLocalProvider()]
    token = str(os.environ.get("MEDIA_CENTER_TMDB_READ_ACCESS_TOKEN") or "").strip()
    if _enabled(os.environ.get("MEDIA_CENTER_METADATA_EXTERNAL_ENABLED")) and token:
        providers.append(
            TmdbMetadataProvider(
                read_access_token=token,
                language=str(os.environ.get("MEDIA_CENTER_METADATA_LOCALE") or "en-US"),
            )
        )
    return tuple(providers)


class MediaEnrichmentWorker:
    def __init__(
        self,
        coordinator: MediaCatalogCoordinator,
        *,
        providers: tuple[MetadataProvider, ...] | None = None,
        publish: Callable[[], Any] | None = None,
        publish_settled: Callable[[], Any] | None = None,
        poll_seconds: float = 2.0,
        work_interval_seconds: float = 0.2,
        publish_interval_seconds: float = 2.0,
        maintenance_interval_jobs: int = 1000,
    ):
        self.coordinator = coordinator
        self.providers = providers or default_metadata_providers()
        self.publish = publish
        self.publish_settled = publish_settled
        self.poll_seconds = max(0.2, min(float(poll_seconds), 30.0))
        self.work_interval_seconds = max(
            0.02, min(float(work_interval_seconds), 5.0)
        )
        self.publish_interval_seconds = max(
            1.0, min(float(publish_interval_seconds), 300.0)
        )
        self.maintenance_interval_jobs = max(
            100, min(int(maintenance_interval_jobs), 10000)
        )
        self._last_publish_monotonic = 0.0
        self._completed_since_maintenance = 0
        self._worked_since_idle = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def ensure_started(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return False
            self._stop.clear()
            self.coordinator.recover_stale_background_jobs()
            self.coordinator.prune_terminal_background_jobs()
            self._thread = threading.Thread(
                target=self._loop,
                name="media-center-enrichment",
                daemon=True,
            )
            self._thread.start()
        return True

    def dispose(self, *, timeout: float = 30.0) -> dict[str, Any]:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return {"stopped": stopped, "worker": "enrichment"}

    def run_once(self) -> dict[str, Any] | None:
        job = self.coordinator.claim_background_job()
        if job is None:
            return None
        self._worked_since_idle = True
        job_id = str(job["id"])
        kind = str(job["kind"])
        subject = self.coordinator.enrichment_subject(str(job["subject_ref"]))
        if subject is None:
            return self.coordinator.fail_background_job(
                job_id, error_code="enrichment_subject_not_found", retryable=False
            )
        providers = [item for item in self.providers if kind in item.supported_jobs]
        if not providers:
            return self.coordinator.fail_background_job(
                job_id, error_code="enrichment_provider_unavailable", retryable=False
            )
        claim_count = 0
        provider_ids: list[str] = []
        provider_error: MetadataProviderError | None = None
        try:
            for provider in providers:
                try:
                    claims = provider.claims(subject, job_kind=kind)
                    for claim in claims[:100]:
                        self.coordinator.record_metadata_claim(
                            subject_ref=str(
                                claim.get("subject_ref") or job["subject_ref"]
                            ),
                            field_name=str(claim.get("field_name") or ""),
                            value=claim.get("value"),
                            provenance=provider.provider_id,
                            confidence=float(claim.get("confidence") or 0),
                            preferred=bool(claim.get("preferred")),
                        )
                    claim_count += len(claims[:100])
                    provider_ids.append(provider.provider_id)
                except MetadataProviderError as exc:
                    provider_error = exc
                    break
            if provider_error is not None:
                result = self.coordinator.fail_background_job(
                    job_id,
                    error_code=provider_error.code,
                    retryable=provider_error.retryable,
                )
            else:
                result = self.coordinator.finish_background_job(
                    job_id,
                    provider_id=",".join(provider_ids),
                    claim_count=claim_count,
                )
        except LookupError as exc:
            result = self.coordinator.fail_background_job(
                job_id, error_code=str(exc), retryable=False
            )
        except Exception:
            result = self.coordinator.fail_background_job(
                job_id, error_code="enrichment_provider_failed", retryable=True
            )
        self._completed_since_maintenance += 1
        if self._completed_since_maintenance >= self.maintenance_interval_jobs:
            try:
                self.coordinator.prune_terminal_background_jobs()
            finally:
                self._completed_since_maintenance = 0
        publish_at = time.monotonic()
        if self.publish and (
            self._last_publish_monotonic == 0.0
            or publish_at - self._last_publish_monotonic
            >= self.publish_interval_seconds
        ):
            try:
                self.publish()
                self._last_publish_monotonic = publish_at
            except Exception:
                pass
        return result

    def status(self) -> dict[str, Any]:
        thread = self._thread
        providers = []
        for provider in self.providers:
            status = getattr(provider, "status", None)
            providers.append(
                dict(status())
                if callable(status)
                else {
                    "provider_id": provider.provider_id,
                    "enabled": True,
                    "state": "unknown",
                }
            )
        return {
            "schema": "adaos.media_center.enrichment_runtime.v1",
            "state": "running" if thread is not None and thread.is_alive() else "idle",
            "providers": providers,
            "poll_seconds": self.poll_seconds,
            "work_interval_seconds": self.work_interval_seconds,
            "publish_interval_seconds": self.publish_interval_seconds,
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            result = self.run_once()
            if result is None:
                if self._worked_since_idle:
                    if self.publish:
                        try:
                            self.publish()
                        except Exception:
                            pass
                    settled_published = True
                    if self.publish_settled:
                        try:
                            settled_published = self.publish_settled() is not False
                        except Exception:
                            settled_published = False
                    if settled_published:
                        self._worked_since_idle = False
                self._wake.wait(self.poll_seconds)
                self._wake.clear()
            else:
                self._stop.wait(self.work_interval_seconds)


__all__ = [
    "DeterministicLocalProvider",
    "MetadataProviderError",
    "MediaEnrichmentWorker",
    "MetadataProvider",
    "TmdbMetadataProvider",
    "default_metadata_providers",
]
