"""Standalone inline ingress; final media delivery uses the shared envelope core."""

from typing import Any

from pyrogram import Client
from pyrogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedAnimation,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InputRichMessage,
    InputRichMessageContent,
    InputTextMessageContent,
    LinkPreviewOptions,
)

from db import get_session
from delivery.assets import cache_assets, cached_assets, pipeline_assets_async
from delivery.models import DeliveryEnvelope, Destination
from i18n import t_
from log import logger
from plugins.filters import platform_filter
from plugins.helpers import build_caption_by_str, build_start_text
from plugins.parse.delivery import deliver
from plugins.parse.reporters import InlineStatusReporter
from repo.settings import SettingsConfig
from services import CacheEntry, CacheParseResult, ParseService, SettingsService, UserService
from services.cache import CacheMediaType, persistent_cache
from services.pipeline import ParsePipeline
from utils.helpers import with_request_id


@Client.on_inline_query(~platform_filter(False))
async def inline_parse_tip(_: Client, query: InlineQuery) -> None:
    async with get_session() as session:
        lang = await UserService(session).get_lang(query.from_user.id)
    _t = t_[lang]
    await query.answer([InlineQueryResultArticle(
        id="help", title=_t("聚合解析"), description=_t("请在聊天框输入链接"),
        input_message_content=InputTextMessageContent(
            build_start_text()[lang], link_preview_options=LinkPreviewOptions(is_disabled=True),
        ),
    )], cache_time=1)


def _raw_url_result(raw_url: str, _t: Any) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id="raw_url",
        title=_t("原始链接"),
        description=raw_url,
        input_message_content=InputTextMessageContent(
            raw_url, link_preview_options=LinkPreviewOptions(is_disabled=True),
        ),
    )


def _placeholder_result(raw_url: str, _t: Any) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id="envelope",
        title=_t("解析完整内容"),
        description=_t("图片、视频和实况预览将在一条消息中展示"),
        input_message_content=InputTextMessageContent(
            _t("正在准备解析结果…"), link_preview_options=LinkPreviewOptions(is_disabled=True),
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_t("查看原文"), url=raw_url)]]),
    )


def _cached_results(entry: CacheEntry, raw_url: str, lang: str, config: SettingsConfig) -> list[Any]:
    _t = t_[lang]
    title = entry.parse_result.title or "-"
    content = entry.parse_result.content
    results: list[Any] = []
    if config.enable_inline_raw_url:
        results.append(_raw_url_result(raw_url, _t))
    if entry.rich:
        caption = build_caption_by_str(
            title, content, raw_url, hide_source=config.hide_source,
            hide_title=config.hide_title, hide_desc=config.hide_desc, rich=True,
        )
        results.append(InlineQueryResultArticle(
            id="cached_rich", title=title, description=content,
            input_message_content=InputRichMessageContent(InputRichMessage(markdown=caption)),
        ))
        return results
    caption = build_caption_by_str(
        title, content, raw_url, entry.telegraph_url,
        hide_source=config.hide_source, hide_title=config.hide_title, hide_desc=config.hide_desc,
    )
    if entry.telegraph_url or not entry.media:
        results.append(InlineQueryResultArticle(
            id="cached_article", title=title, description=content,
            input_message_content=InputTextMessageContent(
                caption, link_preview_options=LinkPreviewOptions(show_above_text=bool(entry.telegraph_url)),
            ),
        ))
        return results
    for index, media in enumerate(entry.media):
        result_id = f"cached_{index}"
        match media.type:
            case CacheMediaType.PHOTO:
                results.append(InlineQueryResultCachedPhoto(
                    id=result_id, photo_file_id=media.file_id, title=title, caption=caption, description=content,
                ))
            case CacheMediaType.VIDEO:
                results.append(InlineQueryResultCachedVideo(
                    id=result_id, video_file_id=media.file_id, title=title, caption=caption, description=content,
                ))
            case CacheMediaType.ANIMATION:
                results.append(InlineQueryResultCachedAnimation(id=result_id, animation_file_id=media.file_id,
                                                                title=title, caption=caption))
            case CacheMediaType.DOCUMENT:
                results.append(InlineQueryResultCachedDocument(id=result_id, document_file_id=media.file_id,
                                                               title=title, caption=caption, description=content))
            case _:
                # Live pairs and uncommon cached types use the shared Rich envelope path.
                results.append(_placeholder_result(raw_url, _t))
                break
    return results or [_placeholder_result(raw_url, _t)]


@Client.on_inline_query(platform_filter(False))
@with_request_id
async def call_inline_parse(cli: Client, query: InlineQuery) -> None:
    async with get_session() as session:
        lang = await UserService(session).get_lang(query.from_user.id)
        config = await SettingsService(session).get_config_by_user(query.from_user.id)
    _t = t_[lang]
    try:
        raw_url = await ParseService().get_raw_url(query.query)
        if cached := await persistent_cache.get(raw_url):
            await query.answer(_cached_results(cached, raw_url, lang, config)[:50], cache_time=60)
            return
        results = []
        if config.enable_inline_raw_url:
            results.append(_raw_url_result(raw_url, _t))
        results.append(_placeholder_result(raw_url, _t))
        await query.answer(results, cache_time=0)
    except Exception as error:
        logger.warning("Inline query failed: error_type=%s", type(error).__name__)
        await query.answer([InlineQueryResultArticle(
            id="error", title=_t("解析失败"), description=_t("链接暂时无法解析"),
            input_message_content=InputTextMessageContent(
                _t("链接暂时无法解析"), link_preview_options=LinkPreviewOptions(is_disabled=True),
            ),
        )], cache_time=1)


def _inline_envelope(dest: Destination, parsed: Any, assets: tuple[Any, ...], config: SettingsConfig,
                     reading_url: str = "") -> DeliveryEnvelope:
    source = str(getattr(parsed, "raw_url", "") or "")
    return DeliveryEnvelope(
        dest,
        source,
        "" if config.hide_title else str(getattr(parsed, "title", "") or ""),
        "" if config.hide_desc else str(getattr(parsed, "content", "") or ""),
        "" if config.hide_source else source,
        reading_url,
        assets,
        rich_preview=True,
    )


@Client.on_chosen_inline_result()
@with_request_id
async def inline_result_download(cli: Client, chosen: ChosenInlineResult) -> None:
    if chosen.result_id != "envelope" or not chosen.inline_message_id:
        return
    async with get_session() as session:
        lang = await UserService(session).get_lang(chosen.from_user.id)
        config = await SettingsService(session).get_config_by_user(chosen.from_user.id)
    reporter = InlineStatusReporter(cli, chosen.inline_message_id, "", t=t_[lang], user_config=config)
    try:
        url = await ParseService().get_raw_url(chosen.query)
        dest = Destination(surface="inline", inline_message_id=chosen.inline_message_id)
        cached = await persistent_cache.get(url)
        if cached:
            parsed = type("CachedParse", (), {
                "raw_url": url, "title": cached.parse_result.title, "content": cached.parse_result.content,
            })()
            envelope = _inline_envelope(dest, parsed, cached_assets(cached.media or []), config,
                                        cached.telegraph_url or "")
            await deliver(cli, envelope, chosen.inline_message_id)
            return
        with ParsePipeline(chosen.query, url, reporter, singleflight=False,
                           richtext_skip_download=False, t=t_[lang]) as pipeline:
            result = await pipeline.run()
            if result is None:
                return
            assets = await pipeline_assets_async(result.processed_list, url)
            parsed = result.parse_result
            envelope = _inline_envelope(dest, parsed, assets, config)
            sent = await deliver(cli, envelope, chosen.inline_message_id)
            if not assets or sent.assets_cached:
                await persistent_cache.set(url, CacheEntry(
                    parse_result=CacheParseResult(title=parsed.title, content=parsed.content),
                    media=cache_assets(assets, sent.assets_cached),
                ))
    except Exception as error:
        logger.warning("Inline delivery failed: error_type=%s", type(error).__name__)
        try:
            await reporter.report_error(t_[lang]("上传"), ValueError("结果无法在当前消息中交付"))
        except Exception:
            logger.debug("Inline error report failed: error_type=%s", type(error).__name__)
