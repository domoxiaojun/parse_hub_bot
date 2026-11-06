"""Instagram candidate preservation and parsing, without changing upstream requests."""

import asyncio
import copy
import logging
import math
from typing import Any, cast

import yt_dlp
from parsehub.errors import ParseError
from parsehub.parsers.base.ytdlp import YtParser
from parsehub.parsers.parser.instagram import InstagramParser
from parsehub.parsers.parser.youtube import YtbParse
from parsehub.provider_api.instagram import InstagramAPI, InstagramMedia, InstagramMediaType, InstagramPost
from parsehub.types import AnyParseResult, ImageRef, VideoRef
from yt_dlp.extractor.instagram import InstagramIE

from ext.instagram_media import FORMAT_SORT, QualityMedia, number, rank

logger = logging.getLogger("parsehub.quality")


def candidates(values: Any) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        url = value.get("url") or value.get("src")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        item = {
            "url": url, "width": int(number(value.get("width") or value.get("config_width"))),
            "height": int(number(value.get("height") or value.get("config_height"))),
            "fps": number(value.get("fps")), "bitrate": number(value.get("bitrate") or value.get("bit_rate")),
        }
        if url not in result or rank(item) > rank(result[url]):
            result[url] = item
    return sorted(result.values(), key=rank, reverse=True)


def media_from_data(data: dict[str, Any], *, graph: bool = False) -> QualityMedia:
    images = candidates(
        data.get("display_resources") if graph else (data.get("image_versions2") or {}).get("candidates")
    )
    fallback = data.get("display_url") or data.get("display_src")
    if fallback:
        images = candidates([*images, {"url": fallback}])
    videos = candidates(data.get("video_versions"))
    if data.get("video_url"):
        dimensions = data.get("dimensions") or {}
        videos = candidates([*videos, {"url": data["video_url"], **dimensions}])
    manifest = data.get("video_dash_manifest") or ""
    is_video = bool(
        data.get("media_type") == 2 or data.get("__typename") == "GraphVideo"
        or data.get("is_video") is True or videos or manifest
    )
    best = next(iter(videos if is_video else images), {})
    return QualityMedia(
        is_video=is_video, display_url=images[0]["url"] if images else "",
        video_url=videos[0]["url"] if videos else "", width=best.get("width", 0), height=best.get("height", 0),
        duration=number(data.get("video_duration")),
        media_id=str(data.get("pk") or data.get("id") or "").split("_", 1)[0],
        images=images, videos=videos, manifest=manifest if isinstance(manifest, str) else "",
        has_audio=data.get("has_audio") is not False, codec=data.get("video_codec"),
    )


def v1_media(data: dict[str, Any]) -> QualityMedia:
    return media_from_data(data)


def graph_media(data: dict[str, Any]) -> QualityMedia:
    return media_from_data(data, graph=True)


def as_quality(media: InstagramMedia) -> QualityMedia:
    if isinstance(media, QualityMedia):
        return media
    return QualityMedia(
        is_video=media.is_video, display_url=media.display_url, video_url=media.video_url,
        width=media.width, height=media.height, duration=media.duration,
        images=candidates([{"url": media.display_url, "width": media.width, "height": media.height}]),
        videos=candidates([{"url": media.video_url, "width": media.width, "height": media.height}]),
    )


async def fill_missing(self: InstagramAPI, session: Any, post: InstagramPost, shortcode: str) -> InstagramPost:
    items = [as_quality(item) for item in post.media]
    if not any(item.is_video and (not (item.video_url or item.manifest) or not item.duration) for item in items):
        return post
    try:
        node = await self._fetch_embed(session, shortcode)
        if node is None:
            return post
        recovered = [as_quality(item) for item in InstagramPost.from_graph(node).media]
    except Exception as error:
        logger.info("event=instagram.recovery status=failed reason=%s", type(error).__name__)
        return post
    for item in items:
        if not item.is_video:
            continue
        matches = [source for source in recovered if item.media_id and source.media_id == item.media_id]
        if len(items) == len(recovered) == 1 and not matches and not (item.media_id and recovered[0].media_id):
            matches = recovered
        if len(matches) != 1 or not matches[0].is_video:
            continue
        source = matches[0]
        item.videos = candidates([*item.videos, *source.videos])
        item.manifest = item.manifest or source.manifest
        item.duration = item.duration or source.duration
        if item.videos:
            item.video_url = item.videos[0]["url"]
            item.width, item.height = item.videos[0]["width"], item.videos[0]["height"]
    post.media = list(items)
    return post


class AuthenticatedYtParser(YtParser, register=False):  # type: ignore[call-arg]
    def get_cookie_text(self) -> str | None:
        cookie = YtbParse.to_netscape_cookie(self.cookie.get_value(), "instagram.com")
        return str(cookie) if cookie else None

    @property
    def cli_args(self) -> list[str]:
        return [*super().cli_args, "-S", FORMAT_SORT, "--format-sort-force"]

    async def _extract_info(self, url: str) -> dict[str, Any]:
        info: dict[str, Any] = await super()._extract_info(url)
        if info.get("_type") == "playlist":
            raise ParseError("instagram_unexpected_playlist")
        return info


def inline_recipe(media: QualityMedia) -> dict[str, Any] | None:
    """Reuse yt-dlp's inline MPD parser and selector; this makes no network request."""
    options = {"quiet": True, "no_warnings": True, "check_formats": False,
               "format_sort": FORMAT_SORT.split(","), "format_sort_force": True}
    with yt_dlp.YoutubeDL(cast(Any, options)) as ydl:
        formats = [{"url": item["url"], "width": item["width"], "height": item["height"],
                    "fps": item["fps"] or None, "tbr": item["bitrate"] / 1000 or None,
                    "ext": "mp4", "format_id": str(i), "vcodec": media.codec,
                    "acodec": None if media.has_audio else "none"} for i, item in enumerate(media.videos)]
        if media.manifest:
            extractor = InstagramIE(ydl)
            try:
                formats.extend(extractor._parse_mpd_formats(
                    extractor._parse_xml(media.manifest, media.media_id or "instagram"), mpd_id="dash",
                ))
            except Exception as error:
                logger.info("event=instagram.manifest status=failed reason=%s", type(error).__name__)
        if not formats:
            return None
        return dict(ydl.process_video_result(cast(Any, {
            "id": media.media_id or "instagram", "title": "Instagram", "duration": media.duration,
            "formats": formats, "http_headers": {"Referer": "https://www.instagram.com/"},
        }), download=False))


async def parse(self: InstagramParser, raw_url: str) -> AnyParseResult:
    from ext.instagram_download import InstagramCarouselResult, InstagramImageResult, InstagramVideoResult

    shortcode = self.get_short_code(raw_url)
    if not shortcode:
        raise ParseError("instagram_invalid_url")
    post = await self._parse(shortcode)
    items = [as_quality(item) for item in post.media]
    recipes: dict[int, dict[str, Any]] = {}
    if post.type == InstagramMediaType.VIDEO:
        if len(items) != 1:
            raise ParseError("instagram_media_count_mismatch")
        items[0].is_video = True
        try:
            info = await AuthenticatedYtParser(proxy=self.proxy, cookie=self.cookie)._parse(raw_url)
            recipes[0] = {**copy.deepcopy(info.info_json), "width": info.width, "height": info.height,
                          "duration": info.duration or items[0].duration}
        except ParseError as error:
            logger.info("event=instagram.extract status=fallback reason=%s", type(error).__name__)
    refs: list[ImageRef | VideoRef] = []
    for index, item in enumerate(items):
        if item.is_video:
            if index not in recipes:
                try:
                    recipe = await asyncio.to_thread(inline_recipe, item)
                    if recipe:
                        recipes[index] = recipe
                except Exception as error:
                    logger.info("event=instagram.extract status=fallback reason=%s", type(error).__name__)
            best = max([*item.videos, recipes.get(index, {})], key=rank)
            if not item.videos and index not in recipes:
                raise ParseError("instagram_video_unavailable")
            refs.append(VideoRef(
                url=best.get("url") or item.video_url or raw_url, thumb_url=item.display_url or None,
                width=int(number(best.get("width"))), height=int(number(best.get("height"))),
                duration=math.ceil(item.duration or number(best.get("duration"))),
            ))
        else:
            if not item.images:
                raise ParseError("instagram_image_unavailable")
            best = item.images[0]
            refs.append(ImageRef(url=best["url"], width=best["width"], height=best["height"]))
    if not refs:
        raise ParseError("instagram_media_unavailable")
    cls = (InstagramVideoResult if post.type == InstagramMediaType.VIDEO else
           InstagramImageResult if post.type == InstagramMediaType.IMAGE else InstagramCarouselResult)
    return cls(post, items, refs, recipes, YtbParse.to_netscape_cookie(self.cookie.get_value(), "instagram.com"))
