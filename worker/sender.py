"""Worker leases/receipts adapter for the shared delivery core."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from pyrogram import Client

from delivery.models import DeliveryError, Destination, from_worker, plan
from delivery.sender import send_envelope
from delivery.transport import TelegramTransport
from worker.config import WorkerSettings
from worker.models import InlineDelivery, MessageDelivery
from worker.rich_delivery import bounded, evidence_for
from worker.sender_runtime import SenderRuntime
from worker.store import Store

logger = logging.getLogger("parsehub.worker")

def create_sender_client(settings: WorkerSettings) -> Client:
    return Client(settings.worker_sender_session_name, api_id=settings.api_id,
                  api_hash=settings.api_hash.get_secret_value(), bot_token=settings.bot_token.get_secret_value(),
                  workdir=settings.sessions_path, in_memory=False, no_updates=True, plugins=None,
                  sleep_threshold=0, proxy=settings.bot_proxy)


class TelegramSender:
    def __init__(self, client: Any, runtime: SenderRuntime | None = None, *,
                 transport: TelegramTransport | None = None):
        self.client = client
        self.runtime = runtime
        self.transport = transport

    @property
    def ready(self) -> bool:
        return self.runtime.ready if self.runtime else True

    def health(self) -> dict[str, Any]:
        return self.runtime.health() if self.runtime else {"deliveryReady": True, "senderState": "ready"}

    async def deliver(self, job: dict[str, Any], target: MessageDelivery | InlineDelivery, store: Store,
                      checkpoint: Callable[[], None]) -> None:
        receipt = job["delivery"]
        if receipt.get("status") in {"sent", "partial", "unknown", "failed", "cancelled"}:
            return
        token = getattr(self.client, "bot_token", "") or ""
        transport = self.transport or TelegramTransport(self.client, token.split(":")[0], store)
        dest = (Destination(chat_id=int(target.chatId), thread_id=target.messageThreadId,
                            reply_to=target.replyToMessageId, silent=target.silent, protect=target.protect)
                if isinstance(target, MessageDelivery) else
                Destination(surface=target.surface, inline_message_id=target.inlineMessageId))

        def resolve(lease: str, media_id: str) -> Any:
            value = store.media_file(lease, media_id)
            if value is None:
                raise DeliveryError("media_expired")
            return value[0]

        try:
            envelopes = [from_worker(item, dest, resolve) for item in job["results"]]
            if dest.surface != "message":
                if not envelopes:
                    raise DeliveryError("empty_delivery")
                footer_links = tuple((e.platform, e.source_url) for e in envelopes if e.source_url)
                envelopes = [replace(envelopes[0], title="", source_url="", reading_url="",
                    body="\n\n".join(filter(None, (e.title + "\n\n" + e.body for e in envelopes))),
                    media=tuple(a for e in envelopes for a in e.media), footer_links=footer_links)]
            plans = [plan(e) for e in envelopes]
        except ValueError as error:
            code = str(error) if isinstance(error, DeliveryError) else "delivery_limits"
            logger.warning("event=delivery.plan_failed job=%s surface=%s results=%s code=%s error_type=%s",
                           job.get("id", "unknown"), dest.surface, len(job["results"]), code, type(error).__name__)
            receipt.update(status="failed", inFlight=False, error={"code": code,
                           "message": "当前结果无法按所选方式完整交付，请检查媒体限制。"})
            checkpoint()
            return
        receipt["totalFrames"] = sum(len(p) for p in plans)
        tasks = receipt.setdefault("tasks", [{} for _ in envelopes])
        delivered: list[int] = []

        def settle() -> None:
            receipt.update(messageIds=[mid for state in tasks for mid in state.get("messageIds", [])],
                           albums=[a for state in tasks for a in state.get("albums", [])],
                           kind=(tasks[0].get("kind") if len(tasks) == 1 else "multiple"),
                           totalFrames=sum(s.get("totalFrames", 0) for s in tasks),
                           completedFrames=sum(s.get("completedFrames", 0) for s in tasks),
                           mediaCount=sum(s.get("mediaCount", 0) for s in tasks),
                           inFlight=any(s.get("inFlight", False) for s in tasks),
                           text=bounded("\n\n".join(s.get("text", "") for s in tasks), 80000))
            if dest.surface != "message" and any(s.get("confirmed") for s in tasks):
                receipt.update(confirmed=True, inlineMessageId=dest.inline_message_id)
            job["evidence"] = evidence_for(job["results"], delivered, receipt["mediaCount"])
            checkpoint()

        for index, envelope in enumerate(envelopes):
            receipt["status"] = "sending"
            try:
                sent = await send_envelope(envelope, transport, tasks[index], settle)
            except asyncio.CancelledError:
                receipt["status"] = ("unknown" if receipt.get("inFlight") else
                                     "partial" if receipt.get("completedFrames") else "cancelled")
                settle()
                raise
            if sent.status != "sent":
                receipt.update(status="partial" if receipt.get("completedFrames") and sent.status == "failed"
                               else sent.status, error=tasks[index].get("error"))
                settle()
                return
            delivered.extend(range(len(job["results"])) if dest.surface != "message" else [index])
            settle()
        receipt.update(status="partial" if any("error" in i or i.get("mediaFailureCount", 0)
                                              for i in job["results"]) else "sent", inFlight=False)
        settle()
