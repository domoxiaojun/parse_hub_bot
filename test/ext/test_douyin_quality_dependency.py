import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from parsehub.errors import DownloadError, ParseError
from parsehub.types import VideoFile, VideoRef

from ext import douyin_quality as quality


def measured(width=1920, height=1080, **overrides):
    return {
        "width": width, "height": height, "fps": 30.0, "bitrate": 2_000_000,
        "codec": "h264", "content_length": 100, "verified": True,
        "ratio": "1080p", "direct_url": "https://cdn.example/best.mp4", **overrides,
    }


def test_real_dimensions_win_over_file_size_ratio_and_default_label():
    candidates = [
        measured(1280, 720, ratio="2160p", content_length=999999),
        measured(1920, 1080, ratio="default", content_length=100),
        measured(3840, 2160, verified=False),
    ]
    assert quality._select_best_play_candidate(candidates) is candidates[1]
    assert quality._select_best_play_candidate([candidates[2]]) is None


def test_rank_prefers_resolution_then_fps_then_bitrate():
    candidates = [
        measured(bitrate=5_000_000), measured(fps=60, bitrate=1_000_000),
        measured(fps=60, bitrate=2_000_000), measured(720, 1280, fps=120),
    ]
    assert quality._select_best_play_candidate(candidates) is candidates[2]


def detail():
    return {"aweme_detail": {"video": {
        "play_addr": {"uri": "video-id", "url_list": ["https://cdn.example/original.mp4"]},
        "bit_rate": [{"play_addr": {"url_list": ["https://cdn.example/high.mp4"]}}],
    }}}


def test_collects_api_bitrates_original_and_all_explicit_ratios():
    candidates = quality._candidates(VideoRef(url="https://cdn.example/original.mp4"), [("web", detail())])
    assert len(candidates) == 9
    assert candidates[0]["ratio"] == "original"
    assert {c["ratio"] for c in candidates if c["source"] == "explicit"} == set(quality.MOBILE_PLAY_RATIOS)
    assert any(c["direct_url"].endswith("/high.mp4") for c in candidates)


@pytest.mark.parametrize("selected", ["api", "explicit", "original"])
def test_selected_rendition_owns_its_backup_urls(selected):
    async def run():
        token = quality.begin_quality("https://www.douyin.com/video/123")
        try:
            payload = detail()
            payload["aweme_detail"]["video"]["bit_rate"][0]["play_addr"]["url_list"].append(
                "https://backup.example/high.mp4"
            )
            quality._attempt.get()["payloads"].append(("web", payload))
            result = SimpleNamespace(raw_url="https://www.douyin.com/video/123", media=VideoRef(
                url="https://cdn.example/original.mp4", backup_urls=["https://backup.example/low.mp4"],
            ))

            async def probe(candidates, _proxy):
                for candidate in candidates:
                    chosen = (
                        candidate["direct_url"].endswith("/high.mp4") if selected == "api"
                        else candidate["ratio"] == "1080p" if selected == "explicit"
                        else candidate["ratio"] == "original"
                    )
                    candidate.update(measured(
                        1920 if chosen else 960, 1080 if chosen else 540,
                        ratio=candidate["ratio"], direct_url=candidate["direct_url"],
                    ))

            with patch.object(quality, "_probe_candidates", new=probe):
                await quality.upgrade_douyin_video(result, result.raw_url, None)
            expected = {
                "api": ["https://backup.example/high.mp4"], "explicit": [],
                "original": ["https://backup.example/low.mp4"],
            }
            assert result.media.backup_urls == expected[selected]
        finally:
            quality.end_quality(token)

    asyncio.run(run())


@pytest.mark.parametrize("original_fails,other_fails,api_failed", [
    (False, False, False), (True, False, False), (False, True, False), (False, False, True),
])
def test_selection_does_not_invent_dimensions_or_hide_incomplete_probes(
    original_fails, other_fails, api_failed, caplog,
):
    caplog.set_level(logging.INFO, logger="parsehub.quality")

    async def run():
        token = quality.begin_quality("https://www.douyin.com/video/123")
        try:
            quality._attempt.get()["payloads"].append(("web", detail()))
            quality._attempt.get()["api_failed"] = api_failed
            result = SimpleNamespace(
                raw_url="https://www.douyin.com/video/123",
                media=VideoRef(url="https://cdn.example/original.mp4", width=1920, height=1080),
            )

            async def probe(candidates, _proxy):
                for candidate in candidates:
                    original = candidate["ratio"] == "original"
                    high = candidate["direct_url"].endswith("/high.mp4")
                    candidate.update(measured(
                        3840 if high else 1920 if original else 1280,
                        2160 if high else 1080 if original else 720,
                        ratio=candidate["ratio"], direct_url=candidate["direct_url"],
                        verified=not ((original and original_fails) or (candidate["ratio"] == "1440p" and other_fails)),
                    ))

            with patch.object(quality, "_probe_candidates", new=probe), \
                    patch.object(quality.DouyinMobileCrawler, "parse", new=AsyncMock()) as mobile:
                await quality.upgrade_douyin_video(result, result.raw_url, None)
            mobile.assert_not_awaited()
            assert result.media.url.endswith("/high.mp4")
            assert (result.media.width, result.media.height) == (3840, 2160)
            assert result._douyin_quality["original_verified"] is not original_fails
            assert result._douyin_quality["highest_confirmed"] is not (original_fails or other_fails or api_failed)
        finally:
            quality.end_quality(token)

    asyncio.run(run())
    assert "event=quality.selected" in caplog.text
    assert "https://" not in caplog.text


def test_crawler_failure_logs_status_without_exposing_cookie_or_claiming_expiry(caplog):
    caplog.set_level(logging.INFO, logger="parsehub.quality")

    async def original(_self, _url):
        request = httpx.Request("GET", "https://douyin.com/?token=SECRET")
        try:
            httpx.Response(403, request=request).raise_for_status()
        except httpx.HTTPStatusError as error:
            raise ParseError("cookie=SECRET") from error

    async def run():
        token = quality.begin_quality("https://douyin.com/video/123")
        try:
            with pytest.raises(ParseError):
                await quality.crawler_observer(original, "web")(None, "https://douyin.com/video/123")
        finally:
            quality.end_quality(token)

    asyncio.run(run())
    assert "http_status=403" in caplog.text
    assert "cookie_valid=unknown" in caplog.text
    assert "SECRET" not in caplog.text


def test_parallel_parses_keep_api_payloads_separate():
    async def run_one(video_id):
        token = quality.begin_quality(f"https://douyin.com/video/{video_id}")
        try:
            async def original(_self, _url):
                await asyncio.sleep(0)
                return {"id": video_id}
            await quality.crawler_observer(original, "mobile")(None, f"https://douyin.com/video/{video_id}")
            assert quality._attempt.get()["payloads"] == [("mobile", {"id": video_id})]
        finally:
            quality.end_quality(token)

    async def run():
        await asyncio.gather(run_one("1"), run_one("2"))
        assert quality._attempt.get() is None
    asyncio.run(run())


def test_download_mismatch_is_an_error_and_probe_failure_is_unverified(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="parsehub.quality")
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 100)
    expected = {**measured(), "highest_confirmed": True}

    async def run():
        with patch.object(quality, "probe_video", new=AsyncMock(return_value=measured(1280, 720))):
            with pytest.raises(DownloadError, match="douyin_quality_mismatch"):
                await quality.verify_file(path, expected, stage="download")
        with patch.object(quality, "probe_video", new=AsyncMock(side_effect=RuntimeError("SECRET"))):
            summary = await quality.verify_file(path, expected, stage="download")
            assert summary == {"highest_confirmed": False, "status": "unverified"}
    asyncio.run(run())
    assert "status=mismatch" in caplog.text
    assert "width=1280 height=720" in caplog.text
    assert "SECRET" not in caplog.text


def test_final_log_reuses_download_probe_for_unchanged_file(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="parsehub.quality")
    source = VideoFile(path=tmp_path / "video.mp4", width=1920, height=1080, duration=1)
    source._douyin_quality = {
        **measured(), "status": "match", "highest_confirmed": True, "bytes": 100,
        "trace": "fixture-trace", "video_id": "123", "at_least_1080p": True,
    }

    async def run():
        with patch.object(quality, "probe_video", new=AsyncMock()) as probe:
            summaries = await quality.audit_processed_video(SimpleNamespace(source=source, output_paths=[source.path]))
            assert summaries[0]["highest_confirmed"]
            assert summaries[0]["at_least_1080p"]
            probe.assert_not_awaited()
    asyncio.run(run())
    assert "event=quality.final" in caplog.text
    assert "trace=fixture-trace video_id=123" in caplog.text


def test_no_candidates_preserves_original_and_marks_unknown():
    async def run():
        result = SimpleNamespace(raw_url="https://douyin.com/video/1", media=VideoRef(url="original"))
        with patch.object(quality.DouyinMobileCrawler, "parse", new=AsyncMock(side_effect=RuntimeError())), \
                patch.object(quality, "_probe_candidates", new=AsyncMock()):
            assert await quality.upgrade_douyin_video(result, result.raw_url, None) is None
        assert result.media.url == "original"
        assert not result._douyin_quality["highest_confirmed"]
    asyncio.run(run())
