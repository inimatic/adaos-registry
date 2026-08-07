"""Research-manager application service."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from adaos.sdk.execution import (
    ContentRef,
    ExecutionBudget,
    ExecutionDeterminism,
    ExecutionNetworkPolicy,
    ExecutionResourceRequest,
    reconcile,
    spec,
    submit,
)

from research import evidence
from research.contracts import ResearchRecord, digest, identity, now
from research.repository import ResearchRepository
from research.tracker import LocalTracker
from research.workflow import state as workflow_state
from research.workflow import transition


class ResearchManager:
    def __init__(self) -> None:
        self.repository = ResearchRepository()
        self.tracker = LocalTracker(self.repository)

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
        if operator not in {"baseline", "max_plus"}:
            raise ValueError("fixture supports baseline and max_plus operators")
        streams = {
            name: int(seed) + offset
            for offset, name in enumerate(ExecutionDeterminism.REQUIRED_STREAMS)
        }
        rng_digest = digest(streams)
        run_id = identity("run", {"trial_id": trial_id, "sample_generation": 0, "rng_digest": rng_digest})
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
