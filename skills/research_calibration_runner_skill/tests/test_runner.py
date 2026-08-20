from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

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


def test_environment_preflight_compares_frozen_runtime(monkeypatch) -> None:
    module = _module()
    identity = {
        "schema": "adaos.runtime.identity.v1",
        "core": {
            "git_commit": "a" * 40,
            "source_tree": {
                "clean": True,
                "tracked_diff_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            },
        },
        "python_version": "3.11.9",
        "platform": "windows-test",
        "current_skill": {
            "name": "research_calibration_runner_skill",
            "version": "0.1.4",
        },
    }
    evaluator_identity = {
        **identity,
        "current_skill": {"name": "research_evaluator_skill", "version": "0.1.9"},
    }
    manager_identity = {
        **identity,
        "current_skill": {"name": "research_manager_skill", "version": "0.13.0"},
    }
    runner_contract_digest = "sha256:" + "2" * 64
    task = {
        "digest": "sha256:" + "1" * 64,
        "environment_spec": {
            "core_commit": "a" * 40,
            "python_version": "3.11.9",
            "platform": "windows-test",
            "core_source_tree_clean": True,
            "core_source_tree_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "component_versions": {
                "research_orchestrator_skill": "0.19.1",
                "research_evaluator_skill": "0.1.9",
                "research_calibration_runner_skill": "0.1.4",
                "research_manager_skill": "0.13.0",
            },
            "runner_contract_digest": runner_contract_digest,
            "minimum_free_disk_bytes": 1024,
        },
    }
    monkeypatch.setattr(module, "sdk_runtime_identity", lambda: identity)
    monkeypatch.setattr(
        module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=4096, used=1024, free=3072),
    )
    monkeypatch.setattr(
        module.builder_automation,
        "standard_prompt_version",
        lambda: "adaos-skill-realization/0.2.0",
    )
    def invoke(_skill, method, *_args, **_kwargs):
        if method == "get_task":
            return {
                "ok": True,
                "task": task,
                "runtime_identity": evaluator_identity,
            }
        if method == "environment_identity":
            return {"ok": True, "runtime_identity": manager_identity}
        if method == "get_runner_contract":
            return {"digest": runner_contract_digest}
        raise AssertionError(method)

    monkeypatch.setattr(
        module,
        "invoke_skill",
        invoke,
    )

    receipt = module._environment_preflight("tlp-calibration-v1")

    assert receipt["task_digest"] == task["digest"]
    task["environment_spec"]["core_commit"] = "b" * 40
    with pytest.raises(RuntimeError, match="core_commit"):
        module._environment_preflight("tlp-calibration-v1")


def test_environment_preflight_rejects_disk_pressure_before_candidate_creation(monkeypatch) -> None:
    module = _module()
    task = {
        "digest": "sha256:" + "1" * 64,
        "environment_spec": {
            "core_commit": "a" * 40,
            "python_version": "3.11.9",
            "platform": "windows-test",
            "core_source_tree_clean": True,
            "core_source_tree_digest": "sha256:" + "2" * 64,
            "component_versions": {
                "research_evaluator_skill": "0.1.32",
                "research_calibration_runner_skill": "0.1.12",
                "research_manager_skill": "0.24.0",
            },
            "runner_contract_digest": "sha256:" + "3" * 64,
            "minimum_free_disk_bytes": 4096,
        },
    }
    local = {
        "core": {
            "git_commit": "a" * 40,
            "source_tree": {"clean": True, "tracked_diff_digest": "sha256:" + "2" * 64},
        },
        "python_version": "3.11.9",
        "platform": "windows-test",
        "current_skill": {"version": "0.1.12"},
    }

    def invoke(_skill, method, *_args, **_kwargs):
        if method == "get_task":
            return {"ok": True, "task": task, "runtime_identity": {**local, "current_skill": {"version": "0.1.32"}}}
        if method == "environment_identity":
            return {"runtime_identity": {**local, "current_skill": {"version": "0.24.0"}}}
        if method == "get_runner_contract":
            return {"digest": "sha256:" + "3" * 64}
        raise AssertionError(method)

    monkeypatch.setattr(module, "invoke_skill", invoke)
    monkeypatch.setattr(module, "sdk_runtime_identity", lambda: local)
    monkeypatch.setattr(
        module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=4096, used=2048, free=2048),
    )

    with pytest.raises(RuntimeError, match="before candidate execution"):
        module._environment_preflight("tlp-calibration-v1")


def test_release_attempt_delegates_exact_terminal_candidate(monkeypatch) -> None:
    module = _module()
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        module,
        "invoke_skill",
        lambda skill, method, arguments, **_kwargs: calls.append((skill, method, arguments))
        or {"ok": True, "runtime_release": {"runtime_removed": True}},
    )

    result = module.release_attempt("tlp_cal_c3_a1_fd_deadbeef", "devcal2_deadbeef")

    assert result["ok"] is True
    assert calls == [
        (
            "builder_sdk_control_skill",
            "release_candidate_runtime",
            {
                "object_type": "skill",
                "object_id": "tlp_cal_c3_a1_fd_deadbeef",
                "development_session_id": "devcal2_deadbeef",
            },
        )
    ]
