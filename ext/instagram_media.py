"""Shared Instagram candidate metadata and ranking primitives."""

import math
from dataclasses import dataclass, field
from typing import Any

from parsehub.provider_api.instagram import InstagramMedia

POLICY = "instagram-resolution-fps-v1"
FORMAT_SORT = "res,fps"


def number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return result if math.isfinite(result) and result > 0 else 0


def rank(item: dict[str, Any]) -> tuple[float, float, float, float]:
    w, h = number(item.get("width")), number(item.get("height"))
    return min(w, h), w * h, number(item.get("fps")), number(item.get("bitrate")) or number(item.get("tbr")) * 1000


@dataclass(slots=True, kw_only=True)
class QualityMedia(InstagramMedia):
    media_id: str = ""
    images: list[dict[str, Any]] = field(default_factory=list)
    videos: list[dict[str, Any]] = field(default_factory=list)
    manifest: str = ""
    has_audio: bool = True
    codec: str | None = None
