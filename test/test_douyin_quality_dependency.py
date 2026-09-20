import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from parsehub.types import VideoRef

from services.douyin_quality import MOBILE_PLAY_RATIOS, _select_best_play_candidate, upgrade_douyin_video


def test_worker_uses_explicit_douyin_quality_order() -> None:
    assert MOBILE_PLAY_RATIOS == ("1080p", "1440p", "2160p", "720p", "540p", "480p", "default")


def test_worker_douyin_quality_selector_ignores_larger_default_alias() -> None:
    selected = _select_best_play_candidate(
        [
            {"ratio": "1080p", "content_length": 100},
            {"ratio": "2160p", "content_length": 200},
            {"ratio": "default", "content_length": 999},
        ]
    )

    assert selected is not None
    assert selected["ratio"] == "2160p"


def test_worker_upgrades_old_parsehub_douyin_result() -> None:
    async def run() -> None:
        result = SimpleNamespace(
            raw_url="https://www.douyin.com/video/1",
            media=VideoRef(url="https://old.example/video.mp4", width=540, height=960),
        )
        detail = {
            "aweme_detail": {
                "video": {
                    "play_addr": {"uri": "video-id", "width": 540, "height": 960},
                }
            }
        }
        best = {"ratio": "1080p", "direct_url": "https://best.example/video.mp4", "content_length": 100}

        with (
            patch("services.douyin_quality._upstream_handles_quality", return_value=False),
            patch("services.douyin_quality.DouyinMobileCrawler.parse", new=AsyncMock(return_value=detail)),
            patch("services.douyin_quality._resolve_best_play_url", new=AsyncMock(return_value=best)),
        ):
            ratio = await upgrade_douyin_video(result, result.raw_url, None)

        assert ratio == "1080p"
        assert result.media.url == best["direct_url"]
        assert (result.media.width, result.media.height) == (1080, 1920)

    asyncio.run(run())
