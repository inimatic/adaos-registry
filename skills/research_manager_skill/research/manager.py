"""Research-manager application service."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

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
    reconcile,
    spec,
    submit,
)

from research import evidence, experiment, guidance
from research.contracts import ResearchRecord, canonical_json, digest, identity, now
from research.repository import ResearchRepository
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
        deadline = time.monotonic() + 15
        while not attempt.terminal and time.monotonic() < deadline:
            time.sleep(0.05)
            attempt = reconcile(attempt.attempt_id)
        if not attempt.terminal:
            raise TimeoutError("fixture attempt did not reach a terminal state")
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
            network=ExecutionNetworkPolicy(mode="unrestricted"),
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
        return {"study": study.to_dict(), "workflow": workflow_state(self.repository, study_id), "counts": counts, "events": self.repository.events(study_id)}


__all__ = ["ResearchManager"]
