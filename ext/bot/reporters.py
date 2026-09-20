"""Status reporter hardening: tolerate deleted status messages, wrapped ``Forbidden`` errors, tracked tasks."""

import asyncio
from typing import Any

from pyrogram.errors import FloodWait, Forbidden, SlowmodeWait
from pyrogram.types import LinkPreviewOptions

from core import bs
from ext.bot.sender import ForkMessageSender, SendFailed, track_background
from log import logger
from plugins.helpers import format_label
from plugins.parse.reporters import InlineStatusReporter, MessageStatusReporter

logger = logger.bind(name="ParseReporter")


class ForkMessageStatusReporter(MessageStatusReporter):
    async def report_error(self, stage: str, error: Exception) -> None:
        if self._config.hide_error:
            return

        t = format_label(self._t(f"{stage}错误:"))
        text = self._t(f"{t} \n```\n{error}```")
        if bs.demo_mode:
            text += self._t("\n\n<b>问题反馈: @MisakaSisters</b>")
        await self._edit_text(text, link_preview_options=LinkPreviewOptions(is_disabled=True))
        if self._config.keep_error_log:
            return

        async def fn() -> None:
            await asyncio.sleep(15)
            await self.dismiss()

        track_background(asyncio.get_running_loop().create_task(fn()))

    async def dismiss(self) -> None:
        if self._msg:
            try:
                await self._msg.delete()
            except Exception as e:
                logger.debug(f"状态消息删除失败: {type(e).__name__}")
            self._msg = None

    async def _edit_text(self, text: str, **kwargs: Any) -> None:
        try:
            if self._msg is None:
                self._msg = await ForkMessageSender(self._cli, self._user_msg, self._config).text(text, **kwargs)
            elif self._msg.text != text:
                await self._msg.edit_text(text, **kwargs)
        except (FloodWait, SlowmodeWait):
            pass
        except Forbidden as e:
            logger.warning(f"状态消息发送失败, Bot 无权限: {e}")
            if self._on_forbidden:
                await self._on_forbidden(self._user_msg, self._config)
        except SendFailed as e:
            # ForkMessageSender wraps the original error; a wrapped Forbidden still means no permission.
            if not isinstance(e.__cause__, Forbidden):
                raise
            logger.warning(f"状态消息发送失败, Bot 无权限: {e.__cause__}")
            if self._on_forbidden:
                await self._on_forbidden(self._user_msg, self._config)


class ForkInlineStatusReporter(InlineStatusReporter):
    async def report_error(self, stage: str, error: Exception) -> None:
        if self._user_config.hide_error:
            return

        text = self._t(f"{format_label(f'{stage}错误:')} \n```\n{error}```")
        if bs.demo_mode:
            text += self._t("\n\n<b>问题反馈: @MisakaSisters</b>")
        await self._edit_inline_text(
            inline_message_id=self._mid, text=text, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        if self._user_config.keep_error_log:
            return

        async def fn() -> None:
            await asyncio.sleep(15)
            await self._edit_inline_text(
                inline_message_id=self._mid,
                text=self._caption,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

        track_background(asyncio.get_running_loop().create_task(fn()))


__all__ = ["ForkInlineStatusReporter", "ForkMessageStatusReporter", "track_background"]
