from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .coordinator import MediaCatalogCoordinator


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
        {"metadata_enrichment", "technical_probe", "fingerprint"}
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
        else:
            descriptor = subject.get("descriptor")
            fingerprint = (
                descriptor.get("fingerprint")
                if isinstance(descriptor, Mapping)
                else None
            )
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
        publish: Callable[[], None] | None = None,
        poll_seconds: float = 2.0,
    ):
        self.coordinator = coordinator
        self.providers = providers or (DeterministicLocalProvider(),)
        self.publish = publish
        self.poll_seconds = max(0.2, min(float(poll_seconds), 30.0))
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
            self._thread = threading.Thread(
                target=self._loop,
                name="media-center-enrichment",
                daemon=True,
            )
            self._thread.start()
        return True

    def dispose(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def run_once(self) -> dict[str, Any] | None:
        job = self.coordinator.claim_background_job()
        if job is None:
            return None
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
        if self.publish:
            try:
                self.publish()
            except Exception:
                pass
        return result

    def _loop(self) -> None:
        while not self._stop.is_set():
            result = self.run_once()
            if result is None:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()


__all__ = [
    "DeterministicLocalProvider",
    "MediaEnrichmentWorker",
    "MetadataProvider",
]
