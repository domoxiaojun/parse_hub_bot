"""Standalone inline ingress; final media delivery uses the shared envelope core."""

from pyrogram import Client
from pyrogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
)

from db import get_session
from delivery.assets import cache_assets, cached_assets, pipeline_assets
from delivery.models import DeliveryEnvelope, DeliveryError, Destination
from i18n import t_
from log import logger
from plugins.filters import platform_filter
from plugins.helpers import build_start_text
from plugins.parse.delivery import deliver
from plugins.parse.reporters import InlineStatusReporter
from services import CacheEntry, CacheParseResult, ParseService, SettingsService, UserService
from services.cache import persistent_cache
from services.pipeline import ParsePipeline
from utils.helpers import with_request_id


@Client.on_inline_query(~platform_filter(False))
async def inline_parse_tip(_: Client, query: InlineQuery) -> None:
    await query.answer([InlineQueryResultArticle(
        id="help", title="聚合解析", description="请在聊天框输入链接",
        input_message_content=InputTextMessageContent(build_start_text()["zh-hans"]),
    )], cache_time=1)


@Client.on_inline_query(platform_filter(False))
@with_request_id
async def call_inline_parse(cli: Client, query: InlineQuery) -> None:
    # Query handling creates a placeholder only. Chosen-result handling owns final delivery.
    raw_url = await ParseService().get_raw_url(query.query)
    await query.answer([InlineQueryResultArticle(
        id="envelope", title="解析完整内容", description="图片、视频和实况预览将在一条消息中展示",
        input_message_content=InputTextMessageContent(
            "正在准备解析结果…", link_preview_options=LinkPreviewOptions(is_disabled=True),
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("查看原文", url=raw_url)]]),
    )], cache_time=0)


@Client.on_chosen_inline_result()
@with_request_id
async def inline_result_download(cli: Client, chosen: ChosenInlineResult) -> None:
    if chosen.result_id != "envelope" or not chosen.inline_message_id:
        return
    async with get_session() as session:
        lang = await UserService(session).get_lang(chosen.from_user.id)
        config = await SettingsService(session).get_config_by_user(chosen.from_user.id)
    reporter = InlineStatusReporter(cli, chosen.inline_message_id, "", t=t_[lang], user_config=config)
    url = await ParseService().get_raw_url(chosen.query)
    dest = Destination(surface="inline", inline_message_id=chosen.inline_message_id)
    cached = await persistent_cache.get(url)
    try:
        if cached:
            envelope = DeliveryEnvelope(dest, url, cached.parse_result.title, cached.parse_result.content,
                                        url, cached.telegraph_url or "", cached_assets(cached.media or []))
            await deliver(cli, envelope, chosen.inline_message_id)
            return
        with ParsePipeline(chosen.query, url, reporter, singleflight=False, t=t_[lang]) as pipeline:
            result = await pipeline.run()
            if result is None:
                return
            assets = pipeline_assets(result.processed_list, url)
            parsed = result.parse_result
            envelope = DeliveryEnvelope(dest, url, parsed.title or "", parsed.content or "", url, media=assets)
            sent = await deliver(cli, envelope, chosen.inline_message_id)
            if not assets or sent.assets_cached:
                await persistent_cache.set(url, CacheEntry(
                    parse_result=CacheParseResult(title=parsed.title, content=parsed.content),
                    media=cache_assets(assets, sent.assets_cached),
                ))
    except DeliveryError as error:
        logger.warning("Inline delivery failed: code={}", str(error))
        if str(error) != "delivery_unknown":
            await reporter.report_error(t_[lang]("上传"), ValueError("结果无法在当前消息中交付"))
