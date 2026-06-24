"""Technical metadata extraction for media files."""

from __future__ import annotations

import logging
import json
import os
import subprocess
from typing import Any, Optional

from lib.models import MediaMetadata

logger = logging.getLogger(__name__)


class TechnicalMetadataExtractor:
    def __init__(self) -> None:
        try:
            import static_ffmpeg

            static_ffmpeg.add_paths()
        except Exception:
            logger.warning("static_ffmpeg is not available; system FFmpeg must be available")

    @staticmethod
    def _metadata_timeout_sec() -> float:
        try:
            return max(1.0, float(os.getenv("MEDIA_INDEXER_METADATA_TIMEOUT_SEC") or 8.0))
        except Exception:
            return 8.0

    def extract(self, media: Any, media_type: Optional[str] = None) -> MediaMetadata:
        if hasattr(media, "media_type"):
            path = media.full_path
            m_type = media.media_type
        else:
            path = str(media)
            m_type = media_type or "unknown"

        metadata = MediaMetadata(file_type=m_type, file_path=path)
        if m_type == "image":
            return self._extract_image_data(path, metadata)
        return self._extract_av_data(path, m_type, metadata)

    def _extract_image_data(self, file_path: str, metadata: MediaMetadata) -> MediaMetadata:
        try:
            from PIL import Image

            with Image.open(file_path) as img:
                metadata.width, metadata.height = img.size
                metadata.image_format = img.format
                metadata.status = "success"
        except Exception as exc:
            logger.debug("image metadata extraction failed for %s: %s", file_path, exc)
            metadata.status = "error"
            metadata.error_msg = str(exc)
        return metadata

    def _extract_av_data(self, file_path: str, media_type: str, metadata: MediaMetadata) -> MediaMetadata:
        try:
            if os.path.getsize(file_path) == 0:
                raise ValueError("empty file")

            proc = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    file_path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._metadata_timeout_sec(),
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "ffprobe failed").strip()[:500])
            probe = json.loads(proc.stdout or "{}")
            fmt = probe.get("format", {})
            metadata.duration_seconds = float(fmt.get("duration", 0))
            metadata.size_bytes = int(fmt.get("size", 0))
            metadata.bit_rate = int(fmt.get("bit_rate", 0))
            metadata.status = "success"

            if media_type == "video":
                video_stream = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"), None)
                if video_stream:
                    metadata.video_codec = video_stream.get("codec_name")
                    metadata.width = int(video_stream.get("width", 0))
                    metadata.height = int(video_stream.get("height", 0))
            elif media_type == "audio":
                audio_stream = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"), None)
                if audio_stream:
                    metadata.audio_codec = audio_stream.get("codec_name")
                    metadata.sample_rate = int(audio_stream.get("sample_rate", 0))
        except Exception as exc:
            logger.debug("FFmpeg metadata extraction failed for %s: %s", file_path, exc)
            metadata.status = "error"
            metadata.error_msg = str(exc)
        return metadata
