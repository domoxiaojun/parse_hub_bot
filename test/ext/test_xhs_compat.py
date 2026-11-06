"""Synthetic upstream response fixtures; no network, cookies, or media downloads."""

import copy
from typing import Any

import pytest
from parsehub.errors import ParseError
from parsehub.provider_api.xhs import XHSAPI, XHSMediaType

from ext.hooks import install_xhs_compat
from ext.xhs_compat import select_stream


def video(url: str = "https://example.com/video.mp4") -> dict[str, Any]:
    return {"masterUrl": url, "duration": 1500, "width": 1080, "height": 1920}


@pytest.mark.parametrize("codec", ["h264", "av1", "h265", "h266", "EF4"])
def test_upstream_video_and_live_photo_use_compatible_selector(codec: str) -> None:
    install_xhs_compat()
    install_xhs_compat()
    api: Any = XHSAPI()
    image = {"urlDefault": "https://example.com/image.jpg", "width": 1080, "height": 1920}
    result = api._XHSAPI__parse_media({"video": {"media": {"stream": {codec: [video()]}}}, "imageList": [image]})
    assert result[0].type == XHSMediaType.VIDEO
    assert result[0].url == video()["masterUrl"]
    assert result[0].width == 1080
    result = api._XHSAPI__parse_media({"imageList": [{**image, "livePhoto": True, "stream": {codec: [video()]}}]})
    assert result[0].type == XHSMediaType.LIVE_PHOTO
    assert result[0].url == video()["masterUrl"] and result[0].thumb_url


def test_preference_and_malformed_groups() -> None:
    first, second = video(), video("https://example.com/other.mp4")
    assert select_stream({"EF4": [second], "h264": [first], "av1": [second]}) == [first]
    assert select_stream({"h264": None, "av1": [None, {}, {"masterUrl": "file:///tmp/x"}], "EF4": [second]}) == [second]
    assert select_stream({"h264": [], "av1": {}, "EF4": []}) is None


def test_no_valid_video_is_parse_error_instead_of_key_error() -> None:
    install_xhs_compat()
    api: Any = XHSAPI()
    with pytest.raises(ParseError, match="未获取到视频流"):
        api._XHSAPI__parse_media({"video": {"media": {"stream": {"EF4": []}}}})


@pytest.mark.parametrize("portrait", [False, True])
def test_4k_wins_across_codecs_and_unsorted_candidates(portrait: bool) -> None:
    install_xhs_compat()
    low = {**video("https://example.com/720.mp4"), "width": 1280, "height": 720, "videoBitrate": 9_000_000}
    high = {**video("https://example.com/4k.mp4"), "width": 3840, "height": 2160, "videoBitrate": 5_000_000}
    if portrait:
        for item in (low, high):
            item["width"], item["height"] = item["height"], item["width"]
    stream = {"h264": [low], "h265": [low, high]}
    original = copy.deepcopy(stream)
    api: Any = XHSAPI()
    image = {"urlDefault": "https://example.com/image.jpg", "width": 1080, "height": 1920}
    parsed = api._XHSAPI__parse_media({"video": {"media": {"stream": stream}}, "imageList": [image]})
    assert parsed[0].url == high["masterUrl"]
    assert (parsed[0].width, parsed[0].height) == (high["width"], high["height"])
    live = api._XHSAPI__parse_media({"imageList": [{**image, "livePhoto": True, "stream": stream}]})
    assert live[0].url == high["masterUrl"]
    assert stream == original


def test_missing_4k_uses_best_available_and_ignores_invalid_ranking_fields() -> None:
    low = {**video(), "width": 1280, "height": 720, "fps": 120, "videoBitrate": 20_000_000}
    high = {**video("https://example.com/1080.mp4"), "fps": 60, "videoBitrate": "8000000"}
    bad = {**video("https://example.com/bad.mp4"), "width": "nan", "height": None, "videoBitrate": "inf"}
    assert select_stream({"h264": [low, bad], "EF4": [high]}) == [high]
    assert select_stream({"h264": [low, {**high, "fps": 30}, high]}) == [high]
