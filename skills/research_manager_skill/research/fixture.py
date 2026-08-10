"""Provider-neutral deterministic no-training research fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    operator_offset = int(hashlib.sha256(args.operator.encode("utf-8")).hexdigest()[:4], 16) % 5
    base = 0.51 + operator_offset / 100.0
    accuracy = round(base + rng.random() * 0.01, 8)
    payload = {
        "schema": "adaos.research.fixture_observation.v1",
        "operator": args.operator,
        "seed": args.seed,
        "metrics": {"accuracy": accuracy, "error_rate": round(1.0 - accuracy, 8)},
    }
    payload["digest"] = f"sha256:{hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}"
    Path(args.output).write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
