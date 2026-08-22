from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evaluation.contracts import digest
from evaluation.public_contract import (
    assert_hidden_profile_is_public,
    project_tlp_consumer_contract,
    project_tlp_probe_contract,
)


ROOT = Path(__file__).resolve().parents[1]


def _generic_contract() -> dict:
    identity = {
        "schema": "adaos.contract.operation_set.v1",
        "contract": "adaos.research.runner.v1",
        "version": "1.9.0",
        "operations": {"prepare_attempt": {}},
    }
    return {**identity, "digest": digest(identity)}


def _public_contract() -> dict:
    return json.loads(
        (ROOT / "benchmarks" / "tlp" / "conformance-fixture.json").read_text(
            encoding="utf-8-sig"
        )
    )


def _hidden_profile() -> dict:
    hidden = json.loads(
        (ROOT / "benchmarks" / "tlp" / "hidden-rubric.json").read_text(
            encoding="utf-8-sig"
        )
    )
    return dict(hidden["implementation_profile"])


def test_public_projection_exposes_every_required_semantic_interface() -> None:
    runner = project_tlp_consumer_contract(_generic_contract(), _public_contract())
    projected = project_tlp_probe_contract(_public_contract())

    domain = projected["domain_conformance"]
    profile = domain["implementation_contract"]
    probe = domain["required_operations"]["implementation_probe"]
    operation = projected["operations"]["implementation_probe"]
    assert profile["required_callable_keys"] == [
        "model_factory",
        "baseline_operator",
        "intervention_operator",
        "training_entrypoint",
    ]
    assert profile["model_input_shape"] == [3, 96, 96]
    assert profile["production_tlp_parameter_shape"] == [128, 4]
    assert probe["input"]["request"]["input_nchw"].startswith("finite nested")
    assert probe["input"]["request"]["theta_c4"].startswith("finite nested")
    assert operation["input_schema"]["required"] == ["request"]
    assert operation["input_schema"]["properties"]["request"]["required"] == [
        "schema",
        "experiment_plan_digest",
        "input_nchw",
        "theta_c4",
    ]
    assert operation["input_schema"]["additionalProperties"] is False
    assert operation["output_schema"]["properties"]["schema"]["const"] == (
        "adaos.research.tlp_operator_probe_result.v1"
    )
    assert projected["digest"] == digest(
        {key: value for key, value in projected.items() if key != "digest"}
    )
    assert runner["contract"] == "adaos.research.runner.v1"
    assert runner["candidate_role"] == "provider"
    assert list(runner["operations"]) == ["prepare_attempt"]
    assert projected["contract"] == "adaos.research.tlp_probe.v1"
    assert projected["capability"] == "research.tlp.implementation_probe"
    assert projected["candidate_role"] == "provider"
    assert_hidden_profile_is_public(domain, _hidden_profile())


def test_freeze_rejects_a_hidden_requirement_absent_from_public_packet() -> None:
    hidden = copy.deepcopy(_hidden_profile())
    hidden["required_callable_keys"].append("secret_evaluator_entrypoint")

    with pytest.raises(ValueError, match="absent from the public contract"):
        assert_hidden_profile_is_public(_public_contract(), hidden)
