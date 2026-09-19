import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from types import SimpleNamespace
from typing import Any, cast

from easy_ai18n import PreLocaleSelector
from parsehub.types import AnyParseResult
from pyrogram import Client, enums
from pyrogram.errors import FloodWait, Forbidden, SlowmodeWait
from pyrogram.types import (
    InlineKeyboardMarkup as Ikm,
)
from pyrogram.types import (
    LinkPreviewOptions,
    Message,
)

from core import bs
from delivery.assets import cache_assets, cached_assets, pipeline_assets
from delivery.models import DeliveryEnvelope, Destination, MediaAsset, SendResult, asset_key
from log import logger
from plugins.parse.delivery import deliver
from repo.settings import SettingsConfig
from services import CacheEntry, CacheParseResult, PipelineResult, StatusReporter
from services.media import ProcessedMedia
from utils.helpers import pack_dir_to_tar_gz, to_list

logger = logger.bind(name="ParseSender")
MAX_RETRIES = 5


class SendFailed(RuntimeError):
    """Raised after retries; ``__cause__`` carries the last Telegram error."""


@dataclass(frozen=True, slots=True)
class MessageSender:
    cli: Client
    msg: Message
    config: SettingsConfig
    delete_after_seconds: float | None = None

    def delete_after(self, seconds: float | int | None) -> "MessageSender":
        if not seconds:
            return self
        return replace(self, delete_after_seconds=float(seconds))

    def _delete_later(self, sent: Message | Sequence[Message]) -> None:
        if self.delete_after_seconds is None:
            return

        messages = to_list(sent)

        async def fn() -> None:
            await asyncio.sleep(self.delete_after_seconds or 0)
            for message in messages:
                try:
                    await message.delete()
                except Exception as e:
                    logger.debug(
                        f"定时删除消息失败: chat_id={message.chat and message.chat.id}, msg_id={message.id}, error={e}"
                    )

        asyncio.get_running_loop().create_task(fn())

    async def _send_and_schedule_delete[T](self, send_coro_fn: Callable[[], Awaitable[T]]) -> T:
        sent = await self._send(send_coro_fn)
        if isinstance(sent, Message) or isinstance(sent, list):
            self._delete_later(sent)
        return sent

    @staticmethod
    async def _send[T](send_coro_fn: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(MAX_RETRIES):
            try:
                return await send_coro_fn()
            except (FloodWait, SlowmodeWait) as e:
                if attempt < MAX_RETRIES - 1:
                    wait_seconds = e.value if isinstance(e.value, int | float) else 0.5
                    logger.warning(f"{e.ID} 重试 ({attempt + 1}/{MAX_RETRIES})，等待 {wait_seconds}s")
                    await asyncio.sleep(float(wait_seconds))
                else:
                    raise
            except Forbidden as e:
                logger.warning(f"消息发送失败, Bot 无权限: {e}")
                raise SendFailed("消息发送失败") from e
            except Exception as e:
                logger.warning(f"消息发送失败: {type(e).__name__}: {e}")
                raise SendFailed("消息发送失败") from e
            await asyncio.sleep(0.5)
        raise SendFailed("消息发送失败")

    async def chat_action(self, action: enums.ChatAction) -> None:
        await self.msg.reply_chat_action(action)

    async def typing(self) -> None:
        await self.chat_action(enums.ChatAction.TYPING)

    async def upload_document(self) -> None:
        await self.chat_action(enums.ChatAction.UPLOAD_DOCUMENT)

    async def upload_photo(self) -> None:
        await self.chat_action(enums.ChatAction.UPLOAD_PHOTO)

    async def upload_video(self) -> None:
        await self.chat_action(enums.ChatAction.UPLOAD_VIDEO)

    async def text(
        self,
        text: str,
        *,
        link_preview_options: LinkPreviewOptions | None = None,
        reply_markup: Ikm | None = None,
    ) -> Message:
        return cast(
            Message,
            await self._send_and_schedule_delete(
                partial(
                    self.msg.reply if self.config.reply_msg else self.msg.answer,
                    text,
                    link_preview_options=link_preview_options,
                    reply_markup=reply_markup,
                )
            ),
        )

    async def text_no_preview(self, text: str, *, reply_markup: Ikm | None = None) -> Message:
        return await self.text(
            text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=reply_markup,
        )


def envelope_for(sender: MessageSender, parse_result: Any, assets: tuple[MediaAsset, ...],
                 custom_content: str = "", reading_url: str = "") -> DeliveryEnvelope:
    if sender.msg.chat is None:
        raise ValueError("missing_destination")
    dest = Destination(chat_id=sender.msg.chat.id, thread_id=sender.msg.message_thread_id,
                       reply_to=sender.msg.id if sender.config.reply_msg else None)
    source = str(getattr(parse_result, "raw_url", "") or "")
    return DeliveryEnvelope(dest, source,
        "" if sender.config.hide_title else str(parse_result.title or ""),
        custom_content or ("" if sender.config.hide_desc else str(parse_result.content or "")),
        "" if sender.config.hide_source else source, reading_url, assets)


async def send_content(sender: MessageSender, envelope: DeliveryEnvelope, mode: str = "preview") -> SendResult:
    request_id = f"{envelope.dest.chat_id}:{sender.msg.id}:{mode}:{envelope.source_id}"
    result = await deliver(sender.cli, envelope, request_id)
    return result


async def send_raw(sender: MessageSender, result: PipelineResult, reporter: StatusReporter, *,
                   _t: PreLocaleSelector, custom_content: str = "") -> None:
    try:
        assets = pipeline_assets(result.processed_list, str(result.parse_result.raw_url), raw=True)
        await send_content(sender, envelope_for(sender, result.parse_result, assets, custom_content), "raw")
        await reporter.dismiss()
    finally:
        result.cleanup()


async def send_zip(sender: MessageSender, result: PipelineResult, reporter: StatusReporter, *,
                   _t: PreLocaleSelector, custom_content: str = "") -> None:
    if result.output_dir is None:
        raise ValueError("missing_archive_directory")
    archive = await asyncio.to_thread(pack_dir_to_tar_gz, result.output_dir)
    try:
        asset = MediaAsset(asset_key(str(result.parse_result.raw_url), 0, [archive]), "document", archive,
                           size=archive.stat().st_size)
        await send_content(sender, envelope_for(sender, result.parse_result, (asset,), custom_content), "zip")
        await reporter.dismiss()
    finally:
        result.cleanup()
        if not bs.debug_skip_cleanup:
            archive.unlink(missing_ok=True)


async def send_media(sender: MessageSender, parse_result: AnyParseResult,
                     processed_list: list[ProcessedMedia], caption: str, *, _t: PreLocaleSelector,
                     custom_content: str = "") -> CacheEntry | None:
    assets = pipeline_assets(processed_list, str(parse_result.raw_url))
    envelope = envelope_for(sender, parse_result, assets, custom_content)
    sent = await send_content(sender, envelope)
    if assets and not sent.assets_cached:
        return None  # An already-confirmed replay must not overwrite its asset cache.
    return CacheEntry(parse_result=CacheParseResult(title=parse_result.title, content=parse_result.content),
                      media=cache_assets(assets, sent.assets_cached))


async def send_cached(sender: MessageSender, entry: CacheEntry, url: str, *, custom_content: str = "") -> None:
    parsed = SimpleNamespace(raw_url=url, title=entry.parse_result.title, content=entry.parse_result.content)
    envelope = envelope_for(sender, parsed, cached_assets(entry.media or []), custom_content, entry.telegraph_url or "")
    await send_content(sender, envelope)
