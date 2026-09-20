"""Self-contained Douyin quality fallback for deployments without ParseHub source."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode

import httpx
from parsehub.provider_api.douyin import DouyinMobileCrawler, DouyinMobileDevice
from parsehub.types import AnyParseResult, VideoRef

PLAY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MOBILE_PLAY_RATIOS = ("1080p", "1440p", "2160p", "720p", "540p", "480p", "default")


def _upstream_handles_quality() -> bool:
    return hasattr(DouyinMobileCrawler, "_attach_best_play")


def _video_ref(result: AnyParseResult) -> VideoRef | None:
    media = result.media
    if isinstance(media, VideoRef):
        return media
    if isinstance(media, Sequence) and not isinstance(media, (str, bytes)):
        videos = [item for item in media if isinstance(item, VideoRef)]
        return videos[0] if len(videos) == 1 else None
    return None


def _pick_video_uri(video: dict[str, Any]) -> str:
    play_addr = video.get("play_addr") or {}
    if uri := play_addr.get("uri"):
        return str(uri)
    best_uri = ""
    best_bitrate = -1
    for item in video.get("bit_rate") or []:
        candidate = str((item.get("play_addr") or {}).get("uri") or "")
        bitrate = int(item.get("bit_rate") or 0)
        if candidate and bitrate >= best_bitrate:
            best_uri, best_bitrate = candidate, bitrate
    return best_uri


def _select_best_play_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    explicit = [item for item in candidates if item.get("ratio") != "default"]
    pool = explicit or candidates
    if not pool:
        return None
    order = {ratio: index for index, ratio in enumerate(MOBILE_PLAY_RATIOS)}
    return max(pool, key=lambda item: (int(item.get("content_length") or 0), -order.get(item["ratio"], 999)))


async def _resolve_best_play_url(video_uri: str, proxy: str | None) -> dict[str, Any] | None:
    headers = {"User-Agent": PLAY_USER_AGENT, "Referer": "https://www.douyin.com/"}
    candidates: list[dict[str, Any]] = []
    async with httpx.AsyncClient(proxy=proxy, timeout=20, follow_redirects=True) as client:
        for ratio in MOBILE_PLAY_RATIOS:
            query = urlencode({"video_id": video_uri, "ratio": ratio, "line": 0})
            url = f"https://aweme.snssdk.com/aweme/v1/play/?{query}"
            try:
                response = await client.head(url, headers=headers)
            except Exception:
                continue
            if response.status_code >= 400:
                continue
            content_type = response.headers.get("content-type", "")
            if content_type and not content_type.lower().startswith("video/"):
                continue
            candidates.append(
                {
                    "ratio": ratio,
                    "direct_url": str(response.url),
                    "content_length": int(response.headers.get("content-length", 0) or 0),
                }
            )
    return _select_best_play_candidate(candidates)


def _dimensions(media: VideoRef, video: dict[str, Any], ratio: str) -> tuple[int, int]:
    source = video.get("play_addr") or {}
    width = int(source.get("width") or video.get("width") or media.width or 0)
    height = int(source.get("height") or video.get("height") or media.height or 0)
    if ratio == "default" or width <= 0 or height <= 0:
        return width, height
    try:
        target = int(ratio.removesuffix("p"))
    except ValueError:
        return width, height
    scale = target / (height if width >= height else width)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


async def upgrade_douyin_video(
    result: AnyParseResult,
    raw_url: str,
    proxy: str | None,
) -> str | None:
    """Replace a public ParseHub result with an explicit quality play URL.

    ParseHub versions containing ``_attach_best_play`` already perform this
    operation upstream, so the Worker does not issue a duplicate mobile call.
    Older public ParseHub wheels still get the same quality guarantee here.
    """
    if _upstream_handles_quality():
        return None
    media = _video_ref(result)
    if media is None:
        return None
    try:
        device = DouyinMobileDevice.resolve()
        crawler = DouyinMobileCrawler(proxy=proxy, device=device)
        detail_payload = await crawler.parse(getattr(result, "raw_url", "") or raw_url)
        detail = detail_payload.get("aweme_detail") or {}
        video = detail.get("video") or {}
        video_uri = _pick_video_uri(video)
        if not video_uri:
            return None
        best = await _resolve_best_play_url(video_uri, proxy)
        if not best:
            return None
        media.url = best["direct_url"]
        media.width, media.height = _dimensions(media, video, best["ratio"])
        return str(best["ratio"])
    except Exception:
        # The original ParseHub result remains a valid fallback when Douyin's
        # optional quality probe is blocked or temporarily unavailable.
        return None


__all__ = ["MOBILE_PLAY_RATIOS", "_select_best_play_candidate", "upgrade_douyin_video"]
