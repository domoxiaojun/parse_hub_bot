"""Standalone Bot adapter. All content delivery goes through the shared core."""

from typing import Any

from core import bs
from delivery.models import DeliveryEnvelope, DeliveryError, SendResult
from delivery.sender import send_envelope
from delivery.transport import MemoryReferences, TelegramTransport


async def deliver(cli: Any, envelope: DeliveryEnvelope, request_id: str) -> SendResult:
    """Deliver one Bot request without coupling it to the Worker journal or SQLite."""
    del request_id  # The interactive Bot has no process-restart recovery path.
    state: dict[str, Any] = {}
    transport = TelegramTransport(cli, bs.bot_token.split(":")[0], MemoryReferences())
    result = await send_envelope(envelope, transport, state, lambda: None)
    if result.status != "sent":
        raise DeliveryError("delivery_unknown" if result.status == "unknown" else "delivery_incomplete")
    return result
