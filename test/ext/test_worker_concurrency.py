"""Deterministic barriers for per-message parallelism and shared capacity."""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ext.worker.jobs import Jobs
from ext.worker.models import ConfigInput
from ext.worker.store import Store
from test.ext.test_worker_service import FakeEngine, request


def stored_job(store: Store, job_id: str) -> dict[str, Any]:
    job = store.job(job_id)
    assert job is not None
    return job


class BarrierEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.entered: list[str] = []
        self.active = 0
        self.peak = 0
        self.two_started = asyncio.Event()
        self.third_started = asyncio.Event()
        self.gates = {name: asyncio.Event() for name in ("a", "b", "c")}
        self.cancelled_urls: set[str] = set()

    async def prepare(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.entered.append(url)
        self.active += 1
        self.peak = max(self.peak, self.active)
        if len(self.entered) == 2:
            self.two_started.set()
        if len(self.entered) == 3:
            self.third_started.set()
        try:
            await self.gates[url.rsplit("/", 1)[-1]].wait()
            return await super().prepare(url, **kwargs)
        except asyncio.CancelledError:
            self.cancelled_urls.add(url)
            raise
        finally:
            self.active -= 1


def test_parallel_links_checkpoint_out_of_order_and_share_global_limit(tmp_path: Path) -> None:
    async def run() -> None:
        engine = BarrierEngine()
        store = Store(tmp_path)
        jobs = Jobs(engine, store, "123")
        jobs.configure(ConfigInput(version="1"))
        first = jobs.create(request("first").model_copy(update={"text": "https://test/a https://test/b"}))
        await asyncio.wait_for(engine.two_started.wait(), 1)
        second = jobs.create(request("second").model_copy(update={"text": "https://test/c"}))
        # Both URLs of the first message have entered before either is released.
        assert engine.entered == ["https://test/a", "https://test/b"]
        engine.gates["b"].set()
        await asyncio.wait_for(engine.third_started.wait(), 1)
        # The early result is durable and leased while the first URL still waits.
        partial = store.job(first["id"])
        assert partial is not None
        assert [item["sourceUrl"] for item in partial["results"]] == ["https://test/b"]
        assert store.renew(partial["results"][0]["leaseId"])
        engine.gates["c"].set()
        await asyncio.wait_for(jobs.tasks[second["id"]], 1)
        assert stored_job(store, second["id"])["status"] == "ready"
        assert stored_job(store, first["id"])["status"] == "running"
        engine.gates["a"].set()
        await asyncio.wait_for(jobs.tasks[first["id"]], 1)
        assert [item["sourceUrl"] for item in stored_job(store, first["id"])["results"]] == [
            "https://test/a", "https://test/b",
        ]
        assert engine.peak == 2
        await jobs.close()
        store.close()

    asyncio.run(run())


def test_cancelling_message_drains_children_but_preserves_other_waiter(tmp_path: Path) -> None:
    async def run() -> None:
        engine = BarrierEngine()
        store = Store(tmp_path)
        jobs = Jobs(engine, store, "123")
        jobs.configure(ConfigInput(version="1"))
        first = jobs.create(request("first").model_copy(update={"text": "https://test/a https://test/b"}))
        await asyncio.wait_for(engine.two_started.wait(), 1)
        second = jobs.create(request("second").model_copy(update={"text": "https://test/a"}))
        # Wait for the second message to register on the shared preparation.
        for _ in range(20):
            if any(len(waiters) == 2 for _, waiters in jobs.shared.values()):
                break
            await asyncio.sleep(0)
        assert any(len(waiters) == 2 for _, waiters in jobs.shared.values())
        await jobs.cancel(first["id"])
        assert engine.cancelled_urls == {"https://test/b"}
        assert stored_job(store, first["id"])["status"] == "cancelled"
        engine.gates["a"].set()
        await asyncio.wait_for(jobs.tasks[second["id"]], 1)
        assert stored_job(store, second["id"])["status"] == "ready"
        assert engine.entered.count("https://test/a") == 1
        assert not jobs.shared
        assert engine.active == 0
        await jobs.close()
        store.close()

    asyncio.run(run())


def test_cancel_releases_early_lease_after_all_children_exit(tmp_path: Path) -> None:
    async def run() -> None:
        engine = BarrierEngine()
        store = Store(tmp_path)
        jobs = Jobs(engine, store, "123")
        jobs.configure(ConfigInput(version="1"))
        checkpointed = asyncio.Event()
        save = store.save_job

        def save_checkpoint(job: dict[str, Any], *args: Any) -> None:
            save(job, *args)
            if any(item.get("leaseId") for item in job["results"]):
                checkpointed.set()

        with patch.object(store, "save_job", side_effect=save_checkpoint):
            job = jobs.create(request("first").model_copy(update={"text": "https://test/a https://test/b"}))
            await asyncio.wait_for(engine.two_started.wait(), 1)
            engine.gates["b"].set()
            await asyncio.wait_for(checkpointed.wait(), 1)
            lease = stored_job(store, job["id"])["results"][0]["leaseId"]
            await jobs.cancel(job["id"])
            assert not store.renew(lease)
            assert not jobs.shared
            assert engine.active == 0
            assert engine.cancelled_urls == {"https://test/a"}
        await jobs.close()
        store.close()

    asyncio.run(run())
