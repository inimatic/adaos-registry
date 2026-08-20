from __future__ import annotations

import hashlib

from evaluation.contracts import digest
from evaluation.tlp_semantics import evaluate_tlp_implementation, hidden_probe_request


_PLAN_DIGEST = "sha256:" + "1" * 64
_SYSTEM_DIGEST = "sha256:" + "2" * 64
_SOURCE = """\
import torch
from torch import nn

class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(3, 64, 3)
        self.pool1 = nn.MaxPool2d(2, 2)
        self.conv2 = nn.Conv2d(64, 128, 3)
        self.theta = nn.Parameter(torch.zeros(128, 4))
"""


def _profile() -> dict:
    return {
        "required_source_signals": ["torch", "Conv2d", "MaxPool2d", "Parameter"],
        "required_callable_keys": [
            "model_factory",
            "baseline_operator",
            "intervention_operator",
            "training_entrypoint",
        ],
        "forbidden_source_signals": ["feature_count = 4", "ConvNetSTL10-stdlib"],
        "model_input_shape": [3, 96, 96],
        "model_output_classes": 10,
        "production_tlp_parameter_shape": [128, 4],
        "window": [2, 2],
        "stride": [2, 2],
        "initial_equivalence_tolerance": 1e-6,
    }


def _source_snapshot(source: str = _SOURCE) -> dict:
    raw = source.encode("utf-8")
    source_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    identity = {
        "schema": "adaos.developer.source_snapshot.v1",
        "project_ref": "skill:tlp_candidate",
        "source_digest": "sha256:" + "3" * 64,
        "files": [
            {
                "path": "handlers/runner.py",
                "size_bytes": len(raw),
                "digest": source_digest,
                "text": source,
            }
        ],
        "omitted": [],
        "limits": {
            "max_file_bytes": 524288,
            "max_total_bytes": 4194304,
            "observed_text_bytes": len(raw),
        },
    }
    return {**identity, "digest": digest(identity)}


def _implementation(snapshot: dict) -> dict:
    return {
        "source_files": [
            {
                "path": "handlers/runner.py",
                "digest": snapshot["files"][0]["digest"],
            }
        ],
        "callables": {
            "model_factory": "Model",
            "baseline_operator": "maxpool2d",
            "intervention_operator": "centered_tlp2d",
            "training_entrypoint": "run_workflow_smoke",
        },
    }


def _arm_trials(snapshot: dict) -> list[dict]:
    implementation = _implementation(snapshot)
    path_digest = digest(implementation)
    result = []
    for arm_id, role in (("maxpool", "baseline"), ("tlp", "intervention")):
        operator = {
            "window": [2, 2],
            "stride": [2, 2],
        }
        if role == "intervention":
            operator.update(
                {
                    "production_parameter_shape": [128, 4],
                    "centered": True,
                    "zero_initialized": True,
                    "initial_max_abs_error": 0.0,
                }
            )
        result.append(
            {
                "arm": {"id": arm_id, "role": role},
                "trial": {
                    "digest": "sha256:" + ("4" if role == "baseline" else "5") * 64,
                    "documents": {
                        "implementation_observation.json": {
                            "schema": "adaos.research.implementation_observation.v1",
                            "experiment_plan_digest": _PLAN_DIGEST,
                            "system_digest": _SYSTEM_DIGEST,
                            "arm": {"id": arm_id, "role": role},
                            "execution_path_digest": path_digest,
                            "implementation": implementation,
                            "observed": {
                                "model": {
                                    "input_shape": [1, 3, 96, 96],
                                    "output_shape": [1, 10],
                                },
                                "operator": operator,
                                "execution": {"same_training_path": True},
                            },
                        }
                    },
                },
            }
        )
    return result


def _probe_result(request: dict, execution_path_digest: str) -> dict:
    return {
        "schema": "adaos.research.tlp_operator_probe_result.v1",
        "experiment_plan_digest": _PLAN_DIGEST,
        "execution_path_digest": execution_path_digest,
        "baseline_nchw": [[[[5.0, 7.0], [9.0, 5.0]], [[4.0, 6.0], [9.0, 8.0]]]],
        "intervention_nchw": [
            [[[6.0, 10.0], [9.0, 8.0]], [[3.5, 8.5], [9.5, 8.5]]]
        ],
        "parameter_shape": [2, 4],
        "centered_theta_c4": [[1.0, -1.0, 3.0, -3.0], [2.5, 0.5, -0.5, -2.5]],
    }


def _evaluate(snapshot: dict, trials: list[dict] | None = None) -> dict:
    request = hidden_probe_request(_PLAN_DIGEST)
    arm_trials = trials if trials is not None else _arm_trials(snapshot)
    path_digest = arm_trials[0]["trial"]["documents"][
        "implementation_observation.json"
    ]["execution_path_digest"]
    return evaluate_tlp_implementation(
        profile=_profile(),
        plan={"digest": _PLAN_DIGEST, "system": {"digest": _SYSTEM_DIGEST}},
        source_snapshot=snapshot,
        expected_source_digest=snapshot["source_digest"],
        arm_trials=arm_trials,
        probe_request=request,
        probe_result=_probe_result(request, path_digest),
    )


def test_scientific_gate_accepts_digest_bound_paired_tlp_execution() -> None:
    result = _evaluate(_source_snapshot())

    assert result["ok"] is True, result["diagnostics"]
    assert "hidden probe" in result["detail"]


def test_scientific_gate_rejects_the_previous_stdlib_surrogate() -> None:
    snapshot = _source_snapshot(
        _SOURCE + "\nfeature_count = 4\nMODEL = 'ConvNetSTL10-stdlib'\n"
    )

    result = _evaluate(snapshot)

    assert result["ok"] is False
    assert any("surrogate source signal" in item for item in result["diagnostics"])


def test_scientific_gate_rejects_a_probe_detached_from_the_validated_source() -> None:
    snapshot = _source_snapshot()
    trials = _arm_trials(snapshot)
    trials[1]["trial"]["documents"]["implementation_observation.json"][
        "implementation"
    ]["source_files"][0]["digest"] = "sha256:" + "f" * 64

    result = _evaluate(snapshot, trials)

    assert result["ok"] is False
    assert any("outside the validated snapshot" in item for item in result["diagnostics"])
    assert any("not bound to its implementation" in item for item in result["diagnostics"])
