"""Vector search storage for media_indexer_skill.

The module is intentionally import-light. FAISS, Pillow, and sentence-transformer
models are loaded only when VectorDatabase is instantiated, so smoke imports do
not download or allocate ML resources.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class VectorDatabase:
    """Multimodal FAISS index with text and image channels."""

    TEXT_MODEL_NAME = "distiluse-base-multilingual-cased-v2"
    CLIP_TEXT_MODEL_NAME = "clip-ViT-B-32-multilingual-v1"
    CLIP_VISION_MODEL_NAME = "clip-ViT-B-32"

    TEXT_MIN_SIMILARITY = 0.10
    IMAGE_MIN_SIMILARITY = 0.22
    INDEX_DIMENSIONS = 512
    SCHEMA_VERSION = 1
    TEXT_EMBEDDINGS_ENV = "MEDIA_INDEXER_ENABLE_TEXT_EMBEDDINGS"
    ML_ENV = "MEDIA_INDEXER_ENABLE_ML"
    IMAGE_EMBEDDINGS_ENV = "MEDIA_INDEXER_ENABLE_IMAGE_EMBEDDINGS"

    def __init__(self) -> None:
        self.faiss = None
        self.text_model = None
        self.text_embeddings_enabled = self._feature_enabled(self.TEXT_EMBEDDINGS_ENV)
        if self.text_embeddings_enabled:
            import faiss

            self.faiss = faiss
            logger.info("Loading text embedding model: %s", self.TEXT_MODEL_NAME)
            self.text_model = self._load_sentence_transformer(self.TEXT_MODEL_NAME)
        else:
            logger.info(
                "Text embeddings disabled; using lexical index. Set %s=1 or %s=1 to enable semantic indexing.",
                self.TEXT_EMBEDDINGS_ENV,
                self.ML_ENV,
            )

        self.clip_text = None
        self.clip_vision = None
        self._image_embeddings_disabled_logged = False

        self.text_index = self.faiss.IndexFlatIP(self.INDEX_DIMENSIONS) if self.faiss is not None else None
        self.image_index = self.faiss.IndexFlatIP(self.INDEX_DIMENSIONS) if self.faiss is not None else None
        self.text_docs: List[Dict[str, Any]] = []
        self.image_docs: List[Dict[str, Any]] = []

    @staticmethod
    def _load_sentence_transformer(model_name: str) -> Any:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(model_name)

    def _feature_enabled(self, name: str) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return False
        value = str(raw).strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _image_embeddings_enabled(self) -> bool:
        return self.faiss is not None and self._feature_enabled(self.IMAGE_EMBEDDINGS_ENV)

    def _ensure_clip_vision(self) -> bool:
        if not self._image_embeddings_enabled():
            if not self._image_embeddings_disabled_logged:
                logger.info(
                    "Image CLIP embeddings disabled; set %s=1 to enable visual indexing.",
                    self.IMAGE_EMBEDDINGS_ENV,
                )
                self._image_embeddings_disabled_logged = True
            return False
        if self.clip_vision is None:
            logger.info("Loading CLIP vision model: %s", self.CLIP_VISION_MODEL_NAME)
            self.clip_vision = self._load_sentence_transformer(self.CLIP_VISION_MODEL_NAME)
        return True

    def _ensure_clip_text(self) -> bool:
        if not self._image_embeddings_enabled():
            return False
        if self.clip_text is None:
            logger.info("Loading CLIP text model: %s", self.CLIP_TEXT_MODEL_NAME)
            self.clip_text = self._load_sentence_transformer(self.CLIP_TEXT_MODEL_NAME)
        return True

    @staticmethod
    def _tokens(text: str) -> List[str]:
        return re.findall(r"[0-9A-Za-zА-Яа-яЁё']+", text.lower(), flags=re.UNICODE)

    @classmethod
    def _expanded_query_tokens(cls, query: str) -> List[str]:
        tokens = cls._tokens(query)
        aliases = {
            "movie": ["film", "video", "кино", "фильм"],
            "film": ["movie", "video", "кино", "фильм"],
            "video": ["movie", "film", "кино", "видео"],
            "кино": ["movie", "film", "video", "фильм"],
            "фильм": ["movie", "film", "video", "кино"],
            "music": ["audio", "song", "track", "музыка", "песня", "трек"],
            "audio": ["music", "song", "track", "музыка", "песня", "трек"],
            "song": ["music", "audio", "track", "песня", "трек"],
            "track": ["music", "audio", "song", "трек", "песня"],
            "музыка": ["music", "audio", "song", "track"],
            "песня": ["music", "audio", "song", "track"],
            "трек": ["music", "audio", "song", "track"],
            "photo": ["image", "picture", "фото", "картинка", "изображение"],
            "image": ["photo", "picture", "фото", "картинка", "изображение"],
            "picture": ["photo", "image", "фото", "картинка", "изображение"],
            "фото": ["photo", "image", "picture"],
            "картинка": ["photo", "image", "picture"],
            "изображение": ["photo", "image", "picture"],
        }
        expanded = list(tokens)
        for token in tokens:
            expanded.extend(aliases.get(token, []))
        return list(dict.fromkeys(expanded))

    @staticmethod
    def _payload_text(payload: Dict[str, Any]) -> str:
        fields = []
        for key in ("display_title", "title", "ner_title", "artist", "year", "quality", "real_file_name", "ftype", "type"):
            value = payload.get(key)
            if value not in (None, "", "---"):
                fields.append(str(value))
        enriched = payload.get("enriched") if isinstance(payload.get("enriched"), dict) else {}
        for key in ("shazam_title", "shazam_subtitle", "shazam_genre", "ocr_text"):
            value = enriched.get(key)
            if value:
                fields.append(str(value))
        return " ".join(fields)

    def _lexical_boost(self, query_norm: str, query_tokens: List[str], doc: Dict[str, Any]) -> float:
        payload = doc.get("payload") if isinstance(doc.get("payload"), dict) else {}
        payload_text = self._payload_text(payload).lower()
        text_norm = str(doc.get("text") or "").lower()
        boost = 0.0
        if query_norm and query_norm in payload_text:
            boost += 28.0
        elif query_norm and query_norm in text_norm:
            boost += 18.0
        field_weights = {
            "display_title": 16.0,
            "title": 16.0,
            "ner_title": 16.0,
            "artist": 18.0,
            "year": 12.0,
            "quality": 8.0,
            "real_file_name": 10.0,
            "ftype": 6.0,
            "type": 6.0,
        }
        for key, weight in field_weights.items():
            field_tokens = set(self._tokens(str(payload.get(key) or "")))
            if field_tokens:
                boost += len(set(query_tokens) & field_tokens) * weight
        return boost

    def _search_lexical(self, query: str, k: int) -> List[Dict[str, Any]]:
        query_norm = query.strip().lower()
        query_tokens = self._expanded_query_tokens(query_norm)
        if not query_norm or not query_tokens:
            return []

        tokenized_docs: List[tuple[Dict[str, Any], List[str]]] = []
        doc_freq: Counter[str] = Counter()
        for doc in self.text_docs:
            text = " ".join([str(doc.get("text") or ""), self._payload_text(doc.get("payload") or {})])
            tokens = self._tokens(text)
            if not tokens:
                continue
            tokenized_docs.append((doc, tokens))
            doc_freq.update(set(tokens))

        total_docs = len(tokenized_docs)
        if not total_docs:
            return []
        avg_len = sum(len(tokens) for _doc, tokens in tokenized_docs) / max(total_docs, 1)
        k1 = 1.45
        b = 0.72
        results: List[Dict[str, Any]] = []
        query_token_set = set(query_tokens)
        for doc, doc_tokens in tokenized_docs:
            text_norm = str(doc.get("text") or "").lower()
            frequencies = Counter(doc_tokens)
            bm25 = 0.0
            matched = 0
            for token in query_token_set:
                freq = frequencies.get(token, 0)
                if freq <= 0:
                    continue
                matched += 1
                idf = math.log(1.0 + (total_docs - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5))
                denom = freq + k1 * (1.0 - b + b * (len(doc_tokens) / max(avg_len, 1.0)))
                bm25 += idf * ((freq * (k1 + 1.0)) / denom)
            substring = query_norm in text_norm or query_norm in self._payload_text(doc.get("payload") or {}).lower()
            boost = self._lexical_boost(query_norm, query_tokens, doc)
            if matched <= 0 and not substring and boost <= 0:
                continue
            coverage = matched / max(len(query_token_set), 1)
            score = min(100.0, bm25 * 22.0 + coverage * 36.0 + (18.0 if substring else 0.0) + boost)
            result = doc.copy()
            result["score"] = round(score, 1)
            result["type"] = "media/text"
            results.append(result)

        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:k]

    def reset(self) -> None:
        self.text_index = self.faiss.IndexFlatIP(self.INDEX_DIMENSIONS) if self.faiss is not None else None
        self.image_index = self.faiss.IndexFlatIP(self.INDEX_DIMENSIONS) if self.faiss is not None else None
        self.text_docs = []
        self.image_docs = []

    @property
    def counts(self) -> Dict[str, int]:
        return {
            "text_count": len(self.text_docs),
            "image_count": len(self.image_docs),
            "total_count": len(self.text_docs) + len(self.image_docs),
        }

    def add_text(self, text: str, payload: Dict[str, Any]) -> None:
        if not text.strip():
            return
        if not self.text_embeddings_enabled:
            self.text_docs.append({"text": text, "payload": payload})
            return

        import numpy as np

        emb = self.text_model.encode(text, normalize_embeddings=True).astype("float32")
        self.text_index.add(np.array([emb]))
        self.text_docs.append({"text": text, "payload": payload})

    def add_image(self, image_path: str, payload: Dict[str, Any]) -> None:
        if not self._ensure_clip_vision():
            return
        try:
            import numpy as np
            from PIL import Image

            with Image.open(image_path) as img:
                emb = self.clip_vision.encode(img, normalize_embeddings=True).astype("float32")
            self.image_index.add(np.array([emb]))
            self.image_docs.append(
                {
                    "text": f"[VISUAL] {Path(image_path).name}",
                    "payload": payload,
                }
            )
        except Exception as exc:
            logger.warning("CLIP failed to read %s: %s", image_path, exc)

    def search(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []

        if not self.text_embeddings_enabled:
            results.extend(self._search_lexical(query, k))
            return results[:k]

        import numpy as np

        if self.text_docs:
            q_text_emb = self.text_model.encode(query, normalize_embeddings=True).astype("float32")
            distances, indexes = self.text_index.search(np.array([q_text_emb]), k)
            for idx, similarity in zip(indexes[0], distances[0]):
                if idx == -1 or idx >= len(self.text_docs):
                    continue
                if similarity >= self.TEXT_MIN_SIMILARITY:
                    result = self.text_docs[idx].copy()
                    result["score"] = round(float(similarity) * 100, 1)
                    result["type"] = "media/text"
                    results.append(result)

        if self.image_docs and self._ensure_clip_text():
            q_img_emb = self.clip_text.encode(query, normalize_embeddings=True).astype("float32")
            distances, indexes = self.image_index.search(np.array([q_img_emb]), k)
            for idx, similarity in zip(indexes[0], distances[0]):
                if idx == -1 or idx >= len(self.image_docs):
                    continue
                raw_similarity = float(similarity)
                if raw_similarity >= self.IMAGE_MIN_SIMILARITY:
                    result = self.image_docs[idx].copy()
                    result["score"] = round(raw_similarity * 100, 1)
                    result["type"] = "image"
                    results.append(result)

        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:k]

    def save(self, directory: str | Path) -> Dict[str, Any]:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        if self.faiss is not None:
            self.faiss.write_index(self.text_index, str(target / "text.index"))
            self.faiss.write_index(self.image_index, str(target / "image.index"))
        else:
            for filename in ("text.index", "image.index"):
                try:
                    (target / filename).unlink(missing_ok=True)
                except Exception:
                    logger.debug("failed to remove stale FAISS index %s", filename, exc_info=True)
        metadata = {
            "schema": self.SCHEMA_VERSION,
            "backend": "faiss" if self.faiss is not None else "lexical",
            "models": {
                "text": self.TEXT_MODEL_NAME,
                "clip_text": self.CLIP_TEXT_MODEL_NAME,
                "clip_vision": self.CLIP_VISION_MODEL_NAME,
                "image_embeddings_enabled": self._image_embeddings_enabled(),
            },
            "text_docs": self.text_docs,
            "image_docs": self.image_docs,
            **self.counts,
        }
        (target / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata

    def load(self, directory: str | Path) -> Dict[str, Any]:
        source = Path(directory)
        metadata_path = source / "metadata.json"
        if not metadata_path.exists():
            return {"loaded": False, "reason": "missing_index_files"}

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if int(metadata.get("schema") or 0) != self.SCHEMA_VERSION:
            return {"loaded": False, "reason": "schema_mismatch"}

        backend = str(metadata.get("backend") or "faiss")
        text_index_path = source / "text.index"
        image_index_path = source / "image.index"
        if backend == "faiss" and self.faiss is not None:
            if not text_index_path.exists() or not image_index_path.exists():
                return {"loaded": False, "reason": "missing_index_files"}
            self.text_index = self.faiss.read_index(str(text_index_path))
            self.image_index = self.faiss.read_index(str(image_index_path))
        elif backend == "faiss":
            return {"loaded": False, "reason": "faiss_disabled"}
        self.text_docs = list(metadata.get("text_docs") or [])
        self.image_docs = list(metadata.get("image_docs") or [])
        return {"loaded": True, **self.counts}
