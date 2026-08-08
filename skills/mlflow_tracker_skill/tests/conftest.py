from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_skill_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_file = tmp_path / ".runtime" / "mlflow_tracker_skill" / "v0.1" / "data" / "db" / "skill_env.json"
    env_file.parent.mkdir(parents=True)
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(env_file))
    monkeypatch.setenv(
        "ADAOS_MLFLOW_TRACKING_URI",
        "http://127.0.0.1:18121/api/services/mlflow_tracker_skill/ui",
    )
    yield
