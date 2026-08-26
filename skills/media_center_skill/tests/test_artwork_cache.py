from __future__ import annotations

import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from media_center.artwork_cache import ExternalArtworkCache  # noqa: E402
from media_center.enrichment import MediaEnrichmentWorker  # noqa: E402


class _Response:
    url = "https://image.tmdb.org/t/p/original/poster.jpg"
    headers = {"Content-Type": "image/jpeg", "Content-Length": "256"}
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, *, chunk_size):
        assert chunk_size == 64 * 1024
        yield b"\xff\xd8\xff" + b"x" * 253


class _Session:
    def __init__(self):
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return _Response()


def test_external_artwork_cache_registers_content_once(tmp_path):
    session = _Session()
    registrations = []

    def register(path, **kwargs):
        registrations.append((Path(path), kwargs))
        return {
            "resource_id": "ref-poster",
            "name": Path(path).name,
            "mime_type": kwargs["mime"],
            "routed_content_path": "/media/references/ref-poster/content",
            "size_bytes": Path(path).stat().st_size,
        }

    cache = ExternalArtworkCache(tmp_path, register=register, session=session)
    subject = {"fingerprint": "source-fingerprint", "metadata": {}}
    candidate = {
        "kind": "poster",
        "url": "https://image.tmdb.org/t/p/original/poster.jpg",
    }

    first = cache.cache(subject, candidate, provider_id="tmdb")
    second = cache.cache(subject, candidate, provider_id="tmdb")

    assert first["descriptor"]["resource_id"] == "ref-poster"
    assert second["descriptor"]["resource_id"] == "ref-poster"
    assert session.calls == 1
    assert len(registrations) == 2
    assert cache.status()["cache_hit_count"] == 1
    assert registrations[0][1]["metadata"]["storage_mode"] == "node_cache"


def test_enrichment_worker_projects_cached_artwork_set(tmp_path):
    claims = []

    class Coordinator:
        def storage_maintenance_active(self):
            return False

        def compact_storage_batch(self, *, limit):
            assert limit == 1
            return {"complete": True}

        def claim_background_job(self):
            return {
                "id": "job-1",
                "kind": "metadata_enrichment",
                "subject_ref": "item:item-1",
            }

        def enrichment_subject(self, _subject_ref):
            return {
                "subject_ref": "item:item-1",
                "media_kind": "video",
                "fingerprint": "fingerprint-1",
                "metadata": {},
            }

        def record_metadata_claim(self, **claim):
            claims.append(claim)
            return {"ok": True}

        def finish_background_job(self, *_args, **kwargs):
            return {"ok": True, "provider_id": kwargs["provider_id"]}

    class Provider:
        provider_id = "tmdb"
        supported_jobs = frozenset({"metadata_enrichment"})

        def accepts(self, *_args, **_kwargs):
            return True

        def claims(self, subject, *, job_kind):
            assert subject["subject_ref"] == "item:item-1"
            assert job_kind == "metadata_enrichment"
            return [
                {
                    "subject_ref": "item:item-1",
                    "field_name": "artwork_candidates",
                    "value": [
                        {
                            "kind": "poster",
                            "url": "https://image.tmdb.org/t/p/original/poster.jpg",
                        }
                    ],
                    "confidence": 0.9,
                }
            ]

    class Cache:
        def cache(self, _subject, candidate, *, provider_id):
            return {
                "state": "ready",
                "kind": candidate["kind"],
                "provider_id": provider_id,
                "source_kind": "external_cached",
                "descriptor": {"resource_id": "ref-poster"},
            }

        def status(self):
            return {"state": "ready"}

    worker = MediaEnrichmentWorker(
        Coordinator(), providers=(Provider(),), artwork_cache=Cache()
    )

    result = worker.run_once()

    assert result["ok"] is True
    by_field = {claim["field_name"]: claim for claim in claims}
    assert by_field["artwork"]["value"]["descriptor"]["resource_id"] == "ref-poster"
    assert len(by_field["artwork_set"]["value"]) == 1
