"""Research-domain contracts owned by the research manager skill."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    encoded = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def identity(kind: str, value: Mapping[str, Any]) -> str:
    return f"{kind}.{digest({'kind': kind, 'value': dict(value)}).split(':', 1)[1]}"


@dataclass(frozen=True, slots=True)
class ResearchRecord:
    SCHEMA: ClassVar[str] = "adaos.research.record.v1"

    kind: str
    record_id: str
    study_id: str
    generation: int
    payload: Mapping[str, Any]
    created_at: str = field(default_factory=now)

    def __post_init__(self) -> None:
        if not self.kind or not self.record_id or not self.study_id:
            raise ValueError("research record identity fields are required")
        if int(self.generation) < 0:
            raise ValueError("generation must be >= 0")
        object.__setattr__(self, "payload", dict(self.payload))

    @property
    def digest(self) -> str:
        return digest(
            {
                "schema": self.SCHEMA,
                "kind": self.kind,
                "record_id": self.record_id,
                "study_id": self.study_id,
                "generation": self.generation,
                "payload": self.payload,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "schema": self.SCHEMA, "digest": self.digest}


ENTITY_KINDS = (
    "study",
    "hypothesis",
    "protocol",
    "analysis_plan",
    "experiment",
    "experiment_revision",
    "experiment_result",
    "trial_group",
    "trial",
    "run",
    "attempt_binding",
    "execution_attempt",
    "observation",
    "artifact_ref",
    "evidence_bundle",
    "claim_decision",
)

IDENTITY_INPUTS = {
    "protocol": ("study_id", "version", "content_digest"),
    "analysis_plan": ("study_id", "version", "content_digest"),
    "experiment": ("study_id", "slug"),
    "experiment_revision": ("experiment_id", "revision", "conditions_digest"),
    "dataset": ("content_digest",),
    "split": ("dataset_digest", "role", "indices_digest"),
    "operator": ("package_digest", "entrypoint", "configuration_digest"),
    "trial": ("study_id", "protocol_digest", "analysis_plan_digest", "operator_digest", "pair_key"),
    "run": ("trial_id", "sample_generation", "rng_digest"),
    "attempt": ("run_id", "attempt_number", "execution_spec_digest"),
    "evidence": ("study_id", "manifest_digest"),
}


def validate_identity_inputs(kind: str, value: Mapping[str, Any]) -> None:
    required = IDENTITY_INPUTS.get(kind)
    if required is None:
        raise ValueError(f"unsupported identity kind: {kind}")
    missing = [item for item in required if value.get(item) in (None, "")]
    if missing:
        raise ValueError(f"{kind} identity is missing: {', '.join(missing)}")


__all__ = [
    "ENTITY_KINDS",
    "IDENTITY_INPUTS",
    "ResearchRecord",
    "canonical_json",
    "digest",
    "identity",
    "now",
    "validate_identity_inputs",
]
