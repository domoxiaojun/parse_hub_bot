"""Keep HTTP alive while the outbound-only Telegram session authenticates."""

import asyncio
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

from pyrogram.errors import FloodWait

logger = logging.getLogger("parsehub.worker")


class SenderRuntime:
    def __init__(self, client: Any, bot_id: str, cooldown_path: Path):
        self.client = client
        self.bot_id = bot_id
        self.cooldown_path = cooldown_path
        self.state = "starting"
        self.retry_at = 0.0
        self.task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready" and self.client.is_connected and self.client.is_initialized

    def health(self) -> dict[str, Any]:
        return {"deliveryReady": self.ready, "senderState": self.state,
                "retryAfterSeconds": max(0, math.ceil(self.retry_at - time.time()))}

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    def _load_cooldown(self) -> float:
        try:
            value = json.loads(self.cooldown_path.read_text())["retryAt"]
        except FileNotFoundError:
            return 0.0
        except (OSError, ValueError, KeyError, TypeError) as error:
            # A torn write must not park the sender in a permanent error state;
            # Telegram still enforces any real flood wait on the next attempt.
            logger.warning("event=sender.cooldown_unreadable error_type=%s", type(error).__name__)
            return 0.0
        if not isinstance(value, int | float) or not math.isfinite(value):
            logger.warning("event=sender.cooldown_unreadable error_type=ValueError")
            return 0.0
        return float(value)

    def _save_cooldown(self) -> None:
        temporary = self.cooldown_path.with_suffix(".tmp")
        with temporary.open("w") as file:
            json.dump({"retryAt": self.retry_at}, file)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.cooldown_path)

    async def _disconnect(self) -> None:
        if self.client.is_initialized:
            await self.client.stop()
        elif self.client.is_connected:
            await self.client.disconnect()

    async def _run(self) -> None:
        backoff = 5
        try:
            self.retry_at = self._load_cooldown()
            while True:
                if self.retry_at > time.time():
                    if self.state != "retrying":
                        self.state = "cooldown"
                    await asyncio.sleep(self.retry_at - time.time())
                self.state = "starting"
                self.retry_at = 0
                try:
                    await self.client.start()
                    if self.client.me is None or str(self.client.me.id) != self.bot_id:
                        raise ValueError("sender identity mismatch")
                    self.cooldown_path.unlink(missing_ok=True)
                    self.state = "ready"
                    logger.info("event=sender.ready")
                    return
                except FloodWait as error:
                    seconds = error.value
                    if not isinstance(seconds, int | float) or not math.isfinite(seconds) or seconds < 0:
                        raise ValueError("invalid Telegram wait") from None
                    self.retry_at = time.time() + math.ceil(seconds) + 1
                    self.state = "cooldown"
                    self._save_cooldown()
                    logger.warning("event=sender.cooldown retry_after_seconds=%s", math.ceil(seconds) + 1)
                    await self._disconnect()
                except (OSError, TimeoutError):
                    await self._disconnect()
                    self.retry_at = time.time() + backoff
                    self.state = "retrying"
                    logger.warning("event=sender.retry retry_after_seconds=%s", backoff)
                    backoff = min(300, backoff * 2)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Invalid credentials/config require intervention; never create a rapid auth retry loop.
            self.state = "error"
            self.retry_at = 0
            logger.error("event=sender.failed error_type=%s", type(error).__name__)
            await self._disconnect()

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self._disconnect()
        self.state = "stopped"
