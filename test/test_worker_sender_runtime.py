"""Offline startup failures: preserve cooldown and keep the control plane alive."""

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pyrogram.errors import FloodWait

from worker.app import create_app
from worker.jobs import Jobs
from worker.models import JobInput
from worker.sender import TelegramSender
from worker.sender_runtime import SenderRuntime
from worker.store import Store


def client():
    return SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), disconnect=AsyncMock(),
                           me=SimpleNamespace(id=123), is_connected=False, is_initialized=False)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def test_floodwait_persists_deadline_without_repeated_authentication(tmp_path):
    async def run():
        fake = client()
        fake.start.side_effect = FloodWait(2753)
        path = tmp_path / "worker_sender_123.cooldown.json"
        runtime = SenderRuntime(fake, "123", path)
        before = time.time()
        runtime.start()
        await until(lambda: runtime.state == "cooldown")
        assert fake.start.call_count == 1 and runtime.ready is False
        saved = json.loads(path.read_text())
        assert saved["retryAt"] >= before + 2753
        assert 2753 <= runtime.health()["retryAfterSeconds"] <= 2754
        await runtime.close()
        replacement = client()
        restarted = SenderRuntime(replacement, "123", path)
        restarted.start()
        await until(lambda: restarted.state == "cooldown")
        replacement.start.assert_not_called()
        assert json.loads(path.read_text()) == saved
        await restarted.close()
    asyncio.run(run())


def test_successful_start_reuses_client_and_clears_expired_cooldown(tmp_path):
    async def run():
        fake = client()

        async def connected():
            fake.is_connected = True
            fake.is_initialized = True

        fake.start.side_effect = connected
        path = tmp_path / "wait.json"
        path.write_text(json.dumps({"retryAt": time.time() - 5}))
        runtime = SenderRuntime(fake, "123", path)
        runtime.start()
        runtime.start()
        await until(lambda: runtime.ready)
        fake.start.assert_awaited_once()
        assert not path.exists()
        await runtime.close()
        fake.stop.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("failure", [ValueError("private secret"), OSError("network private secret")])
def test_failure_stays_in_process_without_busy_retry(tmp_path, failure, caplog):
    async def run():
        fake = client()
        fake.start.side_effect = failure
        runtime = SenderRuntime(fake, "123", tmp_path / "wait.json")
        runtime.start()
        await until(lambda: runtime.state in {"error", "retrying"})
        assert not runtime.ready and fake.start.call_count == 1
        assert "private secret" not in caplog.text
        await runtime.close()
    asyncio.run(run())


def test_health_and_prepare_only_remain_available_during_login_cooldown(tmp_path):
    async def run():
        fake = client()
        fake.start.side_effect = FloodWait(2753)
        runtime = SenderRuntime(fake, "123", tmp_path / "wait.json")
        store = Store(tmp_path)
        engine = SimpleNamespace(configure=lambda _: None, extract_urls=lambda _: ["https://x.com/a/status/1"],
                                 prepare=AsyncMock(return_value={
                                     "access": "public", "canonicalUrl": "https://x.com/a/status/1", "_files": [],
                                 }))
        jobs = Jobs(engine, store, "123", TelegramSender(fake, runtime))
        jobs.configure(SimpleNamespace(model_dump=lambda: {"version": "1"}))
        headers = {"Authorization": "Bearer fixture"}
        try:
            async with TestClient(TestServer(create_app(jobs, "fixture"))) as http:
                # Control plane is already serving before authentication starts.
                initial = await (await http.get("/api/v1/health", headers=headers)).json()
                assert initial["directDelivery"] is True and initial["deliveryReady"] is False
                runtime.start()
                await until(lambda: runtime.state == "cooldown")
                health = await (await http.get("/api/v1/health", headers=headers)).json()
                assert health["ready"] is True and health["senderState"] == "cooldown"
                assert health["retryAfterSeconds"] >= 2753
                payload = {"text": "https://x.com/a/status/1", "accountId": "123", "requestId": "r",
                           "idempotencyKey": "key", "delivery": {"surface": "message", "chatId": "456"}}
                response = await http.post("/api/v1/jobs", headers=headers, json=payload)
                assert response.status == 409
                assert (await response.json())["error"]["code"] == "delivery_not_ready"
                assert store.db.execute("SELECT COUNT(*) FROM worker_jobs").fetchone()[0] == 0
                # Old prepare-only tools do not need a Telegram login.
                payload.pop("delivery")
                response = await http.post("/api/v1/jobs", headers=headers, json=payload)
                assert response.status == 202
                await asyncio.gather(*list(jobs.tasks.values()))
                # Completed delivery receipts are queryable even while sender is unavailable.
                done = {"id": "delivered", "status": "ready", "results": [],
                        "delivery": {"status": "sent", "messageIds": [42]}}
                request = JobInput.model_validate({**payload, "idempotencyKey": "delivered",
                                                  "delivery": {"surface": "message", "chatId": "456"}})
                fingerprint = hashlib.sha256(json.dumps(request.model_dump(exclude={"requestId", "idempotencyKey"}),
                                                       sort_keys=True).encode()).hexdigest()
                store.save_job(done, "delivered", fingerprint)
                assert jobs.create(request)["delivery"]["messageIds"] == [42]
        finally:
            await runtime.close()
            store.close()
    asyncio.run(run())


def test_sender_session_storage_survives_recreation_without_touching_original_session(tmp_path):
    from worker.config import WorkerSettings
    from worker.sender import create_sender_client

    async def run():
        settings = WorkerSettings(bot_token="123:fixture", api_id=1, api_hash="fixture", _env_file=None,
                                  worker_service_key="fixture-service-key-32-characters-long", data_path=tmp_path)
        settings.sessions_path.mkdir()
        original = settings.sessions_path / "bot_123.session"
        original.write_bytes(b"original-session-must-stay-unchanged")
        first = create_sender_client(settings)
        await first.storage.open()
        await first.storage.auth_key(b"fixture-persisted-auth-key")
        await first.storage.user_id(123)
        await first.storage.is_bot(True)
        await first.storage.save()
        await first.storage.close()
        second = create_sender_client(settings)
        await second.storage.open()
        try:
            assert await second.storage.auth_key() == b"fixture-persisted-auth-key"
            assert await second.storage.user_id() == 123
            assert bool(await second.storage.is_bot())
            assert original.read_bytes() == b"original-session-must-stay-unchanged"
        finally:
            await second.storage.close()
    asyncio.run(run())
