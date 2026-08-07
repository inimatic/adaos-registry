"""Event-sourced research lifecycle matching the scenario workflow package."""

from __future__ import annotations

from typing import Any, Mapping

from research.repository import ResearchRepository


TRANSITIONS = {
    ("draft", "submit_protocol_review"): "protocol_review",
    ("protocol_review", "lock_protocol"): "locked",
    ("locked", "approve_smoke"): "smoke",
    ("smoke", "start_execution"): "executing",
    ("executing", "complete_execution"): "qc",
    ("qc", "request_rework"): "executing",
    ("qc", "unblind_test"): "unblinded",
    ("unblinded", "run_analysis"): "analysis",
    ("analysis", "submit_claim_review"): "claim_review",
    ("claim_review", "decide_claim"): "complete",
}


def state(repository: ResearchRepository, study_id: str) -> dict[str, Any]:
    current = "draft"
    generation = 0
    for event in repository.events(study_id):
        if event["event_type"] == "research.protocol.amended":
            current = "protocol_review"
            generation = int(event["payload"]["generation"])
            continue
        if event["event_type"] != "research.workflow.transition":
            continue
        current = str(event["payload"]["target"])
        generation = int(event["payload"]["generation"])
    return {"study_id": study_id, "state": current, "generation": generation}


def transition(
    repository: ResearchRepository,
    *,
    study_id: str,
    command: str,
    expected_generation: int,
    idempotency_key: str,
    actor: str,
    evidence_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    def apply() -> Mapping[str, Any]:
        current = state(repository, study_id)
        if int(expected_generation) != current["generation"]:
            raise ValueError(
                f"stale workflow generation: expected {expected_generation}, current {current['generation']}"
            )
        target = TRANSITIONS.get((current["state"], command))
        if target is None:
            raise ValueError(f"illegal research transition: {current['state']} + {command}")
        if command in {"lock_protocol", "unblind_test", "decide_claim"} and not evidence_refs:
            raise ValueError(f"{command} requires evidence_refs")
        generation = current["generation"] + 1
        event = repository.event(
            study_id,
            "research.workflow.transition",
            {
                "source": current["state"],
                "target": target,
                "command": command,
                "generation": generation,
                "actor": actor,
                "evidence_refs": list(evidence_refs),
            },
        )
        return {"accepted": True, "state": target, "generation": generation, "event_id": event["event_id"]}

    return repository.once(idempotency_key, command, apply)


__all__ = ["TRANSITIONS", "state", "transition"]
