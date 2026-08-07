"""Real, deterministic CPU runner for the STL-10 pool2 TLP control experiment."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import shutil
import tarfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


STL10_URL = "https://ai.stanford.edu/~acoates/stl10/stl10_binary.tar.gz"
STL10_MD5 = "91f7769df0f17e558f3565bffb0c7dfb"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return f"sha256:{value.hexdigest()}"


def _md5_file(path: Path) -> str:
    value = hashlib.md5()  # noqa: S324 - upstream compatibility checksum only
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _safe_extract(archive: Path, root: Path) -> None:
    target = root.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            resolved = (root / member.name).resolve()
            if target != resolved and target not in resolved.parents:
                raise ValueError(f"unsafe STL-10 archive member: {member.name}")
        bundle.extractall(root)  # noqa: S202 - paths were resolved and bounded above


def ensure_stl10(data_root: Path, *, download: bool) -> Path:
    data_root.mkdir(parents=True, exist_ok=True)
    extracted = data_root / "stl10_binary"
    required = (extracted / "train_X.bin", extracted / "train_y.bin", extracted / "test_X.bin", extracted / "test_y.bin")
    if all(path.is_file() for path in required):
        return extracted
    if not download:
        raise FileNotFoundError(
            f"STL-10 binary dataset is not ready under {data_root}; enable the declared download policy"
        )
    archive = data_root / "stl10_binary.tar.gz"
    temporary = archive.with_suffix(".download")
    lock = data_root / ".stl10-download.lock"
    acquired = False
    while not acquired:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(descriptor)
            acquired = True
        except FileExistsError:
            if all(path.is_file() for path in required):
                return extracted
            if lock.exists() and time.time() - lock.stat().st_mtime > 4 * 60 * 60:
                lock.unlink(missing_ok=True)
                continue
            time.sleep(2)
    try:
        if not archive.is_file() or _md5_file(archive) != STL10_MD5:
            temporary.unlink(missing_ok=True)
            with urllib.request.urlopen(STL10_URL, timeout=60) as source, temporary.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            if _md5_file(temporary) != STL10_MD5:
                raise ValueError("downloaded STL-10 archive checksum mismatch")
            os.replace(temporary, archive)
        _safe_extract(archive, data_root)
    finally:
        temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    if not all(path.is_file() for path in required):
        raise FileNotFoundError("STL-10 archive did not produce the required binary files")
    return extracted


def _load_binary(root: Path, split: str):
    import torch

    image_path = root / f"{split}_X.bin"
    label_path = root / f"{split}_y.bin"
    raw = torch.frombuffer(bytearray(image_path.read_bytes()), dtype=torch.uint8).clone()
    images = raw.reshape(-1, 3, 96, 96).transpose(2, 3).contiguous()
    labels = torch.frombuffer(bytearray(label_path.read_bytes()), dtype=torch.uint8).to(torch.long) - 1
    if len(images) != len(labels):
        raise ValueError(f"STL-10 {split} image/label count mismatch")
    return images, labels


def _stratified_indices(labels, *, validation_per_class: int, split_seed: int):
    import torch

    train: list[int] = []
    validation: list[int] = []
    for label in range(10):
        values = torch.nonzero(labels == label, as_tuple=False).flatten()
        generator = torch.Generator().manual_seed(split_seed + label)
        values = values[torch.randperm(len(values), generator=generator)].tolist()
        validation.extend(int(item) for item in values[:validation_per_class])
        train.extend(int(item) for item in values[validation_per_class:])
    return sorted(train), sorted(validation)


def _balanced_limit(indices: Iterable[int], labels, limit: int, seed: int) -> list[int]:
    import torch

    values = list(indices)
    if limit <= 0 or len(values) <= limit:
        return values
    per_class: list[list[int]] = [[] for _ in range(10)]
    for index in values:
        per_class[int(labels[index])].append(index)
    selected: list[int] = []
    base = limit // 10
    remainder = limit % 10
    for label, class_indices in enumerate(per_class):
        generator = torch.Generator().manual_seed(seed + label)
        order = torch.randperm(len(class_indices), generator=generator).tolist()
        count = base + (1 if label < remainder else 0)
        selected.extend(class_indices[item] for item in order[:count])
    return sorted(selected)


def _augment_batch(images, original_indices: list[int], *, epoch: int, augmentation_seed: int):
    import torch
    import torch.nn.functional as functional

    values = images.to(dtype=torch.float32).div_(255.0)
    padded = functional.pad(values, (4, 4, 4, 4), mode="reflect")
    output = torch.empty_like(values)
    for position, original_index in enumerate(original_indices):
        generator = torch.Generator().manual_seed(
            augmentation_seed * 1_000_003 + epoch * 10_007 + int(original_index)
        )
        top = int(torch.randint(0, 9, (1,), generator=generator).item())
        left = int(torch.randint(0, 9, (1,), generator=generator).item())
        sample = padded[position, :, top : top + 96, left : left + 96]
        if float(torch.rand((), generator=generator).item()) < 0.5:
            sample = torch.flip(sample, dims=(2,))
        output[position] = sample
    return output.sub_(0.5).div_(0.5)


def _normalize(images):
    return images.to(dtype=__import__("torch").float32).div(255.0).sub(0.5).div(0.5)


def _model(operator: str, seed: int):
    import torch
    from torch import nn

    class CenteredChannelwiseTLP(nn.Module):
        def __init__(self, channels: int) -> None:
            super().__init__()
            self.theta = nn.Parameter(torch.zeros(channels, 4))

        def forward(self, value):
            weights = self.theta - self.theta.mean(dim=1, keepdim=True)
            phases = torch.stack(
                (
                    value[:, :, 0::2, 0::2],
                    value[:, :, 0::2, 1::2],
                    value[:, :, 1::2, 0::2],
                    value[:, :, 1::2, 1::2],
                ),
                dim=2,
            )
            return (phases + weights.view(1, -1, 4, 1, 1)).amax(dim=2)

    class TLPControlCNN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.conv1 = nn.Conv2d(3, 32, 3, padding=1)
            self.conv2 = nn.Conv2d(32, 64, 3, padding=1)
            self.conv3 = nn.Conv2d(64, 128, 3, padding=1)
            self.relu = nn.ReLU(inplace=False)
            self.pool1 = nn.MaxPool2d(2)
            self.pool2 = CenteredChannelwiseTLP(64) if operator == "tlp" else nn.MaxPool2d(2)
            self.pool3 = nn.MaxPool2d(2)
            self.fc1 = nn.Linear(128 * 12 * 12, 256)
            self.fc2 = nn.Linear(256, 10)

        def forward(self, value):
            value = self.pool1(self.relu(self.conv1(value)))
            value = self.pool2(self.relu(self.conv2(value)))
            value = self.pool3(self.relu(self.conv3(value)))
            value = value.reshape(value.shape[0], -1)
            return self.fc2(self.relu(self.fc1(value)))

    torch.manual_seed(seed)
    return TLPControlCNN()


def _state_digest(model) -> str:
    import torch

    value = hashlib.sha256()
    for name, tensor in sorted(model.state_dict().items()):
        if name.endswith("pool2.theta"):
            continue
        contiguous = tensor.detach().cpu().contiguous()
        value.update(name.encode("utf-8"))
        value.update(str(contiguous.dtype).encode("ascii"))
        value.update(json.dumps(list(contiguous.shape)).encode("ascii"))
        # Keep NumPy optional.  The legacy torch serializer emits the tensor's
        # exact storage bytes deterministically and is available in the CPU
        # wheel itself.
        stream = io.BytesIO()
        torch.save(contiguous, stream, _use_new_zipfile_serialization=False)
        value.update(stream.getvalue())
    return f"sha256:{value.hexdigest()}"


def _write_event(stream, *, name: str, value: float, split: str, epoch: int, direction: str, dataset_digest: str, sequence: int) -> None:
    payload = {
        "schema": "adaos.research.runner_observation.v1",
        "metric": {"namespace": "tlp", "name": name},
        "value": float(value),
        "value_type": "float",
        "unit": "1",
        "direction": direction,
        "split_role": split,
        "dataset_digest": dataset_digest,
        "step": {"axis": "epoch", "value": int(epoch)},
        "aggregation": "mean",
        "observed_at": _now(),
        "producer": {"component": "research_manager_skill.tlp_runner", "sequence": sequence},
        "evidence_role": "diagnostic" if split == "train" else "required",
    }
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    stream.flush()


def run(args: argparse.Namespace) -> dict[str, Any]:
    import torch
    from torch import nn

    torch.set_num_threads(max(1, int(args.cpu_threads)))
    torch.use_deterministic_algorithms(True)
    random.seed(args.seed)
    root = ensure_stl10(Path(args.data_root), download=bool(args.download))
    images, labels = _load_binary(root, "train")
    train_indices, validation_indices = _stratified_indices(
        labels,
        validation_per_class=int(args.validation_per_class),
        split_seed=int(args.split_seed),
    )
    train_indices = _balanced_limit(train_indices, labels, int(args.max_train_samples), args.seed + 101)
    validation_indices = _balanced_limit(validation_indices, labels, int(args.max_validation_samples), args.seed + 202)
    dataset_manifest = {
        "name": "STL10",
        "version": "binary-2011",
        "train_file": _sha256_file(root / "train_X.bin"),
        "labels_file": _sha256_file(root / "train_y.bin"),
        "split_seed": int(args.split_seed),
        "train_indices": hashlib.sha256(json.dumps(train_indices).encode()).hexdigest(),
        "validation_indices": hashlib.sha256(json.dumps(validation_indices).encode()).hexdigest(),
    }
    dataset_digest = f"sha256:{hashlib.sha256(json.dumps(dataset_manifest, sort_keys=True).encode()).hexdigest()}"
    model = _model(args.operator, args.seed).to("cpu")
    initial_state_digest = _state_digest(model)
    optimizer = torch.optim.Adam(model.parameters(), lr=float(args.learning_rate))
    criterion = nn.CrossEntropyLoss()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    observations_path = output_dir / "observations.ndjson"
    history: list[dict[str, Any]] = []
    best_accuracy = -1.0
    best_epoch = -1
    best_state: dict[str, Any] | None = None
    sequence = 0
    started_at = _now()
    with observations_path.open("w", encoding="utf-8") as observation_stream:
        for epoch in range(1, int(args.epochs) + 1):
            model.train()
            order_generator = torch.Generator().manual_seed(args.seed + 1 + epoch * 1009)
            order = torch.randperm(len(train_indices), generator=order_generator).tolist()
            total_loss = 0.0
            total_correct = 0
            total_count = 0
            for offset in range(0, len(order), int(args.batch_size)):
                positions = order[offset : offset + int(args.batch_size)]
                batch_indices = [train_indices[position] for position in positions]
                batch_images = _augment_batch(
                    images[batch_indices],
                    batch_indices,
                    epoch=epoch,
                    augmentation_seed=args.seed + 2,
                )
                batch_labels = labels[batch_indices]
                optimizer.zero_grad(set_to_none=True)
                logits = model(batch_images)
                loss = criterion(logits, batch_labels)
                loss.backward()
                optimizer.step()
                total_loss += float(loss.item()) * len(batch_indices)
                total_correct += int((logits.argmax(dim=1) == batch_labels).sum().item())
                total_count += len(batch_indices)
            train_loss = total_loss / max(1, total_count)
            train_accuracy = total_correct / max(1, total_count)
            model.eval()
            validation_loss = 0.0
            validation_correct = 0
            validation_count = 0
            with torch.no_grad():
                for offset in range(0, len(validation_indices), int(args.batch_size)):
                    batch_indices = validation_indices[offset : offset + int(args.batch_size)]
                    batch_images = _normalize(images[batch_indices])
                    batch_labels = labels[batch_indices]
                    logits = model(batch_images)
                    loss = criterion(logits, batch_labels)
                    validation_loss += float(loss.item()) * len(batch_indices)
                    validation_correct += int((logits.argmax(dim=1) == batch_labels).sum().item())
                    validation_count += len(batch_indices)
            validation_loss /= max(1, validation_count)
            validation_accuracy = validation_correct / max(1, validation_count)
            epoch_result = {
                "epoch": epoch,
                "train_loss": train_loss,
                "train_accuracy": train_accuracy,
                "validation_loss": validation_loss,
                "validation_accuracy": validation_accuracy,
            }
            history.append(epoch_result)
            for name, value, split, direction in (
                ("cross_entropy", train_loss, "train", "minimize"),
                ("top1_accuracy", train_accuracy, "train", "maximize"),
                ("cross_entropy", validation_loss, "validation", "minimize"),
                ("top1_accuracy", validation_accuracy, "validation", "maximize"),
            ):
                sequence += 1
                _write_event(
                    observation_stream,
                    name=name,
                    value=value,
                    split=split,
                    epoch=epoch,
                    direction=direction,
                    dataset_digest=dataset_digest,
                    sequence=sequence,
                )
            if validation_accuracy > best_accuracy:
                best_accuracy = validation_accuracy
                best_epoch = epoch
                best_state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
    if best_state is None:
        raise RuntimeError("training did not produce a validation checkpoint")
    model.load_state_dict(best_state)
    checkpoint_path = output_dir / "checkpoint.pt"
    torch.save(
        {
            "schema": "adaos.research.tlp_checkpoint.v1",
            "operator": args.operator,
            "seed": args.seed,
            "best_epoch": best_epoch,
            "dataset_digest": dataset_digest,
            "initial_state_digest": initial_state_digest,
            "model_state": best_state,
        },
        checkpoint_path,
    )
    predictions_path = output_dir / "predictions.jsonl"
    model.eval()
    with predictions_path.open("w", encoding="utf-8") as stream, torch.no_grad():
        for offset in range(0, len(validation_indices), int(args.batch_size)):
            batch_indices = validation_indices[offset : offset + int(args.batch_size)]
            logits = model(_normalize(images[batch_indices]))
            probabilities = torch.softmax(logits, dim=1)
            for row, original_index in enumerate(batch_indices):
                stream.write(
                    json.dumps(
                        {
                            "sample_index": original_index,
                            "target": int(labels[original_index]),
                            "prediction": int(probabilities[row].argmax()),
                            "probabilities": [round(float(item), 8) for item in probabilities[row]],
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
    tlp_components = None
    if args.operator == "tlp":
        theta = model.pool2.theta.detach().cpu()
        weights = theta - theta.mean(dim=1, keepdim=True)
        tlp_components = {
            "h_mean_abs": float(((weights[:, 0] + weights[:, 2] - weights[:, 1] - weights[:, 3]) / 2).abs().mean()),
            "v_mean_abs": float(((weights[:, 0] + weights[:, 1] - weights[:, 2] - weights[:, 3]) / 2).abs().mean()),
            "checkerboard_mean_abs": float(((weights[:, 0] + weights[:, 3] - weights[:, 1] - weights[:, 2]) / 2).abs().mean()),
            "centered_sum_max_abs": float(weights.sum(dim=1).abs().max()),
        }
    result = {
        "schema": "adaos.research.tlp_result.v1",
        "evidence_class": str(args.evidence_class),
        "operator": args.operator,
        "operator_location": "pool2",
        "seed": int(args.seed),
        "epochs": int(args.epochs),
        "batch_size": int(args.batch_size),
        "learning_rate": float(args.learning_rate),
        "train_samples": len(train_indices),
        "validation_samples": len(validation_indices),
        "dataset_manifest": dataset_manifest,
        "dataset_digest": dataset_digest,
        "initial_state_digest": initial_state_digest,
        "best_epoch": best_epoch,
        "best_validation_accuracy": best_accuracy,
        "history": history,
        "tlp_components": tlp_components,
        "started_at": started_at,
        "finished_at": _now(),
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    artifacts = []
    for path, role, media_type in (
        (result_path, "result", "application/json"),
        (observations_path, "observations", "application/x-ndjson"),
        (checkpoint_path, "checkpoint", "application/vnd.pytorch.checkpoint"),
        (predictions_path, "predictions", "application/x-ndjson"),
    ):
        artifacts.append(
            {
                "path": path.name,
                "role": role,
                "digest": _sha256_file(path),
                "size_bytes": path.stat().st_size,
                "media_type": media_type,
            }
        )
    manifest_path = output_dir / "artifacts.json"
    manifest_path.write_text(json.dumps({"artifacts": artifacts}, sort_keys=True, indent=2), encoding="utf-8")
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--operator", choices=("maxpool", "tlp"), required=True)
    value.add_argument("--seed", type=int, required=True)
    value.add_argument("--epochs", type=int, required=True)
    value.add_argument("--batch-size", type=int, default=32)
    value.add_argument("--learning-rate", type=float, default=0.001)
    value.add_argument("--max-train-samples", type=int, default=0)
    value.add_argument("--max-validation-samples", type=int, default=0)
    value.add_argument("--validation-per-class", type=int, default=100)
    value.add_argument("--split-seed", type=int, default=20260807)
    value.add_argument("--cpu-threads", type=int, default=2)
    value.add_argument("--data-root", required=True)
    value.add_argument("--output-dir", required=True)
    value.add_argument("--evidence-class", default="workflow_validation")
    value.add_argument("--download", action="store_true")
    return value


def main() -> None:
    args = parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
