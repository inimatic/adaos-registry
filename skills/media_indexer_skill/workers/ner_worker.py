from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from lib.ner_predictor import NERPredictor, model_weights_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("media_indexer.ner_worker")


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    items = request.get("items") if isinstance(request.get("items"), list) else []
    predictor = NERPredictor()
    results: Dict[str, Dict[str, str]] = {}
    errors: Dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("path") or item.get("name") or "").strip()
        name = str(item.get("name") or "").strip()
        if not key or not name:
            continue
        try:
            results[key] = predictor.extract_entities(name)
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            logger.warning("NER failed for %s: %s", name, exc)
            errors[key] = f"{type(exc).__name__}: {exc}"
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "model_weights": model_weights_status(),
                "results": results,
                "errors": errors,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
