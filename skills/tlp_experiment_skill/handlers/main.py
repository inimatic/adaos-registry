"""Typed runner-provider boundary for the TLP reference experiment."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from tlp.runner import STL10_MD5, STL10_URL, _sha256_file


_OUTPUT_PREFIX = "tlp-output."
_ARTIFACT_PREFIX = "skill-data:tlp_experiment_skill/"
_REQUIRED_DATASET_FILES = ("train_X.bin", "train_y.bin", "test_X.bin", "test_y.bin")


def _digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _data_root() -> Path:
    env_path = str(os.getenv("ADAOS_SKILL_ENV_PATH") or "").strip()
    if not env_path:
        raise RuntimeError("TLP skill runtime data path is unavailable")
    return Path(env_path).resolve().parent.parent


def _dataset_root() -> Path:
    return _data_root() / "files" / "datasets"


def _dataset_status() -> dict[str, Any]:
    root = _dataset_root() / "stl10_binary"
    ready = all((root / name).is_file() for name in _REQUIRED_DATASET_FILES)
    return {
        "schema": "adaos.research.data_binding_status.v1",
        "owner_ref": "skill:tlp_experiment_skill",
        "logical_name": "stl10-binary-2011",
        "ready": ready,
        "source": STL10_URL,
        "archive_md5": STL10_MD5,
    }


def _output_dir(output_ref: str) -> Path:
    token = str(output_ref or "").strip()
    suffix = token[len(_OUTPUT_PREFIX) :] if token.startswith(_OUTPUT_PREFIX) else ""
    if len(suffix) != 64 or any(ch not in "0123456789abcdef" for ch in suffix):
        raise ValueError("invalid TLP output_ref")
    root = (_data_root() / "internal" / "runs").resolve()
    target = (root / suffix[:24]).resolve()
    if root not in target.parents:
        raise ValueError("TLP output_ref escapes the owned data root")
    return target


def _provider_python_path() -> str:
    bucket = _data_root().parent
    vendor = bucket / "vendor"
    existing = str(os.getenv("PYTHONPATH") or "").strip()
    return os.pathsep.join(item for item in (str(vendor), existing) if item)


def _copy_or_link_tree(source: Path, target: Path) -> tuple[int, str]:
    copied = 0
    mode = "hardlink"
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue
        try:
            os.link(item, destination)
        except OSError:
            shutil.copy2(item, destination)
            mode = "copy"
        copied += 1
    return copied, mode


@tool("adopt_legacy_data")
def adopt_legacy_data() -> dict[str, Any]:
    """Adopt the former research-manager STL-10 payload without owning its DB."""

    current = _dataset_status()
    if current["ready"]:
        return {"ok": True, "skipped": True, "reason": "dataset_ready", "binding": current}
    try:
        from adaos.sdk.core.ctx import get_ctx
        from adaos.services.skill.runtime_env import SkillRuntimeEnvironment

        skills_root = Path(get_ctx().paths.skills_workspace_dir()).resolve()
        legacy = SkillRuntimeEnvironment(skills_root=skills_root, skill_name="research_manager_skill")
        version = legacy.resolve_active_version()
        source = legacy.data_root(version) / "files" / "datasets" if version else None
    except Exception as exc:
        return {"ok": True, "skipped": True, "reason": "legacy_runtime_unavailable", "detail": str(exc)}
    if source is None or not source.is_dir():
        return {"ok": True, "skipped": True, "reason": "legacy_dataset_absent"}
    copied, mode = _copy_or_link_tree(source, _dataset_root())
    binding = _dataset_status()
    if not binding["ready"]:
        raise RuntimeError("legacy TLP dataset adoption did not produce a complete binding")
    return {
        "ok": True,
        "skipped": False,
        "mode": mode,
        "copied_entries": copied,
        "source_owner_ref": "skill:research_manager_skill",
        "target_owner_ref": "skill:tlp_experiment_skill",
        "retention": "retain_until_previous_runtime_retired",
        "binding": binding,
    }


@tool("dataset_status")
def dataset_status() -> dict[str, Any]:
    return _dataset_status()


@tool("prepare_attempt")
def prepare_attempt(request: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(request or {})
    conditions = dict(value.get("conditions") or {})
    profile = dict(value.get("profile_conditions") or {})
    dataset = dict(conditions.get("dataset") or {})
    arm = dict(value.get("arm") or {})
    arm_id = str(arm.get("id") or "").strip()
    if arm_id not in {"maxpool", "tlp"}:
        raise ValueError("TLP provider supports maxpool and tlp arms")
    seed = int(value["seed"])
    attempt_number = int(value["attempt_number"])
    output_ref = _OUTPUT_PREFIX + _digest(
        {
            "experiment_id": value["experiment_id"],
            "run_id": value["run_id"],
            "attempt_number": attempt_number,
        }
    ).split(":", 1)[1]
    output_dir = _output_dir(output_ref)
    output_dir.mkdir(parents=True, exist_ok=True)
    runner = (_SKILL_ROOT / "tlp" / "runner.py").resolve()
    requirements = _SKILL_ROOT / "requirements.in"
    code_digest = _sha256_file(runner)
    environment_digest = _digest(
        {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "requirements": _sha256_file(requirements) if requirements.is_file() else None,
            "runner": code_digest,
        }
    )
    command = [
        sys.executable,
        str(runner),
        "--operator",
        arm_id,
        "--seed",
        str(seed),
        "--epochs",
        str(int(profile["epochs"])),
        "--batch-size",
        str(int(profile.get("batch_size") or 32)),
        "--learning-rate",
        str(float(profile.get("learning_rate") or 0.001)),
        "--max-train-samples",
        str(int(profile.get("max_train_samples") or 0)),
        "--max-validation-samples",
        str(int(profile.get("max_validation_samples") or 0)),
        "--validation-per-class",
        str(int(dataset.get("validation_per_class") or 100)),
        "--split-seed",
        str(int(dataset.get("split_seed") or 20260807)),
        "--cpu-threads",
        str(int(profile.get("cpu_threads") or 2)),
        "--data-root",
        str(_dataset_root()),
        "--output-dir",
        str(output_dir),
        "--evidence-class",
        str(profile.get("evidence_class") or "workflow_validation"),
    ]
    if bool(dataset.get("download")):
        command.append("--download")
    data_identity = {
        "name": dataset.get("name"),
        "version": dataset.get("version"),
        "source": dataset.get("source"),
        "archive_md5": dataset.get("archive_md5"),
        "split_seed": dataset.get("split_seed"),
        "validation_per_class": dataset.get("validation_per_class"),
    }
    return {
        "schema": "adaos.research.runner_preparation.v1",
        "contract": "adaos.research.runner.v1",
        "provider_id": "tlp_experiment_skill",
        "owner_ref": "skill:tlp_experiment_skill",
        "output_ref": output_ref,
        "spec_id": "research.tlp.pool2.v1",
        "command": command,
        "working_directory": str(output_dir),
        "package_ref": {
            "uri": "skill-package:tlp_experiment_skill/tlp/runner.py",
            "digest": code_digest,
            "size_bytes": runner.stat().st_size,
            "media_type": "text/x-python",
            "owner_ref": "skill:tlp_experiment_skill",
            "kind": "execution-package",
        },
        "code_digest": code_digest,
        "environment_digest": environment_digest,
        "environment": {"PYTHONHASHSEED": str(seed), "PYTHONPATH": _provider_python_path()},
        "expected_outputs": [
            "observations.ndjson",
            "result.json",
            "artifacts.json",
            "checkpoint.pt",
            "predictions.jsonl",
        ],
        "parameters": {
            "operator": arm_id,
            "seed": seed,
            "epochs": int(profile["epochs"]),
            "batch_size": int(profile.get("batch_size") or 32),
            "learning_rate": float(profile.get("learning_rate") or 0.001),
        },
        "inputs": [{"kind": "dataset", **data_identity, "digest": _digest(data_identity)}],
        "data_binding": _dataset_status(),
    }


@tool("collect_attempt")
def collect_attempt(output_ref: str) -> dict[str, Any]:
    root = _output_dir(output_ref)
    observations: list[dict[str, Any]] = []
    observations_path = root / "observations.ndjson"
    if observations_path.is_file():
        lines = observations_path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                observations.append(json.loads(line))
            except json.JSONDecodeError:
                if index != len(lines) - 1:
                    raise
    result_path = root / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
    artifacts: list[dict[str, Any]] = []
    manifest_path = root / "artifacts.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("artifacts") or []:
            source = (root / str(item["path"])).resolve()
            if root.resolve() not in source.parents or not source.is_file():
                raise FileNotFoundError("declared TLP artifact is outside the output binding or missing")
            if _sha256_file(source) != item["digest"] or source.stat().st_size != int(item["size_bytes"]):
                raise ValueError(f"TLP artifact integrity mismatch: {source.name}")
            relative = source.relative_to(_data_root()).as_posix()
            artifacts.append({**dict(item), "uri": f"{_ARTIFACT_PREFIX}{relative}"})
    return {
        "schema": "adaos.research.runner_collection.v1",
        "provider_id": "tlp_experiment_skill",
        "output_ref": output_ref,
        "observations": observations,
        "artifacts": artifacts,
        "result": result,
        "complete": result is not None and bool(artifacts),
    }


@tool("verify_artifact")
def verify_artifact(uri: str, digest: str) -> dict[str, Any]:
    value = str(uri or "")
    if not value.startswith(_ARTIFACT_PREFIX):
        return {"ok": False, "reason": "owner_mismatch"}
    relative = value[len(_ARTIFACT_PREFIX) :]
    root = _data_root().resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        return {"ok": False, "reason": "missing"}
    actual = _sha256_file(path)
    return {"ok": actual == str(digest), "actual_digest": actual, "size_bytes": path.stat().st_size}
