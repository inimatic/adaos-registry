from __future__ import annotations

import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from handlers.main import collect_attempt, dataset_status, prepare_attempt, verify_artifact
from tlp.runner import _model, _state_digest, run as run_tlp


def _conditions() -> dict:
    return {
        "dataset": {
            "name": "STL10",
            "version": "binary-2011",
            "split_seed": 7,
            "validation_per_class": 1,
            "download": False,
        }
    }


def test_runner_provider_owns_data_and_exposes_portable_descriptor(monkeypatch, tmp_path: Path) -> None:
    env = tmp_path / "data" / "db" / "skill_env.json"
    env.parent.mkdir(parents=True)
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(env))
    request = {
        "experiment_id": "experiment.test",
        "run_id": "run.test",
        "attempt_number": 1,
        "seed": 17,
        "arm": {"id": "maxpool"},
        "conditions": _conditions(),
        "profile_conditions": {"epochs": 3, "batch_size": 32},
    }

    prepared = prepare_attempt(request)

    assert prepared["contract"] == "adaos.research.runner.v1"
    assert prepared["owner_ref"] == "skill:tlp_experiment_skill"
    assert prepared["package_ref"]["owner_ref"] == "skill:tlp_experiment_skill"
    assert prepared["data_binding"]["owner_ref"] == "skill:tlp_experiment_skill"
    assert "research_manager_skill" not in prepared["package_ref"]["uri"]


def test_collection_and_verification_never_expose_physical_paths(monkeypatch, tmp_path: Path) -> None:
    env = tmp_path / "data" / "db" / "skill_env.json"
    env.parent.mkdir(parents=True)
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(env))
    prepared = prepare_attempt(
        {
            "experiment_id": "experiment.test",
            "run_id": "run.test",
            "attempt_number": 1,
            "seed": 17,
            "arm": {"id": "maxpool"},
            "conditions": _conditions(),
            "profile_conditions": {"epochs": 1},
        }
    )
    output = Path(prepared["working_directory"])
    result = {"best_validation_accuracy": 0.25, "best_epoch": 1, "initial_state_digest": "sha256:test"}
    result_path = output / "result.json"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(result_path.read_bytes()).hexdigest()
    (output / "artifacts.json").write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "path": "result.json",
                        "role": "result",
                        "digest": digest,
                        "size_bytes": result_path.stat().st_size,
                        "media_type": "application/json",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    collected = collect_attempt(prepared["output_ref"])

    artifact = collected["artifacts"][0]
    assert artifact["uri"].startswith("skill-data:tlp_experiment_skill/")
    assert "working_directory" not in collected
    assert verify_artifact(artifact["uri"], digest)["ok"] is True
    assert dataset_status()["owner_ref"] == "skill:tlp_experiment_skill"


def test_tlp_and_control_share_identical_non_operator_initial_state() -> None:
    baseline = _model("maxpool", 17)
    treatment = _model("tlp", 17)
    assert _state_digest(baseline) == _state_digest(treatment)


def test_real_tlp_runner_executes_one_cpu_epoch_on_binary_contract_fixture(tmp_path: Path) -> None:
    binary = tmp_path / "dataset" / "stl10_binary"
    binary.mkdir(parents=True)
    generator = __import__("random").Random(17)
    train_count = 20
    test_count = 10
    (binary / "train_X.bin").write_bytes(bytes(generator.randrange(256) for _ in range(train_count * 3 * 96 * 96)))
    (binary / "train_y.bin").write_bytes(bytes((index % 10) + 1 for index in range(train_count)))
    (binary / "test_X.bin").write_bytes(bytes(generator.randrange(256) for _ in range(test_count * 3 * 96 * 96)))
    (binary / "test_y.bin").write_bytes(bytes((index % 10) + 1 for index in range(test_count)))
    output = tmp_path / "run"
    result = run_tlp(
        Namespace(
            operator="tlp",
            seed=17,
            epochs=1,
            batch_size=10,
            learning_rate=0.001,
            max_train_samples=10,
            max_validation_samples=10,
            validation_per_class=1,
            split_seed=7,
            cpu_threads=1,
            data_root=str(tmp_path / "dataset"),
            output_dir=str(output),
            evidence_class="test-only",
            download=False,
        )
    )
    assert result["operator"] == "tlp"
    assert result["epochs"] == 1
    assert result["tlp_components"]["centered_sum_max_abs"] < 1e-6
    assert {path.name for path in output.iterdir()} >= {
        "result.json", "observations.ndjson", "checkpoint.pt", "predictions.jsonl", "artifacts.json"
    }
