"""Supervise the pinned MLflow server inside the skill runtime boundary."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def data_root() -> Path:
    env_path = str(os.getenv("ADAOS_SKILL_ENV_PATH") or "").strip()
    if not env_path:
        raise RuntimeError("ADAOS_SKILL_ENV_PATH is required for MLflow storage isolation")
    return Path(env_path).resolve().parent.parent


def server_command() -> list[str]:
    root = data_root()
    database = root / "db" / "mlflow.db"
    artifacts = root / "files" / "artifacts"
    database.parent.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    host = str(os.getenv("ADAOS_SERVICE_HOST") or "127.0.0.1")
    port = str(int(os.getenv("ADAOS_SERVICE_PORT") or "18121"))
    backend_uri = str(os.getenv("ADAOS_MLFLOW_BACKEND_STORE_URI") or f"sqlite:///{database.as_posix()}")
    artifact_uri = str(os.getenv("ADAOS_MLFLOW_ARTIFACTS_DESTINATION") or artifacts.as_uri())
    return [
        sys.executable,
        "-m",
        "mlflow",
        "server",
        "--host",
        host,
        "--port",
        port,
        "--backend-store-uri",
        backend_uri,
        "--artifacts-destination",
        artifact_uri,
        "--allowed-hosts",
        "127.0.0.1:*,localhost:*",
    ]


def main() -> None:
    process = subprocess.Popen(server_command())  # noqa: S603 - fixed argv, no shell

    def stop(_signum: int, _frame: object) -> None:
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    raise SystemExit(process.wait())


if __name__ == "__main__":
    main()
