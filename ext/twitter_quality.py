"""Select highest-bitrate MP4 variants without replacing X requests or result parsing."""

import copy
import math
from functools import wraps
from typing import Any

from parsehub.errors import ParseError


def _bitrate(variant: dict[str, Any]) -> float:
    rates = []
    for key in ("bitrate", "bit_rate"):
        try:
            rate = float(variant.get(key, 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(rate) and rate >= 0:
            rates.append(rate)
    return max(rates, default=0)


def best_mp4(variants: Any) -> dict[str, Any] | None:
    if not isinstance(variants, list):
        return None
    candidates = [
        item for item in variants
        if isinstance(item, dict)
        and item.get("content_type") == "video/mp4"
        and isinstance(item.get("url"), str)
        and item["url"].startswith(("https://", "http://"))
    ]
    return max(candidates, key=_bitrate) if candidates else None


def best_mp4_url(variants: Any) -> str:
    candidate = best_mp4(variants)
    return str(candidate["url"]) if candidate else ""


def prefer_mp4_variants(original: Any) -> Any:
    @wraps(original)
    def parse(self: Any, payload: dict[str, Any]) -> Any:
        # Work on a copy: callers and other hooks may retain the original response.
        response = copy.deepcopy(payload)
        if response.get("errors"):
            return original(self, response)
        result = response.get("data", {}).get("tweetResult", {}).get("result")
        if isinstance(result, dict) and not result.get("article"):
            tweet = result.get("tweet") or result
            legacy = tweet.get("legacy") or {}
            for media in (legacy.get("entities") or {}).get("media") or []:
                if media.get("type") not in {"video", "animated_gif"}:
                    continue
                info = media.get("video_info") or {}
                candidate = best_mp4(info.get("variants"))
                if candidate is None:
                    raise ParseError("未获取到可下载的 MP4 视频流")
                info["variants"] = [candidate]
        return original(self, response)
    return parse
