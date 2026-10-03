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
from typing import Any

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


def install_douyin_quality() -> None:
    """Wrap ``ParseService.parse`` so Douyin results get the explicit best-quality play URL."""
    if "douyin" in _installed:
        return
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
    """Extend ``YtbParse.cli_args`` (parse stage only) so Premium sessions get mweb formats, not SABR-only 360p."""
    if "youtube" in _installed:
        return
    from parsehub.parsers.parser.youtube import YtbParse

    from ext import youtube_quality

    original = getattr(YtbParse, "cli_args", None)
    if not isinstance(original, property) or original.fget is None:
        raise HookTargetMissing("parsehub.parsers.parser.youtube.YtbParse.cli_args")
    fget = original.fget

    def cli_args(self: Any) -> list[str]:
        return youtube_quality.extend_cli_args(fget(self))

    YtbParse.cli_args = property(cli_args)
    _installed.add("youtube")


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
        module.process_media_files = process_media_files  # type: ignore[attr-defined]
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
    install_douyin_quality()
    install_youtube_client()
    install_media_processing()
    if bot:
        install_bot_handlers()


def installed() -> frozenset[str]:
    return frozenset(_installed)
