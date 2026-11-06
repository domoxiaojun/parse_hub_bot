"""Runtime overrides applied to pristine upstream modules.

Every fork behaviour that upstream code must pick up is installed here instead
of by editing ``services/``, ``plugins/`` or ``utils/``. Each hook names its
exact target; if upstream renames or removes that target the hook raises at
startup instead of silently degrading, which is the signal to revisit it after
an upstream sync.

Order matters for the log patcher: ``core`` dumps the platform configuration
(proxy URLs included) at import time, so ``install_log_patcher()`` must run
before anything imports ``core``. It only depends on ``log``.
"""

import re
from typing import Any, cast

_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.\-]*://)[^/@\s\"'<>]+:[^/@\s\"'<>]+@", re.IGNORECASE)
_installed: set[str] = set()


class HookTargetMissing(RuntimeError):
    """Upstream no longer exposes what a fork hook needs; review ``ext/hooks.py`` after syncing."""


def mask_credentials(message: str) -> str:
    """Hide ``scheme://user:password@`` in any text; hosts and ports stay visible."""
    return _CREDENTIALS.sub(r"\1***@", message)


def _patch_record(record: Any) -> None:
    record["message"] = mask_credentials(record["message"])


def install_log_patcher() -> None:
    """Mask proxy credentials in every loguru line, including upstream's config dump at import."""
    if "log" in _installed:
        return
    from log import logger

    logger.configure(patcher=_patch_record)
    _installed.add("log")


def install_parsehub_compat() -> None:
    if "parsehub" in _installed:
        return
    from parsehub.types.result import ParseResult

    from ext import parsehub_compat
    from services.parser import ParseService

    for name in ("parse", "get_raw_url"):
        if not callable(getattr(ParseService, name, None)):
            raise HookTargetMissing(f"services.parser.ParseService.{name}")
    if not callable(getattr(ParseResult, "download", None)):
        raise HookTargetMissing("parsehub.types.result.ParseResult.download")
    ParseService.parse = parsehub_compat.parse  # type: ignore[method-assign]
    ParseService.get_raw_url = parsehub_compat.get_raw_url  # type: ignore[method-assign]
    ParseResult.download = parsehub_compat.bounded_download(ParseResult.download)
    _installed.add("parsehub")


def install_douyin_quality() -> None:
    """Wrap ``ParseService.parse`` so Douyin results get the explicit best-quality play URL."""
    if "douyin" in _installed:
        return
    install_parsehub_compat()
    from parsehub.parsers.parser.douyin import DouyinParseResult
    from parsehub.provider_api.douyin import DouyinMobileCrawler, DouyinWebCrawler

    from core import pl_cfg
    from ext import douyin_quality
    from services.parser import ParseService

    original = getattr(ParseService, "parse", None)
    if original is None:
        raise HookTargetMissing("services.parser.ParseService.parse")

    for crawler, source in ((DouyinWebCrawler, "web"), (DouyinMobileCrawler, "mobile")):
        target = getattr(crawler, "parse", None)
        if not callable(target):
            raise HookTargetMissing(f"{crawler.__name__}.parse")
        crawler.parse = douyin_quality.crawler_observer(target, source)
    download = getattr(DouyinParseResult, "_do_download", None)
    if not callable(download):
        raise HookTargetMissing("DouyinParseResult._do_download")
    DouyinParseResult._do_download = douyin_quality.download_verifier(download)

    async def parse(self: ParseService, url: str) -> Any:
        platform = self.parser.get_platform(url)
        if platform is None or platform.id != "douyin":
            return await original(self, url)
        token = douyin_quality.begin_quality(url)
        try:
            result = await original(self, url)
            await douyin_quality.upgrade_douyin_video(result, url, pl_cfg.roll_parser_proxy(platform.id))
            return result
        finally:
            douyin_quality.end_quality(token)

    ParseService.parse = parse  # type: ignore[method-assign]
    _installed.add("douyin")


def install_youtube_client() -> None:
    """Use mweb for parsing and the same 1080p format policy for parsing and downloading."""
    if "youtube" in _installed:
        return
    from parsehub.parsers.parser.youtube import YtbParse, YtbVideoParseResult

    from ext import youtube_quality

    original = getattr(YtbParse, "cli_args", None)
    if not isinstance(original, property) or original.fget is None:
        raise HookTargetMissing("parsehub.parsers.parser.youtube.YtbParse.cli_args")
    fget = original.fget
    download = getattr(YtbVideoParseResult, "cli_args", None)
    if not isinstance(download, property) or download.fget is None:
        raise HookTargetMissing("parsehub.parsers.parser.youtube.YtbVideoParseResult.cli_args")
    download_fget = download.fget

    def cli_args(self: Any) -> list[str]:
        return youtube_quality.extend_cli_args(fget(self))

    def download_args(self: Any) -> list[str]:
        return youtube_quality.download_cli_args(download_fget(self))

    # These replace class descriptors at runtime, rather than setting an instance property.
    cast(Any, YtbParse).cli_args = property(cli_args)
    cast(Any, YtbVideoParseResult).cli_args = property(download_args)
    _installed.add("youtube")


def install_twitter_quality() -> None:
    if "twitter" in _installed:
        return
    from parsehub.provider_api.twitter import ArticleRenderer, Twitter

    from ext.twitter_quality import best_mp4_url, prefer_mp4_variants

    original = getattr(Twitter, "parse", None)
    if not callable(original) or not callable(getattr(ArticleRenderer, "_best_mp4_url", None)):
        raise HookTargetMissing("parsehub.provider_api.twitter MP4 selectors")
    Twitter.parse = prefer_mp4_variants(original)
    ArticleRenderer._best_mp4_url = staticmethod(best_mp4_url)
    _installed.add("twitter")


def install_xhs_compat() -> None:
    """Handle optional/renamed codec groups without replacing XHS request or cookie handling."""
    if "xhs" in _installed:
        return
    from parsehub.provider_api.xhs import XHSAPI

    from ext.xhs_compat import select_stream

    target = "_XHSAPI__select_stream"
    if not callable(getattr(XHSAPI, target, None)):
        raise HookTargetMissing("parsehub.provider_api.xhs.XHSAPI.__select_stream")
    setattr(XHSAPI, target, staticmethod(select_stream))
    _installed.add("xhs")


def install_instagram_quality() -> None:
    if "instagram" in _installed:
        return
    import parsehub.parsers.parser.instagram as parser
    import parsehub.provider_api.instagram as provider

    from ext import instagram_quality

    targets = (
        (provider, "_v1_media", instagram_quality.v1_media),
        (provider, "_graph_media", instagram_quality.graph_media),
        (provider.InstagramAPI, "_fill_missing_video_fields", instagram_quality.fill_missing),
        (parser.InstagramParser, "_do_parse", instagram_quality.parse),
        (parser, "InstagramYtParse", instagram_quality.AuthenticatedYtParser),
    )
    for target, name, _replacement in targets:
        if not callable(getattr(target, name, None)):
            raise HookTargetMissing(f"Instagram.{name}")
    for target, name, replacement in targets:
        setattr(target, name, replacement)
    _installed.add("instagram")


def install_content_compat() -> None:
    if "content" in _installed:
        return
    from urllib.parse import urljoin, urlsplit

    import httpx
    from parsehub.errors import ParseError
    from parsehub.parsers.base.base import BaseParser
    from parsehub.parsers.parser.threads import ThreadsParser
    from parsehub.parsers.parser.twitter import TwitterParser
    from parsehub.parsers.parser.weixin import WXParser
    from parsehub.parsers.parser.youtube import YtbParse
    from parsehub.types.result import ParseResult
    from parsehub.utils.helpers import match_url

    from ext.content_hooks import provenance_wrapper
    from ext.platform_links import extra_match, normalize, valid_host

    original_match = BaseParser.match.__func__

    def match(cls: Any, text: str) -> bool:
        url = match_url(text)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        platform = cls.__platform__.id
        return valid_host(url, platform) and (extra_match(url, platform) or original_match(cls, normalize(url)))

    cast(Any, BaseParser).match = classmethod(match)
    WXParser.__reserved_parameters__ = [*WXParser.__reserved_parameters__, "__biz", "mid", "idx", "sn", "chksm"]
    original_threads = ThreadsParser.get_raw_url

    async def threads_url(self: Any, url: str, **kwargs: Any) -> str:
        return str(await original_threads(self, normalize(url), **kwargs))

    ThreadsParser.get_raw_url = threads_url
    original_twitter = TwitterParser.get_raw_url

    async def twitter_url(self: Any, url: str, **kwargs: Any) -> str:
        if urlsplit(url).hostname == "t.co":
            async with httpx.AsyncClient(proxy=self.proxy, timeout=30, follow_redirects=False) as client:
                for _ in range(10):
                    if not valid_host(url, "twitter"):
                        raise ParseError("不支持的链接目标")
                    response = await client.get(url)
                    if response.is_redirect:
                        url = urljoin(url, response.headers["location"])
                        continue
                    break
                else:
                    raise ParseError("短链接重定向过多")
            if not valid_host(url, "twitter") or "/status/" not in url:
                raise ParseError("请提供原始推文链接")
        return str(await original_twitter(self, url, **kwargs))

    TwitterParser.get_raw_url = twitter_url
    original_info = YtbParse._extract_info

    async def video_info(self: Any, url: str) -> Any:
        info = await original_info(self, url)
        # Finished recordings are finite downloads. Keep live/upcoming streams rejected upstream.
        if info.get("live_status") == "was_live" and not info.get("is_live"):
            info = {**info, "live_status": "not_live"}
        return info

    YtbParse._extract_info = video_info
    ParseResult._do_download = provenance_wrapper(ParseResult._do_download)
    _installed.add("content")


def install_media_processing() -> None:
    """Point the upstream pipeline's media step at the fork preparation."""
    if "media" in _installed:
        return
    import services.media
    import services.pipeline
    from ext.media.processing import process_media_files

    for module in (services.pipeline, services.media):
        if not hasattr(module, "process_media_files"):
            raise HookTargetMissing(f"{module.__name__}.process_media_files")
        cast(Any, module).process_media_files = process_media_files
    _installed.add("media")


def install_bot_handlers() -> None:
    """Replace the upstream parse dispatcher with the fork one; the ``parse`` entry handler is kept."""
    if "bot" in _installed:
        return
    import plugins.parse.handlers
    from ext.bot.handlers import handle_parse

    if not hasattr(plugins.parse.handlers, "handle_parse"):
        raise HookTargetMissing("plugins.parse.handlers.handle_parse")
    plugins.parse.handlers.handle_parse = handle_parse
    _installed.add("bot")


def install(*, bot: bool = False) -> None:
    """Install every hook shared by the Worker and the Bot; ``bot=True`` adds the Bot dispatcher swap."""
    install_log_patcher()
    install_parsehub_compat()
    install_douyin_quality()
    install_youtube_client()
    install_twitter_quality()
    install_xhs_compat()
    install_instagram_quality()
    install_content_compat()
    install_media_processing()
    if bot:
        install_bot_handlers()


def installed() -> frozenset[str]:
    return frozenset(_installed)
