"""NER model wrapper for media filename parsing.

Runtime weights are expected in the skill-owned data store under
``data/files/models``. A legacy package-local path and Google Drive fallback are
kept only for development/bootstrap until Root-hosted model delivery is fully
rolled out.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

LABELS = ["O", "B-TITLE", "I-TITLE", "B-YEAR", "I-YEAR", "B-QUALITY", "I-QUALITY", "B-ARTIST", "I-ARTIST"]
ID2LABEL = {idx: label for idx, label in enumerate(LABELS)}

BASE_DIR = Path(__file__).resolve().parents[1]
LEGACY_MODEL_WEIGHTS_PATH = BASE_DIR / "ml" / "weights" / "model2.pt"
MODEL_ARTIFACT_NAME = "model2.pt"
GDRIVE_MODEL_ID = "19YBXzTYLoizbm8RF8gigUQ0fApZVmpoZ"
GDRIVE_MODEL_URL = f"https://drive.google.com/uc?id={GDRIVE_MODEL_ID}"
BASE_MODEL_NAME = "distilbert-base-multilingual-cased"


def _runtime_models_dir() -> Path:
    override = os.getenv("MEDIA_INDEXER_MODEL_DIR")
    if override:
        return Path(override)
    env_path = os.getenv("ADAOS_SKILL_ENV_PATH")
    if env_path:
        path = Path(env_path)
        data_root = path.parents[1] if path.parent.name == "db" else path.parent
        return data_root / "files" / "models"
    base_dir = Path(os.getenv("ADAOS_BASE_DIR") or Path.home() / ".adaos")
    return base_dir / "state" / "media_indexer_skill" / "models"


def model_weights_path() -> Path:
    runtime_path = _runtime_models_dir() / MODEL_ARTIFACT_NAME
    if runtime_path.exists():
        return runtime_path
    if LEGACY_MODEL_WEIGHTS_PATH.exists():
        return LEGACY_MODEL_WEIGHTS_PATH
    return runtime_path


def _ensure_weights_downloaded(weights_path: Path) -> None:
    if weights_path.exists():
        return

    import gdown

    weights_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("NER weights are missing; downloading temporary Google Drive model to %s", weights_path)
    result = gdown.download(GDRIVE_MODEL_URL, str(weights_path), quiet=False)
    if not result or not weights_path.exists():
        raise FileNotFoundError(f"failed to download NER weights to {weights_path}")


class NERPredictor:
    def __init__(self) -> None:
        import torch
        from transformers import DistilBertForTokenClassification, DistilBertTokenizerFast

        self.torch = torch
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info("Initializing NERPredictor on %s", self.device)

        try:
            self.tokenizer = DistilBertTokenizerFast.from_pretrained(BASE_MODEL_NAME)
            self.model = DistilBertForTokenClassification.from_pretrained(BASE_MODEL_NAME, num_labels=len(LABELS))
            self._load_weights()
        except Exception as exc:
            logger.error("NER model initialization failed: %s", exc)
            self.model = None

    def _load_weights(self) -> None:
        weights_path = model_weights_path()
        _ensure_weights_downloaded(weights_path)
        state_dict = self.torch.load(str(weights_path), map_location=self.device, weights_only=True)
        self.model.load_state_dict(state_dict)
        self.model.to(self.device)
        self.model.eval()
        logger.info("NER weights loaded from %s", weights_path)

    def extract_entities(self, text: str) -> Dict[str, str]:
        if not self.model or not text.strip():
            return {}

        clean_text = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", text)
        clean_text = re.sub(r"[\(\)\[\]\{\}]", " ", clean_text)
        clean_text = clean_text.replace(".", " ").replace("_", " ").replace("-", " ")
        clean_text = re.sub(r"\s+", " ", clean_text).strip()
        if not clean_text:
            return {}

        words = clean_text.split()
        encoding = self.tokenizer(words, is_split_into_words=True, return_tensors="pt", truncation=True, max_length=128)
        word_ids = encoding.word_ids()
        inputs = {key: value.to(self.device) for key, value in encoding.items()}

        with self.torch.no_grad():
            outputs = self.model(**inputs)
            predictions = self.torch.argmax(outputs.logits, dim=2).squeeze().tolist()

        if isinstance(predictions, int):
            predictions = [predictions]

        word_to_label: dict[int, str] = {}
        for token_idx, word_idx in enumerate(word_ids):
            if word_idx is None:
                continue
            if word_idx not in word_to_label and token_idx < len(predictions):
                word_to_label[word_idx] = ID2LABEL[predictions[token_idx]]

        extracted_data = {"title": [], "year": [], "quality": [], "artist": []}
        for word_idx, word in enumerate(words):
            label = word_to_label.get(word_idx, "O")
            if label == "O":
                continue
            if "TITLE" in label:
                extracted_data["title"].append(word)
            elif "YEAR" in label:
                extracted_data["year"].append(word)
            elif "QUALITY" in label:
                extracted_data["quality"].append(word)
            elif "ARTIST" in label:
                extracted_data["artist"].append(word)

        return {
            "title": self._clean_assembled_text(extracted_data["title"]),
            "year": self._clean_assembled_text(extracted_data["year"]),
            "quality": self._clean_assembled_text(extracted_data["quality"]),
            "artist": self._clean_assembled_text(extracted_data["artist"]),
        }

    def _clean_assembled_text(self, words: List[str]) -> str:
        if not words:
            return ""

        assembled = " ".join(words)
        assembled = re.sub(r"\bBlu\s*-?\s*Ray\b", "BluRay", assembled, flags=re.IGNORECASE)
        assembled = re.sub(r"\bWEB\s+DL\b", "WEB-DL", assembled, flags=re.IGNORECASE)
        assembled = re.sub(r"\bWEB\s+Rip\b", "WEB-Rip", assembled, flags=re.IGNORECASE)
        assembled = re.sub(r"\bHD\s+(Rip|TV)\b", r"HD\1", assembled, flags=re.IGNORECASE)
        return assembled.strip()


def model_weights_status() -> dict[str, object]:
    path = model_weights_path()
    return {
        "path": str(path),
        "exists": path.exists(),
        "source": "skill_data_models" if path.exists() and path.parent.name == "models" else "google_drive_temporary",
        "google_drive_id": GDRIVE_MODEL_ID,
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }
