"""The only business send entry point; confirmed batches are never replayed."""

import asyncio
import logging
import secrets
from collections.abc import Callable
from typing import Any, cast

from pyrogram.errors import FloodWait, RPCError, SlowmodeWait

from delivery.models import DeliveryEnvelope, DeliveryError, SendKind, SendResult, plan
from delivery.transport import TelegramTransport, UploadFailure

logger = logging.getLogger("parsehub.delivery")


def _error_fields(error: BaseException) -> tuple[str, str, str, str]:
    """Return bounded identifiers only; never log exception text or media paths."""
    phase = error.phase if isinstance(error, UploadFailure) else "transport_upload"
    error_type = error.cause_type if isinstance(error, UploadFailure) else type(error).__name__
    rpc = error.rpc if isinstance(error, UploadFailure) else str(getattr(error, "ID", "") or "none")
    code = str(error) if isinstance(error, DeliveryError) else "none"
    return phase, error_type, rpc, code


async def send_envelope(envelope: DeliveryEnvelope, transport: TelegramTransport,
                        state: dict[str, Any], checkpoint: Callable[[], None]) -> SendResult:
    # Recovery must precede filesystem validation: receipts outlive leased files.
    # A process may have died after Telegram accepted a request but before its ack
    # was persisted. Never clear that marker and issue a fresh visible request.
    terminal = {"sent", "partial", "unknown", "failed", "cancelled"}
    if state.get("status") not in terminal and state.get("inFlight"):
        state.update(status="unknown", error={"code": "delivery_unknown",
                     "message": "上次发送结果不明确，请先检查已有消息。"})
        checkpoint()
    if state.get("status") in terminal:
        return SendResult(cast(SendKind, state.get("kind", "message")), str(state["status"]),
                          list(state.get("messageIds", [])), list(state.get("albums", [])),
                          confirmed=bool(state.get("confirmed")))
    try:
        batches = plan(envelope)
    except Exception as error:
        phase, error_type, rpc, code = _error_fields(error)
        logger.warning(
            "event=delivery.plan_failed surface=%s media_count=%s phase=%s "
            "error_type=%s rpc=%s code=%s",
            envelope.dest.surface, len(envelope.media), phase, error_type, rpc, code,
        )
        raise
    kind = batches[0].kind
    result = SendResult(kind, str(state.get("status", "pending")), list(state.get("messageIds", [])),
                        list(state.get("albums", [])), confirmed=bool(state.get("confirmed")))
    state.update(kind=kind, totalFrames=len(batches), status="preparing", inFlight=False)
    checkpoint()
    # Upload all assets before the first visible request. Upload failures are definite failures.
    prepared: list[list[Any]] = []
    asset_total = sum(len(batch.assets) for batch in batches)
    asset_index = 0
    try:
        for batch_index, batch in enumerate(batches):
            uploaded_batch = []
            for asset in batch.assets:
                asset_index += 1
                try:
                    uploaded_batch.append(await transport.upload(asset, envelope.dest, rich=kind == "rich"))
                except Exception as error:
                    phase, error_type, rpc, code = _error_fields(error)
                    logger.warning(
                        "event=delivery.upload_failed kind=%s batch=%s/%s asset=%s/%s "
                        "asset_type=%s size_bytes=%s representation=%s phase=%s error_type=%s rpc=%s code=%s",
                        kind, batch_index + 1, len(batches), asset_index, asset_total,
                        asset.type, max(0, asset.size), "preview" if kind == "rich" else "native",
                        phase, error_type, rpc, code,
                    )
                    raise
            prepared.append(uploaded_batch)
    except asyncio.CancelledError:
        state.update(status="cancelled")
        checkpoint()
        raise
    except Exception:
        state.update(status="failed", error={"code": "upload_failed", "message": "媒体准备或上传失败"})
        checkpoint()
        result.status = "failed"
        return result
    ids = state.setdefault("_randomIds", [[secrets.randbits(63) or 1 for _ in range(max(1, len(b.assets)))]
                                          for b in batches])
    checkpoint()
    for index, batch in enumerate(batches):
        if index < state.get("completedFrames", 0):
            continue
        uploaded = prepared[index]
        try:
            for attempt in range(3):
                state.update(status="sending", frameIndex=index, inFlight=True)
                checkpoint()
                try:
                    messages: list[Any]
                    if kind == "rich":
                        response = await transport.rich(envelope.dest, [(batch, uploaded)])
                        if envelope.dest.surface == "message":
                            message_id = (response.get("message_id") if isinstance(response, dict)
                                          else getattr(response, "id", None))
                            if not message_id:
                                raise OSError("missing_delivery_ack")
                            messages = []
                            message_ids = [int(message_id)]
                        else:
                            if response is not True:
                                raise OSError("missing_delivery_ack")
                            messages, message_ids = [], []
                    else:
                        messages = await transport.native(envelope.dest, batch, uploaded, ids[index])
                        message_ids = [int(m.id) for m in messages if getattr(m, "id", None)]
                        if len(message_ids) != max(1, len(batch.assets)) or len(set(message_ids)) != len(message_ids):
                            raise OSError("missing_delivery_ack")
                    break
                except (FloodWait, SlowmodeWait) as error:
                    delay = error.value
                    if attempt == 2 or not isinstance(delay, int | float) or not 0 <= delay <= 60:
                        raise
                    state["inFlight"] = False
                    checkpoint()
                    await asyncio.sleep(delay)
                except RPCError as error:
                    if "FILE_REFERENCE" not in str(getattr(error, "ID", "")) or attempt:
                        raise
                    state["inFlight"] = False
                    checkpoint()
                    # Refresh only the expired member if Telegram identifies its position.
                    import re
                    match = re.search(r"FILE_REFERENCE_(\d+)_", str(getattr(error, "ID", "")))
                    member_index = (int(match[1]) if match else getattr(error, "value", None))
                    indices = ([member_index] if isinstance(member_index, int) and 0 <= member_index < len(uploaded)
                               else range(len(uploaded)))
                    for member in indices:
                        uploaded[member] = await transport.refresh(uploaded[member], envelope.dest)
            else:
                raise DeliveryError("delivery_retry_exhausted")
            if batch.kind == "album":
                groups = {str(getattr(m, "media_group_id", "") or "") for m in messages}
                if len(groups) != 1 or "" in groups:
                    raise OSError("missing_album_ack")
                result.albums.append({"groupedId": groups.pop(), "messageIds": message_ids})
            result.message_ids.extend(message_ids)
            result.confirmed = envelope.dest.surface != "message"
            state.update(messageIds=result.message_ids, albums=result.albums, confirmed=result.confirmed,
                         completedFrames=index + 1, mediaCount=sum(len(b.assets) for b in batches[:index + 1]),
                         inFlight=False, text="\n\n".join(b.text for b in batches if b.text))
            try:
                checkpoint()  # Visible acknowledgement must survive a later cache-write failure.
            except Exception as error:
                logger.error("event=delivery.ack_checkpoint_failed error_type=%s", type(error).__name__)
            if uploaded and messages:
                transport.remember(uploaded, messages, envelope.dest)
            for value in uploaded:
                result.assets_cached.append({"key": value.asset.key, "representation": value.representation,
                                             "refs": value.refs})
        except asyncio.CancelledError:
            state.update(status="unknown" if state.get("inFlight") else
                         "partial" if state.get("completedFrames") else "cancelled")
            checkpoint()
            raise
        except Exception as error:
            known = isinstance(error, (RPCError, DeliveryError)) or not state.get("inFlight")
            status = ("partial" if state.get("completedFrames") else "failed") if known else "unknown"
            state.update(status=status, inFlight=not known, error={
                "code": str(error) if isinstance(error, DeliveryError) else
                "telegram_rejected" if known else "delivery_unknown",
                "message": "交付未完成，请检查已有消息后重试",
            })
            checkpoint()
            result.status = status
            phase, error_type, rpc, code = _error_fields(error)
            logger.warning(
                "event=delivery.failed kind=%s batch=%s/%s status=%s phase=send "
                "error_type=%s rpc=%s code=%s",
                kind, index + 1, len(batches), status, error_type, rpc, code,
            )
            return result
        logger.info("event=delivery.confirmed kind=%s n=%s message_ids=%s grouped_ids=%s", kind,
                    len(batch.assets), message_ids, [a["groupedId"] for a in result.albums])
    state.update(status="sent", inFlight=False)
    try:
        checkpoint()
    except Exception as error:
        logger.error("event=delivery.final_checkpoint_failed error_type=%s", type(error).__name__)
    result.status = "sent"
    return result
