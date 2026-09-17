"""HTTP and persistence integration tests without Telegram/network credentials."""
import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from worker.app import create_app
from worker.jobs import Jobs
from worker.models import ConfigInput, JobInput
from worker.platform_config import PlatformConfigFile
from worker.store import TTL, Store


class FakeEngine:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()
        self.cancelled = False

    def configure(self, config: dict[str, Any]) -> None:
        pass

    def capabilities(self) -> dict[str, Any]:
        return {"platforms": [{"id": "youtube", "name": "YouTube", "contentTypes": ["video"]}],
                "outputModes": ["preview", "raw", "zip"]}

    def extract_urls(self, text: str) -> list[str]:
        return text.split()

    async def prepare(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.started.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        directory = kwargs["directory"] / f"Title_{self.calls}"
        kwargs["register_directory"](directory)
        directory.mkdir()
        path = directory / "video.mp4"
        path.write_bytes(b"media")
        return {"platform": "youtube", "sourceUrl": url, "canonicalUrl": url,
                "content": "hello", "contentType": "post", "contentFormat": "markdown", "access": "public",
                "media": [{"type": "video", "mediaId": "a" * 32, "sizeBytes": 5}], "_files": [str(path)],
                "_mediaFiles": {"a" * 32: {"path": str(path), "sizeBytes": 5, "mimeType": "video/mp4"}}}


def request(key: str, **kwargs: Any) -> JobInput:
    return JobInput(text="https://youtube.com/watch?v=x", accountId="123", requestId="trace",
                    idempotencyKey=key, **kwargs)


def test_http_identity_config_and_private_results(tmp_path: Path) -> None:
    async def run() -> None:
        store = Store(tmp_path)
        jobs = Jobs(FakeEngine(), store, "123")
        config = PlatformConfigFile(tmp_path / "platform_config.yaml", jobs.engine.capabilities()["platforms"])
        jobs.configure(config.active_config())
        async with TestClient(TestServer(create_app(jobs, "secret", config))) as client:
            assert (await client.get("/api/v1/health")).status == 401
            headers = {"Authorization": "Bearer secret"}
            response = await client.get("/api/v1/health", headers=headers)
            assert (await response.json())["ready"] is True
            bad = request("bad").model_dump()
            bad["accountId"] = "456"
            response = await client.post("/api/v1/jobs", json=bad, headers=headers)
            assert (await response.json())["error"]["code"] == "bot_identity_mismatch"
            response = await client.put("/api/v1/config", json={"version": "old-push", "platforms": {}},
                                        headers=headers)
            assert response.status == 409
            response = await client.get("/api/v1/config", headers=headers)
            snapshot = await response.json()
            response = await client.put("/api/v1/config", json={"baseSha256": snapshot["sha256"],
                "platform": "youtube", "cookies": [{"value": "private=secret"}]}, headers=headers)
            assert (await response.json())["requiresRestart"] is True
            assert jobs.config["platforms"]["youtube"]["cookies"] == []
            response = await client.post("/api/v1/jobs", json=request("one").model_dump(), headers=headers)
            job = await response.json()
            await asyncio.gather(*list(jobs.tasks.values()))
            response = await client.get(f"/api/v1/jobs/{job['id']}", headers=headers)
            result = await response.json()
            assert result["status"] == "ready"
            item = result["results"][0]
            assert "_files" not in item
            assert "private=secret" not in str(result)
            assert item["media"][0]["mediaId"] == "a" * 32
            assert "_mediaFiles" not in item and "fileId" not in item["media"][0]
            media_path = f"/api/v1/leases/{item['leaseId']}/media/{'a' * 32}"
            assert (await client.get(media_path)).status == 401
            assert (await client.get(media_path.replace('a' * 32, 'b' * 32), headers=headers)).status == 404
            media_response = await client.get(media_path, headers=headers)
            assert media_response.status == 200
            assert media_response.headers['Cache-Control'] == 'no-store'
            assert await media_response.read() == b'media'
            assert (await client.delete(f"/api/v1/leases/{item['leaseId']}", headers=headers)).status == 200
            assert (await client.delete(f"/api/v1/leases/{item['leaseId']}", headers=headers)).status == 200
            assert (await client.get(media_path, headers=headers)).status == 404
        store.close()
    asyncio.run(run())


def test_cache_fixed_boundary_and_lease(tmp_path: Path) -> None:
    store = Store(tmp_path)
    folder = store.files / "Title"
    store.register_path("fixture", folder, "directory")
    folder.mkdir()
    file = folder / "video"
    file.write_bytes(b"media")
    with patch("worker.store.time.time", return_value=100):
        cache_id = store.publish("canonical", {"_files": [str(file)]}, folder, ["short"], owner="fixture")
    with patch("worker.store.time.time", return_value=100 + TTL - 1):
        assert store.lookup("short") is not None
        lease = store.lease(cache_id)
    with patch("worker.store.time.time", return_value=100 + TTL):
        assert store.lookup("canonical") is None
        store.cleanup()
        assert file.exists()
        store.release(lease)
        store.cleanup()
        assert not file.exists()
    store.close()


def test_media_requires_current_lease_and_cannot_escape_cache(tmp_path: Path) -> None:
    store = Store(tmp_path)
    folder = store.files / 'Title'
    store.register_path('fixture', folder, 'directory')
    folder.mkdir()
    outside = tmp_path / 'private'
    outside.write_bytes(b'private')
    video = folder / 'video'
    video.write_bytes(b'video')
    link = folder / 'link'
    link.symlink_to(outside)
    descriptors = {name: {'path': str(path), 'sizeBytes': path.stat().st_size, 'mimeType': 'video/mp4'}
                   for name, path in [('video', video), ('outside', outside), ('link', link)]}
    cache_id = store.publish('key', {'_mediaFiles': descriptors}, folder, owner='fixture')
    with patch('worker.store.time.time', return_value=100):
        lease = store.lease(cache_id)
        assert store.media_file(lease, 'video') is not None
        assert store.media_file(lease, 'outside') is None
        assert store.media_file(lease, 'link') is None
        assert store.media_file('other-lease', 'video') is None
    with patch('worker.store.time.time', return_value=401):
        assert store.media_file(lease, 'video') is None
    store.close()


def test_singleflight_cancellation_and_cache(tmp_path: Path) -> None:
    async def run() -> None:
        engine = FakeEngine()
        engine.gate.clear()
        store = Store(tmp_path)
        jobs = Jobs(engine, store, "123")
        jobs.configure(ConfigInput(version="1"))
        first = jobs.create(request("1"))
        second = jobs.create(request("2"))
        await engine.started.wait()
        await jobs.cancel(first["id"])
        assert not engine.cancelled
        engine.gate.set()
        await asyncio.gather(*list(jobs.tasks.values()))
        assert engine.calls == 1
        assert store.job(second["id"])["status"] == "ready"
        third = jobs.create(request("3"))
        await asyncio.gather(*list(jobs.tasks.values()))
        assert engine.calls == 1
        assert store.job(third["id"])["results"][0]["cacheHit"] is True
        fourth = jobs.create(request("4", mode="read_only"))
        await asyncio.gather(*list(jobs.tasks.values()))
        assert store.job(fourth["id"])["results"][0]["media"] == []
        assert engine.calls == 1
        await jobs.close()
        store.close()
    asyncio.run(run())


def test_all_waiters_cancel_and_restart_recovery(tmp_path: Path) -> None:
    async def run() -> None:
        engine = FakeEngine()
        engine.gate.clear()
        store = Store(tmp_path)
        jobs = Jobs(engine, store, "123")
        jobs.configure(ConfigInput(version="1"))
        job = jobs.create(request("1"))
        await engine.started.wait()
        await jobs.cancel(job["id"])
        assert engine.cancelled
        assert not list(store.files.iterdir())
        store.save_job({"id": "pending", "status": "running", "results": []}, "pending", "fp")
        await jobs.close()
        store.close()
        restored = Store(tmp_path)
        assert restored.job("pending")["status"] == "interrupted"
        restored.close()
    asyncio.run(run())


def test_refresh_generations_do_not_delete_leased_files(tmp_path: Path) -> None:
    async def run() -> None:
        engine = FakeEngine()
        store = Store(tmp_path)
        jobs = Jobs(engine, store, "123")
        jobs.configure(ConfigInput(version="1"))
        jobs.create(request("1"))
        await asyncio.gather(*list(jobs.tasks.values()))
        files_before = list(store.files.rglob("video.mp4"))
        jobs.create(request("2", refresh=True))
        await asyncio.gather(*list(jobs.tasks.values()))
        assert engine.calls == 2
        store.max_bytes = 1
        store.cleanup()
        assert all(path.exists() for path in files_before)
        await jobs.close()
        store.close()
    asyncio.run(run())


def test_idempotency_modes_and_read_only_does_not_poison_cache(tmp_path: Path) -> None:
    async def run() -> None:
        engine = FakeEngine()
        store = Store(tmp_path)
        jobs = Jobs(engine, store, "123")
        jobs.configure(ConfigInput(version="1"))
        readonly = jobs.create(request("read", mode="read_only"))
        await asyncio.gather(*list(jobs.tasks.values()))
        assert store.job(readonly["id"])["results"][0]["media"] == []
        first = jobs.create(request("normal"))
        assert jobs.create(request("normal"))["id"] == first["id"]
        await asyncio.gather(*list(jobs.tasks.values()))
        assert engine.calls == 2
        jobs.create(request("raw", outputMode="raw"))
        await asyncio.gather(*list(jobs.tasks.values()))
        assert engine.calls == 3
        try:
            jobs.create(request("normal", outputMode="zip"))
        except ValueError as error:
            assert str(error) == "idempotency_conflict"
        else:
            raise AssertionError("Conflicting idempotency key accepted")
        await jobs.close()
        store.close()
    asyncio.run(run())


def test_batch_keeps_order_and_continues_after_failure(tmp_path: Path) -> None:
    class MixedEngine(FakeEngine):
        async def prepare(self, url: str, **kwargs: Any) -> dict[str, Any]:
            if "bad" in url:
                raise RuntimeError("cookie=DO_NOT_RETURN_UPSTREAM_SECRET")
            return await super().prepare(url, **kwargs)

    async def run() -> None:
        store = Store(tmp_path)
        jobs = Jobs(MixedEngine(), store, "123")
        jobs.configure(ConfigInput(version="1"))
        payload = request("batch").model_copy(update={"text": "https://bad.test https://good.test"})
        job = jobs.create(payload)
        await asyncio.gather(*list(jobs.tasks.values()))
        result = store.job(job["id"])
        assert result is not None
        assert result["results"][0]["error"]["code"] == "prepare_failed"
        assert result["results"][1]["sourceUrl"] == "https://good.test"
        assert "DO_NOT_RETURN" not in str(result)
        assert result["status"] == "ready"
        await jobs.close()
        store.close()
    asyncio.run(run())


def test_all_media_failed_is_not_reused(tmp_path: Path) -> None:
    class FailedMediaEngine(FakeEngine):
        async def prepare(self, url: str, **kwargs: Any) -> dict[str, Any]:
            result = await super().prepare(url, **kwargs)
            result.update(media=[], mediaFailureCount=1)
            return result

    async def run() -> None:
        engine = FailedMediaEngine()
        store = Store(tmp_path)
        jobs = Jobs(engine, store, "123")
        jobs.configure(ConfigInput(version="1"))
        for key in ("one", "two"):
            jobs.create(request(key))
            await asyncio.gather(*list(jobs.tasks.values()))
        assert engine.calls == 2
        await jobs.close()
        store.close()
    asyncio.run(run())


def test_ready_idempotency_does_not_resurrect_released_delivery(tmp_path: Path) -> None:
    async def run() -> None:
        store = Store(tmp_path)
        jobs = Jobs(FakeEngine(), store, "123")
        jobs.configure(ConfigInput(version="1"))
        initial = jobs.create(request("same"))
        await asyncio.gather(*list(jobs.tasks.values()))
        ready = jobs.create(request("same"))
        assert ready["id"] == initial["id"]
        store.release(ready["results"][0]["leaseId"])
        try:
            jobs.create(request("same"))
        except ValueError as error:
            assert str(error) == "job_expired"
        else:
            raise AssertionError("released delivery must not be replayed")
        await jobs.close()
        store.close()
    asyncio.run(run())


def test_running_batch_keeps_early_result_files_leased(tmp_path: Path) -> None:
    async def run() -> None:
        store = Store(tmp_path, max_bytes=1)
        jobs = Jobs(FakeEngine(), store, "123")
        directory = store.files / "Title"
        store.register_path("fixture", directory, "directory")
        directory.mkdir()
        media = directory / "video.mp4"
        media.write_bytes(b"video")
        with patch("worker.store.time.time", return_value=1000):
            cache_id = store.publish("key", {"_files": [str(media)]}, directory, owner="fixture")
            lease = store.lease(cache_id)
        job = {"results": [{"leaseId": lease}]}
        with patch("worker.store.time.time", return_value=1250):
            keeper = asyncio.create_task(jobs._keep_partial_leases(job, interval=100))
            await asyncio.sleep(0)
        with patch("worker.store.time.time", return_value=1350):
            store.cleanup()
            assert media.is_file()
        keeper.cancel()
        await asyncio.gather(keeper, return_exceptions=True)
        store.release(lease)
        store.cleanup()
        assert not media.exists()
        store.close()
    asyncio.run(run())


def test_first_short_and_canonical_requests_share_preparation(tmp_path: Path) -> None:
    class AliasEngine(FakeEngine):
        identities = 0

        async def cache_identity(self, url: str, config: dict) -> str:
            self.identities += 1
            return "https://youtube.com/watch?v=x"

        async def prepare(self, url: str, **kwargs: Any) -> dict[str, Any]:
            result = await super().prepare(url, **kwargs)
            result["canonicalUrl"] = "https://youtube.com/watch?v=x"
            return result

    async def run() -> None:
        store = Store(tmp_path)
        engine = AliasEngine()
        engine.gate.clear()
        jobs = Jobs(engine, store, "123")
        jobs.configure(ConfigInput(version="1"))
        short = request("short").model_copy(update={"text": "https://youtu.be/x"})
        first = jobs.create(short)
        second = jobs.create(request("canonical"))
        await engine.started.wait()
        await asyncio.sleep(0)
        engine.gate.set()
        await asyncio.gather(*list(jobs.tasks.values()))
        assert engine.calls == 1
        assert store.job(first["id"])["status"] == "ready"
        assert store.job(second["id"])["status"] == "ready"
        identities = engine.identities
        jobs.create(short.model_copy(update={"idempotencyKey": "short-again"}))
        await asyncio.gather(*list(jobs.tasks.values()))
        assert engine.identities == identities
        assert engine.calls == 1
        await jobs.close()
        store.close()
    asyncio.run(run())


def test_original_database_tables_and_unrelated_downloads_survive(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "db" / "database.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE cache (id INTEGER PRIMARY KEY, raw_url TEXT)")
        db.execute("INSERT INTO cache VALUES (1, 'original-content')")
        db.execute("CREATE TABLE jobs (original TEXT)")
        db.execute("INSERT INTO jobs VALUES ('original-job')")
    downloads = tmp_path / "downloads"
    original = downloads / "original-bot-download"
    original.mkdir(parents=True)
    (original / "video.mp4").write_bytes(b"original")
    orphan = downloads / "worker-abandoned"
    orphan.mkdir()
    store = Store(tmp_path / "state", database_path=database, files_path=downloads)
    assert store.database_path == database
    assert store.files == downloads
    assert orphan.exists()  # A name prefix never establishes ownership.
    assert (original / "video.mp4").read_bytes() == b"original"
    store.cleanup()
    assert store.db.execute("SELECT raw_url FROM cache").fetchone()[0] == "original-content"
    assert store.db.execute("SELECT original FROM jobs").fetchone()[0] == "original-job"
    store.close()
