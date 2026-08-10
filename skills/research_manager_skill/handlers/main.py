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
from research.contracts import digest


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


@tool("create_experiment")
def create_experiment(
    study_id: str,
    slug: str,
    title: str,
    purpose: str,
    conditions: Mapping[str, Any],
    idempotency_key: str,
    experiment_id: str | None = None,
) -> dict[str, Any]:
    return _manager().create_experiment(
        study_id=study_id,
        slug=slug,
        title=title,
        purpose=purpose,
        conditions=conditions,
        experiment_id=experiment_id,
        idempotency_key=idempotency_key,
    )


@tool("revise_experiment")
def revise_experiment(
    experiment_id: str,
    expected_revision: int,
    conditions: Mapping[str, Any],
    rationale: str,
    idempotency_key: str,
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().revise_experiment(
        experiment_id=experiment_id,
        expected_revision=expected_revision,
        conditions=conditions,
        rationale=rationale,
        actor=actor,
        idempotency_key=idempotency_key,
    )


@tool("revise_experiment_json")
def revise_experiment_json(
    experiment_id: str,
    expected_revision: int,
    conditions_json: str,
    rationale: str,
    idempotency_key: str = "",
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().revise_experiment_json(
        experiment_id=experiment_id,
        expected_revision=expected_revision,
        conditions_json=conditions_json,
        rationale=rationale,
        actor=actor,
        idempotency_key=idempotency_key or f"ui:revise:{experiment_id}:{expected_revision}:{digest(conditions_json)}",
    )


@tool("submit_experiment_review")
def submit_experiment_review(
    experiment_id: str,
    expected_generation: int,
    idempotency_key: str = "",
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().submit_experiment_review(
        experiment_id=experiment_id,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key or f"ui:review:{experiment_id}:{expected_generation}",
        actor=actor,
    )


@tool("lock_experiment")
def lock_experiment(
    experiment_id: str,
    expected_generation: int,
    idempotency_key: str = "",
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().lock_experiment(
        experiment_id=experiment_id,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key or f"ui:lock:{experiment_id}:{expected_generation}",
        actor=actor,
    )


@tool("start_experiment")
def start_experiment(
    experiment_id: str,
    profile: str,
    expected_generation: int,
    idempotency_key: str = "",
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().start_experiment(
        experiment_id=experiment_id,
        profile=profile,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key or f"ui:start:{experiment_id}:{profile}:{expected_generation}",
        actor=actor,
    )


@tool("reconcile_experiment")
def reconcile_experiment(experiment_id: str, actor: str = "user:local") -> dict[str, Any]:
    return _manager().reconcile_experiment(experiment_id, actor=actor)


@tool("cancel_experiment")
def cancel_experiment(
    experiment_id: str,
    expected_generation: int,
    idempotency_key: str = "",
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().cancel_experiment(
        experiment_id=experiment_id,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key or f"ui:cancel:{experiment_id}:{expected_generation}",
        actor=actor,
    )


@tool("retry_run")
def retry_run(
    experiment_id: str,
    run_id: str,
    expected_generation: int,
    idempotency_key: str = "",
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().retry_run(
        experiment_id=experiment_id,
        run_id=run_id,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key or f"ui:retry:{experiment_id}:{run_id}:{expected_generation}",
        actor=actor,
    )


@tool("finalize_experiment")
def finalize_experiment(
    experiment_id: str,
    expected_generation: int,
    idempotency_key: str = "",
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().finalize_experiment(
        experiment_id=experiment_id,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key or f"ui:finalize:{experiment_id}:{expected_generation}",
        actor=actor,
    )


@tool("verify_experiment_result")
def verify_experiment_result(result_id: str) -> dict[str, Any]:
    return _manager().verify_experiment_result(result_id)


@tool("accept_tracker_evidence")
def accept_tracker_evidence(
    experiment_id: str,
    actor: str = "user:local",
) -> dict[str, Any]:
    return _manager().accept_tracker_evidence(experiment_id=experiment_id, actor=actor)


@tool("flush_experiment_tracker")
def flush_experiment_tracker(
    experiment_id: str,
    required: bool = False,
) -> dict[str, Any]:
    return _manager().flush_experiment_tracker(experiment_id, required=required)


@tool("delete_tracker_projection")
def delete_tracker_projection(
    experiment_id: str,
    session_id: str,
    accepted_export_digest: str,
) -> dict[str, Any]:
    return _manager().delete_tracker_projection(
        experiment_id=experiment_id,
        session_id=session_id,
        accepted_export_digest=accepted_export_digest,
    )


@tool("get_experiment")
def get_experiment(experiment_id: str) -> dict[str, Any]:
    return _manager().experiment_status(experiment_id)


@tool("list_experiment_attempts")
def list_experiment_attempts(experiment_id: str) -> dict[str, Any]:
    return _manager().experiment_attempts(experiment_id)


@tool("list_experiment_pairs")
def list_experiment_pairs(experiment_id: str) -> dict[str, Any]:
    return _manager().experiment_pairs(experiment_id)


@tool("list_experiment_artifacts")
def list_experiment_artifacts(experiment_id: str) -> dict[str, Any]:
    return _manager().experiment_artifacts(experiment_id)


@tool("describe_experiment")
def describe_experiment(
    experiment_id: str,
    locale: str = "ru",
    channel: str = "text",
    section: str = "all",
    available_actions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Present the workbench and workflow-aware next steps for text or voice."""

    return _manager().describe_experiment(
        experiment_id,
        locale=locale,
        channel=channel,
        section=section,
        available_actions=available_actions,
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
