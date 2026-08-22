from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .coordinator import MediaCatalogCoordinator
from .discovery import text_embedding


class MetadataProvider(Protocol):
    provider_id: str
    supported_jobs: frozenset[str]

    def claims(
        self, subject: Mapping[str, Any], *, job_kind: str
    ) -> list[dict[str, Any]]: ...


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
        self.providers = providers or (DeterministicLocalProvider(),)
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
        provider = next(
            (item for item in self.providers if kind in item.supported_jobs), None
        )
        if provider is None:
            return self.coordinator.fail_background_job(
                job_id, error_code="enrichment_provider_unavailable", retryable=False
            )
        try:
            claims = provider.claims(subject, job_kind=kind)
            for claim in claims[:100]:
                self.coordinator.record_metadata_claim(
                    subject_ref=str(claim.get("subject_ref") or job["subject_ref"]),
                    field_name=str(claim.get("field_name") or ""),
                    value=claim.get("value"),
                    provenance=provider.provider_id,
                    confidence=float(claim.get("confidence") or 0),
                    preferred=bool(claim.get("preferred")),
                )
            result = self.coordinator.finish_background_job(
                job_id,
                provider_id=provider.provider_id,
                claim_count=len(claims[:100]),
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
    "MediaEnrichmentWorker",
    "MetadataProvider",
]
