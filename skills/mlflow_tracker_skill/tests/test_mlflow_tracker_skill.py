from __future__ import annotations

import os
from pathlib import Path

from handlers.main import get_tracking_ui, provider_descriptor
from handlers.service import data_root, server_command, server_environment, service_python


def test_descriptor_exposes_tracker_contract_without_claiming_authority() -> None:
    descriptor = provider_descriptor()
    assert descriptor["provider_id"] == "mlflow"
    assert descriptor["contract_version"] == "1.0-rc1"
    assert descriptor["authority"] == "telemetry-projection"
    assert "typed-scalar-observations" in descriptor["capabilities"]
    assert get_tracking_ui()["presentation"] == "external-tab"


def test_server_storage_is_skill_scoped() -> None:
    root = data_root()
    command = server_command()
    assert root.name == "data"
    backend = command[command.index("--backend-store-uri") + 1]
    artifacts = command[command.index("--artifacts-destination") + 1]
    assert (root / "db" / "mlflow.db").as_posix() in backend
    assert artifacts == (root / "files" / "artifacts").as_uri()
    assert "--allowed-hosts" in command
    assert command[command.index("--allowed-hosts") + 1] == "127.0.0.1:*,localhost:*"
    assert Path(command[0]) == service_python()
    assert any(
        "site-packages" in item.lower()
        for item in server_environment()["PYTHONPATH"].split(os.pathsep)
        if item
    )


def test_pinned_mlflow_client_roundtrip(tmp_path: Path) -> None:
    from mlflow import MlflowClient
    from mlflow.entities import Metric, Param, RunTag

    backend = tmp_path / "contract.db"
    client = MlflowClient(tracking_uri=f"sqlite:///{backend.as_posix()}")
    experiment_id = client.create_experiment("adaos-contract")
    run = client.create_run(
        experiment_id,
        tags={"adaos.session_id": "session.contract", "adaos.attempt_id": "attempt.contract"},
    )
    client.log_batch(
        run.info.run_id,
        metrics=[Metric("validation.tlp.top1_accuracy", 0.5, 1, 1)],
        params=[Param("epochs", "3")],
        tags=[RunTag("adaos.contract_version", "1.0-rc1")],
    )
    client.set_terminated(run.info.run_id, status="FINISHED")
    stored = client.get_run(run.info.run_id)
    assert stored.data.metrics["validation.tlp.top1_accuracy"] == 0.5
    assert stored.data.params["epochs"] == "3"
    assert stored.data.tags["adaos.session_id"] == "session.contract"
