from __future__ import annotations

import json
import urllib.request
import uuid
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from adaos.domain.runtime_bindings import ServiceBinding
from research.contracts import ResearchRecord, digest, identity
from research.manager import ResearchManager
from research.tracker import (
    TRACKER_CONTRACT_VERSION,
    LocalTracker,
    MlflowTracker,
    TrackerBackpressure,
    TrackerDeliveryError,
)


class _MlflowApi:
    def __init__(self) -> None:
        self.available = True
        self.run_exists = False
        self.calls: list[tuple[str, dict | None]] = []

    def request(self, path: str, payload: dict | None = None, *, method: str | None = None) -> dict:
        del method
        self.calls.append((path, payload))
        if not self.available:
            raise RuntimeError("provider outage")
        if path == "/health":
            return {"body": "OK"}
        if path == "/version":
            return {"value": "3.15.1"}
        if path.startswith("/api/2.0/mlflow/experiments/get-by-name"):
            return {"experiment": {"experiment_id": "42"}}
        if path == "/api/2.0/mlflow/runs/search":
            return {"runs": [{"info": {"run_id": "mlflow-run-1"}}]} if self.run_exists else {"runs": []}
        if path == "/api/2.0/mlflow/runs/create":
            self.run_exists = True
            return {"run": {"info": {"run_id": "mlflow-run-1"}}}
        if path.startswith("/api/2.0/mlflow/runs/get"):
            return {"run": {"data": {"params": []}}}
        if path in {
            "/api/2.0/mlflow/runs/log-batch",
            "/api/2.0/mlflow/runs/update",
            "/api/2.0/mlflow/runs/delete",
        }:
            return {}
        raise AssertionError(path)


def _open(provider: MlflowTracker, suffix: str) -> tuple[str, str]:
    session_id = f"session.{suffix}"
    attempt_id = f"attempt.{suffix}"
    provider.open_session(
        session_id=session_id,
        study_id=f"study.{suffix}",
        experiment_id=f"experiment.{suffix}",
        experiment_revision_id=f"revision.{suffix}",
        trial_id=f"trial.{suffix}",
        run_id=f"run.{suffix}",
        attempt_id=attempt_id,
        parameters={"epochs": 3},
        tags={
            "adaos.trial_group_id": f"trial-group.{suffix}",
            "adaos.protocol_digest": "sha256:" + "1" * 64,
            "adaos.analysis_plan_digest": "sha256:" + "2" * 64,
            "adaos.source.code_digest": "sha256:" + "3" * 64,
            "adaos.environment_digest": "sha256:" + "4" * 64,
            "adaos.data_digest": "sha256:" + "5" * 64,
            "adaos.trace_id": f"trace.{suffix}",
            "adaos.evidence_class": "workflow_validation",
        },
        inputs=({"kind": "dataset", "digest": "sha256:" + "5" * 64},),
    )
    return session_id, attempt_id


def _observation(attempt_id: str, sequence: int, *, value: float | None = None) -> dict:
    return {
        "metric": {"namespace": "tlp", "name": "top1_accuracy"},
        "value": float(value if value is not None else sequence / 10),
        "value_type": "float",
        "unit": "1",
        "split_role": "validation",
        "step": {"axis": "epoch", "value": 1},
        "aggregation": "point",
        "producer": {"attempt_id": attempt_id, "sequence": sequence},
        "evidence_role": "primary",
    }


def test_frozen_tracker_contract_schemas_validate_reference_provider() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas" / "tracker.contract.v1.schema.json").read_text(encoding="utf-8"))
    manager = ResearchManager()
    descriptor = LocalTracker(manager.repository).descriptor.to_dict()

    assert TRACKER_CONTRACT_VERSION == "1.0"
    jsonschema.Draft202012Validator(schema["$defs"]["descriptor"]).validate(descriptor)


def test_bounded_outbox_survives_outage_restart_and_requires_terminal_delivery(monkeypatch) -> None:
    manager = ResearchManager()
    api = _MlflowApi()
    api.available = False
    provider = MlflowTracker(manager.repository, "http://127.0.0.1:18121")
    bounded = replace(
        provider.descriptor,
        limits={**dict(provider.descriptor.limits), "max_pending_events": 2},
    )
    provider.descriptor = bounded
    provider.journal.descriptor = bounded
    monkeypatch.setattr(provider, "_request", api.request)
    suffix = uuid.uuid4().hex
    session_id, attempt_id = _open(provider, suffix)

    provider.append_observations(session_id, [_observation(attempt_id, 1)])
    provider.append_observations(session_id, [_observation(attempt_id, 2)])
    assert provider.journal.delivery_status(session_id)["pending"] == 2
    assert provider.health()["state"] == "unavailable"
    with pytest.raises(TrackerBackpressure):
        provider.append_observations(session_id, [_observation(attempt_id, 3)])
    with pytest.raises(TrackerDeliveryError):
        provider.close_session(session_id, "succeeded", {"observations_complete": True})
    assert provider.get_session(session_id)["status"] == "running"

    restarted = MlflowTracker(manager.repository, "http://127.0.0.1:18121")
    restarted.descriptor = bounded
    restarted.journal.descriptor = bounded
    api.available = True
    monkeypatch.setattr(restarted, "_request", api.request)
    flushed = restarted.flush(session_id, required=True)
    assert flushed["delivery"]["pending"] == 0
    closed = restarted.close_session(
        session_id,
        "succeeded",
        {"observations_complete": True, "artifacts_complete": True},
    )
    assert closed["session"]["status"] == "succeeded"


def test_ordering_duplicate_steps_large_artifact_provider_link_and_accepted_deletion(monkeypatch) -> None:
    manager = ResearchManager()
    api = _MlflowApi()
    provider = MlflowTracker(manager.repository, "http://127.0.0.1:18121")
    monkeypatch.setattr(provider, "_request", api.request)
    suffix = uuid.uuid4().hex
    session_id, attempt_id = _open(provider, suffix)
    observations = [_observation(attempt_id, 2), _observation(attempt_id, 1)]

    first = provider.append_observations(session_id, observations)
    duplicate = provider.append_observations(session_id, [observations[0]])
    assert len(first["accepted"]) == 2
    assert duplicate["duplicates"] == [first["accepted"][0]]
    history = provider.metric_history(session_id, "tlp", "top1_accuracy")
    assert {item["producer"]["sequence"] for item in history} == {1, 2}
    artifact = {
        "uri": "adaos-content:sha256/" + "a" * 64,
        "digest": "sha256:" + "a" * 64,
        "size_bytes": 5 * 1024 * 1024 * 1024,
        "media_type": "application/octet-stream",
        "role": "checkpoint",
    }
    provider.append_artifacts(session_id, [artifact])
    provider.close_session(session_id, "succeeded", {"observations_complete": True, "artifacts_complete": True})
    exported = provider.export_session(session_id)
    assert exported["contract_version"] == "1.0"
    contract_schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "tracker.contract.v1.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(
        {**contract_schema, "$ref": "#/$defs/export"}
    ).validate(exported)
    assert exported["provider_links"][0]["mlflow_run_id"] == "mlflow-run-1"
    assert next(item for item in exported["events"] if item["event_kind"] == "artifact")["payload"]["size_bytes"] == artifact["size_bytes"]

    experiment_export = provider.export_experiment(f"experiment.{suffix}")
    export_id = identity(
        "tracker_export",
        {"experiment_id": f"experiment.{suffix}", "export_digest": experiment_export["export_digest"]},
    )
    manager.repository.put(
        ResearchRecord("tracker_export", export_id, f"study.{suffix}", 0, experiment_export)
    )
    manager.repository.put(
        ResearchRecord(
            "tracker_evidence_acceptance",
            identity("tracker_evidence_acceptance", {"export": experiment_export["export_digest"]}),
            f"study.{suffix}",
            0,
            {
                "experiment_id": f"experiment.{suffix}",
                "tracker_export_record_id": export_id,
                "tracker_export_digest": experiment_export["export_digest"],
            },
        )
    )
    deleted = provider.delete_provider_session(
        session_id,
        accepted_export_digest=experiment_export["export_digest"],
    )
    assert deleted["deleted"] is True
    assert deleted["link"]["state"] == "deleted"
    assert any(path == "/api/2.0/mlflow/runs/delete" for path, _payload in api.calls)
    frozen = manager.repository.get("tracker_export", export_id)
    assert frozen is not None
    digest_input = dict(frozen.payload)
    frozen_digest = digest_input.pop("export_digest")
    assert digest(digest_input) == frozen_digest


def test_external_mlflow_requires_tls_binding_and_sends_resolved_auth(monkeypatch) -> None:
    manager = ResearchManager()
    with pytest.raises(ValueError, match="TLS"):
        MlflowTracker(
            manager.repository,
            ServiceBinding(
                binding_id="binding.mlflow.insecure",
                capability="tracker.experiment",
                provider_ref="service:mlflow-tracker",
                consumer_ref="skill:research-manager",
                endpoint="http://mlflow.example.test",
                protocol="mlflow-rest",
                protocol_version="2.0",
            ),
        )
    binding = ServiceBinding(
        binding_id="binding.mlflow.external",
        capability="tracker.experiment",
        provider_ref="service:mlflow-tracker",
        consumer_ref="skill:research-manager",
        endpoint="https://mlflow.example.test",
        protocol="mlflow-rest",
        protocol_version="2.0",
        health_endpoint="https://mlflow.example.test/health",
        ui_endpoint="https://mlflow.example.test/",
        secret_ref="skill-secret:mlflow-token",
    )
    provider = MlflowTracker(
        manager.repository,
        binding,
        auth_headers={"Authorization": "Bearer resolved-test-secret"},
    )
    requests: list[urllib.request.Request] = []

    class _Response:
        status = 200

        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    def urlopen(request: urllib.request.Request, timeout: float):  # noqa: ANN202
        assert timeout == 5.0
        requests.append(request)
        return _Response(b'"3.15.1"' if request.full_url.endswith("/version") else b"OK")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    probe = provider.capability_probe()

    assert probe["provider_version"] == "3.15.1"
    assert probe["authenticated"] is True
    assert probe["binding_id"] == binding.binding_id
    assert all(request.get_header("Authorization") == "Bearer resolved-test-secret" for request in requests)
