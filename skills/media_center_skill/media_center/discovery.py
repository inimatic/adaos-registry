from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable
from typing import Any


EMBEDDING_DIMENSIONS = 48
_WORD = re.compile(r"[a-z0-9]+")
_CYRILLIC = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)
_PHONETIC_GROUPS = {
    **{letter: "1" for letter in "bfpv"},
    **{letter: "2" for letter in "cgjkqsxz"},
    **{letter: "3" for letter in "dt"},
    "l": "4",
    **{letter: "5" for letter in "mn"},
    "r": "6",
}


def fold_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    without_marks = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return " ".join(_WORD.findall(without_marks.translate(_CYRILLIC)))


def phonetic_code(value: Any) -> str:
    token = fold_text(value).replace(" ", "")
    if not token:
        return ""
    first = token[0]
    previous = _PHONETIC_GROUPS.get(first, "")
    encoded: list[str] = []
    for character in token[1:]:
        current = _PHONETIC_GROUPS.get(character, "")
        if current and current != previous:
            encoded.append(current)
        previous = current
    return (first + "".join(encoded) + "0000000")[:8]


def phonetic_terms(value: Any) -> set[str]:
    return {
        code
        for token in fold_text(value).split()
        if len(token) >= 3 and (code := phonetic_code(token))
    }


def text_embedding(value: Any, *, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    bounded_dimensions = max(16, min(256, int(dimensions)))
    folded = fold_text(value)[:4096]
    vector = [0.0] * bounded_dimensions
    features: list[str] = folded.split()
    compact = folded.replace(" ", "_")
    features.extend(compact[index : index + 3] for index in range(max(0, len(compact) - 2)))
    for feature in features[:8192]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % bounded_dimensions
        vector[bucket] += 1.0 if digest[4] & 1 else -1.0
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude:
        vector = [round(value / magnitude, 6) for value in vector]
    return vector


def cosine_similarity(left: Iterable[Any], right: Iterable[Any]) -> float:
    try:
        left_values = [float(value) for value in left]
        right_values = [float(value) for value in right]
    except (TypeError, ValueError):
        return 0.0
    if not left_values or len(left_values) != len(right_values):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left_values, right_values, strict=True)) / (
        left_norm * right_norm
    )


def discovery_score(
    query: Any,
    candidate: Any,
    *,
    candidate_embedding: Iterable[Any] = (),
) -> tuple[float, list[str]]:
    query_text = fold_text(query)
    candidate_text = fold_text(candidate)
    if not query_text or not candidate_text:
        return 0.0, []
    query_tokens = set(query_text.split())
    candidate_tokens = set(candidate_text.split())
    token_overlap = len(query_tokens & candidate_tokens) / max(1, len(query_tokens))
    query_phonetic = phonetic_terms(query_text)
    candidate_phonetic = phonetic_terms(candidate_text)
    phonetic_overlap = len(query_phonetic & candidate_phonetic) / max(
        1, len(query_phonetic)
    )
    query_trigrams = {
        query_text[index : index + 3]
        for index in range(max(1, len(query_text) - 2))
    }
    candidate_trigrams = {
        candidate_text[index : index + 3]
        for index in range(max(1, len(candidate_text) - 2))
    }
    trigram_overlap = len(query_trigrams & candidate_trigrams) / max(
        1, len(query_trigrams | candidate_trigrams)
    )
    semantic = max(
        0.0,
        cosine_similarity(text_embedding(query_text), candidate_embedding),
    )
    substring = 1.0 if query_text in candidate_text else 0.0
    score = min(
        1.0,
        substring * 0.35
        + token_overlap * 0.3
        + phonetic_overlap * 0.2
        + trigram_overlap * 0.1
        + semantic * 0.05,
    )
    reasons: list[str] = []
    if substring:
        reasons.append("normalized_substring")
    if token_overlap:
        reasons.append("token_overlap")
    if phonetic_overlap:
        reasons.append("phonetic_overlap")
    if trigram_overlap >= 0.2:
        reasons.append("trigram_similarity")
    if semantic >= 0.55:
        reasons.append("local_text_embedding")
    return score, reasons


__all__ = [
    "EMBEDDING_DIMENSIONS",
    "cosine_similarity",
    "discovery_score",
    "fold_text",
    "phonetic_code",
    "phonetic_terms",
    "text_embedding",
]
