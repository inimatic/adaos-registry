from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


ARM_IDS = (
    "C0_raw",
    "C1_reviewed_prose",
    "C2_staged",
    "C3_typed_execution",
    "C4_over_specified",
)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop("digest", None)
    return "sha256:" + hashlib.sha256(canonical(payload)).hexdigest()


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _schema(name: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "schemas" / name
    return json.loads(path.read_text(encoding="utf-8"))


def validate(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    errors = sorted(Draft202012Validator(_schema(name)).iter_errors(payload), key=lambda item: list(item.absolute_path))
    if errors:
        issues = []
        for error in errors[:20]:
            location = ".".join(str(item) for item in error.absolute_path) or "$"
            issues.append(f"{location}: {error.message}")
        raise ValueError(f"{name} invalid: {'; '.join(issues)}")
    if payload.get("digest") != digest(payload):
        raise ValueError(f"{name} digest does not match its content")
    return payload


def _verify_input(item: Mapping[str, Any]) -> None:
    path = Path(str(item["path"])).resolve()
    if not path.is_file():
        raise ValueError(f"evaluation input is unavailable: {item['input_id']}")
    declared = str(item["digest"])
    if path.suffix.lower() == ".json":
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            value = None
        if isinstance(value, Mapping) and str(value.get("digest") or "") == declared:
            return
    if file_digest(path) != declared:
        raise ValueError(f"evaluation input digest mismatch: {item['input_id']}")


def freeze_task(value: Mapping[str, Any]) -> dict[str, Any]:
    task = copy.deepcopy(dict(value))
    schema_version = str(task.get("schema_version") or "1.0.0")
    task.update(
        {
            "schema": "adaos.research.calibration_task.v1",
            "schema_version": schema_version,
            "frozen_at": now(),
        }
    )
    task.pop("digest", None)
    task["digest"] = digest(task)
    validated = validate("research.calibration_task.v1.schema.json", task)
    arms = {str(item["arm_id"]): dict(item) for item in validated["arms"]}
    if set(arms) != set(ARM_IDS):
        raise ValueError("calibration task must define each C0-C4 arm exactly once")
    inputs = {str(item["input_id"]): dict(item) for item in validated["inputs"]}
    hidden_ids = {str(item["input_id"]) for item in validated["hidden_inputs"]}
    if len(inputs) != len(validated["inputs"]) or len(hidden_ids) != len(validated["hidden_inputs"]):
        raise ValueError("evaluation input ids must be unique")
    if set(inputs) & hidden_ids:
        raise ValueError("visible and hidden evaluation input ids must be disjoint")
    for item in [*validated["inputs"], *validated["hidden_inputs"]]:
        _verify_input(item)
    for arm_id, arm in arms.items():
        declared = set(str(item) for item in arm["instruction_input_ids"])
        if not declared.issubset(inputs):
            raise ValueError(f"{arm_id} references an unknown instruction input")
        permitted = {input_id for input_id, item in inputs.items() if arm_id in set(item["visible_arms"])}
        if declared != permitted:
            raise ValueError(f"{arm_id} instruction inputs do not match input visibility declarations")
    expected_kinds = {
        "C0_raw": set(),
        "C1_reviewed_prose": {"reviewed_prose"},
        "C2_staged": {"research_compilation"},
        "C3_typed_execution": {"research_compilation", "automation_brief", "conformance_fixture"},
        "C4_over_specified": {"research_compilation", "automation_brief", "conformance_fixture", "prescribed_scaffold"},
    }
    for arm_id, kinds in expected_kinds.items():
        actual = {inputs[input_id]["kind"] for input_id in arms[arm_id]["instruction_input_ids"]}
        if actual != kinds:
            raise ValueError(f"{arm_id} requires exact input kinds {sorted(kinds)}")
    seeds = list(validated["repetitions"]["paired_seeds"])
    if validated["repetitions"]["attempts_per_arm"] != len(seeds):
        raise ValueError("attempts_per_arm must equal paired_seeds length")
    if schema_version == "1.1.0" and validated["repetitions"]["model_random_seed_control"] != "unsupported_not_claimed":
        raise ValueError("calibration must not claim unsupported model random-seed control")
    check_ids = [str(item["check_id"]) for item in validated["rubric"]["checks"]]
    if len(check_ids) != len(set(check_ids)):
        raise ValueError("rubric check ids must be unique")
    return validated


def validate_result(value: Mapping[str, Any]) -> dict[str, Any]:
    return validate("research.calibration_result.v1.schema.json", value)


__all__ = ["ARM_IDS", "canonical", "digest", "file_digest", "freeze_task", "now", "validate", "validate_result"]
