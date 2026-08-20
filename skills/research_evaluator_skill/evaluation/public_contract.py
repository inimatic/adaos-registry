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
    """Attach the public domain surface without exposing hidden probe values."""

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
    projected["domain_conformance"] = copy.deepcopy(dict(public_conformance))
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
]
