from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("research_calibration_runner.handlers.main", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _packet(module):
    identity = {
        "schema": "adaos.research.calibration_packet.v1",
        "packet_id": "packet-tlp-C3-1-fixed_downstream",
        "task_id": "tlp-calibration-v1",
        "task_digest": "sha256:" + "1" * 64,
        "arm_id": "C3_typed_execution",
        "attempt_index": 1,
        "paired_seed": 17,
        "base_request": "Implement the frozen research direction.",
        "budget_view": "fixed_downstream",
        "budget": {
            "max_model_tokens": 80000,
            "max_wall_seconds": 7200,
            "max_attempts": 1,
            "max_human_interventions": 0,
        },
        "artifact_inputs": [
            {
                "ref": "artifact://skill/source_direction/part0",
                "audience": "research.calibration.c3_typed_execution",
                "context_digest": "sha256:" + "2" * 64,
                "source_manifest_digest": "sha256:" + "3" * 64,
                "root_path": "C:/state/views/source",
                "items": [],
            }
        ],
        "instruction_inputs": [],
        "prohibited_actions": ["Do not inspect evaluator material."],
    }
    return {**identity, "created_at": "2026-08-18T00:00:00Z", "digest": module._digest(identity)}


def test_packet_digest_and_candidate_identity_are_stable() -> None:
    module = _module()
    packet = _packet(module)

    validated = module._validate_packet(packet)

    assert validated["digest"] == packet["digest"]
    assert module._candidate_id(packet) == module._candidate_id(packet)
    assert module._candidate_id(packet).startswith("tlp_cal_c3_a1_fd_")


def test_packet_tampering_fails_closed() -> None:
    module = _module()
    packet = _packet(module)
    packet["base_request"] = "tampered"

    with pytest.raises(ValueError, match="digest"):
        module._validate_packet(packet)


def test_artifact_ref_is_converted_without_trusting_packet_path() -> None:
    module = _module()

    source = module._artifact_source(
        "artifact://skill/tlp_compiler_calibration/part0",
        "research.calibration.c0_raw",
    )

    assert source == {
        "skill_id": "tlp_compiler_calibration",
        "group_id": "part0",
        "audience": "research.calibration.c0_raw",
    }
    with pytest.raises(ValueError, match="unsupported"):
        module._artifact_source("D:/private/evaluator", "research.calibration.c0_raw")


def test_packet_budget_is_complete_and_bounded() -> None:
    module = _module()
    budget = module._validate_packet(_packet(module))["budget"]

    assert budget == {
        "max_model_tokens": 80000,
        "max_wall_seconds": 7200,
        "max_attempts": 1,
        "max_human_interventions": 0,
    }
