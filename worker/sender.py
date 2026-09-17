"""Outbound-only Telegram delivery with durable per-frame acknowledgement."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pyrogram import Client, types
from pyrogram.errors import FloodWait, MessageNotModified, RPCError, SlowmodeWait

from worker.config import WorkerSettings
from worker.models import InlineDelivery, MessageDelivery
from worker.reading_delivery import prepare_reading_frame
from worker.rich_delivery import LivePhotoFrame, bounded, build_frames, evidence_for
from worker.sender_runtime import SenderRuntime
from worker.store import Store


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
                response = None
                for attempt in range(3):
                    try:
                        if isinstance(target, MessageDelivery):
                            reply = (types.ReplyParameters(message_id=previous_message_id)
                                     if previous_message_id else None)
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
                        else:
                            if isinstance(frame, LivePhotoFrame):
                                raise ValueError("inline_live_photo_plan")
                            response = await self.client.edit_inline_text(
                                inline_message_id=target.inlineMessageId, rich_message=frame.payload,
                            )
                            if not response:
                                raise OSError("missing_delivery_ack")
                        break
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
                        response = True
                        break
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
                receipt.update(status=status,
                               inFlight=not known, error={"code": "telegram_rejected" if known else "delivery_unknown",
                                                          "message": "交付未完成，请检查已有消息后重试"})
                checkpoint()
                return
        degraded = any("error" in item or item.get("mediaFailureCount", 0) > 0 for item in job["results"])
        receipt.update(status="partial" if degraded else "sent", inFlight=False)
        checkpoint()
