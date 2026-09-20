"""Fork parse dispatcher installed over ``plugins.parse.handlers.handle_parse`` by ``ext.hooks``.

The upstream ``parse`` message handler (command parsing, URL extraction,
rate-limit notices, auto-delete) is kept verbatim; only the per-URL pipeline
result dispatch is replaced so every mode goes through the shared envelope core.
"""

from dataclasses import replace

from parsehub.types import RichTextParseResult
from pyrogram.types import InputRichMessage

from ext.bot.captions import build_caption, create_richtext_telegraph
from ext.bot.reporters import ForkMessageStatusReporter
from ext.bot.sender import ForkMessageSender, send_cached, send_media, send_raw, send_zip
from ext.cache import CacheEntry, CacheParseResult, persistent_cache
from log import logger
from plugins.parse.context import ParseOptions, ParseRequest
from plugins.parse.reporters import disable_progress_on_report_forbidden
from repo.settings import ParseMode
from services import ParsePipeline, ParseService
from services.cache import parse_cache
from utils.rate_limit import parse_rate_limit

logger = logger.bind(name="Parse")


def _get_parse_user_id(req: ParseRequest) -> int | None:
    return req.chat_id


@parse_rate_limit(_get_parse_user_id)
async def handle_parse(req: ParseRequest) -> bool:
    options = ParseOptions.from_mode(req.mode, bypass_cache=req.bypass_cache)
    logger.info(f"收到解析请求: url={req.url}, chat_id={req.chat_id}, msg_id={req.msg.id}, mode={req.mode}")
    if req.bypass_cache:
        logger.debug("bypass_cache=True 绕过缓存")

    reporter = ForkMessageStatusReporter(
        req.cli, req.msg, t=req.t_, config=req.config, on_forbidden=disable_progress_on_report_forbidden
    )
    sender = ForkMessageSender(req.cli, req.msg, req.config)
    try:
        raw_url = await ParseService().get_raw_url(req.url)
    except Exception as e:
        await reporter.report_error(req.t_("获取原始链接"), e)
        return False

    if options.use_caching and not req.bypass_cache and (cached := await persistent_cache.get(raw_url)):
        logger.debug("file_id 缓存命中, 直接发送")
        try:
            await send_cached(sender, cached, raw_url, custom_content=req.custom_content)
        except Exception as e:
            logger.exception(e)
            logger.error("从缓存发送失败, 以上为错误信息")
            return False
        return True

    cached_parse_result = None if req.bypass_cache else await parse_cache.get(raw_url)
    with ParsePipeline(
        req.url,
        raw_url,
        reporter,
        parse_result=cached_parse_result,
        singleflight=options.singleflight,
        skip_media_processing=options.skip_media_processing,
        # The shared envelope must contain every final asset, including GIF galleries and articles.
        gif_only_skip_download_count_threshold=0,
        richtext_skip_download=False,
        save_metadata=options.save_metadata,
        t=req.t_,
    ) as pipeline:
        if (result := await pipeline.run()) is None:
            if pipeline.waited:
                logger.debug("Singleflight 等待完成, 重新检查缓存")
                if not req.bypass_cache and (cached := await persistent_cache.get(raw_url)):
                    try:
                        await send_cached(sender, cached, raw_url, custom_content=req.custom_content)
                    except Exception as e:
                        logger.exception(e)
                        logger.error("从缓存发送失败, 以上为错误信息")
                        return False
                    return True
                return await handle_parse(replace(req, delete_share_url_msg=False))
            logger.debug("Pipeline 返回 None, 跳过后续处理")
            return False

        parse_result = result.parse_result
        await parse_cache.set(raw_url, parse_result)

        if isinstance(parse_result, RichTextParseResult) and req.mode == ParseMode.PREVIEW:
            await sender.typing()
            if req.config.rich_mode:
                caption = build_caption(parse_result, config=req.config, custom_content=req.custom_content, rich=True)
                await sender.rich_message(InputRichMessage(markdown=caption))
                await persistent_cache.set(
                    raw_url,
                    CacheEntry(
                        parse_result=CacheParseResult(title=parse_result.title, content=parse_result.markdown_content),
                        rich=True,
                    ),
                )
            else:
                telegraph_url = await create_richtext_telegraph(req.cli, parse_result)
                caption = build_caption(
                    parse_result, telegraph_url, config=req.config, custom_content=req.custom_content
                )
                await sender.text_with_preview_above(caption)
                await persistent_cache.set(
                    raw_url,
                    CacheEntry(
                        parse_result=CacheParseResult(title=parse_result.title, content=parse_result.content),
                        telegraph_url=telegraph_url,
                    ),
                )
            await reporter.dismiss()
            return True

        if req.mode == ParseMode.RAW:
            return await send_raw(sender, result, reporter, _t=req.t_, custom_content=req.custom_content)
        if req.mode == ParseMode.ZIP:
            return await send_zip(sender, result, reporter, _t=req.t_, custom_content=req.custom_content)

        logger.debug(f"开始上传媒体: media_count={len(result.processed_list)}")
        await reporter.report(req.t_("上 传 中..."))
        try:
            media_cache_entry = await send_media(
                sender, parse_result, result.processed_list, _t=req.t_, custom_content=req.custom_content
            )
            if media_cache_entry:
                await persistent_cache.set(raw_url, media_cache_entry)
            await reporter.dismiss()
            return True
        except Exception as e:
            logger.exception(e)
            logger.error("上传失败, 以上为错误信息")
            await reporter.report_error(req.t_("上传"), e)
            return False
