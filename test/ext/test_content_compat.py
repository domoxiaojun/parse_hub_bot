import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from parsehub import ParseHub
from parsehub.errors import ParseError
from parsehub.parsers.parser.twitter import TwitterParser
from parsehub.parsers.parser.weixin import WXParser
from parsehub.parsers.parser.youtube import YtbParse

from ext.content_hooks import provenance_wrapper
from ext.hooks import install_content_compat
from ext.platform_links import DOMAINS, advertised_patterns


@pytest.mark.parametrize("url,expected", [
    ("https://mp.weixin.qq.com/s?__biz=fixture&mid=1&sn=fixture", "weixin"),
    ("https://www.threads.net/@user/post/fixture", "threads"),
    ("https://t.co/fixture", "twitter"),
    ("https://www.youtube.com/live/fixture", "youtube"),
    ("https://evilyoutube.com/watch?v=fixture", None),
    ("https://notdouyin.com/video/123", None),
    ("https://www.xiaohongshu.com.evil.example/explore/123", None),
    ("https://b23.tv/fixture", "bilibili"),
    ("https://www.instagram.com/reel/fixture", "instagram"),
    ("https://bgm.tv/blog/123456", "bangumi"),
    ("https://bangumi.tv/subject/topic/123456", "bangumi"),
    ("https://chii.in/group/topic/123456", "bangumi"),
    ("https://medium.com/@test/example-123456789abc", "medium"),
    ("https://www.toutiao.com/article/123456789/", "toutiao"),
    ("https://weixin.qq.com/sph/Abcd1234", "weixin_channels"),
    ("https://channels.weixin.qq.com/finder-preview/pages/sph?id=Abcd1234", "weixin_channels"),
    ("https://s.izuiyou.com/s/Abcd1234", "zuiyou"),
    ("https://www.bilibili.com/audio/au12345", "bilibili"),
    ("https://weixin.qq.com.evil.example/sph/Abcd1234", None),
])
def test_provider_matching(url: str, expected: str | None) -> None:
    install_content_compat()
    platform = ParseHub().get_platform(url)
    assert (platform.id if platform else None) == expected


def test_weixin_required_parameters_are_retained() -> None:
    install_content_compat()
    result = asyncio.run(WXParser().get_raw_url("https://mp.weixin.qq.com/s?__biz=ABC%2B&mid=1&idx=2&sn=xyz&tracking=x"))
    assert parse_qs(urlsplit(result).query) == {"__biz": ["ABC+"], "mid": ["1"], "idx": ["2"], "sn": ["xyz"]}


@pytest.mark.parametrize("destination,accepted", [
    ("https://x.com/user/status/123456789", True), ("http://127.0.0.1/private", False),
    ("https://eviltwitter.com/user/status/123456789", False)])
def test_tco_redirect_checks_before_next_request(monkeypatch: Any, destination: str, accepted: bool) -> None:
    install_content_compat()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "t.co":
            return httpx.Response(302, headers={"Location": destination})
        return httpx.Response(200)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(handler)))

    async def run() -> None:
        if accepted:
            assert await TwitterParser().get_raw_url("https://t.co/fixture") == destination
        else:
            with pytest.raises(ParseError):
                await TwitterParser().get_raw_url("https://t.co/fixture")
            assert calls == ["https://t.co/fixture"]
    asyncio.run(run())


@pytest.mark.parametrize("status,accepted", [("was_live", True), ("is_live", False), ("is_upcoming", False)])
def test_recordings_are_finite_and_live_streams_still_rejected(monkeypatch: Any, status: str, accepted: bool) -> None:
    install_content_compat()
    info = {"live_status": status, "title": "Recording", "description": "Description", "thumbnail": "", "duration": 5}
    monkeypatch.setattr("parsehub.parsers.base.ytdlp._run_ytdlp_json", AsyncMock(return_value=info))

    async def run() -> None:
        if accepted:
            result = await YtbParse()._parse("https://www.youtube.com/live/fixture")
            assert result.title == "Recording" and result.description == "Description"
        else:
            with pytest.raises(ParseError, match="不支持直播"):
                await YtbParse()._parse("https://www.youtube.com/live/fixture")
    asyncio.run(run())
    assert info["live_status"] == status


def test_download_provenance_does_not_guess_when_contract_differs() -> None:
    async def run() -> None:
        files = [SimpleNamespace(path=Path("a.jpg")), SimpleNamespace(path=Path("b.jpg"))]
        original = AsyncMock(return_value=SimpleNamespace(media=files))
        wrapped = provenance_wrapper(original)
        result = await wrapped(SimpleNamespace(media=[SimpleNamespace(url="https://example.com/a"),
                                                       SimpleNamespace(url="https://example.com/b")]))
        assert [f._reader_original_url for f in result.media] == ["https://example.com/a", "https://example.com/b"]
        mismatch = SimpleNamespace(path=Path("new.jpg"))
        original.return_value = SimpleNamespace(media=[mismatch])
        await wrapped(SimpleNamespace(media=[]))
        assert not hasattr(mismatch, "_reader_original_url")
    asyncio.run(run())


def test_every_registered_provider_has_domains() -> None:
    assert {p["id"] for p in ParseHub().get_platforms()} <= DOMAINS.keys()
    assert len(advertised_patterns("weixin", "old")) == 2
