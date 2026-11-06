"""Highest-bitrate X MP4 selection with synthetic responses only."""

import copy
from typing import Any

import pytest
from parsehub.errors import ParseError
from parsehub.provider_api.twitter import ArticleRenderer, Twitter

from ext.hooks import install_twitter_quality
from ext.twitter_quality import best_mp4_url


def variant(name: str, rate: Any, field: str = "bitrate") -> dict[str, Any]:
    return {"url": f"https://example.com/{name}.mp4", "content_type": "video/mp4", field: rate}


@pytest.mark.parametrize("kind", ["video", "animated_gif"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_regular_media_selects_highest_mp4_without_mutating_payload(kind: str, wrapped: bool) -> None:
    install_twitter_quality()
    original_parse = Twitter.parse
    install_twitter_quality()
    assert Twitter.parse is original_parse
    variants = [variant("high", 8_000_000), variant("low", 256_000), {
        "url": "https://example.com/stream.m3u8", "content_type": "application/x-mpegURL", "bitrate": 99_000_000,
    }]
    tweet = {"rest_id": "123", "legacy": {"full_text": "caption", "entities": {"media": [
        {"type": "photo", "media_url_https": "https://example.com/p.jpg", "original_info": {}},
        {"type": kind, "media_url_https": "https://example.com/cover.jpg", "original_info": {
            "width": 1920, "height": 1080,
        }, "video_info": {"variants": variants, "duration_millis": 1500}},
    ]}}}
    payload = {"data": {"tweetResult": {"result": {"tweet": tweet} if wrapped else tweet}}}
    saved = copy.deepcopy(payload)
    result = Twitter(proxy=None).parse(payload)
    assert result.media[1].url == "https://example.com/high.mp4"
    assert result.media[0].url.endswith("name=orig")
    assert result.full_text == "caption"
    assert payload == saved


def test_article_selector_supports_both_bitrate_fields_and_malformed_variants() -> None:
    install_twitter_quality()
    variants = [
        None, {}, variant("bad", "NaN"), variant("negative", -1),
        variant("low", 256_000, "bit_rate"), variant("high", "8000000", "bit_rate"),
        {**variant("file", 99_000_000), "url": "file:///tmp/private.mp4"},
    ]
    assert ArticleRenderer._best_mp4_url(variants) == "https://example.com/high.mp4"
    assert best_mp4_url([variant("low", 1, "bit_rate"), variant("high", 2)]) == "https://example.com/high.mp4"
    assert best_mp4_url([{"content_type": "application/x-mpegURL", "url": "https://example.com/a"}]) == ""


def test_missing_mp4_is_explicit_and_api_errors_are_preserved() -> None:
    install_twitter_quality()
    payload = {"data": {"tweetResult": {"result": {"legacy": {"entities": {"media": [
        {"type": "video", "video_info": {"variants": []}},
    ]}}}}}}
    with pytest.raises(ParseError, match="MP4"):
        Twitter(proxy=None).parse(payload)
    with pytest.raises(Exception, match="synthetic upstream error"):
        Twitter(proxy=None).parse({"errors": [{"message": "synthetic upstream error"}], "data": None})
