import asyncio
import logging
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from parsehub.errors import ParseError
from parsehub.parsers.parser.douyin import DouyinVideoParseResult, parse_video_info
from parsehub.provider_api.douyin import DouyinMobileCrawler, DouyinWebCrawler
from parsehub.types import DownloadResult, VideoFile, VideoRef
from parsehub.types.result import ParseResult
from pydantic import SecretStr

from core import pl_cfg
from ext import douyin_quality as quality
from ext import hooks
from ext.worker.upstream_adapter import OriginalPipelineAdapter
from services.parser import ParseService


def test_installed_hooks_capture_select_and_verify_download(tmp_path, caplog):
    hooks.install()
    caplog.set_level(logging.INFO, logger="parsehub.quality")
    url = "https://www.douyin.com/video/123"
    detail = {"aweme_detail": {"video": {"play_addr": {"uri": "123"}}}}
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 100)
    actual = {"width": 1920, "height": 1080, "codec": "h264", "fps": 30, "bitrate": 1000}

    async def parse(_url, **kwargs):
        crawler = DouyinWebCrawler(cookie={})
        await crawler.parse(url)
        result = DouyinVideoParseResult(video=VideoRef(url="https://cdn.example/original"))
        result.raw_url = url
        return result

    async def probe(candidates, proxy):
        for candidate in candidates:
            candidate.update(**actual, verified=True, content_length=100)

    async def run():
        service = ParseService()
        file = VideoFile(path=path, width=1920, height=1080, duration=1)
        with (
            patch.object(service.parser, "parse", new=parse),
            patch.object(DouyinWebCrawler, "fetch_one_video", new=AsyncMock(return_value=detail)),
            patch.object(quality, "_probe_candidates", new=probe),
            patch.object(quality, "probe_video", new=AsyncMock(return_value=actual)),
            patch.object(ParseResult, "_do_download", new=AsyncMock(return_value=DownloadResult(
                output_dir=tmp_path, media=file,
            ))),
        ):
            result = await service.parse(url)
            assert result._douyin_quality["highest_confirmed"]
            assert result._douyin_quality["candidates"] == 8
            downloaded = await result._do_download(output_dir=tmp_path)
            assert downloaded.media._douyin_quality["status"] == "match"
            assert quality._attempt.get() is None
    asyncio.run(run())
    assert "event=quality.api" in caplog.text
    assert "event=quality.selected" in caplog.text
    assert "event=quality.verified" in caplog.text


@pytest.fixture(scope="module")
def real_douyin_videos(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required for real media verification")
    root = tmp_path_factory.mktemp("douyin-streams")
    videos = {}
    for name, size in (("low", "960x540"), ("high", "1920x1080")):
        path = root / f"{name}.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
            "-t", "0.2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(path),
        ], check=True, capture_output=True)
        videos[name] = path.read_bytes()
    return videos


@pytest.mark.parametrize("story,web_failure,original_failure", [
    (False, False, False), (True, False, False), (False, True, False), (False, False, True),
], ids=["normal_mobile", "story_mobile", "web_fallback", "default_probe_failed"])
def test_official_mobile_parser_selects_real_1080p_with_quality_hook(
    tmp_path, real_douyin_videos, story, web_failure, original_failure, caplog,
):
    hooks.install()
    caplog.set_level(logging.INFO, logger="parsehub.quality")
    url = "https://www.douyin.com/video/123"
    original_client = httpx.AsyncClient
    requested_ratios = set()

    def serve(request):
        assert request.url.host in {"cdn.example", "aweme.snssdk.com"}
        if original_failure and request.url.path == "/low.mp4":
            return httpx.Response(403)
        # Explicit ratio labels all alias the low stream. Only the API's HD URL is 1080P.
        data = real_douyin_videos["high" if request.url.path == "/high.mp4" else "low"]
        if ratio := request.url.params.get("ratio"):
            requested_ratios.add(ratio)
        headers = {"content-type": "video/mp4", "content-length": str(len(data))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        if value := request.headers.get("range"):
            start, end = map(int, value.removeprefix("bytes=").split("-"))
            end = min(end, len(data) - 1)
            headers.update({"content-range": f"bytes {start}-{end}/{len(data)}",
                            "content-length": str(end - start + 1)})
            return httpx.Response(206, headers=headers, content=data[start:end + 1])
        return httpx.Response(200, headers=headers, content=data)

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(serve))

    async def mobile_fetch(crawler, aweme_id):
        assert aweme_id == "123"
        detail = {"is_story": int(story), "video": {
            "is_bytevc1": 0, "duration": 200,
            "play_addr": {"uri": "fixture", "url_list": ["https://cdn.example/low.mp4"],
                          "width": 960, "height": 540},
            "bit_rate": [{"is_bytevc1": 0, "bit_rate": 5000000, "play_addr": {
                "url_list": ["https://cdn.example/high.mp4"], "width": 1920, "height": 1080,
            }}],
        }}
        await crawler._attach_story_default_play(detail)
        assert parse_video_info(detail["video"])["height"] == 540
        return {"aweme_detail": detail}

    async def web_fetch(_crawler, _aweme_id):
        assert web_failure
        request = httpx.Request("GET", "https://www.douyin.com/fixture")
        try:
            httpx.Response(403, request=request).raise_for_status()
        except httpx.HTTPStatusError as error:
            raise ParseError("fixture web unavailable") from error

    async def download(result, **kwargs):
        # Simulate downloader I/O only; selection, HTTP probing and ffprobe remain real.
        async with client() as http:
            response = await http.get(result.media.url)
        response.raise_for_status()
        path = tmp_path / "download.mp4"
        path.write_bytes(response.content)
        return DownloadResult(output_dir=tmp_path, media=VideoFile(path=path))

    async def run():
        cookie = SecretStr("sessionid=fixture") if web_failure else None
        with (
            patch.object(type(pl_cfg), "roll_cookie", return_value=cookie),
            patch.object(type(pl_cfg), "roll_parser_proxy", return_value=None),
            patch.object(DouyinMobileCrawler, "fetch_one_video", new=mobile_fetch),
            patch.object(DouyinWebCrawler, "fetch_one_video", new=web_fetch),
            patch.object(httpx, "AsyncClient", new=client),
            patch.object(ParseResult, "_do_download", new=download),
        ):
            result = await ParseService().parse(url)
            assert result.media.url == "https://cdn.example/high.mp4"
            assert (result.media.width, result.media.height) == (1920, 1080)
            downloaded = await result._do_download(output_dir=tmp_path)
            summaries = await quality.audit_processed_video(SimpleNamespace(
                source=downloaded.media, output_paths=[downloaded.media.path],
            ))
            assert summaries[0]["status"] == "match"
            assert summaries[0]["at_least_1080p"]
            assert summaries[0]["highest_confirmed"] is not (web_failure or original_failure)
            final_log = next(record.message for record in caplog.records if "event=quality.final" in record.message)
            assert f"trace={result._douyin_quality['trace']}" in final_log
            assert "width=1920 height=1080" in final_log
            assert "at_least_1080p=True" in final_log
    asyncio.run(run())
    assert requested_ratios == set(quality.MOBILE_PLAY_RATIOS)
    assert "event=quality.api" in caplog.text
    assert "event=quality.selected" in caplog.text
    assert "event=quality.verified" in caplog.text


def test_douyin_bypasses_unverifiable_telegram_and_old_parse_caches(tmp_path):
    from core import bs

    url = "https://www.douyin.com/video/123"
    fresh = SimpleNamespace(_douyin_quality={"policy": quality.QUALITY_POLICY})
    service = SimpleNamespace(
        get_raw_url=AsyncMock(return_value=url), parse=AsyncMock(return_value=fresh),
        parser=SimpleNamespace(get_platform=lambda _: SimpleNamespace(id="douyin")),
    )

    async def run():
        with (
            patch.object(bs, "download_dir", tmp_path),
            patch("ext.worker.upstream_adapter.persistent_cache.get", new=AsyncMock()) as persistent,
            patch("ext.worker.upstream_adapter.parse_cache.get", new=AsyncMock(return_value=SimpleNamespace())),
            patch("ext.worker.upstream_adapter.parse_cache.set", new=AsyncMock()),
        ):
            result = await OriginalPipelineAdapter(service).prepare(
                url, mode="read_only", output_mode="preview", refresh=False,
                directory=tmp_path, progress=None, use_persistent_cache=True,
            )
            persistent.assert_not_awaited()
            service.parse.assert_awaited_once_with(url)
            assert result.pipeline.parse_result is fresh
    asyncio.run(run())
