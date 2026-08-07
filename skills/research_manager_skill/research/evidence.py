"""Portable content-addressed evidence manifests and deterministic verification."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from research.contracts import ResearchRecord, canonical_json, digest, identity, now
from research.repository import ResearchRepository


def _root() -> Path:
    env_path = str(os.getenv("ADAOS_SKILL_ENV_PATH") or "").strip()
    if not env_path:
        raise RuntimeError("skill runtime data path is unavailable")
    root = Path(env_path).resolve().parent.parent / "files" / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _manifest(repository: ResearchRepository, study_id: str) -> dict[str, Any]:
    records = [
        item
        for item in repository.list(study_id)
        if item.kind not in {"evidence_bundle", "claim_decision"}
    ]
    refs = [
        {
            "uri": f"research-record:{item.kind}/{item.record_id}",
            "digest": item.digest,
            "size_bytes": len(canonical_json(item.to_dict()).encode("utf-8")),
            "media_type": "application/vnd.adaos.research-record+json",
            "kind": item.kind,
            "record_id": item.record_id,
        }
        for item in records
    ]
    return {
        "schema": "adaos.research.evidence_manifest.v1",
        "study_id": study_id,
        "content_refs": refs,
        "verification": {"operation": "research_manager_skill.verify_evidence", "algorithm": "sha256-canonical-json-v1"},
    }


def export(repository: ResearchRepository, study_id: str) -> dict[str, Any]:
    existing = repository.list(study_id, "evidence_bundle")
    if existing:
        return existing[-1].to_dict()
    manifest = _manifest(repository, study_id)
    manifest["manifest_digest"] = digest(manifest)
    payload = canonical_json(manifest).encode("utf-8")
    bundle_id = identity("evidence", {"study_id": study_id, "manifest_digest": manifest["manifest_digest"]})
    path = _root() / f"{bundle_id}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    record = repository.put(
        ResearchRecord(
            kind="evidence_bundle",
            record_id=bundle_id,
            study_id=study_id,
            generation=0,
            payload={
                "schema": "adaos.research.evidence_bundle.v1",
                "manifest_digest": manifest["manifest_digest"],
                "content_ref": {
                    "uri": f"skill-data:files/evidence/{bundle_id}.json",
                    "digest": digest(payload),
                    "size_bytes": len(payload),
                    "media_type": "application/vnd.adaos.research-evidence+json",
                },
                "finalized_at": now(),
            },
        )
    )
    return record.to_dict()


def verify(repository: ResearchRepository, bundle_id: str) -> dict[str, Any]:
    bundle = repository.get("evidence_bundle", bundle_id)
    if bundle is None:
        raise KeyError(bundle_id)
    path = _root() / f"{bundle_id}.json"
    payload = path.read_bytes()
    content = dict(bundle.payload["content_ref"])
    errors: list[str] = []
    if digest(payload) != content["digest"]:
        errors.append("bundle_content_digest_mismatch")
    manifest = json.loads(payload.decode("utf-8"))
    claimed = str(manifest.pop("manifest_digest", ""))
    if digest(manifest) != claimed or claimed != bundle.payload["manifest_digest"]:
        errors.append("manifest_digest_mismatch")
    for ref in manifest.get("content_refs") or []:
        record = repository.get(str(ref["kind"]), str(ref["record_id"]))
        if record is None:
            errors.append(f"missing:{ref['kind']}:{ref['record_id']}")
        elif record.digest != ref["digest"]:
            errors.append(f"digest:{ref['kind']}:{ref['record_id']}")
    return {
        "schema": "adaos.research.evidence_verification.v1",
        "bundle_id": bundle_id,
        "ok": not errors,
        "manifest_digest": claimed,
        "checked_refs": len(manifest.get("content_refs") or []),
        "errors": errors,
    }


__all__ = ["export", "verify"]
