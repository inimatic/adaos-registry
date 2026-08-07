"""Deterministic no-training execution fixture used by TLP conformance."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator", choices=("baseline", "max_plus"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    base = 0.51 if args.operator == "baseline" else 0.57
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
