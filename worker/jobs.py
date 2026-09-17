"""Cancellable per-caller jobs sharing content preparation, never sharing leases."""
import asyncio
import copy
import hashlib
import json
import logging
import uuid
from typing import Any

from worker.models import ConfigInput, JobInput
from worker.store import Store

logger = logging.getLogger("parsehub.worker")


class Jobs:
    def __init__(self, engine: Any, store: Store, bot_id: str, sender: Any = None) -> None:
        self.engine = engine
        self.store = store
        self.bot_id = bot_id
        self.sender = sender
        self.config: dict[str, Any] | None = None
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.shared: dict[str, tuple[asyncio.Task[tuple[str, dict[str, Any]]], set[str]]] = {}
        self.capacity = asyncio.Semaphore(2)

    def configure(self, config: ConfigInput) -> None:
        snapshot = config.model_dump()
        self.engine.configure(snapshot)
        self.config = snapshot

    def create(self, request: JobInput) -> dict[str, Any]:
        if request.accountId != self.bot_id:
            raise ValueError("bot_identity_mismatch")
        if self.config is None:
            raise ValueError("worker_not_configured")
        if request.delivery is not None and self.sender is None:
            raise ValueError("delivery_unavailable")
        fingerprint = hashlib.sha256(json.dumps(request.model_dump(exclude={"requestId", "idempotencyKey"}),
                                               sort_keys=True).encode()).hexdigest()
        existing = self.store.idempotent(request.idempotencyKey, fingerprint)
        if existing:
            if existing.get("delivery") is not None:
                # Delivery receipts outlive leases; re-querying must never send again.
                return existing
            if existing["status"] == "ready":
                # An idempotency key denotes one delivery attempt, not a cache key.
                # Do not silently resurrect a released or expired attempt.
                for item in existing["results"]:
                    if item.get("leaseId") and not self.store.renew(item["leaseId"]):
                        raise ValueError("job_expired")
            return existing
        if request.delivery is not None and not self.sender.ready:
            raise ValueError("delivery_not_ready")
        try:
            urls = list(dict.fromkeys(self.engine.extract_urls(request.text)))
        except Exception as error:
            code = getattr(error, "code", "unsupported_url")
            raise ValueError("too_many_links" if code == "too_many_urls" else "unsupported_url") from None
        if not urls or len(urls) > 10:
            raise ValueError("unsupported_url" if not urls else "too_many_links")
        job: dict[str, Any] = {"id": uuid.uuid4().hex, "status": "queued", "stage": "queued", "results": []}
        if request.delivery is not None:
            job["delivery"] = {"status": "pending", "surface": request.delivery.surface,
                               "messageIds": [], "completedFrames": 0, "inFlight": False}
        self.store.save_job(job, request.idempotencyKey, fingerprint)
        snapshot = copy.deepcopy(self.config)
        task = asyncio.create_task(self._run(job, request, urls, snapshot))
        self.tasks[job["id"]] = task
        task.add_done_callback(lambda _: self.tasks.pop(job["id"], None))
        return job

    @staticmethod
    def key(url: str, request: JobInput) -> str:
        transport = "direct" if request.delivery is not None else "files"
        return hashlib.sha256(
            f"v7-original-pipeline:{transport}:{request.accountId}:{request.outputMode}:{url}".encode()
        ).hexdigest()

    async def _prepare(self, url: str, request: JobInput, config: dict[str, Any], key: str,
                       job_id: str) -> tuple[str, dict[str, Any]]:
        owner = uuid.uuid4().hex

        async def progress(stage: str) -> None:
            # Only controlled stage names are logged, never URLs, credentials or upstream exceptions.
            stage = stage if stage in {"parse", "download", "convert", "upload", "ready"} else "prepare"
            current = asyncio.current_task()
            for shared_task, waiters in self.shared.values():
                if shared_task is current:
                    for waiter in waiters:
                        pending = self.store.job(waiter)
                        if pending and pending["status"] == "running":
                            pending["stage"] = stage
                            self.store.save_job(pending)
            logger.info("event=prepare.progress job=%s stage=%s", job_id, stage)

        try:
            async with self.capacity:
                result = await self.engine.prepare(
                    url, mode=request.mode, output_mode=request.outputMode,
                    config=config, progress=progress, directory=self.store.files,
                    refresh=request.refresh,
                    register_directory=lambda path: self.store.register_path(owner, path, "directory", existing=True),
                    register_file=lambda path: self.store.register_path(owner, path, "file"),
                    use_persistent_cache=request.delivery is not None,
                )
            if result.get("access") != "public":
                raise ValueError("content_restricted")
            # Never keep a partial conversion as the canonical cache result. A retry may
            # recover after a codec/runtime update, as with HEIC support in Worker.
            reusable = result.get("mediaFailureCount", 0) == 0
            # A read-only parse must not become an auto hit that suppresses future media downloads.
            reusable = reusable and request.mode != "read_only"
            if not result.get("_files"):
                self.store.discard_preparation(owner)
            canonical_key = self.key(result.get("canonicalUrl", url), request)
            cache_id = self.store.publish(canonical_key, result, self.store.files, [key], reusable, owner=owner)
            return cache_id, result
        except BaseException:
            self.store.discard_preparation(owner)
            raise

    async def _result(self, url: str, request: JobInput, config: dict[str, Any], job_id: str) -> dict[str, Any]:
        key = self.key(url, request)
        hit = None if request.refresh else self.store.lookup(key)
        if hit:
            cache_id, result = hit
        else:
            # Share only byte-identical submitted URLs. Resolving or cleaning a short URL
            # here would add a network request that the upstream ParseService does not own.
            shared_key = f"{key}:{request.mode}:{config['version']}:{request.refresh}"
            if shared_key not in self.shared:
                task = asyncio.create_task(self._prepare(url, request, config, key, job_id))
                self.shared[shared_key] = (task, set())
            task, waiters = self.shared[shared_key]
            waiters.add(job_id)
            try:
                cache_id, result = await asyncio.shield(task)
            finally:
                waiters.discard(job_id)
                if not waiters:
                    self.shared.pop(shared_key, None)
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        self.store.remember_alias(key, self.key(result.get("canonicalUrl", url), request))
        public = {key: copy.deepcopy(value) for key, value in result.items() if not key.startswith("_")}
        public["sourceUrl"] = url
        public["cacheHit"] = hit is not None
        public["leaseId"] = self.store.lease(cache_id)
        if request.mode == "read_only":
            public["media"] = []
        return public

    async def _run(self, job: dict[str, Any], request: JobInput, urls: list[str], config: dict[str, Any]) -> None:
        logger.info("event=job.start job=%s request=%s", job["id"],
                    json.dumps(request.requestId))
        job.update(status="running", stage="prepare")
        self.store.save_job(job)
        keeper = asyncio.create_task(self._keep_partial_leases(job))
        try:
            for url in urls:
                try:
                    result = await self._result(url, request, config, job["id"])
                    job["results"].append(result)
                except Exception as error:
                    code = getattr(error, "code", None)
                    if code not in {"unsupported_url", "upstream_challenge", "credentials_required",
                                    "credentials_invalid", "content_unavailable", "upstream_contract",
                                    "upstream_http", "content_restricted", "media_failed", "visibility_unknown",
                                    "media_processing_failed", "upload_failed", "file_too_large"}:
                        code = "prepare_failed"
                    job["results"].append({"sourceUrl": url, "error": {"code": code, "message": "解析准备失败"}})
                    logger.warning("event=prepare.failed job=%s code=%s", job["id"], code)
                self.store.save_job(job)
            if request.delivery is not None:
                job.update(stage="delivery")
                self.store.save_job(job)
                await self.sender.deliver(job, request.delivery, self.store, lambda: self.store.save_job(job))
                success = job["delivery"]["status"] == "sent"
                if not success:
                    logger.warning("event=delivery.incomplete job=%s delivery=%s frames=%s/%s code=%s",
                                   job["id"], job["delivery"]["status"], job["delivery"].get("completedFrames", 0),
                                   job["delivery"].get("totalFrames", 0),
                                   (job["delivery"].get("error") or {}).get("code", "none"))
                job.update(status="ready" if success else "failed", stage="delivered" if success else "delivery_failed")
                if not success:
                    job["error"] = {"code": "delivery_incomplete", "message": "部分或全部解析结果未完成交付"}
            else:
                job.update(status="ready", stage="ready")
        except asyncio.CancelledError:
            job.update(status="cancelled", stage="cancelled")
            if request.delivery is not None and job["delivery"]["status"] in {"pending", "sending"}:
                job["delivery"]["status"] = "unknown" if job["delivery"].get("inFlight") else "cancelled"
            if request.delivery is None:
                for item in job["results"]:
                    if item.get("leaseId"):
                        self.store.release(item["leaseId"])
        except Exception as error:
            logger.warning("event=job.failed job=%s stage=%s error_type=%s", job["id"], job.get("stage"),
                           type(error).__name__)
            job.update(status="failed", stage="failed", error={"code": "worker_internal", "message": "任务处理失败"})
            if request.delivery is not None:
                job["delivery"]["status"] = "unknown" if job["delivery"].get("inFlight") else "failed"
        finally:
            keeper.cancel()
            await asyncio.gather(keeper, return_exceptions=True)
            self.store.save_job(job)
            if request.delivery is not None:
                # Commit terminal receipt first. Cached files stay under the fixed TTL.
                for item in job["results"]:
                    if item.get("leaseId"):
                        self.store.release(item["leaseId"])
            logger.info("event=job.end job=%s status=%s", job["id"], job["status"])

    async def _keep_partial_leases(self, job: dict[str, Any], interval: float = 60) -> None:
        # A later batch item may take much longer than the lease lifetime.
        # gptbot starts renewing only after the complete job is ready.
        while True:
            for item in job["results"]:
                if item.get("leaseId"):
                    self.store.renew(item["leaseId"])
            await asyncio.sleep(interval)

    async def cancel(self, job_id: str) -> None:
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        job = self.store.job(job_id)
        if job:
            if job.get("delivery") is not None and job["status"] not in {"queued", "running"}:
                return
            for item in job["results"]:
                if item.get("leaseId"):
                    self.store.release(item["leaseId"])
            job.update(status="cancelled", stage="cancelled")
            self.store.save_job(job)

    async def close(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        for shared_task, _ in self.shared.values():
            shared_task.cancel()
        await asyncio.gather(*(task for task, _ in self.shared.values()), return_exceptions=True)
