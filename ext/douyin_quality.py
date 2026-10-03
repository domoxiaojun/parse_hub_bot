"""Select Douyin streams using measured parameters and log the evidence."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence
from contextvars import ContextVar, Token
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from parsehub.errors import DownloadError
from parsehub.provider_api.douyin import DouyinMobileCrawler, DouyinMobileDevice
from parsehub.types import AnyParseResult, VideoFile, VideoRef

from ext.media.video_probe import VideoProbeError, probe_remote_video, probe_video

PLAY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MOBILE_PLAY_RATIOS = ("1080p", "1440p", "2160p", "720p", "540p", "480p", "default")
QUALITY_POLICY = "measured-resolution-fps-bitrate-v1"
logger = logging.getLogger("parsehub.quality")
_attempt: ContextVar[dict[str, Any] | None] = ContextVar("douyin_quality_attempt", default=None)


def _video_id(url: str) -> str:
    match = re.search(r"/(?:video|note)/(\d+)", urlsplit(url).path)
    value = match[1] if match else parse_qs(urlsplit(url).query).get("modal_id", [""])[0]
    return value if value.isdigit() else "unknown"


def begin_quality(url: str) -> Token[dict[str, Any] | None]:
    token = _attempt.set({"trace": uuid.uuid4().hex[:12], "video_id": _video_id(url), "payloads": []})
    _event("start", policy=QUALITY_POLICY)
    return token


def end_quality(token: Token[dict[str, Any] | None]) -> None:
    _attempt.reset(token)


def _event(name: str, *, warning: bool = False, **fields: Any) -> None:
    context = _attempt.get() or {}
    fields = {"trace": context.get("trace", "none"), "video_id": context.get("video_id", "unknown"), **fields}
    # Controlled labels/numbers only: never upstream messages, URLs or cookies.
    values = " ".join(f"{key}={value}" for key, value in fields.items())
    message = "event=quality." + name + " platform=douyin " + values
    logger.log(logging.WARNING if warning else logging.INFO, message)


def crawler_observer(original: Any, source: str) -> Any:
    @wraps(original)
    async def parse(self: Any, raw_url: str) -> dict[str, Any]:
        context = _attempt.get()
        if context is not None:
            context["video_id"] = _video_id(raw_url)
        try:
            response: dict[str, Any] = await original(self, raw_url)
        except Exception as error:
            if context is not None:
                context["api_failed"] = True
                cause: BaseException | None = error
                status: int | str = "unknown"
                for _ in range(8):
                    if isinstance(cause, httpx.HTTPStatusError):
                        status = cause.response.status_code
                        break
                    cause = cause.__cause__ if cause else None
                _event("api", warning=True, source=source, status="failed", http_status=status,
                       reason=type(error).__name__, cookie_valid="unknown")
            raise
        if context is not None:
            context["payloads"].append((source, response))
            _event("api", source=source, status="ok", cookie_valid="unknown")
        return response
    return parse


def _video_ref(result: AnyParseResult) -> VideoRef | None:
    media = result.media
    if isinstance(media, VideoRef):
        return media
    if isinstance(media, Sequence) and not isinstance(media, (str, bytes)):
        videos = [item for item in media if isinstance(item, VideoRef)]
        return videos[0] if len(videos) == 1 else None
    return None


def _pick_video_uri(video: dict[str, Any]) -> str:
    addresses = [video.get("play_addr") or {}, *(item.get("play_addr") or {} for item in video.get("bit_rate") or [])]
    return next((str(address["uri"]) for address in addresses if address.get("uri")), "")


def _candidates(media: VideoRef, payloads: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = [{"ratio": "original", "source": "parser", "direct_url": media.url}]
    uris: set[str] = set()
    for source, payload in payloads:
        video = (payload.get("aweme_detail") or {}).get("video") or {}
        if uri := _pick_video_uri(video):
            uris.add(uri)
        addresses = [video.get(key) or {} for key in ("play_addr", "play_addr_h264", "play_addr_265")]
        addresses.extend(item.get("play_addr") or {} for item in video.get("bit_rate") or [])
        for index, address in enumerate(addresses):
            for url in address.get("url_list") or []:
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    candidates.append({"ratio": f"stream_{index}", "source": source,
                                       "direct_url": url.replace("playwm", "play")})
    for uri in sorted(uris):
        for ratio in MOBILE_PLAY_RATIOS:
            query = urlencode({"video_id": uri, "ratio": ratio, "line": 0})
            candidates.append({"ratio": ratio, "source": "explicit",
                               "direct_url": f"https://aweme.snssdk.com/aweme/v1/play/?{query}"})
    # Keep the original label when an API URL is an exact duplicate.
    return list({item["direct_url"]: item for item in reversed(candidates)}.values())[::-1]


def _rank(candidate: dict[str, Any]) -> tuple[int, float, int]:
    return candidate["width"] * candidate["height"], candidate["fps"], candidate["bitrate"]


def _select_best_play_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    verified = [item for item in candidates if item.get("verified")]
    return max(verified, key=_rank) if verified else None


async def _probe_candidates(candidates: list[dict[str, Any]], proxy: str | None) -> None:
    semaphore = asyncio.Semaphore(3)
    headers = {"User-Agent": PLAY_USER_AGENT, "Referer": "https://www.douyin.com/"}
    async with httpx.AsyncClient(proxy=proxy, headers=headers, timeout=12, follow_redirects=True) as client:
        async def probe(item: dict[str, Any]) -> None:
            async with semaphore:
                try:
                    item.update(await probe_remote_video(client, item["direct_url"]))
                    item["verified"] = True
                    _event("candidate", source=item["source"], requested=item["ratio"], status="verified",
                           width=item["width"], height=item["height"], fps=item["fps"], codec=item["codec"],
                           bitrate=item["bitrate"], bytes=item["content_length"],
                           at_least_1080p=min(item["width"], item["height"]) >= 1080)
                except Exception as error:
                    item["verified"] = False
                    reason = str(error) if isinstance(error, VideoProbeError) else type(error).__name__
                    status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else "unknown"
                    _event("candidate", warning=True, source=item["source"], requested=item["ratio"],
                           status="unverified", reason=reason, http_status=status)
        await asyncio.gather(*(probe(item) for item in candidates))


async def upgrade_douyin_video(result: AnyParseResult, raw_url: str, proxy: str | None) -> str | None:
    media = _video_ref(result)
    if media is None:
        _event("skip", reason="not_single_video")
        return None
    context = _attempt.get() or {}
    payloads = list(context.get("payloads", []))
    discovery_complete = bool(payloads)
    if not payloads:
        try:
            crawler = DouyinMobileCrawler(proxy=proxy, device=DouyinMobileDevice.resolve())
            payload = await crawler.parse(getattr(result, "raw_url", "") or raw_url)
            payloads.append(("mobile", payload))
            discovery_complete = True
        except Exception as error:
            _event("discovery", warning=True, status="failed", reason=type(error).__name__)
    candidates = _candidates(media, payloads)
    discovery_complete = discovery_complete and any(item["source"] == "explicit" for item in candidates)
    try:
        await _probe_candidates(candidates, proxy)
    except Exception as error:
        _event("probe", warning=True, status="failed", reason=type(error).__name__)
    best = _select_best_play_candidate(candidates)
    verified = sum(bool(item.get("verified")) for item in candidates)
    report = {
        "policy": QUALITY_POLICY, "trace": context.get("trace", "none"),
        "video_id": _video_id(getattr(result, "raw_url", "") or raw_url),
        "candidates": len(candidates), "verified_candidates": verified,
        "highest_confirmed": (
            discovery_complete and not context.get("api_failed", False) and verified == len(candidates)
            and all(item.get("fps", 0) > 0 and item.get("bitrate", 0) > 0 for item in candidates)
        ),
        "scope": "discovered_candidates",
    }
    if best is None:
        report["highest_confirmed"] = False
        result._douyin_quality = report
        _event("selected", warning=True, **report, status="original_unverified", reason="no_verified_candidate")
        return None
    # A failed default-stream probe must not veto a verified HD candidate.
    # Incomplete probes already keep highest_confirmed false above.
    original = next((item for item in candidates if item["ratio"] == "original"), None)
    report["original_verified"] = bool(original and original.get("verified"))
    media.url = best["direct_url"]
    media.width, media.height = best["width"], best["height"]
    report.update({key: best[key] for key in ("width", "height", "fps", "bitrate", "codec", "content_length")})
    report["requested"] = best["ratio"]
    result._douyin_quality = report
    _event("selected", warning=not report["highest_confirmed"], **report, status="verified",
           reason="max_measured_resolution_fps_bitrate")
    return str(best["ratio"])


async def verify_file(path: Path, report: dict[str, Any], *, stage: str) -> dict[str, Any]:
    context = {key: report[key] for key in ("trace", "video_id") if key in report}
    try:
        actual = await probe_video(path)
    except Exception as error:
        _event("verified", warning=True, **context, stage=stage, status="unverified",
               highest_confirmed=False, reason=type(error).__name__)
        return {"highest_confirmed": False, "status": "unverified"}
    expected = bool(report.get("width") and report.get("height"))
    mismatch = expected and (
        (actual["width"], actual["height"], actual["codec"]) != (report["width"], report["height"], report["codec"])
        or actual["fps"] + 0.1 < report["fps"]
        or (actual["bitrate"] > 0 and report["bitrate"] > 0
            and abs(actual["bitrate"] - report["bitrate"]) > report["bitrate"] * 0.01)
        or (stage == "download" and path.stat().st_size != report["content_length"])
    )
    status = "mismatch" if mismatch else "match" if expected else "measured_only"
    summary = {**actual, "bytes": path.stat().st_size, "status": status,
               "at_least_1080p": min(actual["width"], actual["height"]) >= 1080,
               "highest_confirmed": bool(report.get("highest_confirmed")) and expected and not mismatch}
    _event("verified", warning=not summary["highest_confirmed"], **context, stage=stage, **summary)
    if mismatch:
        raise DownloadError("douyin_quality_mismatch")
    return summary


def download_verifier(original: Any) -> Any:
    @wraps(original)
    async def download(self: Any, **kwargs: Any) -> Any:
        result = await original(self, **kwargs)
        report = getattr(self, "_douyin_quality", {})
        files = result.media if isinstance(result.media, Sequence) else [result.media]
        for file in files:
            if isinstance(file, VideoFile):
                summary = await verify_file(Path(file.path), report, stage="download")
                file._douyin_quality = {**report, **summary}
                if "width" in summary:
                    file.width, file.height = summary["width"], summary["height"]
        return result
    return download


async def audit_processed_video(processed: Any) -> list[dict[str, Any]]:
    """Reuse download measurements for untouched files; measure transformed outputs separately."""
    source = processed.source
    if not isinstance(source, VideoFile):
        return []
    report = getattr(source, "_douyin_quality", {})
    summaries = []
    for path in processed.output_paths or [source.path]:
        path = Path(path)
        if path == Path(source.path) and report.get("status"):
            summary = {key: report[key] for key in (
                "width", "height", "codec", "fps", "bitrate", "bytes", "status", "highest_confirmed", "at_least_1080p"
            ) if key in report}
            if report["status"] == "unverified":
                summary = {"status": "unverified", "highest_confirmed": False}
        else:
            summary = await verify_file(path, report, stage="output")
        summary["video_id"] = report.get("video_id", "unknown")
        _event("final", warning=not summary.get("highest_confirmed"), trace=report.get("trace", "none"),
               **summary, scope="discovered_candidates",
               measurement="download" if path == Path(source.path) else "output")
        summaries.append(summary)
    return summaries
