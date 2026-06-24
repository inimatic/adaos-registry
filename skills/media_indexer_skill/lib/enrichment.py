"""Optional metadata enrichment for Media Indexer.

External services and heavy OCR libraries are opt-in. The default path stays
lightweight and safe for the in-process event handler.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _feature_enabled(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


class EnrichmentService:
    def __init__(self) -> None:
        logger.info("Initializing EnrichmentService")

        self.ia = None
        if _feature_enabled("MEDIA_INDEXER_ENABLE_IMDB"):
            try:
                from imdb import Cinemagoer

                self.ia = Cinemagoer()
            except ImportError:
                logger.warning("Cinemagoer is not installed; IMDb enrichment disabled.")

        self.reader = None
        if _feature_enabled("MEDIA_INDEXER_ENABLE_OCR"):
            try:
                import easyocr

                self.reader = easyocr.Reader(["ru", "en"])
            except ImportError:
                logger.warning("EasyOCR is not installed; OCR enrichment disabled.")

        self.shazam = None
        if _feature_enabled("MEDIA_INDEXER_ENABLE_AUDIO_ID_INLINE"):
            try:
                from shazamio import Shazam

                self.shazam = Shazam()
                logger.info("Shazamio initialized for inline audio recognition.")
            except ImportError:
                logger.warning("Shazamio is not installed; inline audio recognition disabled.")

        self.local_cache = {
            "inter stellar": "Space, black holes, gravity and saving humanity.",
            "the dark knight": "Batman faces the Joker in Gotham.",
            "the matrix": "Neo discovers that the world is a simulation.",
            "terminator": "A robot assassin travels from the future.",
            "inception": "A team enters dreams to steal secrets.",
            "interstellar": "Space, black holes, gravity and saving humanity.",
            "incredibles": "A superhero family hides its powers.",
        }

    def enrich(self, file_path: str, media_type: str) -> Dict[str, Any]:
        if media_type == "video":
            return {}
        if media_type == "audio":
            return self._run_async(self.enrich_audio(file_path))
        if media_type == "image":
            return self.enrich_image(file_path)
        return {}

    def enrich_video(self, title: str) -> Dict[str, Any]:
        if self.ia:
            try:
                movies = self.ia.search_movie(title)
                if movies:
                    best_match = movies[0]
                    self.ia.update(best_match, info=["plot"])
                    plot = best_match.get("plot", [""])[0]
                    return {"imdb": {"plot": plot}}
            except Exception:
                logger.warning("IMDb lookup failed; using local fallback cache.", exc_info=True)

        clean_title_lower = title.lower().strip()
        for key, plot in self.local_cache.items():
            if key in clean_title_lower or clean_title_lower in key:
                return {"imdb": {"plot": plot}}
        return {}

    def enrich_image(self, file_path: str) -> Dict[str, Any]:
        if not self.reader:
            return {}
        try:
            import cv2
            import numpy as np

            img_array = np.fromfile(file_path, dtype=np.uint8)
            img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            if img is None:
                logger.warning("Failed to decode image: %s", file_path)
                return {}
            results = self.reader.readtext(img, detail=0)
            text = " ".join(results)
            return {"ocr_text": text} if text else {}
        except Exception as exc:
            logger.warning("EasyOCR failed for %s: %s", file_path, exc)
            return {}

    async def enrich_audio(self, file_path: str) -> Dict[str, Any]:
        if not self.shazam:
            return {}
        try:
            out = await self.shazam.recognize(file_path)
            track = out.get("track") if isinstance(out, dict) else None
            if not isinstance(track, dict):
                return {}
            return {
                "shazam_title": track.get("title"),
                "shazam_subtitle": track.get("subtitle"),
                "shazam_genre": (track.get("genres") or {}).get("primary")
                if isinstance(track.get("genres"), dict)
                else None,
            }
        except Exception as exc:
            logger.warning("Shazam failed for %s: %s", file_path, exc)
            return {}

    def _run_async(self, coro: Any) -> Dict[str, Any]:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
