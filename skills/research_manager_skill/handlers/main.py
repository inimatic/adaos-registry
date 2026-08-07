"""AdaOS tool entrypoints for the research manager."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from adaos.sdk.core.decorators import tool

_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from research.manager import ResearchManager


def _manager() -> ResearchManager:
    return ResearchManager()


@tool("ensure_schema")
def ensure_schema() -> dict[str, Any]:
    manager = _manager()
    return {"ok": True, "binding": manager.repository._db.binding.to_dict(), "health": dict(manager.repository._db.health())}


@tool("rehydrate")
def rehydrate() -> dict[str, Any]:
    return ensure_schema()


@tool("create_study")
def create_study(
    title: str,
    hypothesis: str,
    protocol: Mapping[str, Any],
    analysis_plan: Mapping[str, Any],
    splits: Mapping[str, Mapping[str, Any]],
    idempotency_key: str,
    mode: str = "confirmatory",
    study_id: str | None = None,
) -> dict[str, Any]:
    return _manager().create_study(
        title=title,
        hypothesis=hypothesis,
        protocol=protocol,
        analysis_plan=analysis_plan,
        splits=splits,
        mode=mode,
        study_id=study_id,
        idempotency_key=idempotency_key,
    )


@tool("advance_workflow")
def advance_workflow(
    study_id: str,
    command: str,
    expected_generation: int,
    idempotency_key: str,
    actor: str = "user:local",
    evidence_refs: Sequence[str] = (),
) -> dict[str, Any]:
    return _manager().advance(
        study_id=study_id,
        command=command,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key,
        actor=actor,
        evidence_refs=evidence_refs,
    )


@tool("amend_protocol")
def amend_protocol(
    study_id: str,
    content: Mapping[str, Any],
    reason: str,
    prior_trials: str,
    expected_generation: int,
    idempotency_key: str,
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().amend_protocol(
        study_id=study_id,
        content=content,
        reason=reason,
        prior_trials=prior_trials,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key,
        actor=actor,
    )


@tool("materialize_trials")
def materialize_trials(
    study_id: str,
    matrix: Sequence[Mapping[str, Any]],
    idempotency_key: str,
) -> dict[str, Any]:
    return _manager().materialize_trials(study_id=study_id, matrix=matrix, idempotency_key=idempotency_key)


@tool("run_fixture")
def run_fixture(
    study_id: str,
    trial_id: str,
    split_role: str,
    seed: int,
    idempotency_key: str,
) -> dict[str, Any]:
    return _manager().run_fixture(
        study_id=study_id,
        trial_id=trial_id,
        split_role=split_role,
        seed=seed,
        idempotency_key=idempotency_key,
    )


@tool("unblind_test")
def unblind_test(
    study_id: str,
    expected_generation: int,
    idempotency_key: str,
    reason: str,
    evidence_refs: Sequence[str],
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().unblind_test(
        study_id=study_id,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key,
        actor=actor,
        reason=reason,
        evidence_refs=evidence_refs,
    )


@tool("export_evidence")
def export_evidence(study_id: str) -> dict[str, Any]:
    return _manager().export_evidence(study_id)


@tool("verify_evidence")
def verify_evidence(bundle_id: str) -> dict[str, Any]:
    return _manager().verify_evidence(bundle_id)


@tool("decide_claim")
def decide_claim(
    study_id: str,
    verdict: str,
    rationale: str,
    bundle_id: str,
    expected_generation: int,
    idempotency_key: str,
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().decide_claim(
        study_id=study_id,
        verdict=verdict,
        rationale=rationale,
        bundle_id=bundle_id,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key,
        actor=actor,
    )


@tool("get_study")
def get_study(study_id: str) -> dict[str, Any]:
    return _manager().status(study_id)
