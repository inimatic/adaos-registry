"""Research-manager application service."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import jsonschema

from adaos.domain.execution import ExecutionSpec
from adaos.domain.runtime_bindings import ServiceBinding
from adaos.sdk.data.secrets import get as get_secret
from adaos.sdk.skills import invoke as invoke_skill

from adaos.sdk.execution import (
    ContentRef,
    ExecutionBudget,
    ExecutionDeterminism,
    ExecutionNetworkPolicy,
    ExecutionResourceRequest,
    cancel as cancel_execution,
    capabilities as execution_capabilities,
    reconcile,
    spec,
    submit,
)

from research import evidence, experiment, guidance
from research.contracts import ResearchRecord, canonical_json, digest, identity, now
from research.repository import ResearchRepository
from research.runner_contract import descriptor as runner_contract_descriptor
from research.tracker import LocalTracker, MlflowTracker, normalize_observation
from research.workflow import state as workflow_state
from research.workflow import transition


class ResearchManager:
    def __init__(self) -> None:
        self.repository = ResearchRepository()
        self.tracker = LocalTracker(self.repository)
        self._trackers: dict[str, Any] = {"local-tracker": self.tracker}

    def _tracker_provider(self, provider_id: str):
        provider_id = str(provider_id or "local-tracker")
        if provider_id not in self._trackers:
            if provider_id != "mlflow":
                raise ValueError(f"unsupported tracker provider: {provider_id}")
            endpoint = str(
                os.getenv("ADAOS_MLFLOW_TRACKING_URI")
                or "http://127.0.0.1:18121/api/services/mlflow_tracker_skill/ui"
            )
            binding_json = str(os.getenv("ADAOS_MLFLOW_SERVICE_BINDING_JSON") or "").strip()
            binding: ServiceBinding | str = endpoint
            auth_headers: dict[str, str] = {}
            if binding_json:
                raw = dict(json.loads(binding_json))
                raw.pop("schema", None)
                binding = ServiceBinding(**raw)
            elif urlsplit(endpoint).hostname not in {"127.0.0.1", "localhost", "::1"}:
                secret_ref = str(os.getenv("ADAOS_MLFLOW_SECRET_REF") or "").strip() or None
                binding = ServiceBinding(
                    binding_id="service-binding.mlflow.external",
                    capability="tracker.experiment",
                    provider_ref="service:mlflow-tracker",
                    consumer_ref="skill:research_manager_skill",
                    endpoint=endpoint,
                    protocol="mlflow-rest",
                    protocol_version="2.0",
                    health_endpoint=f"{endpoint.rstrip('/')}/health",
                    ui_endpoint=endpoint,
                    secret_ref=secret_ref,
                    metadata={"authentication": "bearer" if secret_ref else "none"},
                )
            if isinstance(binding, ServiceBinding) and binding.secret_ref:
                secret_name = binding.secret_ref.split(":", 1)[1]
                secret_value = get_secret(secret_name)
                if not secret_value:
                    raise RuntimeError("MLflow service binding secret is unavailable")
                header_name = str(binding.metadata.get("auth_header") or "Authorization")
                scheme = str(binding.metadata.get("auth_scheme") or "Bearer").strip()
                auth_headers[header_name] = f"{scheme} {secret_value}".strip()
            self._trackers[provider_id] = MlflowTracker(
                self.repository,
                binding,
                auth_headers=auth_headers,
            )
        return self._trackers[provider_id]

    def _tracker_for_binding(self, binding: ResearchRecord):
        return self._tracker_provider(str(binding.payload.get("tracker_provider") or "local-tracker"))

    @staticmethod
    def _acceptance_receipt(
        profile: str,
        *,
        checks: Sequence[Mapping[str, Any]],
        errors: Sequence[str],
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        identity = {
            "schema": "adaos.builder.acceptance_receipt.v1",
            "profile": str(profile),
            "ok": not errors,
            "checks": [dict(item) for item in checks],
            "errors": [str(item) for item in errors],
            "evidence": dict(evidence or {}),
        }
        return {**identity, "receipt_digest": digest(identity)}

    @staticmethod
    def _acceptance_split_bindings(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        raw = value.get("split_bindings") if isinstance(value.get("split_bindings"), Mapping) else value.get("splits")
        if not isinstance(raw, Mapping):
            raise ValueError("runner dataset_status must return split_bindings")
        result: dict[str, dict[str, Any]] = {}
        for role in ("validation", "robustness", "test"):
            item = raw.get(role)
            if not isinstance(item, Mapping):
                raise ValueError(f"runner dataset_status omits the {role} split binding")
            projected = {
                "digest": str(item.get("digest") or "").strip(),
                "dataset_digest": str(item.get("dataset_digest") or "").strip(),
                "locator": str(item.get("locator") or "").strip(),
                "sealed": bool(item.get("sealed")),
            }
            if any(not projected[key] for key in ("digest", "dataset_digest", "locator")):
                raise ValueError(f"runner {role} split binding has incomplete immutable identity")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", projected["digest"]):
                raise ValueError(f"runner {role} split digest is not sha256")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", projected["dataset_digest"]):
                raise ValueError(f"runner {role} dataset digest is not sha256")
            result[role] = projected
        if len({item["digest"] for item in result.values()}) != 3:
            raise ValueError("validation, robustness, and sealed test split digests must be distinct")
        if len({item["dataset_digest"] for item in result.values()}) != 1:
            raise ValueError("all split bindings must resolve to one immutable dataset")
        if result["test"]["sealed"] is not True:
            raise ValueError("runner test split binding must be sealed")
        return result

    @staticmethod
    def _acceptance_conditions(
        plan: Mapping[str, Any],
        *,
        runner_id: str,
        dataset_digest: str,
    ) -> dict[str, Any]:
        execution: dict[str, Any] = {}
        for stage_id, raw_profile in dict(plan["execution"]).items():
            profile = dict(raw_profile)
            seeds = list(profile.get("seeds") or [])
            if not seeds or any(
                isinstance(seed, bool) or not isinstance(seed, int)
                for seed in seeds
            ):
                raise ValueError(
                    f"ExperimentPlan execution.{stage_id}.seeds must contain "
                    "non-empty integer RNG seeds; pairing-unit labels belong in "
                    "randomization.allocation.planned_units"
                )
            if len(set(seeds)) != len(seeds):
                raise ValueError(
                    f"ExperimentPlan execution.{stage_id}.seeds must be unique"
                )
            evidence_class = str(profile["evidence_class"])
            manager_profile = (
                "preflight"
                if evidence_class == "workflow_smoke"
                else "confirmatory"
                if evidence_class == "confirmatory"
                else str(stage_id)
            )
            if manager_profile in execution:
                raise ValueError(f"ExperimentPlan maps multiple stages to {manager_profile}")
            execution[manager_profile] = {
                "source_stage_id": str(stage_id),
                "epochs": int(profile["epochs"]),
                "seeds": seeds,
                "device": str(profile["device"]),
                "network_mode": str(profile.get("network_mode") or "unrestricted"),
                "workers": 0,
                "wall_time_s": int(profile["max_wall_time_minutes"]) * 60,
                "workload": dict(profile.get("workload") or {}),
                "input_policy": dict(profile.get("input_policy") or {}),
                "evidence_class": evidence_class,
                "inference_allowed": bool(profile["inference_allowed"]),
            }
        analysis = dict(plan["analysis"])
        randomization = dict(plan["randomization"])
        dataset = dict(plan["dataset"])
        result_record = dict(dict(plan["runner_contract"])["result_record"])
        return {
            "dataset": {
                "name": str(dataset["logical_name"]),
                "version": str(dataset_digest),
                "policy_digest": str(dataset["policy_digest"]),
                "split_strategy": str(dataset["split_strategy"]),
                "evaluation_seal": str(dataset["evaluation_seal"]),
            },
            "operators": dict(plan["operators"]),
            "execution": execution,
            "randomization": {
                "named_streams": list(randomization["named_streams"]),
                "paired": True,
                "unit": str(randomization["unit"]),
                "invariant_fields": list(randomization["invariant_fields"]),
                "varied_fields": list(randomization["varied_fields"]),
            },
            "analysis": {
                "primary_metric": str(analysis["primary_metric"]),
                "primary_estimand": str(analysis["primary_estimand"]),
                "primary_contrast": dict(analysis["primary_contrast"]),
                "paired": True,
                "result_metric_path": str(result_record["primary_metric_path"]),
                "result_step_path": str(result_record["step_path"]),
                "initialization_digest_path": str(result_record["pairing_identity_path"]),
                "uncertainty": dict(analysis["uncertainty"]),
                "stopping_rule": dict(analysis["stopping_rule"]),
            },
            "tracker": {"provider": "local-tracker", "required_delivery": "durable-before-finalize"},
            "runner": {
                "provider": runner_id,
                "contract": "adaos.research.runner.v1",
                "data_owner": runner_id,
            },
        }

    @staticmethod
    def _workflow_smoke_contract() -> dict[str, Any]:
        return dict(runner_contract_descriptor()["workflow_smoke_evidence"])

    @classmethod
    def _validate_workflow_smoke_evidence(
        cls,
        *,
        trial: Mapping[str, Any],
        collected: Mapping[str, Any],
        verified_artifacts: Sequence[Mapping[str, Any]],
        expected_seed_labels: Sequence[str],
        expected_profile: Mapping[str, Any],
        expected_plan_digest: str | None = None,
        expected_system_digest: str | None = None,
        expected_arm: Mapping[str, Any] | None = None,
    ) -> None:
        contract = cls._workflow_smoke_contract()
        documents = dict(trial.get("documents") or {})
        schemas = dict(contract["documents"])
        for name, schema in schemas.items():
            if name not in documents:
                raise ValueError(f"workflow smoke omitted required document {name}")
            try:
                jsonschema.validate(documents[name], schema)
            except jsonschema.ValidationError as exc:
                location = ".".join(str(item) for item in exc.absolute_path)
                suffix = f" at {location}" if location else ""
                raise ValueError(
                    f"workflow smoke document {name} violates the consumer ABI{suffix}: "
                    f"{exc.message}"
                ) from exc

        run_log = dict(documents["run_log.json"])
        expected_workload = dict(expected_profile.get("workload") or {})
        actual_workload = dict(run_log.get("workload") or {})
        expected_limits = [
            dict(item)
            for item in expected_workload.get("limits") or []
            if isinstance(item, Mapping)
        ]
        actual_limits = [
            dict(item)
            for item in actual_workload.get("limits") or []
            if isinstance(item, Mapping)
        ]
        if (
            str(actual_workload.get("mode") or "")
            != str(expected_workload.get("mode") or "")
            or actual_limits != expected_limits
        ):
            raise ValueError("workflow smoke did not preserve the accepted workload bounds")
        observed = dict(actual_workload.get("observed") or {})
        for item in expected_limits:
            name = str(item.get("name") or "")
            value = observed.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"workflow smoke omitted observed workload unit {name}")
            if int(value) > int(item["maximum"]):
                raise ValueError(f"workflow smoke exceeded workload limit {name}")
        if dict(run_log.get("input_policy") or {}) != dict(
            expected_profile.get("input_policy") or {}
        ):
            raise ValueError("workflow smoke did not preserve the accepted input policy")
        run_network = dict(run_log.get("network") or {})
        expected_network_mode = str(expected_profile.get("network_mode") or "unrestricted")
        if str(run_network.get("mode") or "") != expected_network_mode:
            raise ValueError("workflow smoke did not preserve the accepted network mode")
        if expected_network_mode == "offline" and run_network.get("accessed") is not False:
            raise ValueError("workflow smoke reports network access under an offline policy")
        if list(run_log.get("seeds") or []) != list(expected_seed_labels):
            raise ValueError(
                "workflow smoke run_log.json seeds differ from the authoritative "
                "pairing-unit identities"
            )

        implementation = dict(documents["implementation_observation.json"])
        implementation_identity = dict(implementation["implementation"])
        if str(implementation.get("execution_path_digest") or "") != digest(
            implementation_identity
        ):
            raise ValueError(
                "workflow smoke execution_path_digest is not the canonical implementation digest"
            )
        expected_arm_value = dict(expected_arm or {})
        if expected_plan_digest and str(implementation.get("experiment_plan_digest") or "") != str(
            expected_plan_digest
        ):
            raise ValueError(
                "workflow smoke implementation observation differs from the accepted ExperimentPlan"
            )
        if expected_system_digest and str(implementation.get("system_digest") or "") != str(
            expected_system_digest
        ):
            raise ValueError(
                "workflow smoke implementation observation differs from the accepted scientific system"
            )
        if expected_arm_value:
            actual_arm = dict(implementation.get("arm") or {})
            if any(
                str(actual_arm.get(key) or "") != str(expected_arm_value.get(key) or "")
                for key in ("id", "role")
            ):
                raise ValueError(
                    "workflow smoke implementation observation differs from the requested arm"
                )

        outputs = {
            str(item.get("path") or ""): dict(item)
            for item in trial.get("outputs") or []
            if isinstance(item, Mapping)
        }
        index_files = [
            dict(item)
            for item in dict(documents["artifacts_index.json"]).get("files") or []
            if isinstance(item, Mapping)
        ]
        if any(str(item.get("path") or "") == "artifacts_index.json" for item in index_files):
            raise ValueError(
                "workflow-smoke artifacts_index.json must not index itself; "
                "self-indexing cannot have a stable content digest"
            )
        artifacts = [
            dict(item)
            for item in collected.get("artifacts") or []
            if isinstance(item, Mapping)
        ]
        if int(collected.get("tracker_session_calls") or 0) != 0:
            raise ValueError(
                "workflow-smoke provider crossed the tracking boundary; "
                "tracker_session_calls must be 0"
            )
        if not bool(collected.get("complete")):
            raise ValueError("workflow-smoke collection is not complete")
        index_digests = [str(item.get("digest") or "") for item in index_files]
        artifact_digests = [str(item.get("digest") or "") for item in artifacts]
        if len(set(index_digests)) != len(index_digests):
            raise ValueError(
                "artifacts_index.json contains duplicate artifact identities"
            )
        if len(set(artifact_digests)) != len(artifact_digests):
            raise ValueError("collect_attempt returned duplicate artifact identities")
        if set(index_digests) != set(artifact_digests):
            missing = sorted(set(index_digests) - set(artifact_digests))
            extra = sorted(set(artifact_digests) - set(index_digests))
            raise ValueError(
                "workflow-smoke collection must exactly equal artifacts_index.json.files "
                f"by digest and exclude the index itself; missing={missing}, extra={extra}"
            )
        if len(verified_artifacts) != len(artifacts) or not all(
            bool(item.get("ok") or item.get("verified"))
            for item in verified_artifacts
        ):
            raise ValueError("verify_artifact rejected one or more indexed identities")

        collected_by_digest = dict(zip(artifact_digests, artifacts, strict=True))
        for item in index_files:
            path = str(item.get("path") or "")
            digest_value = str(item.get("digest") or "")
            content_ref = dict(item.get("content_ref") or {})
            output = outputs.get(path)
            collected_item = collected_by_digest.get(digest_value)
            if output is None or collected_item is None:
                raise ValueError(
                    f"indexed workflow-smoke artifact {path or '<empty>'} has no "
                    "matching trial output and collection identity"
                )
            if any(
                str(value or "") != digest_value
                for value in (
                    output.get("digest"),
                    content_ref.get("digest"),
                    collected_item.get("digest"),
                )
            ):
                raise ValueError(
                    f"workflow-smoke artifact {path} has inconsistent content digests"
                )

    @classmethod
    def _prepare_acceptance_arm(
        cls,
        *,
        developer_validation: Any,
        candidate_id: str,
        compilation: Mapping[str, Any],
        plan: Mapping[str, Any],
        brief: Mapping[str, Any],
        conditions: Mapping[str, Any],
        manager_profile: str,
        profile_conditions: Mapping[str, Any],
        input_policy: Mapping[str, Any],
        network_mode: str,
        seeds: Sequence[int],
        arm: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Prepare one arm without allowing the consumer to rewrite its semantics."""

        arm_value = dict(arm)
        arm_id = str(arm_value.get("id") or "").strip()
        if not arm_id:
            raise ValueError("ExperimentPlan arm has no stable id")
        arm_token = re.sub(r"[^A-Za-z0-9_.-]+", "-", arm_id).strip("-.")
        smoke_request = {
            "experiment_id": f"acceptance:{str(compilation.get('digest') or '')[-16:]}",
            "experiment_revision_id": str(plan.get("digest") or "acceptance-plan"),
            "trial_id": f"acceptance-trial-{arm_token}",
            "run_id": f"acceptance-run-{arm_token}",
            "attempt_number": 1,
            "profile": str(manager_profile),
            "seed": int(seeds[0]),
            "arm": arm_value,
            "conditions": dict(conditions),
            "profile_conditions": dict(profile_conditions),
        }
        prepared = developer_validation.invoke_skill(
            candidate_id,
            "prepare_attempt",
            {"request": smoke_request},
            timeout=120,
        )
        if not isinstance(prepared, Mapping):
            raise ValueError("prepare_attempt returned a non-object value")
        prepared = dict(prepared)
        if prepared.get("contract") != "adaos.research.runner.v1":
            raise ValueError("prepare_attempt returned an incompatible runner contract")
        if str(prepared.get("provider_id") or "") != candidate_id:
            raise ValueError("prepare_attempt returned another provider identity")
        package = dict(prepared.get("package_ref") or {})
        package_ref = ContentRef(
            uri=str(package["uri"]),
            digest=str(package["digest"]),
            size_bytes=int(package["size_bytes"]),
            media_type=str(package["media_type"]),
            owner_ref=str(package["owner_ref"]),
            kind=str(package.get("kind") or "execution-package"),
            metadata=dict(package.get("metadata") or {}),
        )
        command = [str(item) for item in prepared.get("command") or []]
        expected_outputs = [str(item) for item in prepared.get("expected_outputs") or []]
        required_smoke_outputs = [
            str(item)
            for item in cls._workflow_smoke_contract().get("required_expected_outputs") or []
        ]
        missing_smoke_outputs = [
            item for item in required_smoke_outputs if item not in expected_outputs
        ]
        if missing_smoke_outputs:
            raise ValueError(
                "prepare_attempt expected_outputs omits workflow-smoke consumer documents: "
                + ", ".join(missing_smoke_outputs)
            )
        required_fields = {
            "code_digest": str(prepared.get("code_digest") or ""),
            "environment_digest": str(prepared.get("environment_digest") or ""),
            "working_directory": str(prepared.get("working_directory") or ""),
            "output_ref": str(prepared.get("output_ref") or ""),
            "spec_id": str(prepared.get("spec_id") or ""),
        }
        if len(command) < 2 or any(not item for item in required_fields.values()):
            raise ValueError("prepare_attempt returned an incomplete executable package")
        if not all(
            re.fullmatch(r"sha256:[0-9a-f]{64}", required_fields[key])
            for key in ("code_digest", "environment_digest")
        ):
            raise ValueError("runner code or environment identity is not a SHA-256 digest")
        protocol_digest = str(brief.get("prototype_digest") or "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", protocol_digest):
            raise ValueError("AutomationBrief has no immutable ResearchPrototype identity")
        execution_spec = ExecutionSpec(
            spec_id=required_fields["spec_id"],
            owner_ref=f"skill:{candidate_id}",
            command=tuple(command),
            working_directory=required_fields["working_directory"],
            trial_id=smoke_request["trial_id"],
            run_id=smoke_request["run_id"],
            package_ref=package_ref,
            code_digest=required_fields["code_digest"],
            environment_digest=required_fields["environment_digest"],
            environment={
                str(key): str(item)
                for key, item in dict(prepared.get("environment") or {}).items()
            }
            | {
                "ADAOS_RESEARCH_WORKLOAD_JSON": json.dumps(
                    dict(profile_conditions.get("workload") or {}),
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                "ADAOS_RESEARCH_INPUT_POLICY_JSON": json.dumps(
                    dict(input_policy),
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            },
            resources=ExecutionResourceRequest(
                cpu_cores=int(profile_conditions.get("cpu_threads") or 2),
                memory_mb=int(profile_conditions.get("memory_mb") or 4096),
                wall_time_s=int(profile_conditions.get("wall_time_s") or 1800),
                max_log_bytes=int(
                    profile_conditions.get("max_log_bytes") or 2 * 1024 * 1024
                ),
            ),
            network=ExecutionNetworkPolicy(mode=network_mode),
            determinism=ExecutionDeterminism(
                mode="exploratory",
                rng_streams={
                    name: int(seeds[0]) + index
                    for index, name in enumerate(ExecutionDeterminism.REQUIRED_STREAMS)
                },
                deterministic_algorithms=True,
            ),
            budget=ExecutionBudget(
                max_attempts=1,
                max_compute_seconds=int(
                    profile_conditions.get("max_compute_seconds")
                    or profile_conditions.get("wall_time_s")
                    or 1800
                ),
                max_storage_bytes=int(
                    profile_conditions.get("max_storage_bytes")
                    or 2 * 1024 * 1024 * 1024
                ),
            ),
            expected_outputs=tuple(expected_outputs),
            metadata={
                "protocol_digest": protocol_digest,
                "stage": "workflow_smoke",
                "evidence_class": "workflow_smoke",
                "epochs": int(profile_conditions["epochs"]),
                "seeds": [int(item) for item in seeds],
                "inference_allowed": bool(profile_conditions.get("inference_allowed")),
                "workload": dict(profile_conditions.get("workload") or {}),
                "input_policy": dict(input_policy),
                "network_mode": network_mode,
                "runner_output_ref": required_fields["output_ref"],
                "manager_profile": manager_profile,
                "arm_id": arm_id,
                "arm_role": str(arm_value.get("role") or ""),
                "experiment_plan_digest": str(plan.get("digest") or ""),
                "system_digest": str(dict(plan.get("system") or {}).get("digest") or ""),
            },
        )
        return {
            "arm": arm_value,
            "prepared_attempt": prepared,
            "execution_spec": execution_spec.to_dict(),
            "execution_spec_digest": execution_spec.digest,
            "output_ref": required_fields["output_ref"],
            "protocol_digest": protocol_digest,
        }

    @classmethod
    def _execute_acceptance_arm(
        cls,
        *,
        developer_validation: Any,
        candidate_id: str,
        prepared_arm: Mapping[str, Any],
        plan: Mapping[str, Any],
        profile_conditions: Mapping[str, Any],
        seeds: Sequence[int],
    ) -> dict[str, Any]:
        arm = dict(prepared_arm["arm"])
        execution_spec = dict(prepared_arm["execution_spec"])
        trial = developer_validation.execute_spec(
            candidate_id,
            execution_spec,
            idempotency_key=(
                "consumer-smoke-"
                + digest(
                    {
                        "candidate_id": candidate_id,
                        "protocol_digest": str(prepared_arm["protocol_digest"]),
                        "spec_digest": str(prepared_arm["execution_spec_digest"]),
                        "arm_id": str(arm.get("id") or ""),
                    }
                ).removeprefix("sha256:")[:24]
            ),
            timeout=min(float(profile_conditions.get("wall_time_s") or 1800), 3600),
        )
        if not bool(trial.get("ok")):
            raise RuntimeError(
                f"consumer workflow smoke failed for arm {arm.get('id')}: "
                + str(trial.get("failure") or trial.get("missing_outputs") or "unknown")
            )
        collected = developer_validation.invoke_skill(
            candidate_id,
            "collect_attempt",
            {"output_ref": str(prepared_arm["output_ref"])},
            timeout=120,
        )
        if not isinstance(collected, Mapping):
            raise ValueError("collect_attempt returned a non-object value")
        collected = dict(collected)
        observations = [
            dict(item)
            for item in collected.get("observations") or []
            if isinstance(item, Mapping)
        ]
        if len(observations) != len(list(collected.get("observations") or [])):
            raise ValueError("collect_attempt returned a non-object observation")
        acceptance_session = {
            "session_id": (
                "acceptance-session-"
                + digest(
                    {
                        "candidate_id": candidate_id,
                        "arm_id": str(arm.get("id") or ""),
                        "spec_digest": str(prepared_arm["execution_spec_digest"]),
                    }
                ).removeprefix("sha256:")[:24]
            ),
            "attempt_id": (
                "acceptance-attempt-"
                + digest(
                    {
                        "candidate_id": candidate_id,
                        "arm_id": str(arm.get("id") or ""),
                    }
                ).removeprefix("sha256:")[:24]
            ),
            "opened_at": "1970-01-01T00:00:00+00:00",
        }
        normalized_observations = [
            normalize_observation(acceptance_session, item)
            for item in observations
        ]
        artifacts = [
            dict(item)
            for item in collected.get("artifacts") or []
            if isinstance(item, Mapping)
        ]
        for artifact in artifacts:
            if not str(artifact.get("role") or "").strip():
                raise ValueError(
                    "collect_attempt artifact role is required by ResearchManager ingestion"
                )
        verified_artifacts = []
        for artifact in artifacts:
            ContentRef(
                uri=str(artifact["uri"]),
                digest=str(artifact["digest"]),
                size_bytes=int(artifact["size_bytes"]),
                media_type=str(artifact["media_type"]),
                owner_ref=str(artifact["owner_ref"]),
                kind=str(artifact.get("kind") or "workflow-smoke-evidence"),
                metadata=dict(artifact.get("metadata") or {}),
            )
            verified = developer_validation.invoke_skill(
                candidate_id,
                "verify_artifact",
                {
                    "uri": str(artifact.get("uri") or ""),
                    "digest": str(artifact.get("digest") or ""),
                },
                timeout=60,
            )
            verified_artifacts.append(dict(verified or {}))
        cls._validate_workflow_smoke_evidence(
            trial=trial,
            collected=collected,
            verified_artifacts=verified_artifacts,
            expected_seed_labels=[f"seed-{int(item)}" for item in seeds],
            expected_profile=profile_conditions,
            expected_plan_digest=str(plan.get("digest") or ""),
            expected_system_digest=str(dict(plan.get("system") or {}).get("digest") or "") or None,
            expected_arm=arm,
        )
        return {
            **dict(prepared_arm),
            "trial": trial,
            "collected": collected,
            "normalized_observations": normalized_observations,
            "verified_artifacts": verified_artifacts,
            "collection_ok": bool(collected.get("complete")) and bool(artifacts),
            "verification_ok": len(verified_artifacts) == len(artifacts)
            and all(bool(item.get("ok")) for item in verified_artifacts),
        }

    def validate_development_candidate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate a DEV runner from the consumer side, without scientific execution.

        Builder supplies a digest-verified Development Session envelope.  This
        method deliberately invokes the active DEV skill through the platform
        SDK instead of importing candidate code or trusting candidate tests.
        The real three-epoch run remains a governed Study action after release.
        """

        value = dict(request or {})
        profile = str(value.get("profile") or "").strip()
        execute_workflow_smoke = bool(
            value.get(
                "execute_workflow_smoke",
                profile == "research.consumer-contracts",
            )
        )
        checks: list[dict[str, Any]] = []
        errors: list[str] = []
        candidate_ref = str(value.get("candidate_ref") or "").strip()
        kind, separator, candidate_id = candidate_ref.partition(":")
        if separator != ":" or kind != "skill" or not candidate_id:
            errors.append("candidate_ref must identify one DEV skill")
            return self._acceptance_receipt(profile, checks=checks, errors=errors)
        instructions = (
            dict(value.get("instructions"))
            if isinstance(value.get("instructions"), Mapping)
            else {}
        )
        compilation = (
            dict(instructions.get("research_compilation"))
            if isinstance(instructions.get("research_compilation"), Mapping)
            else {}
        )
        brief = (
            dict(instructions.get("automation_brief"))
            if isinstance(instructions.get("automation_brief"), Mapping)
            else {}
        )
        consumer_contract = (
            dict(instructions.get("consumer_contract"))
            if isinstance(instructions.get("consumer_contract"), Mapping)
            else {}
        )
        contract_inputs = {
            str(item.get("kind") or ""): dict(item)
            for item in value.get("contract_inputs") or []
            if isinstance(item, Mapping)
        }

        if profile == "research.traceability":
            expected = {
                "research_compilation": str(compilation.get("digest") or ""),
                "automation_brief": str(brief.get("digest") or ""),
                "consumer_contract": str(consumer_contract.get("digest") or ""),
            }
            for contract_kind, expected_digest in expected.items():
                descriptor = contract_inputs.get(contract_kind) or {}
                actual = str(descriptor.get("digest") or "")
                ok = bool(expected_digest) and actual == expected_digest
                checks.append(
                    {
                        "id": f"traceability.{contract_kind}",
                        "ok": ok,
                        "expected_digest": expected_digest or None,
                        "actual_digest": actual or None,
                    }
                )
                if not ok:
                    errors.append(f"{contract_kind} digest is absent or differs from its contract input")
            brief_compilation = str(brief.get("compilation_digest") or "")
            compilation_digest = str(compilation.get("digest") or "")
            linked = bool(compilation_digest) and brief_compilation == compilation_digest
            checks.append(
                {
                    "id": "traceability.brief_to_compilation",
                    "ok": linked,
                    "compilation_digest": compilation_digest or None,
                }
            )
            if not linked:
                errors.append("AutomationBrief is not linked to the exact ResearchCompilation")
            return self._acceptance_receipt(
                profile,
                checks=checks,
                errors=errors,
                evidence={
                    "candidate_ref": candidate_ref,
                    "development_session_id": value.get("development_session_id"),
                },
            )

        if profile != "research.consumer-contracts":
            errors.append(f"unsupported acceptance profile: {profile}")
            return self._acceptance_receipt(profile, checks=checks, errors=errors)

        canonical_contract = runner_contract_descriptor()
        supplied_contract_digest = str(consumer_contract.get("digest") or "")
        if supplied_contract_digest != canonical_contract["digest"] or consumer_contract != canonical_contract:
            errors.append("Development Session consumer_contract differs from the active ResearchManager ABI")
            return self._acceptance_receipt(
                profile,
                checks=[
                    {
                        "id": "runner.consumer_contract_identity",
                        "ok": False,
                        "expected_digest": canonical_contract["digest"],
                        "actual_digest": supplied_contract_digest or None,
                    }
                ],
                errors=errors,
            )
        checks.append(
            {
                "id": "runner.consumer_contract_identity",
                "ok": True,
                "digest": supplied_contract_digest,
            }
        )

        if (
            compilation.get("schema") == "adaos.research.compilation_projection.v1"
            and isinstance(compilation.get("experiment_plan"), Mapping)
        ):
            # Builder receives the compact, content-addressed public execution
            # projection.  It deliberately omits audit-only facet wrappers but
            # carries the exact accepted ExperimentPlan at the top level.
            plan = dict(compilation["experiment_plan"])
        else:
            plan_facet = (
                dict(dict(compilation.get("facets") or {}).get("experiment_plan") or {})
                if isinstance(compilation.get("facets"), Mapping)
                else {}
            )
            plan = (
                dict(plan_facet.get("payload") or {})
                if isinstance(plan_facet.get("payload"), Mapping)
                else {}
            )
        if not plan:
            errors.append("accepted ResearchCompilation has no ExperimentPlan payload")
            return self._acceptance_receipt(profile, checks=checks, errors=errors)

        consumer_evidence: dict[str, Any] = {
            "candidate_ref": candidate_ref,
            "compilation_digest": compilation.get("digest"),
            "scientific_execution_started": False,
        }
        try:
            from adaos.sdk.developer import validation as developer_validation

            native = developer_validation.validate_skill(
                candidate_id,
                strict=True,
                probe_tools=True,
                run_tests=True,
            )
            checks.append(
                {
                    "id": "candidate.native_validation",
                    "ok": bool(native.get("ok")),
                    "digest": native.get("digest"),
                    "source_digest": native.get("source_digest"),
                }
            )
            if not bool(native.get("ok")):
                errors.append("candidate failed native DEV validation or packaged tests")
            activation = developer_validation.activate_skill(candidate_id)
            checks.append(
                {
                    "id": "candidate.dev_activation",
                    "ok": bool(activation.get("ok")),
                    "version": activation.get("version"),
                }
            )
            dataset_status = developer_validation.invoke_skill(
                candidate_id,
                "dataset_status",
                {},
                timeout=60,
            )
            if not isinstance(dataset_status, Mapping):
                raise ValueError("dataset_status returned a non-object value")
            dataset_status = dict(dataset_status)
            consumer_evidence["dataset_status"] = dataset_status
            splits = self._acceptance_split_bindings(dataset_status)
            expected_dataset = str(dict(plan["dataset"])["id"])
            actual_dataset = str(
                dataset_status.get("dataset_id")
                or dataset_status.get("logical_name")
                or dataset_status.get("id")
                or ""
            )
            dataset_matches = actual_dataset == expected_dataset
            checks.append(
                {
                    "id": "runner.dataset_binding",
                    "ok": dataset_matches,
                    "expected_dataset": expected_dataset,
                    "actual_dataset": actual_dataset or None,
                    "dataset_digest": splits["validation"]["dataset_digest"],
                    "ready": dataset_status.get("ready"),
                }
            )
            if not dataset_matches:
                errors.append("runner dataset identity differs from the accepted ExperimentPlan")

            conditions = self._acceptance_conditions(
                plan,
                runner_id=candidate_id,
                dataset_digest=splits["validation"]["dataset_digest"],
            )
            smoke_profile = next(
                (
                    (profile_id, dict(profile_value))
                    for profile_id, profile_value in dict(conditions["execution"]).items()
                    if str(dict(profile_value).get("evidence_class") or "") == "workflow_smoke"
                ),
                None,
            )
            if smoke_profile is None:
                raise ValueError("ExperimentPlan has no workflow_smoke execution profile")
            manager_profile, profile_conditions = smoke_profile
            input_policy = dict(profile_conditions.get("input_policy") or {})
            network_mode = str(profile_conditions.get("network_mode") or "unrestricted")
            execution_ready_without_network = bool(
                dataset_status.get("execution_ready_without_network")
            )
            readiness_ok = bool(
                input_policy.get("readiness") != "required_before_execution"
                or (
                    execution_ready_without_network
                    if network_mode == "offline"
                    else bool(dataset_status.get("ready"))
                    or input_policy.get("source") == "deterministic_contract_fixture"
                )
            )
            accepted_dataset_ready = bool(
                input_policy.get("source") != "accepted_dataset"
                or dataset_status.get("ready")
            )
            checks.append(
                {
                    "id": "runner.input_readiness",
                    "ok": readiness_ok and accepted_dataset_ready,
                    "source": input_policy.get("source"),
                    "readiness": input_policy.get("readiness"),
                    "network_mode": network_mode,
                    "dataset_ready": bool(dataset_status.get("ready")),
                    "execution_ready_without_network": execution_ready_without_network,
                }
            )
            if not readiness_ok:
                raise ValueError(
                    "workflow smoke input is not ready under the accepted network policy"
                )
            if not accepted_dataset_ready:
                raise ValueError("accepted dataset is not ready before workflow smoke")
            seeds = list(profile_conditions.get("seeds") or [])
            if len(seeds) != 1:
                raise ValueError("workflow_smoke must expose one bounded seed")
            arms = [dict(item) for item in dict(plan["operators"])["arms"] if isinstance(item, Mapping)]
            baseline = next((item for item in arms if item.get("role") == "baseline"), None)
            if baseline is None:
                raise ValueError("ExperimentPlan has no baseline arm")
            intervention = next(
                (item for item in arms if item.get("role") == "intervention"), None
            )
            # ExperimentPlan v1.4 makes the system boundary executable.  Its
            # workflow smoke must therefore exercise both sides of the
            # intervention, while legacy plans retain the prior baseline-only
            # preparation behavior for stored-record compatibility.
            smoke_arms = [baseline]
            if isinstance(plan.get("system"), Mapping):
                if intervention is None:
                    raise ValueError("ExperimentPlan system has no intervention arm")
                smoke_arms.append(intervention)
            prepared_arms = [
                self._prepare_acceptance_arm(
                    developer_validation=developer_validation,
                    candidate_id=candidate_id,
                    compilation=compilation,
                    plan=plan,
                    brief=brief,
                    conditions=conditions,
                    manager_profile=str(manager_profile),
                    profile_conditions=profile_conditions,
                    input_policy=input_policy,
                    network_mode=network_mode,
                    seeds=[int(item) for item in seeds],
                    arm=arm,
                )
                for arm in smoke_arms
            ]
            first_prepared = prepared_arms[0]
            consumer_evidence.update(
                {
                    "experiment_plan_digest": plan.get("digest"),
                    "system_digest": dict(plan.get("system") or {}).get("digest"),
                    "prepared_attempt": first_prepared["prepared_attempt"],
                    "execution_spec": first_prepared["execution_spec"],
                    "prepared_arms": prepared_arms,
                    "paired_smoke_required": len(smoke_arms) == 2,
                }
            )
            checks.append(
                {
                    "id": "runner.prepare_attempt",
                    "ok": True,
                    "contract": "adaos.research.runner.v1",
                    "provider_id": candidate_id,
                    "arm_ids": [str(item["arm"]["id"]) for item in prepared_arms],
                    "arm_count": len(prepared_arms),
                    "profile": manager_profile,
                    "seed": seeds[0],
                }
            )
            if execute_workflow_smoke:
                arm_trials = [
                    self._execute_acceptance_arm(
                        developer_validation=developer_validation,
                        candidate_id=candidate_id,
                        prepared_arm=item,
                        plan=plan,
                        profile_conditions=profile_conditions,
                        seeds=[int(seed) for seed in seeds],
                    )
                    for item in prepared_arms
                ]
                first_trial = arm_trials[0]
                collection_ok = all(bool(item["collection_ok"]) for item in arm_trials)
                verification_ok = all(bool(item["verification_ok"]) for item in arm_trials)
                checks.extend(
                    [
                        {
                            "id": "runner.workflow_smoke",
                            "ok": all(bool(item["trial"].get("ok")) for item in arm_trials),
                            "trial_digests": [item["trial"].get("digest") for item in arm_trials],
                            "arm_ids": [str(item["arm"]["id"]) for item in arm_trials],
                            "paired": len(arm_trials) == 2,
                        },
                        {
                            "id": "runner.collection",
                            "ok": collection_ok,
                            "artifact_count": sum(
                                len(item["collected"].get("artifacts") or [])
                                for item in arm_trials
                            ),
                        },
                        {
                            "id": "runner.artifact_verification",
                            "ok": verification_ok,
                            "verified_count": sum(
                                1
                                for arm_trial in arm_trials
                                for item in arm_trial["verified_artifacts"]
                                if item.get("ok")
                            ),
                        },
                    ]
                )
                if not collection_ok:
                    errors.append("collect_attempt did not return complete portable evidence")
                if not verification_ok:
                    errors.append("verify_artifact rejected one or more collected identities")
                consumer_evidence.update(
                    {
                        "trial": first_trial["trial"],
                        "collected": first_trial["collected"],
                        "verified_artifacts": first_trial["verified_artifacts"],
                        "arm_trials": arm_trials,
                        "scientific_execution_started": False,
                        "workflow_smoke_executed": True,
                        "paired_workflow_smoke_executed": len(arm_trials) == 2,
                    }
                )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            checks.append(
                {
                    "id": "runner.consumer_probe",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        return self._acceptance_receipt(
            profile,
            checks=checks,
            errors=errors,
            evidence=consumer_evidence,
        )

    def create_study(
        self,
        *,
        title: str,
        hypothesis: str,
        protocol: Mapping[str, Any],
        analysis_plan: Mapping[str, Any],
        splits: Mapping[str, Mapping[str, Any]],
        mode: str,
        study_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if mode not in {"exploratory", "confirmatory"}:
            raise ValueError("study mode must be exploratory or confirmatory")
        title = str(title or "").strip()
        hypothesis = str(hypothesis or "").strip()
        if not title or not hypothesis:
            raise ValueError("title and hypothesis are required")
        study_id = str(study_id or "").strip() or identity("study", {"title": title})

        def apply() -> Mapping[str, Any]:
            split_digests = {
                role: str((splits.get(role) or {}).get("digest") or "").strip()
                for role in ("validation", "robustness", "test")
            }
            if any(not value for value in split_digests.values()):
                raise ValueError("validation, robustness and test split digests are required")
            if len(set(split_digests.values())) != 3:
                raise ValueError("validation and robustness splits must not alias sealed test")
            study = self.repository.put(
                ResearchRecord("study", study_id, study_id, 0, {"title": title, "mode": mode})
            )
            hypothesis_id = identity("hypothesis", {"study_id": study_id, "statement": hypothesis})
            hypothesis_record = self.repository.put(
                ResearchRecord(
                    "hypothesis",
                    hypothesis_id,
                    study_id,
                    0,
                    {"statement": hypothesis, "status": "proposed"},
                )
            )
            protocol_digest = digest(protocol)
            protocol_id = identity(
                "protocol",
                {"study_id": study_id, "version": 1, "content_digest": protocol_digest},
            )
            protocol_record = self.repository.put(
                ResearchRecord(
                    "protocol",
                    protocol_id,
                    study_id,
                    1,
                    {
                        "version": 1,
                        "content": dict(protocol),
                        "content_digest": protocol_digest,
                        "parent_digest": None,
                        "amendment_reason": None,
                    },
                )
            )
            plan_digest = digest(analysis_plan)
            plan_id = identity(
                "analysis_plan",
                {"study_id": study_id, "version": 1, "content_digest": plan_digest},
            )
            plan_record = self.repository.put(
                ResearchRecord(
                    "analysis_plan",
                    plan_id,
                    study_id,
                    1,
                    {"version": 1, "content": dict(analysis_plan), "content_digest": plan_digest},
                )
            )
            for role, split_digest in split_digests.items():
                source = dict(splits[role])
                split_id = identity(
                    "split",
                    {"study_id": study_id, "dataset_digest": source.get("dataset_digest", split_digest), "role": role, "indices_digest": split_digest},
                )
                self.repository.put(
                    ResearchRecord(
                        "split_binding",
                        split_id,
                        study_id,
                        0,
                        {
                            "role": role,
                            "digest": split_digest,
                            "dataset_digest": source.get("dataset_digest", split_digest),
                            "locator": source.get("locator"),
                            "sealed": role == "test",
                        },
                    )
                )
            self.repository.event(study_id, "research.study.created", {"mode": mode})
            return {
                "study": study.to_dict(),
                "hypothesis": hypothesis_record.to_dict(),
                "protocol": protocol_record.to_dict(),
                "analysis_plan": plan_record.to_dict(),
                "workflow": workflow_state(self.repository, study_id),
            }

        return self.repository.once(idempotency_key, "create_study", apply)

    @staticmethod
    def _study_realization_payload(realization: Mapping[str, Any]) -> dict[str, Any]:
        value = {
            "schema": "adaos.research.study_realization.v1",
            **{
                key: str(realization.get(key) or "").strip()
                for key in (
                    "direction_ref",
                    "task_ref",
                    "compilation_ref",
                    "compilation_digest",
                    "implementation_track_ref",
                    "development_session_id",
                    "project_release_ref",
                    "project_release_digest",
                    "runner_ref",
                    "runner_contract",
                )
            },
        }
        prefixes = {
            "direction_ref": "research-direction:",
            "task_ref": "research-task:",
            "compilation_ref": "research-compilation:",
            "implementation_track_ref": "implementation-track:",
            "project_release_ref": "project-release:",
            "runner_ref": "skill:",
        }
        missing = [key for key, prefix in prefixes.items() if not value[key].startswith(prefix)]
        for key in ("compilation_digest", "project_release_digest"):
            token = value[key]
            if len(token) != 71 or not token.startswith("sha256:"):
                missing.append(key)
            else:
                try:
                    int(token[7:], 16)
                except ValueError:
                    missing.append(key)
        if not value["development_session_id"]:
            missing.append("development_session_id")
        if value["runner_contract"] != "adaos.research.runner.v1":
            missing.append("runner_contract")
        if missing:
            raise ValueError(
                "study realization has invalid or missing bindings: "
                + ", ".join(sorted(set(missing)))
            )
        return value

    def create_compiled_study(
        self,
        *,
        title: str,
        hypothesis: str,
        protocol: Mapping[str, Any],
        analysis_plan: Mapping[str, Any],
        splits: Mapping[str, Mapping[str, Any]],
        realization: Mapping[str, Any],
        mode: str,
        study_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        binding = self._study_realization_payload(realization)
        resolved_study_id = str(study_id or "").strip() or identity(
            "study",
            {
                "compilation_digest": binding["compilation_digest"],
                "project_release_digest": binding["project_release_digest"],
            },
        )

        def apply() -> Mapping[str, Any]:
            created = self.create_study(
                title=title,
                hypothesis=hypothesis,
                protocol=protocol,
                analysis_plan=analysis_plan,
                splits=splits,
                mode=mode,
                study_id=resolved_study_id,
                idempotency_key=f"{idempotency_key}:study",
            )
            realization_id = identity(
                "study_realization",
                {
                    "study_id": resolved_study_id,
                    "compilation_digest": binding["compilation_digest"],
                    "project_release_digest": binding["project_release_digest"],
                    "runner_ref": binding["runner_ref"],
                },
            )
            record = self.repository.put(
                ResearchRecord(
                    "study_realization",
                    realization_id,
                    resolved_study_id,
                    0,
                    binding,
                )
            )
            self.repository.event(
                resolved_study_id,
                "research.study.realization_bound",
                {
                    "realization_id": realization_id,
                    "realization_digest": record.digest,
                    "compilation_digest": binding["compilation_digest"],
                    "project_release_digest": binding["project_release_digest"],
                    "runner_ref": binding["runner_ref"],
                },
            )
            return {**created, "realization": record.to_dict()}

        return self.repository.once(
            idempotency_key,
            "create_compiled_study",
            apply,
        )

    def advance(
        self,
        *,
        study_id: str,
        command: str,
        expected_generation: int,
        idempotency_key: str,
        actor: str,
        evidence_refs: Sequence[str] = (),
    ) -> dict[str, Any]:
        if command == "lock_protocol":
            protocols = self.repository.list(study_id, "protocol")
            plans = self.repository.list(study_id, "analysis_plan")
            if not protocols or not plans:
                raise ValueError("protocol and analysis plan are required")
            evidence_refs = (*evidence_refs, protocols[-1].digest, plans[-1].digest)
        return transition(
            self.repository,
            study_id=study_id,
            command=command,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
            evidence_refs=tuple(evidence_refs),
        )

    def amend_protocol(
        self,
        *,
        study_id: str,
        content: Mapping[str, Any],
        reason: str,
        prior_trials: str,
        expected_generation: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        if prior_trials not in {"retain", "invalidate"}:
            raise ValueError("prior_trials must be retain or invalidate")

        def apply() -> Mapping[str, Any]:
            current = workflow_state(self.repository, study_id)
            if current["generation"] != int(expected_generation):
                raise ValueError("stale workflow generation")
            protocols = self.repository.list(study_id, "protocol")
            if not protocols:
                raise ValueError("study protocol is missing")
            previous = protocols[-1]
            version = int(previous.payload["version"]) + 1
            content_digest = digest(content)
            protocol_id = identity(
                "protocol",
                {"study_id": study_id, "version": version, "content_digest": content_digest},
            )
            amended = self.repository.put(
                ResearchRecord(
                    "protocol",
                    protocol_id,
                    study_id,
                    version,
                    {
                        "version": version,
                        "content": dict(content),
                        "content_digest": content_digest,
                        "parent_digest": previous.digest,
                        "amendment_reason": str(reason),
                        "amended_by": actor,
                    },
                )
            )
            dispositions = []
            for trial in self.repository.list(study_id, "trial"):
                disposition_id = identity(
                    "trial_disposition",
                    {"trial_id": trial.record_id, "protocol_digest": amended.digest, "disposition": prior_trials},
                )
                dispositions.append(
                    self.repository.put(
                        ResearchRecord(
                            "trial_disposition",
                            disposition_id,
                            study_id,
                            version,
                            {"trial_id": trial.record_id, "disposition": prior_trials, "protocol_digest": amended.digest},
                        )
                    ).to_dict()
                )
            event = self.repository.event(
                study_id,
                "research.protocol.amended",
                {
                    "protocol_id": amended.record_id,
                    "parent_digest": previous.digest,
                    "prior_trials": prior_trials,
                    "generation": current["generation"] + 1,
                    "actor": actor,
                },
            )
            return {"protocol": amended.to_dict(), "trial_dispositions": dispositions, "event": event, "workflow": workflow_state(self.repository, study_id)}

        return self.repository.once(idempotency_key, "amend_protocol", apply)

    def materialize_trials(
        self,
        *,
        study_id: str,
        matrix: Sequence[Mapping[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        def apply() -> Mapping[str, Any]:
            current = workflow_state(self.repository, study_id)
            if current["state"] not in {"locked", "smoke"}:
                raise ValueError("trials require a locked protocol")
            protocol = self.repository.list(study_id, "protocol")[-1]
            plan = self.repository.list(study_id, "analysis_plan")[-1]
            groups: list[dict[str, Any]] = []
            trials: list[dict[str, Any]] = []
            for group_index, item in enumerate(matrix):
                pair_key = str(item.get("pair_key") or f"pair-{group_index + 1}")
                operators = [dict(value) for value in item.get("operators") or []]
                if len(operators) < 2:
                    raise ValueError("paired trial group requires at least two operators")
                group_id = identity("trial_group", {"study_id": study_id, "pair_key": pair_key, "protocol": protocol.digest})
                group = self.repository.put(
                    ResearchRecord("trial_group", group_id, study_id, protocol.generation, {"pair_key": pair_key, "operator_count": len(operators)})
                )
                groups.append(group.to_dict())
                for operator in operators:
                    operator_digest = digest(operator)
                    trial_id = identity(
                        "trial",
                        {
                            "study_id": study_id,
                            "protocol_digest": protocol.digest,
                            "analysis_plan_digest": plan.digest,
                            "operator_digest": operator_digest,
                            "pair_key": pair_key,
                        },
                    )
                    trial = self.repository.put(
                        ResearchRecord(
                            "trial",
                            trial_id,
                            study_id,
                            protocol.generation,
                            {
                                "trial_group_id": group_id,
                                "pair_key": pair_key,
                                "operator": operator,
                                "operator_digest": operator_digest,
                                "protocol_digest": protocol.digest,
                                "analysis_plan_digest": plan.digest,
                            },
                        )
                    )
                    trials.append(trial.to_dict())
            return {"trial_groups": groups, "trials": trials}

        return self.repository.once(idempotency_key, "materialize_trials", apply)

    @staticmethod
    def _runtime_run_dir(run_id: str) -> Path:
        env_path = str(os.getenv("ADAOS_SKILL_ENV_PATH") or "").strip()
        if not env_path:
            raise RuntimeError("skill runtime data path is unavailable")
        root = Path(env_path).resolve().parent.parent / "internal" / "fixture_runs" / run_id
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _runtime_data_root() -> Path:
        env_path = str(os.getenv("ADAOS_SKILL_ENV_PATH") or "").strip()
        if not env_path:
            raise RuntimeError("skill runtime data path is unavailable")
        return Path(env_path).resolve().parent.parent

    @staticmethod
    def _await_terminal_attempt(attempt: Any, *, timeout_s: float) -> Any:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while not attempt.terminal and time.monotonic() < deadline:
            time.sleep(0.05)
            attempt = reconcile(attempt.attempt_id)
        if attempt.terminal:
            return attempt

        cancellation_error = ""
        try:
            attempt = cancel_execution(attempt.attempt_id)
        except Exception as exc:
            cancellation_error = f"{type(exc).__name__}: {exc}"
        details = attempt.to_dict() if hasattr(attempt, "to_dict") else {
            "attempt_id": str(getattr(attempt, "attempt_id", "")),
            "status": str(getattr(attempt, "status", "")),
        }
        details = {
            "attempt_id": str(details.get("attempt_id") or ""),
            "status": str(details.get("status") or "unknown"),
            "failure": details.get("failure"),
            "last_heartbeat_at": details.get("last_heartbeat_at"),
            "cancellation_error": cancellation_error or None,
        }
        raise TimeoutError(
            "fixture attempt did not reach a terminal state: "
            + json.dumps(details, ensure_ascii=True, sort_keys=True)
        )

    def run_fixture(
        self,
        *,
        study_id: str,
        trial_id: str,
        split_role: str,
        seed: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if self.repository.list(study_id, "evidence_bundle"):
            raise ValueError("evidence is finalized; new runs are not admitted")
        current = workflow_state(self.repository, study_id)
        if current["state"] not in {"smoke", "executing", "qc", "unblinded", "analysis"}:
            raise ValueError("fixture execution is not admitted in the current workflow state")
        if split_role == "test" and current["state"] not in {"unblinded", "analysis"}:
            raise PermissionError("sealed test split has not been unblinded")
        trial = self.repository.get("trial", trial_id)
        if trial is None or trial.study_id != study_id:
            raise KeyError(trial_id)
        operator = str(trial.payload["operator"].get("name") or "")
        if not operator:
            raise ValueError("fixture operator name is required")
        streams = {
            name: int(seed) + offset
            for offset, name in enumerate(ExecutionDeterminism.REQUIRED_STREAMS)
        }
        rng_digest = digest(streams)
        # The legacy fixture can exercise validation and the later, explicitly
        # unblinded test split for the same trial/seed.  They are distinct
        # logical runs: their admission rules, inputs and tracker tags differ.
        run_id = identity(
            "run",
            {
                "trial_id": trial_id,
                "sample_generation": 0,
                "rng_digest": rng_digest,
                "split_role": split_role,
            },
        )
        run_record = self.repository.put(
            ResearchRecord(
                "run",
                run_id,
                study_id,
                0,
                {"trial_id": trial_id, "sample_generation": 0, "rng_streams": streams, "rng_digest": rng_digest},
            )
        )
        self.tracker.register_run(
            run_id=run_id,
            study_id=study_id,
            trial_id=trial_id,
            parameters={"operator": operator, "seed": seed},
            tags={"split_role": split_role, "mode": str(self.repository.get("study", study_id).payload["mode"])},
        )
        fixture = Path(__file__).with_name("fixture.py").resolve()
        code_digest = digest(fixture.read_bytes())
        environment_digest = digest({"python": sys.version, "executable": sys.executable})
        run_dir = self._runtime_run_dir(run_id)
        execution = spec(
            "research.fixture.v1",
            (sys.executable, str(fixture), "--operator", operator, "--seed", str(seed), "--output", "observation.json"),
            working_directory=run_dir,
            trial_id=trial_id,
            run_id=run_id,
            package_ref=ContentRef(
                uri="skill-package:research_manager_skill/research/fixture.py",
                digest=code_digest,
                size_bytes=fixture.stat().st_size,
                media_type="text/x-python",
                owner_ref="skill:research_manager_skill",
                kind="execution-package",
            ),
            code_digest=code_digest,
            environment_digest=environment_digest,
            resources=ExecutionResourceRequest(cpu_cores=1, memory_mb=128, wall_time_s=10, max_log_bytes=64 * 1024),
            network=ExecutionNetworkPolicy(mode="unrestricted"),
            determinism=ExecutionDeterminism(
                mode=str(self.repository.get("study", study_id).payload["mode"]),
                rng_streams=streams,
                deterministic_algorithms=True,
            ),
            budget=ExecutionBudget(max_attempts=3, max_compute_seconds=10, max_storage_bytes=1024 * 1024),
            expected_outputs=("observation.json",),
            metadata={"operator_digest": trial.payload["operator_digest"], "trusted_fixture": True},
        )
        attempt = submit(execution, idempotency_key=idempotency_key)
        attempt = self._await_terminal_attempt(
            attempt,
            timeout_s=max(30.0, float(execution.resources.wall_time_s) + 20.0),
        )
        attempt_record = self.repository.put(
            ResearchRecord("execution_attempt", attempt.attempt_id, study_id, attempt.attempt_number, attempt.to_dict())
        )
        observations: list[dict[str, Any]] = []
        if attempt.status == "succeeded":
            result = json.loads((run_dir / "observation.json").read_text(encoding="utf-8"))
            for name, value in sorted(result["metrics"].items()):
                tracked = self.tracker.observe(run_id=run_id, name=name, value=value, split_role=split_role)
                observation = self.repository.put(
                    ResearchRecord(
                        "observation",
                        tracked["observation_id"],
                        study_id,
                        0,
                        {**tracked, "run_digest": run_record.digest, "attempt_id": attempt.attempt_id},
                    )
                )
                observations.append(observation.to_dict())
        self.tracker.finalize(run_id, attempt.status)
        return {"run": run_record.to_dict(), "attempt": attempt_record.to_dict(), "observations": observations, "tracker": self.tracker.export(run_id)}

    def create_experiment(
        self,
        *,
        study_id: str,
        slug: str,
        title: str,
        purpose: str,
        conditions: Mapping[str, Any],
        idempotency_key: str,
        experiment_id: str | None = None,
    ) -> dict[str, Any]:
        return experiment.create(
            self.repository,
            study_id=study_id,
            slug=slug,
            title=title,
            purpose=purpose,
            conditions=conditions,
            experiment_id=experiment_id,
            idempotency_key=idempotency_key,
        )

    def revise_experiment(
        self,
        *,
        experiment_id: str,
        expected_revision: int,
        conditions: Mapping[str, Any],
        rationale: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return experiment.revise(
            self.repository,
            experiment_id=experiment_id,
            expected_revision=expected_revision,
            conditions=conditions,
            rationale=rationale,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def revise_experiment_json(
        self,
        *,
        experiment_id: str,
        expected_revision: int,
        conditions_json: str,
        rationale: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        try:
            conditions = json.loads(conditions_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"experiment conditions are not valid JSON: {exc.msg}") from exc
        if not isinstance(conditions, dict):
            raise ValueError("experiment conditions JSON must contain an object")
        return self.revise_experiment(
            experiment_id=experiment_id,
            expected_revision=expected_revision,
            conditions=conditions,
            rationale=rationale,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def submit_experiment_review(
        self,
        *,
        experiment_id: str,
        expected_generation: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        return experiment.transition(
            self.repository,
            experiment_id=experiment_id,
            command="submit_review",
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
        )

    def lock_experiment(
        self,
        *,
        experiment_id: str,
        expected_generation: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        value = experiment.get_experiment(self.repository, experiment_id)
        study_lifecycle = workflow_state(self.repository, value.study_id)
        if study_lifecycle["state"] == "protocol_review":
            self.advance(
                study_id=value.study_id,
                command="lock_protocol",
                expected_generation=study_lifecycle["generation"],
                idempotency_key=f"{idempotency_key}:study-protocol",
                actor=actor,
            )
        elif study_lifecycle["state"] not in {"locked", "smoke", "executing", "qc", "unblinded", "analysis"}:
            raise ValueError("study protocol is not ready for experiment lock")
        return experiment.transition(
            self.repository,
            experiment_id=experiment_id,
            command="lock",
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
        )

    def assess_experiment_execution(
        self,
        *,
        experiment_id: str,
        profile: str,
    ) -> dict[str, Any]:
        """Check immutable profile requirements against the active executor.

        This read-only admission gate intentionally runs before protocol lock. It
        does not prepare runner artifacts or reserve resources; those checks remain
        part of submission. Its purpose is to reject known provider mismatches
        without leaving a locked protocol or a synthetic Run behind.
        """

        if profile not in {"preflight", "confirmatory"}:
            raise ValueError("execution profile must be preflight or confirmatory")
        revision = experiment.latest_revision(self.repository, experiment_id)
        conditions = dict(revision.payload["conditions"])
        profile_conditions = dict(dict(conditions["execution"])[profile])
        provider = dict(execution_capabilities())
        features = {str(item) for item in provider.get("features") or ()}
        network_mode = str(profile_conditions.get("network_mode") or "unrestricted")
        gpu_count = int(profile_conditions.get("gpu_count") or 0)
        blockers: list[dict[str, Any]] = []
        input_readiness: dict[str, Any] = {
            "declared": False,
            "source": None,
            "required_before_execution": False,
            "satisfied": True,
            "reason": "not_declared",
        }
        if network_mode == "offline" and "network_offline" not in features:
            blockers.append(
                {
                    "code": "network_policy_unenforceable",
                    "requirement": "network_offline",
                    "requested": network_mode,
                }
            )
        if network_mode == "allowlist" and "network_allowlist" not in features:
            blockers.append(
                {
                    "code": "network_policy_unenforceable",
                    "requirement": "network_allowlist",
                    "requested": network_mode,
                }
            )
        if gpu_count > 0 and "gpu_allocation" not in features:
            blockers.append(
                {
                    "code": "accelerator_unavailable",
                    "requirement": "gpu_allocation",
                    "requested": gpu_count,
                }
            )
        input_policy = profile_conditions.get("input_policy")
        if isinstance(input_policy, Mapping):
            source = str(input_policy.get("source") or "").strip()
            required = (
                str(input_policy.get("readiness") or "").strip()
                == "required_before_execution"
            )
            input_readiness = {
                "declared": True,
                "source": source or None,
                "required_before_execution": required,
                "satisfied": True,
                "reason": "not_required" if not required else "profile_owned_fixture",
            }
            if required and source == "accepted_dataset":
                runner = dict(conditions.get("runner") or {})
                runner_provider = str(runner.get("provider") or "").strip()
                if not runner_provider:
                    input_readiness.update(
                        satisfied=False,
                        reason="runner_provider_missing",
                    )
                    blockers.append(
                        {
                            "code": "runner_provider_missing",
                            "requirement": "accepted_dataset",
                        }
                    )
                else:
                    try:
                        value = invoke_skill(
                            runner_provider,
                            "dataset_status",
                            {},
                            timeout=30,
                        )
                        if not isinstance(value, Mapping):
                            raise RuntimeError(
                                "runner provider returned a non-object dataset status"
                            )
                        dataset_status = dict(value)
                        ready = bool(dataset_status.get("ready"))
                        offline_ready = bool(
                            dataset_status.get("execution_ready_without_network")
                        )
                        satisfied = ready and (
                            network_mode != "offline" or offline_ready
                        )
                        input_readiness.update(
                            satisfied=satisfied,
                            reason=(
                                "ready"
                                if satisfied
                                else (
                                    "not_ready_without_network"
                                    if ready and network_mode == "offline"
                                    else "not_ready"
                                )
                            ),
                            runner_provider=runner_provider,
                            dataset_id=(
                                dataset_status.get("dataset_id")
                                or dataset_status.get("logical_name")
                                or dataset_status.get("id")
                            ),
                            dataset_ready=ready,
                            execution_ready_without_network=offline_ready,
                        )
                        if not satisfied:
                            blockers.append(
                                {
                                    "code": "dataset_not_ready",
                                    "requirement": (
                                        "accepted_dataset_offline"
                                        if network_mode == "offline"
                                        else "accepted_dataset"
                                    ),
                                    "runner_provider": runner_provider,
                                }
                            )
                    except Exception as exc:
                        input_readiness.update(
                            satisfied=False,
                            reason="dataset_status_unavailable",
                            runner_provider=runner_provider,
                            error={
                                "type": type(exc).__name__,
                                "message": str(exc)[:500],
                            },
                        )
                        blockers.append(
                            {
                                "code": "dataset_status_unavailable",
                                "requirement": "accepted_dataset",
                                "runner_provider": runner_provider,
                            }
                        )
            elif required and source == "deterministic_contract_fixture":
                input_readiness.update(
                    satisfied=True,
                    reason="profile_owned_fixture",
                )
            elif required:
                input_readiness.update(
                    satisfied=False,
                    reason="unsupported_input_source",
                )
                blockers.append(
                    {
                        "code": "unsupported_input_source",
                        "requirement": source or "declared_input_source",
                    }
                )
        return {
            "schema": "adaos.execution.admission.v1",
            "admitted": not blockers,
            "experiment_id": experiment_id,
            "experiment_revision_id": revision.record_id,
            "profile": profile,
            "provider": provider,
            "requested": {
                "network_mode": network_mode,
                "gpu_count": gpu_count,
                "input_policy": dict(input_policy) if isinstance(input_policy, Mapping) else None,
            },
            "enforcement": {
                "network_policy": (
                    "not_required"
                    if network_mode == "unrestricted"
                    else (
                        "available"
                        if (
                            (network_mode == "offline" and "network_offline" in features)
                            or (
                                network_mode == "allowlist"
                                and "network_allowlist" in features
                            )
                        )
                        else "unavailable"
                    )
                ),
                "network_observation_required": True,
                "input_readiness": input_readiness,
            },
            "blockers": blockers,
        }

    def execution_provider_status(self) -> dict[str, Any]:
        """Expose the active executor contract without creating research state.

        Research formulation may use this read-only projection to choose an
        explicitly requested, non-inferential workflow-smoke policy.  The
        executor remains the authority: consumers receive a content-addressed
        capability snapshot rather than inferring support from provider names.
        """

        provider = dict(execution_capabilities())
        provider["features"] = sorted(
            {str(item) for item in provider.get("features") or ()}
        )
        return {
            "schema": "adaos.execution.provider_status.v1",
            "provider": provider,
            "provider_digest": digest(provider),
            "admission_contract": "adaos.execution.admission.v1",
        }

    def _experiment_records(self, experiment_id: str, kind: str) -> list[ResearchRecord]:
        value = experiment.get_experiment(self.repository, experiment_id)
        return [
            item
            for item in self.repository.list(value.study_id, kind)
            if item.payload.get("experiment_id") == experiment_id
        ]

    def _materialize_experiment_plan(
        self,
        *,
        experiment_id: str,
        profile: str,
    ) -> tuple[ResearchRecord, list[tuple[ResearchRecord, ResearchRecord, int, str]]]:
        value = experiment.get_experiment(self.repository, experiment_id)
        revision = experiment.latest_revision(self.repository, experiment_id)
        conditions = dict(revision.payload["conditions"])
        tracker_provider = str(dict(conditions.get("tracker") or {}).get("provider") or "local-tracker")
        tracker = self._tracker_provider(tracker_provider)
        tracker_health = tracker.health()
        if not bool(tracker_health.get("ok")):
            raise RuntimeError(f"tracker provider {tracker_provider} is not ready")
        profile_conditions = dict(dict(conditions["execution"])[profile])
        protocol = self.repository.list(value.study_id, "protocol")[-1]
        analysis_plan = self.repository.list(value.study_id, "analysis_plan")[-1]
        seeds = [int(seed) for seed in profile_conditions["seeds"]]
        arms = [dict(item) for item in dict(conditions["operators"])["arms"]]
        plan: list[tuple[ResearchRecord, ResearchRecord, int, str]] = []
        for seed in seeds:
            pair_key = f"seed-{seed}"
            group_id = identity(
                "trial_group",
                {
                    "experiment_id": experiment_id,
                    "experiment_revision_id": revision.record_id,
                    "profile": profile,
                    "seed": seed,
                },
            )
            group = self.repository.put(
                ResearchRecord(
                    "trial_group",
                    group_id,
                    value.study_id,
                    revision.generation,
                    {
                        "experiment_id": experiment_id,
                        "experiment_revision_id": revision.record_id,
                        "pair_key": pair_key,
                        "seed": seed,
                        "profile": profile,
                        "operator_count": len(arms),
                    },
                )
            )
            for arm in arms:
                arm_id = str(arm["id"])
                trial_id = identity(
                    "trial",
                    {
                        "experiment_id": experiment_id,
                        "experiment_revision_id": revision.record_id,
                        "profile": profile,
                        "pair_key": pair_key,
                        "arm_id": arm_id,
                    },
                )
                trial = self.repository.put(
                    ResearchRecord(
                        "trial",
                        trial_id,
                        value.study_id,
                        revision.generation,
                        {
                            "experiment_id": experiment_id,
                            "experiment_revision_id": revision.record_id,
                            "trial_group_id": group_id,
                            "pair_key": pair_key,
                            "seed": seed,
                            "profile": profile,
                            "operator": arm,
                            "operator_digest": digest(arm),
                            "protocol_digest": protocol.digest,
                            "analysis_plan_digest": analysis_plan.digest,
                        },
                    )
                )
                plan.append((group, trial, seed, arm_id))
        return revision, plan

    def _attempt_bindings(self, experiment_id: str, run_id: str | None = None) -> list[ResearchRecord]:
        values = self._experiment_records(experiment_id, "attempt_binding")
        if run_id is not None:
            values = [item for item in values if item.payload.get("run_id") == run_id]
        return sorted(values, key=lambda item: (int(item.payload["attempt_number"]), item.created_at, item.record_id))

    def _latest_attempt_status(self, experiment_id: str, attempt_id: str, initial: str) -> str:
        value = experiment.get_experiment(self.repository, experiment_id)
        status = initial
        for event in self.repository.events(value.study_id):
            if event["event_type"] != "research.experiment.attempt":
                continue
            payload = dict(event["payload"])
            if payload.get("experiment_id") == experiment_id and payload.get("attempt_id") == attempt_id:
                status = str(payload["status"])
        return status

    def _record_attempt_status(self, experiment_id: str, attempt: Any) -> None:
        value = experiment.get_experiment(self.repository, experiment_id)
        current = self._latest_attempt_status(experiment_id, attempt.attempt_id, "")
        if current == attempt.status:
            return
        self.repository.event(
            value.study_id,
            "research.experiment.attempt",
            {
                "experiment_id": experiment_id,
                "attempt_id": attempt.attempt_id,
                "run_id": attempt.run_id,
                "status": attempt.status,
                "attempt_number": attempt.attempt_number,
                "terminal": attempt.terminal,
            },
        )

    def _submit_runner_attempt(
        self,
        *,
        experiment_id: str,
        revision: ResearchRecord,
        trial: ResearchRecord,
        seed: int,
        arm_id: str,
        profile: str,
        command_key: str,
    ) -> dict[str, Any]:
        conditions = dict(revision.payload["conditions"])
        tracker_provider = str(dict(conditions.get("tracker") or {}).get("provider") or "local-tracker")
        tracker = self._tracker_provider(tracker_provider)
        profile_conditions = dict(dict(conditions["execution"])[profile])
        runner_binding = dict(conditions.get("runner") or {})
        runner_provider = str(runner_binding.get("provider") or "").strip()
        if not runner_provider:
            raise ValueError("experiment runner provider is missing")
        streams = {
            name: seed + offset
            for offset, name in enumerate(ExecutionDeterminism.REQUIRED_STREAMS)
        }
        rng_digest = digest(streams)
        run_id = identity(
            "run",
            {
                "trial_id": trial.record_id,
                "sample_generation": 0,
                "rng_digest": rng_digest,
            },
        )
        run_record = self.repository.put(
            ResearchRecord(
                "run",
                run_id,
                trial.study_id,
                0,
                {
                    "experiment_id": experiment_id,
                    "experiment_revision_id": revision.record_id,
                    "trial_id": trial.record_id,
                    "trial_group_id": trial.payload["trial_group_id"],
                    "pair_key": trial.payload["pair_key"],
                    "arm_id": arm_id,
                    "profile": profile,
                    "seed": seed,
                    "sample_generation": 0,
                    "rng_streams": streams,
                    "rng_digest": rng_digest,
                },
            )
        )
        attempt_number = len(self._attempt_bindings(experiment_id, run_id)) + 1
        arm = dict(trial.payload["operator"])
        prepared = invoke_skill(
            runner_provider,
            "prepare_attempt",
            {
                "request": {
                    "experiment_id": experiment_id,
                    "experiment_revision_id": revision.record_id,
                    "trial_id": trial.record_id,
                    "run_id": run_id,
                    "attempt_number": attempt_number,
                    "profile": profile,
                    "seed": seed,
                    "arm": arm,
                    "conditions": conditions,
                    "profile_conditions": profile_conditions,
                }
            },
            timeout=30,
        )
        if not isinstance(prepared, Mapping):
            raise RuntimeError("runner provider returned a non-object preparation")
        prepared = dict(prepared)
        if prepared.get("contract") != "adaos.research.runner.v1":
            raise ValueError("runner provider contract is incompatible")
        if str(prepared.get("provider_id") or "") != runner_provider:
            raise ValueError("runner provider identity mismatch")
        package = dict(prepared.get("package_ref") or {})
        package_ref = ContentRef(
            uri=str(package["uri"]),
            digest=str(package["digest"]),
            size_bytes=int(package["size_bytes"]),
            media_type=str(package["media_type"]),
            owner_ref=str(package["owner_ref"]),
            kind=str(package.get("kind") or "execution-package"),
            metadata=dict(package.get("metadata") or {}),
        )
        code_digest = str(prepared["code_digest"])
        environment_digest = str(prepared["environment_digest"])
        execution = spec(
            str(prepared["spec_id"]),
            tuple(str(item) for item in prepared["command"]),
            working_directory=str(prepared["working_directory"]),
            trial_id=trial.record_id,
            run_id=run_id,
            package_ref=package_ref,
            code_digest=code_digest,
            environment_digest=environment_digest,
            environment={str(key): str(value) for key, value in dict(prepared.get("environment") or {}).items()},
            resources=ExecutionResourceRequest(
                cpu_cores=int(profile_conditions.get("cpu_threads") or 2),
                memory_mb=int(profile_conditions.get("memory_mb") or 4096),
                wall_time_s=int(profile_conditions.get("wall_time_s") or 7200),
                max_log_bytes=int(profile_conditions.get("max_log_bytes") or 2 * 1024 * 1024),
            ),
            network=ExecutionNetworkPolicy(
                mode=str(profile_conditions.get("network_mode") or "unrestricted")
            ),
            determinism=ExecutionDeterminism(
                mode="confirmatory" if profile == "confirmatory" else "exploratory",
                rng_streams=streams,
                deterministic_algorithms=True,
            ),
            budget=ExecutionBudget(
                max_attempts=int(profile_conditions.get("max_attempts") or 3),
                max_compute_seconds=int(profile_conditions.get("max_compute_seconds") or 10800),
                max_storage_bytes=int(profile_conditions.get("max_storage_bytes") or 2 * 1024 * 1024 * 1024),
            ),
            expected_outputs=tuple(str(item) for item in prepared.get("expected_outputs") or ()),
            metadata={
                "experiment_id": experiment_id,
                "experiment_revision_id": revision.record_id,
                "pair_key": trial.payload["pair_key"],
                "arm_id": arm_id,
                "profile": profile,
                "operator_digest": trial.payload["operator_digest"],
                "runner_provider": runner_provider,
                "runner_output_ref": str(prepared["output_ref"]),
            },
        )
        attempt = submit(execution, idempotency_key=f"{command_key}:{run_id}:attempt:{attempt_number}")
        session_id = identity(
            "tracking_session",
            {"run_id": run_id, "attempt_id": attempt.attempt_id},
        )
        trace_id = identity(
            "trace",
            {"experiment_revision_id": revision.record_id, "attempt_id": attempt.attempt_id},
        )
        binding = self.repository.put(
            ResearchRecord(
                "attempt_binding",
                attempt.attempt_id,
                trial.study_id,
                attempt_number,
                {
                    "experiment_id": experiment_id,
                    "experiment_revision_id": revision.record_id,
                    "trial_id": trial.record_id,
                    "run_id": run_id,
                    "attempt_id": attempt.attempt_id,
                    "attempt_number": attempt_number,
                    "session_id": session_id,
                    "tracker_provider": tracker_provider,
                    "runner_provider": runner_provider,
                    "runner_contract": str(prepared["contract"]),
                    "data_owner_skill_id": str(dict(revision.payload["conditions"])["runner"]["data_owner"]),
                    "runner_output_ref": str(prepared["output_ref"]),
                    "profile": profile,
                    "arm_id": arm_id,
                    "seed": seed,
                    "initial_attempt": attempt.to_dict(),
                },
            )
        )
        tracker.open_session(
            session_id=session_id,
            study_id=trial.study_id,
            experiment_id=experiment_id,
            experiment_revision_id=revision.record_id,
            trial_id=trial.record_id,
            run_id=run_id,
            attempt_id=attempt.attempt_id,
            parameters={**dict(prepared.get("parameters") or {}), "conditions_digest": revision.payload["conditions_digest"]},
            tags={
                "adaos.study_id": trial.study_id,
                "adaos.experiment_id": experiment_id,
                "adaos.experiment_revision_id": revision.record_id,
                "adaos.trial_group_id": str(trial.payload["trial_group_id"]),
                "adaos.trial_id": trial.record_id,
                "adaos.run_id": run_id,
                "adaos.attempt_id": attempt.attempt_id,
                "adaos.protocol_digest": str(trial.payload["protocol_digest"]),
                "adaos.analysis_plan_digest": str(trial.payload["analysis_plan_digest"]),
                "adaos.source.code_digest": code_digest,
                "adaos.environment_digest": environment_digest,
                "adaos.runner_provider": runner_provider,
                "adaos.trace_id": trace_id,
                "adaos.evidence_class": str(profile_conditions.get("evidence_class") or "workflow_validation"),
                "adaos.profile": profile,
            },
            inputs=tuple(dict(item) for item in prepared.get("inputs") or ()),
        )
        self._record_attempt_status(experiment_id, attempt)
        return {"run": run_record.to_dict(), "binding": binding.to_dict(), "attempt": attempt.to_dict()}

    def start_experiment(
        self,
        *,
        experiment_id: str,
        profile: str,
        expected_generation: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        if profile not in {"preflight", "confirmatory"}:
            raise ValueError("execution profile must be preflight or confirmatory")

        def apply() -> Mapping[str, Any]:
            lifecycle = experiment.state(self.repository, experiment_id)
            if lifecycle["state"] != "locked" or lifecycle["generation"] != int(expected_generation):
                raise ValueError("experiment must be locked at the expected generation")
            revision, plan = self._materialize_experiment_plan(experiment_id=experiment_id, profile=profile)
            submissions = [
                self._submit_runner_attempt(
                    experiment_id=experiment_id,
                    revision=revision,
                    trial=trial,
                    seed=seed,
                    arm_id=arm_id,
                    profile=profile,
                    command_key=idempotency_key,
                )
                for _group, trial, seed, arm_id in plan
            ]
            transition_result = experiment.transition(
                self.repository,
                experiment_id=experiment_id,
                command="start_preflight" if profile == "preflight" else "start_execution",
                expected_generation=expected_generation,
                idempotency_key=f"{idempotency_key}:transition",
                actor=actor,
                execution_profile=profile,
                evidence_refs=(revision.digest,),
            )
            return {"experiment_id": experiment_id, "profile": profile, "submissions": submissions, "lifecycle": transition_result}

        return self.repository.once(idempotency_key, "start_experiment", apply)

    def _collect_runner_output(self, binding: ResearchRecord) -> dict[str, Any]:
        provider = str(binding.payload.get("runner_provider") or "").strip()
        output_ref = str(binding.payload.get("runner_output_ref") or "").strip()
        if not provider and binding.payload.get("workdir"):
            # Compatibility reader for attempts produced before runner-provider
            # ownership was introduced. New attempts never expose or consume a
            # foreign physical path through this branch.
            root = Path(str(binding.payload["workdir"])).resolve()
            observations: list[dict[str, Any]] = []
            observation_path = root / "observations.ndjson"
            if observation_path.is_file():
                for line in observation_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        observations.append(json.loads(line))
            result_path = root / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
            artifacts: list[dict[str, Any]] = []
            manifest_path = root / "artifacts.json"
            if manifest_path.is_file():
                for item in json.loads(manifest_path.read_text(encoding="utf-8")).get("artifacts") or []:
                    source = (root / str(item["path"])).resolve()
                    relative = source.relative_to(self._runtime_data_root()).as_posix()
                    artifacts.append({**dict(item), "uri": f"skill-data:{relative}"})
            return {
                "schema": "adaos.research.runner_collection.v1",
                "provider_id": "legacy-local",
                "observations": observations,
                "artifacts": artifacts,
                "result": result,
                "complete": result is not None and bool(artifacts),
            }
        if not provider or not output_ref:
            raise ValueError("attempt binding has no runner-provider output reference")
        result = invoke_skill(
            provider,
            "collect_attempt",
            {"output_ref": output_ref},
            timeout=30,
        )
        if not isinstance(result, Mapping):
            raise RuntimeError("runner provider returned a non-object collection")
        value = dict(result)
        if str(value.get("provider_id") or "") != provider:
            raise ValueError("runner collection provider identity mismatch")
        return value

    def _ingest_attempt_progress(self, binding: ResearchRecord) -> list[dict[str, Any]]:
        collection = self._collect_runner_output(binding)
        session_id = str(binding.payload["session_id"])
        tracker = self._tracker_for_binding(binding)
        session = tracker.get_session(session_id)
        values: list[dict[str, Any]] = []
        for payload in collection.get("observations") or []:
            if not isinstance(payload, Mapping):
                raise ValueError("runner observation must be an object")
            values.append(normalize_observation(session, payload))
        if not values:
            return []
        receipt = tracker.append_observations(session_id, values)
        exported = tracker.export_session(session_id)
        by_id = {
            item["event_id"]: item
            for item in exported["events"]
            if item["event_kind"] == "observation"
        }
        records: list[dict[str, Any]] = []
        for event_id in [*receipt["accepted"], *receipt["duplicates"]]:
            event = by_id[event_id]
            record = self.repository.put(
                ResearchRecord(
                    "observation",
                    event_id,
                    binding.study_id,
                    0,
                    {
                        **dict(event["payload"]),
                        "experiment_id": binding.payload["experiment_id"],
                        "experiment_revision_id": binding.payload["experiment_revision_id"],
                        "trial_id": binding.payload["trial_id"],
                        "run_id": binding.payload["run_id"],
                        "attempt_id": binding.payload["attempt_id"],
                        "session_id": session_id,
                        "payload_digest": event["payload_digest"],
                    },
                )
            )
            records.append(record.to_dict())
        return records

    def _ingest_attempt_artifacts(self, binding: ResearchRecord) -> list[dict[str, Any]]:
        collection = self._collect_runner_output(binding)
        artifacts = []
        for item in collection.get("artifacts") or []:
            item = dict(item)
            artifacts.append(
                {
                    "uri": str(item["uri"]),
                    "digest": str(item["digest"]),
                    "size_bytes": int(item["size_bytes"]),
                    "media_type": str(item["media_type"]),
                    "role": str(item["role"]),
                    "owner_ref": f"skill:{binding.payload['runner_provider']}",
                    "runner_provider": str(binding.payload["runner_provider"]),
                    # Reconciliation is repeatable.  Timestamping the same
                    # immutable artifact with wall-clock time would turn an
                    # otherwise identical replay into a tracker conflict.
                    "observed_at": binding.created_at,
                }
            )
        session_id = str(binding.payload["session_id"])
        tracker = self._tracker_for_binding(binding)
        receipt = tracker.append_artifacts(session_id, artifacts)
        exported = tracker.export_session(session_id)
        by_id = {
            item["event_id"]: item
            for item in exported["events"]
            if item["event_kind"] == "artifact"
        }
        records: list[dict[str, Any]] = []
        for event_id in [*receipt["accepted"], *receipt["duplicates"]]:
            event = by_id[event_id]
            record = self.repository.put(
                ResearchRecord(
                    "artifact_ref",
                    event_id,
                    binding.study_id,
                    0,
                    {
                        **dict(event["payload"]),
                        "experiment_id": binding.payload["experiment_id"],
                        "experiment_revision_id": binding.payload["experiment_revision_id"],
                        "trial_id": binding.payload["trial_id"],
                        "run_id": binding.payload["run_id"],
                        "attempt_id": binding.payload["attempt_id"],
                        "session_id": session_id,
                    },
                )
            )
            records.append(record.to_dict())
        return records

    def reconcile_experiment(self, experiment_id: str, *, actor: str = "user:local") -> dict[str, Any]:
        bindings = self._attempt_bindings(experiment_id)
        if not bindings:
            return self.experiment_status(experiment_id)
        terminal_statuses = {"succeeded", "failed", "cancelled", "lost"}
        current_by_run: dict[str, ResearchRecord] = {}
        for binding in bindings:
            run_id = str(binding.payload["run_id"])
            current = current_by_run.get(run_id)
            if current is None or int(binding.payload["attempt_number"]) > int(current.payload["attempt_number"]):
                current_by_run[run_id] = binding
        statuses: dict[str, str] = {}
        for run_id, binding in current_by_run.items():
            initial = str(dict(binding.payload["initial_attempt"])["status"])
            known = self._latest_attempt_status(experiment_id, str(binding.payload["attempt_id"]), initial)
            if known in terminal_statuses:
                statuses[run_id] = known
                continue
            attempt = reconcile(str(binding.payload["attempt_id"]))
            self._record_attempt_status(experiment_id, attempt)
            self._ingest_attempt_progress(binding)
            if attempt.terminal:
                if attempt.status == "succeeded":
                    artifacts = self._ingest_attempt_artifacts(binding)
                else:
                    artifacts = []
                self._tracker_for_binding(binding).close_session(
                    str(binding.payload["session_id"]),
                    attempt.status,
                    {
                        "observations_complete": attempt.status == "succeeded",
                        "artifacts_complete": attempt.status == "succeeded" and bool(artifacts),
                        "required_delivery_state": "delivered",
                    },
                )
                self.repository.put(
                    ResearchRecord(
                        "execution_attempt",
                        attempt.attempt_id,
                        binding.study_id,
                        int(binding.payload["attempt_number"]),
                        {
                            **attempt.to_dict(),
                            "experiment_id": experiment_id,
                            "experiment_revision_id": binding.payload["experiment_revision_id"],
                            "session_id": binding.payload["session_id"],
                        },
                    )
                )
            statuses[run_id] = attempt.status
        lifecycle = experiment.state(self.repository, experiment_id)
        if statuses and all(status in terminal_statuses for status in statuses.values()):
            status_digest = digest(statuses)
            if all(status == "succeeded" for status in statuses.values()) and lifecycle["state"] in {"running", "cancelling"}:
                experiment.transition(
                    self.repository,
                    experiment_id=experiment_id,
                    command="mark_results_ready",
                    expected_generation=lifecycle["generation"],
                    idempotency_key=f"reconcile:{experiment_id}:{status_digest}:ready",
                    actor=actor,
                    execution_profile=lifecycle.get("execution_profile"),
                    evidence_refs=tuple(
                        item.digest for item in self._experiment_records(experiment_id, "execution_attempt")
                    ),
                )
            elif lifecycle["state"] == "cancelling":
                experiment.transition(
                    self.repository,
                    experiment_id=experiment_id,
                    command="mark_cancelled",
                    expected_generation=lifecycle["generation"],
                    idempotency_key=f"reconcile:{experiment_id}:{status_digest}:cancelled",
                    actor=actor,
                    execution_profile=lifecycle.get("execution_profile"),
                )
            elif lifecycle["state"] == "running":
                experiment.transition(
                    self.repository,
                    experiment_id=experiment_id,
                    command="mark_failed",
                    expected_generation=lifecycle["generation"],
                    idempotency_key=f"reconcile:{experiment_id}:{status_digest}:failed",
                    actor=actor,
                    execution_profile=lifecycle.get("execution_profile"),
                )
        return self.experiment_status(experiment_id)

    def cancel_experiment(
        self,
        *,
        experiment_id: str,
        expected_generation: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        lifecycle = experiment.state(self.repository, experiment_id)
        if lifecycle["state"] != "running" or lifecycle["generation"] != int(expected_generation):
            raise ValueError("only the current running experiment can be cancelled")
        transition_result = experiment.transition(
            self.repository,
            experiment_id=experiment_id,
            command="request_cancel",
            expected_generation=expected_generation,
            idempotency_key=f"{idempotency_key}:transition",
            actor=actor,
            execution_profile=lifecycle.get("execution_profile"),
        )
        cancelled = []
        latest: dict[str, ResearchRecord] = {}
        for binding in self._attempt_bindings(experiment_id):
            run_id = str(binding.payload["run_id"])
            if run_id not in latest or int(binding.payload["attempt_number"]) > int(latest[run_id].payload["attempt_number"]):
                latest[run_id] = binding
        for binding in latest.values():
            initial = str(dict(binding.payload["initial_attempt"])["status"])
            status = self._latest_attempt_status(experiment_id, str(binding.payload["attempt_id"]), initial)
            if status in {"succeeded", "failed", "cancelled", "lost"}:
                continue
            attempt = cancel_execution(str(binding.payload["attempt_id"]))
            self._record_attempt_status(experiment_id, attempt)
            cancelled.append(attempt.to_dict())
        return {"lifecycle": transition_result, "cancelled": cancelled}

    def retry_run(
        self,
        *,
        experiment_id: str,
        run_id: str,
        expected_generation: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        lifecycle = experiment.state(self.repository, experiment_id)
        if lifecycle["state"] not in {"failed", "cancelled"} or lifecycle["generation"] != int(expected_generation):
            raise ValueError("retry requires the current failed or cancelled experiment")
        run_record = self.repository.get("run", run_id)
        if run_record is None or run_record.payload.get("experiment_id") != experiment_id:
            raise KeyError(run_id)
        bindings = self._attempt_bindings(experiment_id, run_id)
        if not bindings:
            raise ValueError("run has no physical attempt to retry")
        last = bindings[-1]
        last_status = self._latest_attempt_status(
            experiment_id,
            str(last.payload["attempt_id"]),
            str(dict(last.payload["initial_attempt"])["status"]),
        )
        if last_status not in {"failed", "cancelled", "lost"}:
            raise ValueError("run retry is admitted only after a terminal unsuccessful attempt")
        trial = self.repository.get("trial", str(run_record.payload["trial_id"]))
        if trial is None:
            raise KeyError(run_record.payload["trial_id"])
        revision = experiment.latest_revision(self.repository, experiment_id)
        submission = self._submit_runner_attempt(
            experiment_id=experiment_id,
            revision=revision,
            trial=trial,
            seed=int(run_record.payload["seed"]),
            arm_id=str(run_record.payload["arm_id"]),
            profile=str(run_record.payload["profile"]),
            command_key=idempotency_key,
        )
        transition_result = experiment.transition(
            self.repository,
            experiment_id=experiment_id,
            command="retry",
            expected_generation=expected_generation,
            idempotency_key=f"{idempotency_key}:transition",
            actor=actor,
            execution_profile=str(run_record.payload["profile"]),
        )
        return {"submission": submission, "lifecycle": transition_result}

    @staticmethod
    def _result_path(result: Mapping[str, Any], path: str) -> Any:
        current: Any = result
        for token in str(path or "").split("."):
            if not token or not isinstance(current, Mapping) or token not in current:
                return None
            current = current[token]
        return current

    def _experiment_summary(self, experiment_id: str) -> dict[str, Any]:
        revision = experiment.latest_revision(self.repository, experiment_id)
        conditions = dict(revision.payload["conditions"])
        analysis = dict(conditions.get("analysis") or {})
        arms = [str(dict(item).get("id") or "") for item in dict(conditions.get("operators") or {}).get("arms") or []]
        contrast = dict(analysis.get("primary_contrast") or {})
        minuend = str(contrast.get("minuend") or (arms[1] if len(arms) > 1 else ""))
        subtrahend = str(contrast.get("subtrahend") or (arms[0] if arms else ""))
        metric_path = str(analysis.get("result_metric_path") or "best_validation_accuracy")
        step_path = str(analysis.get("result_step_path") or "best_epoch")
        initialization_path = str(analysis.get("initialization_digest_path") or "initial_state_digest")
        successful: dict[str, tuple[ResearchRecord, dict[str, Any]]] = {}
        for binding in self._attempt_bindings(experiment_id):
            status = self._latest_attempt_status(
                experiment_id,
                str(binding.payload["attempt_id"]),
                str(dict(binding.payload["initial_attempt"])["status"]),
            )
            if status == "succeeded":
                result = self._collect_runner_output(binding).get("result")
                if isinstance(result, Mapping):
                    successful[str(binding.payload["run_id"])] = (binding, dict(result))
        pairs: dict[int, dict[str, Any]] = {}
        for binding, result in successful.values():
            seed = int(binding.payload["seed"])
            arm = str(binding.payload["arm_id"])
            metric = self._result_path(result, metric_path)
            if metric is None:
                raise ValueError(f"runner result omits analysis metric path: {metric_path}")
            pairs.setdefault(seed, {"seed": seed})[arm] = float(metric)
            step = self._result_path(result, step_path)
            if step is not None:
                pairs[seed][f"{arm}_step"] = int(step)
            initialization = self._result_path(result, initialization_path)
            if initialization is not None:
                pairs[seed][f"{arm}_initialization_digest"] = str(initialization)
        paired_values = []
        for item in pairs.values():
            if minuend in item and subtrahend in item:
                item["delta"] = item[minuend] - item[subtrahend]
                left_digest = item.get(f"{minuend}_initialization_digest")
                right_digest = item.get(f"{subtrahend}_initialization_digest")
                item["paired_initialization"] = bool(left_digest and left_digest == right_digest)
                paired_values.append(float(item["delta"]))
        mean_delta = sum(paired_values) / len(paired_values) if paired_values else None
        confidence_interval = None
        if len(paired_values) >= 2:
            import random

            generator = random.Random(20260807)
            bootstrap = sorted(
                sum(generator.choice(paired_values) for _ in paired_values) / len(paired_values)
                for _ in range(10000)
            )
            confidence_interval = [bootstrap[249], bootstrap[9749]]
        return {
            "schema": "adaos.research.experiment_summary.v1",
            "experiment_id": experiment_id,
            "completed_runs": len(successful),
            "paired_seed_count": len(paired_values),
            "pairs": [pairs[key] for key in sorted(pairs)],
            "primary_estimand": str(
                analysis.get("primary_estimand")
                or f"{analysis.get('primary_metric')}.{minuend}_minus_{subtrahend}"
            ),
            "primary_contrast": {"minuend": minuend, "subtrahend": subtrahend},
            "mean_delta": mean_delta,
            "paired_bootstrap_95": confidence_interval,
        }

    def finalize_experiment(
        self,
        *,
        experiment_id: str,
        expected_generation: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        def apply() -> Mapping[str, Any]:
            lifecycle = experiment.state(self.repository, experiment_id)
            if lifecycle["state"] != "results_ready" or lifecycle["generation"] != int(expected_generation):
                raise ValueError("experiment results are not ready at the expected generation")
            summary = self._experiment_summary(experiment_id)
            if not summary["paired_seed_count"]:
                raise ValueError("experiment cannot be finalized without a complete paired result")
            if any(not item.get("paired_initialization") for item in summary["pairs"] if "delta" in item):
                raise ValueError("paired experiment initialization lineage does not match")
            tracker_export = self.tracker.export_experiment(experiment_id)
            if any(session["session"]["status"] == "running" for session in tracker_export["sessions"]):
                raise ValueError("tracker sessions must be finalized before experiment result")
            pending = [
                event["event_id"]
                for session in tracker_export["sessions"]
                for event in session["events"]
                if event["delivery_state"] != "delivered"
            ]
            if pending:
                raise ValueError(f"tracker delivery is incomplete for {len(pending)} event(s)")
            tracker_acceptance = self._accept_tracker_evidence(
                experiment_id=experiment_id,
                tracker_export=tracker_export,
                actor=actor,
            )
            value = experiment.get_experiment(self.repository, experiment_id)
            revision = experiment.latest_revision(self.repository, experiment_id)
            artifact_refs = [item.to_dict() for item in self._experiment_records(experiment_id, "artifact_ref")]
            result_payload = {
                "schema": "adaos.research.experiment_result.v1",
                "experiment_id": experiment_id,
                "experiment_revision_id": revision.record_id,
                "conditions_digest": revision.payload["conditions_digest"],
                "execution_profile": lifecycle.get("execution_profile"),
                "evidence_class": (
                    "workflow_validation"
                    if lifecycle.get("execution_profile") == "preflight"
                    else "confirmatory"
                ),
                "summary": summary,
                "tracker_export_digest": tracker_export["export_digest"],
                "tracker_export_record_id": tracker_acceptance["export"]["record_id"],
                "tracker_acceptance_id": tracker_acceptance["acceptance"]["record_id"],
                "artifact_refs": artifact_refs,
                "finalized_by": actor,
                "finalized_at": now(),
            }
            result_id = identity(
                "experiment_result",
                {
                    "experiment_id": experiment_id,
                    "revision_id": revision.record_id,
                    "summary_digest": digest(summary),
                    "tracker_export_digest": tracker_export["export_digest"],
                },
            )
            result = self.repository.put(
                ResearchRecord(
                    "experiment_result",
                    result_id,
                    value.study_id,
                    revision.generation,
                    result_payload,
                )
            )
            transition_result = experiment.transition(
                self.repository,
                experiment_id=experiment_id,
                command="finalize",
                expected_generation=expected_generation,
                idempotency_key=f"{idempotency_key}:transition",
                actor=actor,
                execution_profile=lifecycle.get("execution_profile"),
                evidence_refs=(result.digest, tracker_export["export_digest"]),
            )
            return {
                "result": result.to_dict(),
                "tracker_export": tracker_export,
                "tracker_acceptance": tracker_acceptance,
                "lifecycle": transition_result,
                "verification": self.verify_experiment_result(result_id),
            }

        return self.repository.once(idempotency_key, "finalize_experiment", apply)

    def _accept_tracker_evidence(
        self,
        *,
        experiment_id: str,
        tracker_export: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        value = experiment.get_experiment(self.repository, experiment_id)
        export_payload = dict(tracker_export)
        export_digest = str(export_payload.get("export_digest") or "")
        digest_input = dict(export_payload)
        digest_input.pop("export_digest", None)
        if not export_digest or digest(digest_input) != export_digest:
            raise ValueError("tracker export digest is invalid")
        export_id = identity(
            "tracker_export",
            {"experiment_id": experiment_id, "export_digest": export_digest},
        )
        export_record = self.repository.put(
            ResearchRecord(
                "tracker_export",
                export_id,
                value.study_id,
                0,
                export_payload,
            )
        )
        acceptance_id = identity(
            "tracker_evidence_acceptance",
            {"experiment_id": experiment_id, "tracker_export_digest": export_digest},
        )
        existing_acceptance = self.repository.get(
            "tracker_evidence_acceptance",
            acceptance_id,
        )
        if existing_acceptance is not None:
            return {
                "export": export_record.to_dict(),
                "acceptance": existing_acceptance.to_dict(),
            }
        acceptance = self.repository.put(
            ResearchRecord(
                "tracker_evidence_acceptance",
                acceptance_id,
                value.study_id,
                0,
                {
                    "schema": "adaos.research.tracker_evidence_acceptance.v1",
                    "experiment_id": experiment_id,
                    "tracker_export_record_id": export_record.record_id,
                    "tracker_export_digest": export_digest,
                    "accepted_by": actor,
                    "accepted_at": now(),
                },
            )
        )
        return {"export": export_record.to_dict(), "acceptance": acceptance.to_dict()}

    def accept_tracker_evidence(
        self,
        *,
        experiment_id: str,
        actor: str = "user:local",
    ) -> dict[str, Any]:
        tracker_export = self._tracker_provider_for_experiment(experiment_id).export_experiment(experiment_id)
        if any(item["session"]["status"] == "running" for item in tracker_export["sessions"]):
            raise ValueError("tracker sessions must be finalized before evidence acceptance")
        pending = [
            event["event_id"]
            for item in tracker_export["sessions"]
            for event in item["events"]
            if event["delivery_state"] != "delivered"
        ]
        if pending:
            raise ValueError(f"tracker delivery is incomplete for {len(pending)} event(s)")
        return self._accept_tracker_evidence(
            experiment_id=experiment_id,
            tracker_export=tracker_export,
            actor=actor,
        )

    def _tracker_provider_for_experiment(self, experiment_id: str):
        revision = experiment.latest_revision(self.repository, experiment_id)
        provider_id = str(
            dict(dict(revision.payload["conditions"]).get("tracker") or {}).get("provider")
            or "local-tracker"
        )
        return self._tracker_provider(provider_id)

    def flush_experiment_tracker(self, experiment_id: str, *, required: bool = False) -> dict[str, Any]:
        provider = self._tracker_provider_for_experiment(experiment_id)
        sessions = provider.query_sessions(experiment_id=experiment_id)
        results = [
            provider.flush(str(session["session_id"]), required=required)
            for session in sessions
        ]
        return {
            "schema": "adaos.research.experiment_tracker_flush.v1",
            "experiment_id": experiment_id,
            "provider_id": provider.descriptor.provider_id,
            "sessions": results,
        }

    def delete_tracker_projection(
        self,
        *,
        experiment_id: str,
        session_id: str,
        accepted_export_digest: str,
    ) -> dict[str, Any]:
        provider = self._tracker_provider_for_experiment(experiment_id)
        session = provider.get_session(session_id)
        if session["experiment_id"] != experiment_id:
            raise ValueError("tracking session belongs to another experiment")
        delete = getattr(provider, "delete_provider_session", None)
        if not callable(delete):
            raise ValueError("selected tracker does not expose a deletable projection")
        return dict(delete(session_id, accepted_export_digest=accepted_export_digest))

    def verify_experiment_result(self, result_id: str) -> dict[str, Any]:
        result = self.repository.get("experiment_result", result_id)
        if result is None:
            raise KeyError(result_id)
        errors: list[str] = []
        expected_export_digest = str(result.payload["tracker_export_digest"])
        tracker_export = None
        export_record_id = str(result.payload.get("tracker_export_record_id") or "")
        if export_record_id:
            export_record = self.repository.get("tracker_export", export_record_id)
            tracker_export = dict(export_record.payload) if export_record else None
        if tracker_export is None:
            accepted = [
                item
                for item in self.repository.list(result.study_id, "tracker_evidence_acceptance")
                if item.payload.get("experiment_id") == result.payload["experiment_id"]
                and item.payload.get("tracker_export_digest") == expected_export_digest
            ]
            if accepted:
                export_record = self.repository.get(
                    "tracker_export",
                    str(accepted[-1].payload["tracker_export_record_id"]),
                )
                tracker_export = dict(export_record.payload) if export_record else None
        verification_source = "accepted-export"
        if tracker_export is None:
            # Compatibility for evidence finalized under tracker 1.0-rc1.
            tracker_export = self._tracker_provider_for_experiment(
                str(result.payload["experiment_id"])
            ).export_experiment(str(result.payload["experiment_id"]))
            verification_source = "live-journal-legacy"
        digest_input = dict(tracker_export)
        actual_export_digest = str(digest_input.pop("export_digest", ""))
        if digest(digest_input) != actual_export_digest or actual_export_digest != expected_export_digest:
            errors.append("tracker_export_digest_mismatch")
        for record in result.payload.get("artifact_refs") or []:
            payload = dict(record["payload"])
            uri = str(payload["uri"])
            runner_provider = str(payload.get("runner_provider") or "").strip()
            if runner_provider:
                verification = invoke_skill(
                    runner_provider,
                    "verify_artifact",
                    {"uri": uri, "digest": str(payload["digest"])},
                    timeout=30,
                )
                if not isinstance(verification, Mapping) or not bool(verification.get("ok")):
                    errors.append(f"artifact_verification:{uri}")
                continue
            if not uri.startswith("skill-data:"):
                errors.append(f"unsupported_artifact_uri:{uri}")
                continue
            path = self._runtime_data_root() / uri.split(":", 1)[1]
            if not path.is_file():
                errors.append(f"missing_artifact:{uri}")
            elif digest(path.read_bytes()) != payload["digest"]:
                errors.append(f"artifact_digest:{uri}")
        return {
            "schema": "adaos.research.experiment_result_verification.v1",
            "result_id": result_id,
            "ok": not errors,
            "errors": errors,
            "tracker_export_digest": actual_export_digest,
            "tracker_verification_source": verification_source,
            "checked_artifacts": len(result.payload.get("artifact_refs") or []),
        }

    def experiment_status(self, experiment_id: str) -> dict[str, Any]:
        value = experiment.get_experiment(self.repository, experiment_id)
        revision = experiment.latest_revision(self.repository, experiment_id)
        lifecycle = experiment.state(self.repository, experiment_id)
        runs = self._experiment_records(experiment_id, "run")
        bindings = self._attempt_bindings(experiment_id)
        attempts = []
        for binding in bindings:
            initial = dict(binding.payload["initial_attempt"])
            status = self._latest_attempt_status(experiment_id, str(binding.payload["attempt_id"]), str(initial["status"]))
            attempts.append(
                {
                    "attempt_id": binding.payload["attempt_id"],
                    "run_id": binding.payload["run_id"],
                    "arm_id": binding.payload["arm_id"],
                    "seed": binding.payload["seed"],
                    "attempt_number": binding.payload["attempt_number"],
                    "status": status,
                    "terminal": status in {"succeeded", "failed", "cancelled", "lost"},
                    "session_id": binding.payload["session_id"],
                    "runner_provider": binding.payload.get("runner_provider") or "legacy-local",
                    "runner_output_ref": binding.payload.get("runner_output_ref"),
                }
            )
        observations = self._experiment_records(experiment_id, "observation")
        artifacts = self._experiment_records(experiment_id, "artifact_ref")
        results = self._experiment_records(experiment_id, "experiment_result")
        conditions = dict(revision.payload["conditions"])
        dataset = dict(conditions["dataset"])
        runner = dict(conditions.get("runner") or {})
        runner_provider = str(runner.get("provider") or "").strip()
        data_owner_skill_id = str(
            runner.get("data_owner")
            or value.payload.get("data_owner_skill_id")
            or runner_provider
            or "research_manager_skill"
        )
        if runner_provider:
            try:
                data_status = invoke_skill(runner_provider, "dataset_status", {}, timeout=30)
                if not isinstance(data_status, Mapping):
                    raise RuntimeError("runner provider returned a non-object data status")
                data_projection = dict(data_status)
            except Exception as exc:
                data_projection = {
                    "owner_ref": f"skill:{data_owner_skill_id}",
                    "logical_name": str(dataset.get("name") or "dataset"),
                    "ready": False,
                    "state": "unavailable",
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                }
        else:
            data_root_value = str(dataset.get("data_root") or "").strip()
            data_root = Path(data_root_value) if data_root_value else self._runtime_data_root() / "files" / "datasets"
            required_files = [
                str(item).replace("\\", "/").lstrip("/")
                for item in dataset.get("required_files") or []
                if str(item).strip()
            ]
            dataset_ready = (
                all((data_root / name).is_file() for name in required_files)
                if required_files
                else data_root.is_dir() and any(data_root.iterdir())
            )
            data_projection = {
                "owner_ref": "skill:research_manager_skill",
                "logical_name": str(dataset.get("name") or "legacy-dataset"),
                "ready": dataset_ready,
                "legacy": True,
            }
        tracker_provider = str(dict(dict(revision.payload["conditions"]).get("tracker") or {}).get("provider") or "local-tracker")
        try:
            tracker_health = dict(self._tracker_provider(tracker_provider).health())
        except Exception as exc:
            tracker_health = {
                "ok": False,
                "state": "unavailable",
                "provider_id": tracker_provider,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            }
        return {
            "schema": "adaos.research.experiment_workbench.v1",
            "experiment": value.to_dict(),
            "research_space": {
                "schema": "adaos.research.space.v1",
                "space_id": f"skill:{data_owner_skill_id}/experiment:{experiment_id}",
                "control_plane_owner_ref": "skill:research_manager_skill",
                "data_owner_ref": f"skill:{data_owner_skill_id}",
                "runner_provider": runner_provider or "legacy-local",
            },
            "revision": revision.to_dict(),
            "revision_count": len(experiment.revisions(self.repository, experiment_id)),
            "conditions_document": json.dumps(revision.payload["conditions"], ensure_ascii=False, sort_keys=True, indent=2),
            "lifecycle": lifecycle,
            "dataset": {**data_projection, "download_declared": bool(dataset.get("download"))},
            "tracker": tracker_health,
            "runs": [item.to_dict() for item in runs],
            "attempts": attempts,
            "observations": [item.to_dict() for item in observations],
            "artifacts": [item.to_dict() for item in artifacts],
            "summary": self._experiment_summary(experiment_id),
            "result": results[-1].to_dict() if results else None,
            "result_verification": self.verify_experiment_result(results[-1].record_id) if results else None,
        }

    def experiment_attempts(self, experiment_id: str) -> dict[str, Any]:
        status = self.experiment_status(experiment_id)
        return {"schema": "adaos.research.attempt_list.v1", "items": status["attempts"]}

    def experiment_pairs(self, experiment_id: str) -> dict[str, Any]:
        return {"schema": "adaos.research.pair_result_list.v1", "items": self._experiment_summary(experiment_id)["pairs"]}

    def experiment_artifacts(self, experiment_id: str) -> dict[str, Any]:
        items = []
        for record in self._experiment_records(experiment_id, "artifact_ref"):
            payload = dict(record.payload)
            items.append(
                {
                    "artifact_id": record.record_id,
                    "role": payload["role"],
                    "run_id": payload["run_id"],
                    "attempt_id": payload["attempt_id"],
                    "uri": payload["uri"],
                    "digest": payload["digest"],
                    "size_bytes": payload["size_bytes"],
                    "media_type": payload["media_type"],
                }
            )
        return {"schema": "adaos.research.artifact_list.v1", "items": items}

    def describe_experiment(
        self,
        experiment_id: str,
        *,
        locale: str = "ru",
        channel: str = "text",
        section: str = "all",
        available_actions: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        experiment.get_experiment(self.repository, experiment_id)
        return guidance.describe(
            {"lifecycle": experiment.state(self.repository, experiment_id)},
            locale=locale,
            channel=channel,
            section=section,
            available_actions=available_actions,
        )

    def unblind_test(
        self,
        *,
        study_id: str,
        expected_generation: int,
        idempotency_key: str,
        actor: str,
        reason: str,
        evidence_refs: Sequence[str],
    ) -> dict[str, Any]:
        if not reason or not evidence_refs:
            raise ValueError("unblind requires reason and evidence")
        result = self.advance(
            study_id=study_id,
            command="unblind_test",
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
            evidence_refs=evidence_refs,
        )
        binding = next(
            item for item in self.repository.list(study_id, "split_binding") if item.payload["role"] == "test"
        )
        self.repository.event(
            study_id,
            "research.test.unblinded",
            {"actor": actor, "reason": reason, "evidence_refs": list(evidence_refs), "split_digest": binding.payload["digest"]},
        )
        return {**result, "test_binding": {**dict(binding.payload), "sealed": False}}

    def export_evidence(self, study_id: str) -> dict[str, Any]:
        if workflow_state(self.repository, study_id)["state"] not in {"analysis", "claim_review", "complete"}:
            raise ValueError("evidence can be finalized only after analysis")
        return evidence.export(self.repository, study_id)

    def verify_evidence(self, bundle_id: str) -> dict[str, Any]:
        return evidence.verify(self.repository, bundle_id)

    def decide_claim(
        self,
        *,
        study_id: str,
        verdict: str,
        rationale: str,
        bundle_id: str,
        expected_generation: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        if verdict not in {"accepted", "rejected", "inconclusive"}:
            raise ValueError("unsupported claim verdict")
        verification = self.verify_evidence(bundle_id)
        if not verification["ok"]:
            raise ValueError("evidence bundle failed verification")
        decision_id = identity("claim_decision", {"study_id": study_id, "bundle_id": bundle_id, "verdict": verdict, "rationale": rationale})
        decision = self.repository.put(
            ResearchRecord(
                "claim_decision",
                decision_id,
                study_id,
                0,
                {"verdict": verdict, "rationale": rationale, "bundle_id": bundle_id, "decided_by": actor, "decided_at": now()},
            )
        )
        workflow = self.advance(
            study_id=study_id,
            command="decide_claim",
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
            evidence_refs=(bundle_id, verification["manifest_digest"]),
        )
        return {"decision": decision.to_dict(), "workflow": workflow}

    def status(self, study_id: str) -> dict[str, Any]:
        study = self.repository.get("study", study_id)
        if study is None:
            raise KeyError(study_id)
        records = self.repository.list(study_id)
        counts: dict[str, int] = {}
        for record in records:
            counts[record.kind] = counts.get(record.kind, 0) + 1
        return {
            "study": study.to_dict(),
            "realizations": [item.to_dict() for item in self.repository.list(study_id, "study_realization")],
            "workflow": workflow_state(self.repository, study_id),
            "counts": counts,
            "events": self.repository.events(study_id),
        }


__all__ = ["ResearchManager"]
