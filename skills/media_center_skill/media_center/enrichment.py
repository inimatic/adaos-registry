from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

import requests

from .coordinator import MediaCatalogCoordinator
from .discovery import fold_text, semantic_embedding


_log = logging.getLogger("adaos.skill.media_center.enrichment")


class MetadataProvider(Protocol):
    provider_id: str
    supported_jobs: frozenset[str]

    def claims(
        self, subject: Mapping[str, Any], *, job_kind: str
    ) -> list[dict[str, Any]]: ...

    def accepts(self, subject: Mapping[str, Any], *, job_kind: str) -> bool: ...

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

    def accepts(self, subject: Mapping[str, Any], *, job_kind: str) -> bool:
        return job_kind in self.supported_jobs

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
            vector, backend = semantic_embedding(" ".join(search_parts))
            claims.append(
                {
                    "subject_ref": subject_ref,
                    "field_name": "semantic_embedding_v1",
                    "value": vector,
                    "confidence": 1.0,
                }
            )
            claims.append(
                {
                    "subject_ref": subject_ref,
                    "field_name": "semantic_embedding_backend",
                    "value": backend,
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
    title = re.sub(
        r"\.(?:aac|aiff?|alac|ape|avi|flac|m4[av]|mka|mkv|mov|mp[234]|mpeg|mpg|ogg|opus|wav|webm|wma|wmv)$",
        "",
        raw_title,
        flags=re.I,
    )
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
        "artists": evidence.get("artists") or evidence.get("artist") or [],
        "album": str(evidence.get("album") or "")[:300],
        "external_ids": dict(evidence.get("external_ids") or {})
        if isinstance(evidence.get("external_ids"), Mapping)
        else {},
    }


class TmdbMetadataProvider:
    provider_id = "media_center.tmdb.v1"
    supported_jobs = frozenset({"metadata_enrichment"})

    def accepts(self, subject: Mapping[str, Any], *, job_kind: str) -> bool:
        return (
            job_kind in self.supported_jobs
            and str(subject.get("media_kind") or "").strip().lower() == "video"
        )

    def __init__(
        self,
        *,
        credential: str,
        language: str = "en-US",
        api_base: str = "https://api.themoviedb.org/3",
        timeout_seconds: float = 10.0,
        minimum_interval_seconds: float = 0.25,
        cache_ttl_seconds: float = 86400.0,
        cache_limit: int = 1000,
        session: requests.Session | None = None,
    ) -> None:
        value = str(credential or "").strip()
        if not value:
            raise ValueError("tmdb_credential_required")
        self._credential = value
        self._credential_kind = (
            "api_key" if re.fullmatch(r"[0-9a-fA-F]{32}", value) else "bearer"
        )
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
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._requests = 0
        self._cache_hits = 0
        self._failures = 0
        self._last_error = ""
        self._last_success_at = 0.0

    def _request_json(
        self, path: str, *, params: Mapping[str, Any], cache_key: str
    ) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] <= self.cache_ttl_seconds:
                self._cache.move_to_end(cache_key)
                self._cache_hits += 1
                return dict(cached[1])
            wait = self.minimum_interval_seconds - (
                now - self._last_request_monotonic
            )
            if wait > 0:
                time.sleep(wait)
            self._last_request_monotonic = time.monotonic()
        try:
            request_params = dict(params)
            headers = {"Accept": "application/json"}
            if self._credential_kind == "api_key":
                request_params["api_key"] = self._credential
            else:
                headers["Authorization"] = f"Bearer {self._credential}"
            response = self._session.get(
                f"{self.api_base}/{path.lstrip('/')}",
                headers=headers,
                params=request_params,
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
            bounded = dict(payload) if isinstance(payload, Mapping) else {}
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

    def _search(self, evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = str(evidence.get("tmdb_kind") or "movie")
        title = str(evidence.get("title") or "").strip()
        if not title:
            return []
        year = evidence.get("year")
        params: dict[str, Any] = {
            "query": title,
            "language": self.language,
            "include_adult": "false",
            "page": 1,
        }
        if year:
            params["first_air_date_year" if kind == "tv" else "year"] = int(year)
        payload = self._request_json(
            f"search/{kind}",
            params=params,
            cache_key=f"search|{kind}|{self.language}|{year or ''}|{fold_text(title)}",
        )
        results = payload.get("results") if isinstance(payload, Mapping) else []
        return [
            dict(item)
            for item in list(results or [])[:10]
            if isinstance(item, Mapping)
        ]

    def _details(self, kind: str, external_id: Any) -> dict[str, Any]:
        return self._request_json(
            f"{kind}/{int(external_id)}",
            params={
                "language": self.language,
                "append_to_response": (
                    "credits,videos,images,external_ids,alternative_titles,"
                    + ("content_ratings" if kind == "tv" else "release_dates")
                ),
                "include_image_language": f"{self.language.split('-', 1)[0]},en,null",
            },
            cache_key=f"details|{kind}|{self.language}|{int(external_id)}",
        )

    def validate(self) -> dict[str, Any]:
        payload = self._request_json(
            "authentication",
            params={},
            cache_key="credential-validation",
        )
        if payload.get("success") is False:
            raise MetadataProviderError(
                "tmdb_authentication_failed", retryable=False
            )
        return {
            "ok": True,
            "provider_id": self.provider_id,
            "state": "ready",
            "credential_kind": self._credential_kind,
        }

    @staticmethod
    def _match_score(result: Mapping[str, Any], evidence: Mapping[str, Any]) -> float:
        expected = fold_text(evidence.get("title"))
        actual = fold_text(result.get("title") or result.get("name"))
        original = fold_text(
            result.get("original_title") or result.get("original_name")
        )
        score = 0.0
        if expected and expected in {actual, original}:
            score += 5.0
        elif expected and (expected in actual or actual in expected):
            score += 2.0
        expected_year = evidence.get("year")
        date = str(result.get("release_date") or result.get("first_air_date") or "")
        if expected_year and date.startswith(str(expected_year)):
            score += 3.0
        score += min(1.0, float(result.get("popularity") or 0.0) / 100.0)
        return score

    def claims(
        self, subject: Mapping[str, Any], *, job_kind: str
    ) -> list[dict[str, Any]]:
        if job_kind not in self.supported_jobs:
            raise LookupError("enrichment_provider_unavailable")
        evidence = _external_subject(subject)
        if evidence["media_kind"] != "video":
            return []
        exact_id = evidence["external_ids"].get("tmdb")
        if exact_id:
            selected = {"id": exact_id}
            confidence = 0.99
        else:
            results = self._search(evidence)
            if not results:
                return []
            selected = max(results, key=lambda item: self._match_score(item, evidence))
            confidence = min(0.95, 0.7 + self._match_score(selected, evidence) / 20.0)
        kind = str(evidence["tmdb_kind"])
        details = self._details(kind, selected.get("id"))
        result = {**selected, **details}
        subject_ref = str(evidence["subject_ref"])
        genres = [
            str(item.get("name"))
            for item in list(result.get("genres") or [])[:30]
            if isinstance(item, Mapping) and item.get("name")
        ]
        credits = result.get("credits") if isinstance(result.get("credits"), Mapping) else {}
        actors = [
            {
                "name": str(item.get("name") or "")[:200],
                "role": str(item.get("character") or "")[:200],
                "order": int(item.get("order") or 0),
                "tmdb_id": item.get("id"),
            }
            for item in list(credits.get("cast") or [])[:20]
            if isinstance(item, Mapping) and item.get("name")
        ]
        directors = [
            str(item.get("name"))
            for item in list(credits.get("crew") or [])[:100]
            if isinstance(item, Mapping)
            and str(item.get("job") or "").lower() in {"director", "series director"}
            and item.get("name")
        ][:20]
        videos = result.get("videos") if isinstance(result.get("videos"), Mapping) else {}
        trailers = [
            {
                "name": str(item.get("name") or "")[:200],
                "provider": "youtube",
                "key": str(item.get("key") or "")[:100],
                "official": bool(item.get("official")),
            }
            for item in list(videos.get("results") or [])[:30]
            if isinstance(item, Mapping)
            and str(item.get("site") or "").lower() == "youtube"
            and str(item.get("type") or "").lower() in {"trailer", "teaser"}
            and item.get("key")
        ][:10]
        artwork_candidates = []
        for artwork_kind, path_key in (("poster", "poster_path"), ("backdrop", "backdrop_path")):
            path = str(result.get(path_key) or "")
            if path.startswith("/"):
                artwork_candidates.append(
                    {
                        "kind": artwork_kind,
                        "url": f"https://image.tmdb.org/t/p/original{path}",
                        "provider": "tmdb",
                        "language": self.language,
                    }
                )
        external_ids = {
            key.removesuffix("_id"): value
            for key, value in dict(result.get("external_ids") or {}).items()
            if key.endswith("_id") and value not in (None, "")
        }
        external_ids["tmdb"] = result.get("id")
        content_rating = ""
        ratings_key = "content_ratings" if kind == "tv" else "release_dates"
        ratings = result.get(ratings_key) if isinstance(result.get(ratings_key), Mapping) else {}
        for entry in list(ratings.get("results") or [])[:50]:
            if not isinstance(entry, Mapping) or str(entry.get("iso_3166_1") or "") not in {"US", "RU"}:
                continue
            if kind == "tv":
                content_rating = str(entry.get("rating") or "")
            else:
                dates = entry.get("release_dates") or []
                content_rating = next(
                    (str(value.get("certification") or "") for value in dates if isinstance(value, Mapping) and value.get("certification")),
                    "",
                )
            if content_rating:
                break
        fields = {
            "tmdb_id": result.get("id"),
            "title": result.get("title") or result.get("name"),
            "original_title": result.get("original_title")
            or result.get("original_name"),
            "overview": result.get("overview"),
            "release_date": result.get("release_date")
            or result.get("first_air_date"),
            "genres": genres,
            "runtime_minutes": result.get("runtime") or next(iter(result.get("episode_run_time") or []), None),
            "content_rating": content_rating,
            "actors": actors,
            "directors": directors,
            "trailers": trailers,
            "artwork_candidates": artwork_candidates,
            "external_ids": external_ids,
            "popularity": result.get("popularity"),
            "vote_average": result.get("vote_average"),
            "vote_count": result.get("vote_count"),
            "tagline": result.get("tagline"),
            "status": result.get("status"),
            "external_media_kind": str(evidence["tmdb_kind"]),
        }
        return [
            {
                "subject_ref": subject_ref,
                "field_name": field_name,
                "value": value,
                "confidence": confidence if field_name == "title" else max(0.7, confidence - 0.05),
            }
            for field_name, value in fields.items()
            if value not in (None, "", [])
        ][:100]

    def status(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "kind": "external",
            "enabled": True,
            "state": "degraded" if self._last_error else "ready",
            "language": self.language,
            "privacy": "normalized_title_year_kind_only",
            "credential_kind": self._credential_kind,
            "request_count": self._requests,
            "cache_hit_count": self._cache_hits,
            "failure_count": self._failures,
            "cache_entries": len(self._cache),
            "last_error": self._last_error,
            "last_success_at": self._last_success_at or None,
        }


class MusicBrainzMetadataProvider:
    provider_id = "media_center.musicbrainz.v1"
    supported_jobs = frozenset({"metadata_enrichment"})

    def accepts(self, subject: Mapping[str, Any], *, job_kind: str) -> bool:
        return (
            job_kind in self.supported_jobs
            and str(subject.get("media_kind") or "").strip().lower() == "audio"
        )

    def __init__(
        self,
        *,
        api_base: str = "https://musicbrainz.org/ws/2",
        timeout_seconds: float = 10.0,
        minimum_interval_seconds: float = 1.05,
        cache_ttl_seconds: float = 86400.0,
        session: requests.Session | None = None,
    ) -> None:
        self.api_base = str(api_base).rstrip("/")
        self.timeout_seconds = max(2.0, min(float(timeout_seconds), 30.0))
        self.minimum_interval_seconds = max(1.0, float(minimum_interval_seconds))
        self.cache_ttl_seconds = max(60.0, min(float(cache_ttl_seconds), 604800.0))
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._last_request_monotonic = 0.0
        self._cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._requests = 0
        self._cache_hits = 0
        self._failures = 0
        self._last_error = ""
        self._last_success_at = 0.0

    def _request(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        cache_key = f"{path}|{sorted(dict(params).items())!r}"
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] <= self.cache_ttl_seconds:
                self._cache_hits += 1
                return dict(cached[1])
            wait = self.minimum_interval_seconds - (now - self._last_request_monotonic)
            if wait > 0:
                time.sleep(wait)
            self._last_request_monotonic = time.monotonic()
        try:
            response = self._session.get(
                f"{self.api_base}/{path.lstrip('/')}",
                params=dict(params) | {"fmt": "json"},
                headers={
                    "Accept": "application/json",
                    "User-Agent": "AdaOS-MediaCenter/1.0 (https://inimatic.com)",
                },
                timeout=self.timeout_seconds,
            )
            self._requests += 1
            if response.status_code == 429:
                raise MetadataProviderError("musicbrainz_rate_limited")
            if response.status_code >= 500:
                raise MetadataProviderError("musicbrainz_upstream_unavailable")
            response.raise_for_status()
            payload = response.json()
            result = dict(payload) if isinstance(payload, Mapping) else {}
        except MetadataProviderError as exc:
            self._failures += 1
            self._last_error = exc.code
            raise
        except (requests.RequestException, ValueError, TypeError) as exc:
            self._failures += 1
            self._last_error = type(exc).__name__
            raise MetadataProviderError("musicbrainz_request_failed") from exc
        self._cache[cache_key] = (time.monotonic(), result)
        while len(self._cache) > 1000:
            self._cache.popitem(last=False)
        self._last_error = ""
        self._last_success_at = time.time()
        return result

    def claims(
        self, subject: Mapping[str, Any], *, job_kind: str
    ) -> list[dict[str, Any]]:
        if job_kind not in self.supported_jobs:
            raise LookupError("enrichment_provider_unavailable")
        evidence = _external_subject(subject)
        if evidence["media_kind"] != "audio":
            return []
        external_ids = evidence["external_ids"]
        recording_id = external_ids.get("musicbrainz_recording") or external_ids.get("musicbrainz")
        if not recording_id and not re.search(
            r"[^\W\d_]", str(evidence.get("title") or ""), flags=re.UNICODE
        ):
            return []
        if recording_id:
            recording = self._request(
                f"recording/{recording_id}",
                {"inc": "artists+releases+genres+tags"},
            )
            confidence = 0.99
        else:
            query = f'recording:"{str(evidence.get("title") or "")[:200]}"'
            artists = evidence.get("artists") or []
            if isinstance(artists, str):
                artists = [artists]
            if artists:
                query += f' AND artist:"{str(artists[0])[:200]}"'
            if evidence.get("album"):
                query += f' AND release:"{str(evidence["album"])[:200]}"'
            payload = self._request("recording", {"query": query, "limit": 5})
            candidates = [
                item for item in list(payload.get("recordings") or [])[:5]
                if isinstance(item, Mapping)
            ]
            if not candidates:
                return []
            expected = fold_text(evidence.get("title"))
            recording = max(
                candidates,
                key=lambda item: (
                    int(fold_text(item.get("title")) == expected),
                    int(item.get("score") or 0),
                ),
            )
            confidence = min(0.95, max(0.6, float(recording.get("score") or 60) / 100.0))
        artist_credit = recording.get("artist-credit") or []
        artists = [
            str((item.get("artist") or {}).get("name") or item.get("name") or "")
            for item in list(artist_credit)[:20]
            if isinstance(item, Mapping)
        ]
        releases = [
            item for item in list(recording.get("releases") or [])[:20]
            if isinstance(item, Mapping)
        ]
        ranked_genres = [
            (str(item.get("name") or "").strip(), int(item.get("count") or 0))
            for item in [
                *list(recording.get("genres") or [])[:30],
                *list(recording.get("tags") or [])[:50],
            ]
            if isinstance(item, Mapping) and str(item.get("name") or "").strip()
        ]
        genres = []
        seen_genres: set[str] = set()
        for name, _count in sorted(
            ranked_genres, key=lambda value: (-value[1], value[0].casefold())
        ):
            normalized = fold_text(name)
            if not normalized or normalized in seen_genres:
                continue
            seen_genres.add(normalized)
            genres.append(name)
            if len(genres) >= 12:
                break
        fields = {
            "title": recording.get("title"),
            "artists": [value for value in artists if value],
            "album": releases[0].get("title") if releases else evidence.get("album"),
            "release_date": releases[0].get("date") if releases else "",
            "genres": genres,
            "external_ids": {
                **external_ids,
                "musicbrainz_recording": recording.get("id"),
                **(
                    {"musicbrainz_release": releases[0].get("id")}
                    if releases else {}
                ),
            },
            "artwork_candidates": (
                [{
                    "kind": "cover",
                    "url": f"https://coverartarchive.org/release/{releases[0].get('id')}/front-500",
                    "provider": "cover_art_archive",
                }]
                if releases and releases[0].get("id") else []
            ),
        }
        return [
            {
                "subject_ref": str(evidence["subject_ref"]),
                "field_name": field,
                "value": value,
                "confidence": confidence,
            }
            for field, value in fields.items()
            if value not in (None, "", [], {})
        ][:100]

    def status(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "kind": "external",
            "enabled": True,
            "state": "degraded" if self._last_error else "ready",
            "privacy": "normalized_audio_tags_only",
            "request_count": self._requests,
            "cache_hit_count": self._cache_hits,
            "failure_count": self._failures,
            "last_error": self._last_error,
            "last_success_at": self._last_success_at or None,
        }


def metadata_provider_configuration(
    settings: Mapping[str, Any] | None = None,
    *,
    tmdb_credential_configured: bool = False,
) -> list[dict[str, Any]]:
    values = dict(settings or {})
    external_enabled = _enabled(values.get("external_enabled"))
    tmdb_enabled = external_enabled and _enabled(values.get("tmdb_enabled"))
    musicbrainz_enabled = external_enabled and _enabled(
        values.get("musicbrainz_enabled")
    )
    tmdb_ready = tmdb_enabled and bool(tmdb_credential_configured)
    return [
        {
            "provider_id": "media_center.deterministic_local.v1",
            "kind": "local",
            "enabled": True,
            "state": "ready",
            "reason": "built_in",
            "privacy": "indexed_evidence_only",
        },
        {
            "provider_id": "media_center.tmdb.v1",
            "kind": "external",
            "enabled": tmdb_enabled,
            "ready": tmdb_ready,
            "state": (
                "ready"
                if tmdb_ready
                else "credentials_missing" if tmdb_enabled else "disabled"
            ),
            "reason": (
                "configured"
                if tmdb_ready
                else (
                    "credentials_missing"
                    if tmdb_enabled
                    else (
                        "provider_disabled"
                        if external_enabled
                        else "external_metadata_disabled"
                    )
                )
            ),
            "language": str(values.get("locale") or "ru-RU"),
            "privacy": "normalized_title_year_kind_only",
        },
        {
            "provider_id": "media_center.musicbrainz.v1",
            "kind": "external",
            "enabled": musicbrainz_enabled,
            "state": "ready" if musicbrainz_enabled else "disabled",
            "reason": (
                "configured"
                if musicbrainz_enabled
                else (
                    "provider_disabled"
                    if external_enabled
                    else "external_metadata_disabled"
                )
            ),
            "privacy": "normalized_audio_tags_only",
        },
    ]


def default_metadata_providers(
    settings: Mapping[str, Any] | None = None,
    *,
    tmdb_credential: str = "",
) -> tuple[MetadataProvider, ...]:
    values = dict(settings or {})
    external_enabled = _enabled(values.get("external_enabled"))
    providers: list[MetadataProvider] = [DeterministicLocalProvider()]
    credential = str(tmdb_credential or "").strip()
    if external_enabled and _enabled(values.get("tmdb_enabled")) and credential:
        providers.append(
            TmdbMetadataProvider(
                credential=credential,
                language=str(values.get("locale") or "ru-RU"),
            )
        )
    if external_enabled and _enabled(values.get("musicbrainz_enabled")):
        providers.append(MusicBrainzMetadataProvider())
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
        maintenance_interval_jobs: int = 100,
        provider_configuration: Iterable[Mapping[str, Any]] | None = None,
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
        self.provider_configuration = tuple(
            dict(item) for item in (provider_configuration or ())
        )
        self._last_publish_monotonic = 0.0
        self._completed_since_maintenance = 0
        self._worked_since_idle = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._loop_failure_count = 0
        self._last_loop_error = ""
        self._last_loop_error_at = 0.0
        self._storage_maintenance_complete = False
        self._storage_maintenance_state: dict[str, Any] = {}

    def ensure_started(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return False
            self._stop.clear()
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
        maintenance_active = getattr(
            self.coordinator, "storage_maintenance_active", None
        )
        if callable(maintenance_active) and maintenance_active():
            return None
        maintenance = getattr(self.coordinator, "compact_storage_batch", None)
        if callable(maintenance) and not self._storage_maintenance_complete:
            compacted = maintenance(limit=250)
            if isinstance(compacted, Mapping):
                self._storage_maintenance_state = dict(compacted)
                self._storage_maintenance_complete = bool(
                    compacted.get("complete")
                )
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
        providers = []
        for item in self.providers:
            if kind not in item.supported_jobs:
                continue
            accepts = getattr(item, "accepts", None)
            if callable(accepts) and not accepts(subject, job_kind=kind):
                continue
            providers.append(item)
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
                self.coordinator.prune_terminal_background_jobs(batch_size=5000)
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
        with self._lock:
            thread = self._thread
            loop_failure_count = self._loop_failure_count
            last_loop_error = self._last_loop_error
            last_loop_error_at = self._last_loop_error_at
            storage_maintenance = dict(self._storage_maintenance_state)
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
        active_provider_ids = {
            str(item.get("provider_id") or "") for item in providers
        }
        configured = self.provider_configuration or tuple(
            metadata_provider_configuration()
        )
        providers.extend(
            status
            for status in configured
            if str(status.get("provider_id") or "") not in active_provider_ids
        )
        return {
            "schema": "adaos.media_center.enrichment_runtime.v1",
            "state": "running" if thread is not None and thread.is_alive() else "idle",
            "providers": providers,
            "poll_seconds": self.poll_seconds,
            "work_interval_seconds": self.work_interval_seconds,
            "publish_interval_seconds": self.publish_interval_seconds,
            "loop_failure_count": loop_failure_count,
            "last_error": last_loop_error,
            "last_error_at": last_loop_error_at,
            "storage_maintenance": storage_maintenance,
        }

    def _loop(self) -> None:
        try:
            self.coordinator.recover_stale_background_jobs()
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                result = self.run_once()
            except Exception as exc:
                with self._lock:
                    self._loop_failure_count += 1
                    failure_count = self._loop_failure_count
                    self._last_loop_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                    self._last_loop_error_at = time.time()
                if failure_count == 1 or failure_count % 30 == 0:
                    _log.warning(
                        "media enrichment loop retry failure_count=%s error=%s",
                        failure_count,
                        self._last_loop_error,
                    )
                self._wake.wait(min(5.0, self.poll_seconds))
                self._wake.clear()
                continue
            with self._lock:
                self._last_loop_error = ""
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
