"""Experiment aggregate, immutable condition revisions, and lifecycle."""

from __future__ import annotations

from typing import Any, Mapping

from research.contracts import ResearchRecord, digest, identity
from research.repository import ResearchRepository


EXPERIMENT_TRANSITIONS = {
    ("draft", "submit_review"): "review",
    ("review", "return_to_draft"): "draft",
    ("review", "lock"): "locked",
    ("locked", "start_preflight"): "running",
    ("locked", "start_execution"): "running",
    ("running", "request_cancel"): "cancelling",
    ("running", "mark_results_ready"): "results_ready",
    ("running", "mark_failed"): "failed",
    ("cancelling", "mark_cancelled"): "cancelled",
    ("cancelling", "mark_results_ready"): "results_ready",
    ("failed", "retry"): "running",
    ("cancelled", "retry"): "running",
    ("results_ready", "finalize"): "finalized",
}


def validate_conditions(conditions: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(conditions)
    required_sections = ("dataset", "operators", "execution", "randomization", "analysis")
    missing = [key for key in required_sections if not isinstance(value.get(key), Mapping)]
    if missing:
        raise ValueError(f"experiment conditions require object sections: {', '.join(missing)}")
    dataset = dict(value["dataset"])
    if not str(dataset.get("name") or "").strip() or not str(dataset.get("version") or "").strip():
        raise ValueError("dataset name and version are required")
    operators = dict(value["operators"])
    arms = list(operators.get("arms") or [])
    arm_ids = [str(dict(item).get("id") or "") for item in arms]
    if len(arms) < 2 or len(set(arm_ids)) != len(arms) or any(not item for item in arm_ids):
        raise ValueError("experiment requires at least two uniquely identified operator arms")
    execution = dict(value["execution"])
    for profile in ("preflight", "confirmatory"):
        item = dict(execution.get(profile) or {})
        if int(item.get("epochs") or 0) < 1:
            raise ValueError(f"execution.{profile}.epochs must be >= 1")
        seeds = list(item.get("seeds") or [])
        if not seeds or len(set(int(seed) for seed in seeds)) != len(seeds):
            raise ValueError(f"execution.{profile}.seeds must be unique and non-empty")
    randomization = dict(value["randomization"])
    required_streams = {"initialization", "data_ordering", "augmentation", "operator_initialization", "analysis"}
    if set(randomization.get("named_streams") or []) != required_streams:
        raise ValueError("randomization must declare all five named RNG streams")
    analysis = dict(value["analysis"])
    if not bool(analysis.get("paired")) or not str(analysis.get("primary_metric") or "").strip():
        raise ValueError("analysis must declare a paired primary metric")
    tracker = dict(value.get("tracker") or {"provider": "local-tracker"})
    if str(tracker.get("provider") or "local-tracker") not in {"local-tracker", "mlflow"}:
        raise ValueError("tracker.provider must be local-tracker or mlflow")
    if str(tracker.get("required_delivery") or "durable-before-finalize") != "durable-before-finalize":
        raise ValueError("tracker.required_delivery must be durable-before-finalize")
    value["tracker"] = tracker
    return value


def _records(repository: ResearchRepository, study_id: str, kind: str, experiment_id: str) -> list[ResearchRecord]:
    return [
        item
        for item in repository.list(study_id, kind)
        if item.record_id == experiment_id or item.payload.get("experiment_id") == experiment_id
    ]


def get_experiment(repository: ResearchRepository, experiment_id: str) -> ResearchRecord:
    record = repository.get("experiment", experiment_id)
    if record is None:
        raise KeyError(experiment_id)
    return record


def revisions(repository: ResearchRepository, experiment_id: str) -> list[ResearchRecord]:
    experiment = get_experiment(repository, experiment_id)
    return sorted(
        _records(repository, experiment.study_id, "experiment_revision", experiment_id),
        key=lambda item: (item.generation, item.record_id),
    )


def latest_revision(repository: ResearchRepository, experiment_id: str) -> ResearchRecord:
    values = revisions(repository, experiment_id)
    if not values:
        raise ValueError("experiment has no condition revision")
    return values[-1]


def state(repository: ResearchRepository, experiment_id: str) -> dict[str, Any]:
    experiment = get_experiment(repository, experiment_id)
    current = "draft"
    generation = 0
    execution_profile = None
    for event in repository.events(experiment.study_id):
        if event["event_type"] != "research.experiment.transition":
            continue
        payload = dict(event["payload"])
        if payload.get("experiment_id") != experiment_id:
            continue
        current = str(payload["target"])
        generation = int(payload["generation"])
        if payload.get("execution_profile"):
            execution_profile = payload["execution_profile"]
    return {
        "experiment_id": experiment_id,
        "state": current,
        "generation": generation,
        "execution_profile": execution_profile,
    }


def create(
    repository: ResearchRepository,
    *,
    study_id: str,
    slug: str,
    title: str,
    purpose: str,
    conditions: Mapping[str, Any],
    experiment_id: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    normalized = validate_conditions(conditions)
    experiment_id = str(experiment_id or "").strip() or identity("experiment", {"study_id": study_id, "slug": slug})

    def apply() -> Mapping[str, Any]:
        if repository.get("study", study_id) is None:
            raise KeyError(study_id)
        experiment = repository.put(
            ResearchRecord(
                "experiment",
                experiment_id,
                study_id,
                0,
                {
                    "slug": str(slug),
                    "title": str(title),
                    "purpose": str(purpose),
                    "experiment_id": experiment_id,
                },
            )
        )
        conditions_digest = digest(normalized)
        revision_id = identity(
            "experiment_revision",
            {"experiment_id": experiment_id, "revision": 1, "conditions_digest": conditions_digest},
        )
        revision = repository.put(
            ResearchRecord(
                "experiment_revision",
                revision_id,
                study_id,
                1,
                {
                    "experiment_id": experiment_id,
                    "revision": 1,
                    "conditions": normalized,
                    "conditions_digest": conditions_digest,
                    "parent_revision_id": None,
                    "rationale": "initial experiment conditions",
                },
            )
        )
        repository.event(
            study_id,
            "research.experiment.created",
            {"experiment_id": experiment_id, "revision_id": revision_id},
        )
        return {"experiment": experiment.to_dict(), "revision": revision.to_dict(), "lifecycle": state(repository, experiment_id)}

    return repository.once(idempotency_key, "create_experiment", apply)


def revise(
    repository: ResearchRepository,
    *,
    experiment_id: str,
    expected_revision: int,
    conditions: Mapping[str, Any],
    rationale: str,
    actor: str,
    idempotency_key: str,
) -> dict[str, Any]:
    normalized = validate_conditions(conditions)

    def apply() -> Mapping[str, Any]:
        lifecycle = state(repository, experiment_id)
        if lifecycle["state"] not in {"draft", "review"}:
            raise ValueError("locked or executed experiment conditions cannot be rewritten")
        previous = latest_revision(repository, experiment_id)
        if previous.generation != int(expected_revision):
            raise ValueError(
                f"stale experiment revision: expected {expected_revision}, current {previous.generation}"
            )
        revision_number = previous.generation + 1
        conditions_digest = digest(normalized)
        revision_id = identity(
            "experiment_revision",
            {
                "experiment_id": experiment_id,
                "revision": revision_number,
                "conditions_digest": conditions_digest,
            },
        )
        revision = repository.put(
            ResearchRecord(
                "experiment_revision",
                revision_id,
                previous.study_id,
                revision_number,
                {
                    "experiment_id": experiment_id,
                    "revision": revision_number,
                    "conditions": normalized,
                    "conditions_digest": conditions_digest,
                    "parent_revision_id": previous.record_id,
                    "rationale": str(rationale),
                    "actor": str(actor),
                },
            )
        )
        repository.event(
            previous.study_id,
            "research.experiment.revised",
            {
                "experiment_id": experiment_id,
                "revision_id": revision_id,
                "parent_revision_id": previous.record_id,
                "actor": actor,
            },
        )
        return {"revision": revision.to_dict(), "lifecycle": state(repository, experiment_id)}

    return repository.once(idempotency_key, "revise_experiment", apply)


def transition(
    repository: ResearchRepository,
    *,
    experiment_id: str,
    command: str,
    expected_generation: int,
    idempotency_key: str,
    actor: str,
    execution_profile: str | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    def apply() -> Mapping[str, Any]:
        current = state(repository, experiment_id)
        if current["generation"] != int(expected_generation):
            raise ValueError(
                f"stale experiment generation: expected {expected_generation}, current {current['generation']}"
            )
        target = EXPERIMENT_TRANSITIONS.get((current["state"], command))
        if target is None:
            raise ValueError(f"illegal experiment transition: {current['state']} + {command}")
        if command == "lock":
            revision = latest_revision(repository, experiment_id)
            evidence_refs_local = (*evidence_refs, revision.digest)
        else:
            evidence_refs_local = evidence_refs
        generation = current["generation"] + 1
        experiment = get_experiment(repository, experiment_id)
        event = repository.event(
            experiment.study_id,
            "research.experiment.transition",
            {
                "experiment_id": experiment_id,
                "source": current["state"],
                "target": target,
                "command": command,
                "generation": generation,
                "actor": actor,
                "execution_profile": execution_profile or current.get("execution_profile"),
                "evidence_refs": list(evidence_refs_local),
            },
        )
        return {"accepted": True, "state": target, "generation": generation, "event_id": event["event_id"]}

    return repository.once(idempotency_key, f"experiment:{command}", apply)


__all__ = [
    "EXPERIMENT_TRANSITIONS",
    "create",
    "get_experiment",
    "latest_revision",
    "revise",
    "revisions",
    "state",
    "transition",
    "validate_conditions",
]
