"""Outbound-only Telegram delivery with durable per-frame acknowledgement."""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pyrogram import Client, types
from pyrogram.errors import FloodWait, MessageNotModified, RPCError, SlowmodeWait

from worker.config import WorkerSettings
from worker.models import InlineDelivery, MessageDelivery
from worker.reading_delivery import prepare_reading_frame
from worker.rich_delivery import (
    DeliveryFrame,
    LivePhotoFrame,
    bounded,
    build_frames,
    evidence_for,
    live_photo_fallback_frame,
)
from worker.sender_runtime import SenderRuntime
from worker.store import Store

logger = logging.getLogger("parsehub.worker")


def _error_fields(error: BaseException) -> str:
    """Log only the error class and Telegram's RPC identifier, never message text."""
    rpc = getattr(error, "ID", None) if isinstance(error, RPCError) else None
    return f"error_type={type(error).__name__} rpc={rpc or 'none'}"


def create_sender_client(settings: WorkerSettings) -> Client:
    # Persist authorization across restarts without sharing bot.py's session database.
    return Client(settings.worker_sender_session_name, api_id=settings.api_id,
                  api_hash=settings.api_hash.get_secret_value(), bot_token=settings.bot_token.get_secret_value(),
                  workdir=settings.sessions_path, in_memory=False, no_updates=True, plugins=None,
                  sleep_threshold=0, proxy=settings.bot_proxy)


class TelegramSender:
    def __init__(self, client: Any, runtime: SenderRuntime | None = None):
        self.client = client
        self.runtime = runtime

    @property
    def ready(self) -> bool:
        return self.runtime.ready if self.runtime else True

    def health(self) -> dict[str, Any]:
        return self.runtime.health() if self.runtime else {"deliveryReady": True, "senderState": "ready"}

    async def deliver(
        self, job: dict[str, Any], target: MessageDelivery | InlineDelivery, store: Store,
        checkpoint: Callable[[], None],
    ) -> None:
        receipt = job["delivery"]

        def resolve(lease_id: str, media_id: str) -> Path:
            media = store.media_file(lease_id, media_id)
            if media is None:
                raise ValueError("media_expired")
            return media[0]

        try:
            frames = build_frames(job["results"], resolve, inline=target.surface != "message")
        except ValueError as error:
            overflow = str(error) in {"inline_media_limit", "inline_rich_block_limit", "inline_rich_text_limit"}
            if isinstance(target, InlineDelivery) and overflow:
                try:
                    frames = [await prepare_reading_frame(job["results"], receipt, checkpoint, resolve)]
                except asyncio.CancelledError:
                    receipt.update(status="cancelled", inFlight=False)
                    checkpoint()
                    raise
                except Exception as reading_error:
                    media_overflow = (isinstance(reading_error, ValueError)
                                      and str(reading_error) == "inline_media_overflow")
                    receipt.update(status="failed", inFlight=False, error={
                        "code": "delivery_limits" if media_overflow else "reading_page_failed",
                        "message": "内联媒体结果超限，请用普通消息重新解析。" if media_overflow
                        else "完整阅读版生成失败，请用普通消息重新解析。",
                    })
                    checkpoint()
                    return
            else:
                receipt.update(status="failed", inFlight=False, error={
                    "code": "delivery_limits", "message": "解析结果无法在当前消息中交付",
                })
                checkpoint()
                return
        receipt["totalFrames"] = len(frames)
        delivered_indices: list[int] = []
        previous_message_id = target.replyToMessageId if isinstance(target, MessageDelivery) else None
        for index, frame in enumerate(frames):
            receipt.update(status="sending", frameIndex=index, inFlight=True)
            checkpoint()  # Must succeed before starting a visible side effect.
            try:
                try:
                    response = await self._send_with_retry(target, frame, previous_message_id, receipt, checkpoint)
                except RPCError as error:
                    if not (isinstance(frame, LivePhotoFrame) and isinstance(target, MessageDelivery)):
                        raise
                    # Telegram rejected the native live photo before anything was sent:
                    # deliver the same photo and video as a Rich frame instead of losing the item.
                    logger.warning("event=delivery.live_photo_fallback job=%s frame=%s %s",
                                   job["id"], index, _error_fields(error))
                    frame = live_photo_fallback_frame(frame)
                    response = await self._send_with_retry(target, frame, previous_message_id, receipt, checkpoint)
                if isinstance(target, MessageDelivery):
                    assert response is not None
                    receipt["messageIds"].append(response.id)
                    previous_message_id = response.id
                else:
                    receipt.update(inlineMessageId=target.inlineMessageId, confirmed=True)
                delivered_indices.extend(frame.completed_result_indices)
                receipt.update(inFlight=False, completedFrames=index + 1)
                receipt["mediaCount"] = receipt.get("mediaCount", 0) + frame.media_count
                job["evidence"] = evidence_for(job["results"], delivered_indices, receipt["mediaCount"])
                receipt["text"] = bounded(
                    (receipt.get("text", "") + "\n\n" + frame.text).strip(), 80_000,
                )
                checkpoint()
            except asyncio.CancelledError:
                receipt.update(status="unknown" if receipt.get("inFlight") else
                               "partial" if receipt.get("completedFrames") else "cancelled")
                checkpoint()
                raise
            except Exception as error:
                # A known RPC rejection did not send this frame. Transport failures are ambiguous.
                known = isinstance(error, RPCError)
                status = ("partial" if receipt.get("completedFrames") else "failed") if known else "unknown"
                logger.warning("event=delivery.failed job=%s frame=%s/%s kind=%s status=%s %s",
                               job["id"], index, len(frames),
                               "live_photo" if isinstance(frame, LivePhotoFrame) else "rich", status,
                               _error_fields(error))
                receipt.update(status=status,
                               inFlight=not known, error={"code": "telegram_rejected" if known else "delivery_unknown",
                                                          "message": "交付未完成，请检查已有消息后重试"})
                checkpoint()
                return
        degraded = any("error" in item or item.get("mediaFailureCount", 0) > 0 for item in job["results"])
        receipt.update(status="partial" if degraded else "sent", inFlight=False)
        checkpoint()

    async def _send(
        self, target: MessageDelivery | InlineDelivery, frame: DeliveryFrame, previous_message_id: int | None,
    ) -> Any:
        if isinstance(target, MessageDelivery):
            reply = types.ReplyParameters(message_id=previous_message_id) if previous_message_id else None
            if isinstance(frame, LivePhotoFrame):
                response = await self.client.send_live_photo(
                    chat_id=int(target.chatId), live_photo=frame.video, photo=frame.photo,
                    width=frame.width, height=frame.height,
                    reply_parameters=reply, message_thread_id=target.messageThreadId,
                )
            else:
                response = await self.client.send_rich_message(
                    chat_id=int(target.chatId), rich_message=frame.payload,
                    reply_parameters=reply, message_thread_id=target.messageThreadId,
                )
            if not response or not getattr(response, "id", None):
                raise OSError("missing_delivery_ack")
            return response
        if isinstance(frame, LivePhotoFrame):
            raise ValueError("inline_live_photo_plan")
        response = await self.client.edit_inline_text(
            inline_message_id=target.inlineMessageId, rich_message=frame.payload,
        )
        if not response:
            raise OSError("missing_delivery_ack")
        return response

    async def _send_with_retry(
        self, target: MessageDelivery | InlineDelivery, frame: DeliveryFrame, previous_message_id: int | None,
        receipt: dict[str, Any], checkpoint: Callable[[], None],
    ) -> Any:
        for attempt in range(3):
            try:
                return await self._send(target, frame, previous_message_id)
            except (FloodWait, SlowmodeWait) as error:
                if attempt == 2 or not isinstance(error.value, int | float) or error.value > 60:
                    raise
                receipt["inFlight"] = False
                checkpoint()
                await asyncio.sleep(max(0, error.value))
                receipt["inFlight"] = True
                checkpoint()
            except MessageNotModified:
                if isinstance(target, MessageDelivery):
                    raise
                return True
        raise OSError("retry_exhausted")
