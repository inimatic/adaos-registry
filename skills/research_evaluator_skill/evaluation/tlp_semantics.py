from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from evaluation.contracts import digest


def hidden_probe_request(experiment_plan_digest: str) -> dict[str, Any]:
    """Create a deterministic, evaluator-owned operator input.

    The operation and shapes are public in the C3 consumer contract.  Numeric
    values remain hidden until evaluation so a generated runner must implement
    the actual centered max-plus operation rather than replaying a fixture.
    """

    identity = {
        "schema": "adaos.research.tlp_operator_probe.v1",
        "experiment_plan_digest": str(experiment_plan_digest),
        "input_nchw": [
            [
                [
                    [1.0, 5.0, 2.0, 4.0],
                    [3.0, 0.0, 7.0, 6.0],
                    [8.0, 9.0, -1.0, 2.0],
                    [4.0, 3.0, 5.0, 0.0],
                ],
                [
                    [-2.0, 1.0, 6.0, 3.0],
                    [4.0, 2.0, 0.0, 5.0],
                    [7.0, -3.0, 2.0, 8.0],
                    [1.0, 9.0, 4.0, -1.0],
                ],
            ]
        ],
        "theta_c4": [[2.0, 0.0, 4.0, -2.0], [3.0, 1.0, 0.0, -2.0]],
    }
    return {**identity, "digest": digest(identity)}


def _pool_expected(
    values: Sequence[Any], theta: Sequence[Any] | None
) -> list[list[list[list[float]]]]:
    result: list[list[list[list[float]]]] = []
    for batch in values:
        batch_result = []
        for channel_index, channel in enumerate(batch):
            centered = [0.0, 0.0, 0.0, 0.0]
            if theta is not None:
                raw = [float(item) for item in theta[channel_index]]
                mean = sum(raw) / 4.0
                centered = [item - mean for item in raw]
            channel_result = []
            for row in range(0, 4, 2):
                output_row = []
                for column in range(0, 4, 2):
                    window = [
                        float(channel[row][column]),
                        float(channel[row][column + 1]),
                        float(channel[row + 1][column]),
                        float(channel[row + 1][column + 1]),
                    ]
                    output_row.append(
                        max(value + centered[index] for index, value in enumerate(window))
                    )
                channel_result.append(output_row)
            batch_result.append(channel_result)
        result.append(batch_result)
    return result


def _flatten_numbers(value: Any) -> list[float]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result: list[float] = []
        for item in value:
            result.extend(_flatten_numbers(item))
        return result
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("probe output contains a non-numeric leaf")
    return [float(value)]


def _numbers_close(actual: Any, expected: Any, *, tolerance: float = 1e-6) -> bool:
    try:
        actual_values = _flatten_numbers(actual)
        expected_values = _flatten_numbers(expected)
    except ValueError:
        return False
    return len(actual_values) == len(expected_values) and all(
        math.isclose(left, right, rel_tol=0.0, abs_tol=tolerance)
        for left, right in zip(actual_values, expected_values, strict=True)
    )


def evaluate_tlp_implementation(
    *,
    profile: Mapping[str, Any],
    plan: Mapping[str, Any],
    source_snapshot: Mapping[str, Any] | None,
    expected_source_digest: str | None,
    arm_trials: Sequence[Mapping[str, Any]],
    probe_request: Mapping[str, Any],
    probe_result: Mapping[str, Any] | None,
    probe_error: str | None = None,
) -> dict[str, Any]:
    """Independently reject semantic substitution on the TLP benchmark."""

    problems: list[str] = []
    snapshot = dict(source_snapshot or {})
    snapshot_files = {
        str(item.get("path") or ""): dict(item)
        for item in snapshot.get("files") or []
        if isinstance(item, Mapping)
    }
    if not snapshot.get("source_digest") or not snapshot.get("digest"):
        problems.append("bounded source snapshot is unavailable")
    if expected_source_digest and str(snapshot.get("source_digest") or "") != str(
        expected_source_digest
    ):
        problems.append("bounded source snapshot differs from the natively validated source")

    plan_digest = str(plan.get("digest") or "")
    system_digest = str(dict(plan.get("system") or {}).get("digest") or "")
    observations: dict[str, dict[str, Any]] = {}
    execution_path_digests: set[str] = set()
    implementations: list[dict[str, Any]] = []
    declared_source_text: dict[str, str] = {}
    trial_refs: list[str] = []
    for row in arm_trials:
        arm = dict(row.get("arm") or {})
        trial = dict(row.get("trial") or {})
        trial_refs.append(str(trial.get("digest") or ""))
        document = dict(
            dict(trial.get("documents") or {}).get("implementation_observation.json")
            or {}
        )
        arm_id = str(arm.get("id") or "")
        if not arm_id or not document:
            problems.append("paired arm implementation observation is missing")
            continue
        if str(document.get("experiment_plan_digest") or "") != plan_digest:
            problems.append(f"{arm_id} observation has another ExperimentPlan")
        if str(document.get("system_digest") or "") != system_digest:
            problems.append(f"{arm_id} observation has another scientific system")
        actual_arm = dict(document.get("arm") or {})
        if any(
            str(actual_arm.get(key) or "") != str(arm.get(key) or "")
            for key in ("id", "role")
        ):
            problems.append(f"{arm_id} observation has another arm identity")
        path_digest = str(document.get("execution_path_digest") or "")
        if path_digest:
            execution_path_digests.add(path_digest)
        implementation = dict(document.get("implementation") or {})
        implementations.append(implementation)
        if path_digest != digest(implementation):
            problems.append(f"{arm_id} execution path is not bound to its implementation")
        callables = dict(implementation.get("callables") or {})
        for key in profile.get("required_callable_keys") or []:
            if not str(callables.get(str(key)) or ""):
                problems.append(f"{arm_id} implementation omitted callable {key}")
        for declared in implementation.get("source_files") or []:
            if not isinstance(declared, Mapping):
                problems.append(f"{arm_id} implementation has an invalid source identity")
                continue
            source_path = str(declared.get("path") or "")
            source = snapshot_files.get(source_path)
            if source is None or str(source.get("digest") or "") != str(
                declared.get("digest") or ""
            ):
                problems.append(f"{arm_id} implementation source is outside the validated snapshot")
                continue
            declared_source_text[source_path] = str(source.get("text") or "")
        observations[str(arm.get("role") or arm_id)] = dict(document.get("observed") or {})
    if set(observations) != {"baseline", "intervention"}:
        problems.append("workflow smoke did not observe both accepted arms")
    if len(execution_path_digests) != 1:
        problems.append("paired arms do not bind one shared production execution path")
    if len({digest(item) for item in implementations}) != 1:
        problems.append("paired arms declare different production implementations")
    python_text = "\n".join(
        text
        for path, text in declared_source_text.items()
        if path.lower().endswith(".py")
    )
    for signal in profile.get("required_source_signals") or []:
        if str(signal) not in python_text:
            problems.append(f"missing production source signal: {signal}")
    lowered_source = python_text.lower()
    for signal in profile.get("forbidden_source_signals") or []:
        if str(signal).lower() in lowered_source:
            problems.append(f"surrogate source signal present: {signal}")

    expected_input = list(profile.get("model_input_shape") or [])
    expected_classes = int(profile.get("model_output_classes") or 0)
    expected_parameter_shape = list(profile.get("production_tlp_parameter_shape") or [])
    expected_window = list(profile.get("window") or [])
    expected_stride = list(profile.get("stride") or [])
    tolerance = float(profile.get("initial_equivalence_tolerance") or 1e-6)
    for role, observed in observations.items():
        model = dict(observed.get("model") or {})
        operator = dict(observed.get("operator") or {})
        execution = dict(observed.get("execution") or {})
        input_shape = list(model.get("input_shape") or [])
        output_shape = list(model.get("output_shape") or [])
        if input_shape[-3:] != expected_input:
            problems.append(f"{role} observed another model input geometry")
        if not output_shape or int(output_shape[-1]) != expected_classes:
            problems.append(f"{role} observed another output space")
        if list(operator.get("window") or []) != expected_window:
            problems.append(f"{role} observed another pooling window")
        if list(operator.get("stride") or []) != expected_stride:
            problems.append(f"{role} observed another pooling stride")
        if execution.get("same_training_path") is not True:
            problems.append(f"{role} did not confirm the shared training path")
        if role == "intervention":
            if list(operator.get("production_parameter_shape") or []) != expected_parameter_shape:
                problems.append("intervention observed another trainable parameter shape")
            if operator.get("centered") is not True or operator.get("zero_initialized") is not True:
                problems.append("intervention omitted centered zero initialization")
            try:
                initial_error = float(operator.get("initial_max_abs_error"))
            except (TypeError, ValueError):
                initial_error = math.inf
            if not math.isfinite(initial_error) or initial_error > tolerance:
                problems.append("intervention failed initial MaxPool equivalence")

    result = dict(probe_result or {})
    if probe_error:
        problems.append("hidden operator probe failed to execute")
    if str(result.get("schema") or "") != "adaos.research.tlp_operator_probe_result.v1":
        problems.append("hidden operator probe returned another schema")
    if str(result.get("experiment_plan_digest") or "") != plan_digest:
        problems.append("hidden operator probe returned another ExperimentPlan")
    probe_path_digest = str(result.get("execution_path_digest") or "")
    if execution_path_digests and probe_path_digest not in execution_path_digests:
        problems.append("hidden operator probe is not bound to the smoke execution path")
    theta = list(probe_request.get("theta_c4") or [])
    input_values = list(probe_request.get("input_nchw") or [])
    if list(result.get("parameter_shape") or []) != [len(theta), 4]:
        problems.append("hidden operator probe returned another parameter shape")
    centered_theta = [
        [float(value) - sum(float(item) for item in channel) / 4.0 for value in channel]
        for channel in theta
    ]
    if not _numbers_close(result.get("centered_theta_c4"), centered_theta, tolerance=tolerance):
        problems.append("hidden operator probe did not center theta per channel")
    if not _numbers_close(
        result.get("baseline_nchw"), _pool_expected(input_values, None), tolerance=tolerance
    ):
        problems.append("hidden operator probe baseline is not 2x2 MaxPool")
    if not _numbers_close(
        result.get("intervention_nchw"),
        _pool_expected(input_values, theta),
        tolerance=tolerance,
    ):
        problems.append("hidden operator probe intervention is not centered TLP")

    refs = [
        str(snapshot.get("digest") or ""),
        *trial_refs,
        f"developer-probe://{str(probe_request.get('digest') or '')}",
    ]
    return {
        "ok": not problems,
        "detail": (
            "accepted scientific system executed through paired observations and hidden probe"
            if not problems
            else "; ".join(dict.fromkeys(problems))
        ),
        "evidence_refs": list(dict.fromkeys(item for item in refs if item)),
        "diagnostics": problems,
    }


__all__ = ["evaluate_tlp_implementation", "hidden_probe_request"]
