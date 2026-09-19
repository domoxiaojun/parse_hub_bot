"""Standalone Bot adapter. All content delivery goes through the shared core."""

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import make_url

from core import bs
from delivery.models import DeliveryEnvelope, DeliveryError, SendResult
from delivery.sender import send_envelope
from delivery.transport import TelegramTransport
from worker.store import Store


async def deliver(cli: Any, envelope: DeliveryEnvelope, request_id: str) -> SendResult:
    url = make_url(bs.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise DeliveryError("delivery_journal_requires_sqlite")
    store = Store(bs.data_path, database_path=Path(url.database), files_path=bs.download_dir, recover=False)
    identity = hashlib.sha256(f"legacy:{bs.bot_token.split(':')[0]}:{request_id}".encode()).hexdigest()
    job = store.job(identity) or {"id": identity, "status": "running", "delivery": {}}

    def checkpoint() -> None:
        status = job["delivery"].get("status")
        job["status"] = "ready" if status == "sent" else "failed" if status in {
            "failed", "partial", "unknown", "cancelled"} else "running"
        store.save_job(job)

    try:
        transport = TelegramTransport(cli, bs.bot_token.split(":")[0], store)
        result = await send_envelope(envelope, transport, job["delivery"], checkpoint)
        if result.status != "sent":
            raise DeliveryError("delivery_unknown" if result.status == "unknown" else "delivery_incomplete")
        return result
    finally:
        store.close()
