from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from evaluation.contracts import digest


PUBLIC_IMPLEMENTATION_PROFILE_KEYS = (
    "required_source_signals",
    "required_callable_keys",
    "model_input_shape",
    "model_output_classes",
    "production_tlp_parameter_shape",
    "window",
    "stride",
    "initial_equivalence_tolerance",
)


def project_tlp_consumer_contract(
    consumer_contract: Mapping[str, Any],
    public_conformance: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the generic runner ABI as an explicit candidate provider rail."""

    if (
        consumer_contract.get("schema") != "adaos.contract.operation_set.v1"
        or consumer_contract.get("contract") != "adaos.research.runner.v1"
        or not consumer_contract.get("digest")
    ):
        raise ValueError("generic research runner consumer contract is invalid")
    if (
        public_conformance.get("schema")
        != "adaos.research.tlp_conformance_fixture.v1"
        or "implementation_probe"
        not in dict(public_conformance.get("required_operations") or {})
    ):
        raise ValueError("public TLP implementation conformance contract is incomplete")
    profile = dict(public_conformance.get("implementation_contract") or {})
    callable_keys = [str(item) for item in profile.get("required_callable_keys") or []]
    if not callable_keys or any(not item for item in callable_keys):
        raise ValueError("public TLP implementation contract has no callable surface")

    projected = copy.deepcopy(dict(consumer_contract))
    projected.pop("digest", None)
    projected["candidate_role"] = "provider"
    return {**projected, "digest": digest(projected)}


def project_tlp_probe_contract(
    public_conformance: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an independent public TLP probe ABI without hidden values."""

    if (
        public_conformance.get("schema")
        != "adaos.research.tlp_conformance_fixture.v1"
        or "implementation_probe"
        not in dict(public_conformance.get("required_operations") or {})
    ):
        raise ValueError("public TLP implementation conformance contract is incomplete")
    probe = dict(
        dict(public_conformance.get("required_operations") or {}).get(
            "implementation_probe"
        )
        or {}
    )
    input_schema = probe.get("input_schema")
    output_schema = probe.get("output_schema")
    if not isinstance(input_schema, Mapping) or not isinstance(output_schema, Mapping):
        raise ValueError("public TLP implementation probe has no machine schemas")
    projected = {
        "schema": "adaos.contract.operation_set.v1",
        "contract": "adaos.research.tlp_probe.v1",
        "version": "1.0.0",
        "consumer_ref": "skill:research_evaluator_skill",
        "capability": "research.tlp.implementation_probe",
        "candidate_role": "provider",
        "operations": {
            "implementation_probe": {
                "description": str(
                    probe.get("purpose") or "TLP implementation probe"
                ),
                "input_schema": copy.deepcopy(dict(input_schema)),
                "output_schema": copy.deepcopy(dict(output_schema)),
            }
        },
        "domain_conformance": copy.deepcopy(dict(public_conformance)),
    }
    return {**projected, "digest": digest(projected)}


def assert_hidden_profile_is_public(
    public_conformance: Mapping[str, Any],
    hidden_profile: Mapping[str, Any],
) -> None:
    """Reject a calibration whose judge requires an undisclosed interface."""

    public_profile = dict(public_conformance.get("implementation_contract") or {})
    mismatches = [
        key
        for key in PUBLIC_IMPLEMENTATION_PROFILE_KEYS
        if public_profile.get(key) != hidden_profile.get(key)
    ]
    if mismatches:
        raise ValueError(
            "hidden TLP evaluator requires an interface absent from the public contract: "
            + ", ".join(mismatches)
        )


__all__ = [
    "PUBLIC_IMPLEMENTATION_PROFILE_KEYS",
    "assert_hidden_profile_is_public",
    "project_tlp_consumer_contract",
    "project_tlp_probe_contract",
]
