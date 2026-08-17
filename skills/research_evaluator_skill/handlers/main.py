from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from evaluation.contracts import ARM_IDS, freeze_task  # noqa: E402
from evaluation.harness import evaluate_candidate, prepare_arm, summarize  # noqa: E402
from evaluation.repository import EvaluationRepository  # noqa: E402


@tool(summary="Ensure the independent research-evaluation ledger.", side_effects="local_write")
def ensure_schema() -> dict[str, Any]:
    repository = EvaluationRepository()
    return {"ok": True, "binding": repository._db.binding.to_dict(), "health": dict(repository._db.health())}


@tool(summary="Freeze one immutable C0-C4 calibration task and preregistration.", side_effects="local_write")
def freeze_calibration(task: Mapping[str, Any], **_: Any) -> dict[str, Any]:
    repository = EvaluationRepository()
    existing = repository.find_task(str(task.get("task_id") or ""))
    stored = existing if existing else repository.put_task(freeze_task(task))
    return {
        "ok": True,
        "task": stored,
        "task_id": stored["task_id"],
        "task_digest": stored["digest"],
        "message": "Calibration task frozen. Visible and hidden inputs are digest-verified.",
    }


@tool(summary="Read one frozen calibration task without projecting hidden input paths.", side_effects="none")
def get_task(task_id: str, **_: Any) -> dict[str, Any]:
    task = EvaluationRepository().get_task(str(task_id))
    public = {key: value for key, value in task.items() if key != "hidden_inputs"}
    public["hidden_input_count"] = len(task.get("hidden_inputs") or [])
    return {"ok": True, "task": public}


@tool(summary="Materialize one contamination-checked arm packet for a paired attempt.", side_effects="local_write")
def prepare_calibration_arm(
    task_id: str,
    arm_id: str,
    attempt_index: int,
    budget_view: str = "fixed_downstream",
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    packet = prepare_arm(task, arm_id, attempt_index, budget_view=budget_view)
    return {"ok": True, "packet": repository.put_packet(packet)}


@tool(summary="Materialize every preregistered arm packet for one budget view.", side_effects="local_write")
def prepare_calibration_suite(
    task_id: str,
    budget_view: str = "fixed_downstream",
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    stored = []
    for attempt_index in range(1, int(task["repetitions"]["attempts_per_arm"]) + 1):
        for arm_id in ARM_IDS:
            packet = repository.put_packet(
                prepare_arm(task, arm_id, attempt_index, budget_view=budget_view)
            )
            stored.append(
                {
                    "packet_id": packet["packet_id"],
                    "digest": packet["digest"],
                    "arm_id": packet["arm_id"],
                    "attempt_index": packet["attempt_index"],
                    "paired_seed": packet["paired_seed"],
                    "instruction_kinds": [item["kind"] for item in packet["instruction_inputs"]],
                    "context_digests": [item["context_digest"] for item in packet["artifact_inputs"]],
                }
            )
    return {
        "ok": True,
        "task_id": task["task_id"],
        "task_digest": task["digest"],
        "budget_view": budget_view,
        "count": len(stored),
        "packets": stored,
    }


@tool(summary="Independently score one candidate against the frozen calibration rubric.", side_effects="local_write")
def record_calibration_result(
    task_id: str,
    candidate: Mapping[str, Any],
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    stored = repository.put_result(evaluate_candidate(task, candidate))
    return {
        "ok": True,
        "result": stored,
        "evidence_valid_completion": stored["metrics"]["evidence_valid_completion"],
    }


@tool(summary="Summarize C0-C4 evidence-valid completion and failure attribution.", side_effects="none")
def summarize_calibration(
    task_id: str,
    budget_view: str = "fixed_downstream",
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    summary = summarize(task, repository.results(str(task_id), budget_view=budget_view))
    lines = [
        f"- **{item['arm_id']}** — `{item['evidence_valid_completions']}/{item['completed']}` "
        f"evidence-valid · complete `{item['complete']}` · failures `{item['failure_stages']}`"
        for item in summary["arms"]
    ]
    content = (
        "## Research compilation calibration\n\n"
        + "\n".join(lines)
        + f"\n\n**Budget:** `{summary['budget_view']}` · **Frozen task:** `{summary['task_digest']}` "
        + f"· **complete:** `{summary['complete']}`"
    )
    return {"ok": True, "summary": summary, "content": content}


__all__ = [
    "ensure_schema",
    "freeze_calibration",
    "get_task",
    "prepare_calibration_arm",
    "prepare_calibration_suite",
    "record_calibration_result",
    "summarize_calibration",
]
