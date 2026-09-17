"""Authenticated loopback API. No bot plugins or update receiver are imported."""
import asyncio
import contextlib
import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast

from aiohttp import web
from pydantic import ValidationError

from worker.jobs import Jobs
from worker.models import JobInput
from worker.platform_config import ConfigConflict, PlatformConfigFile


def create_app(jobs: Jobs, service_key: str, platform_config: PlatformConfigFile | None = None) -> web.Application:
    @web.middleware
    async def authenticated(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {service_key}"):
            return web.json_response({"error": {"code": "unauthorized", "message": "需要服务认证"}}, status=401)
        try:
            return cast(web.StreamResponse, await handler(request))
        except (ValidationError, json.JSONDecodeError):
            return web.json_response({"error": {"code": "invalid_request", "message": "请求参数无效"}}, status=400)
        except ConfigConflict:
            return web.json_response({"error": {"code": "config_conflict", "message": "配置已更新，请刷新后重试"}},
                                     status=409)
        except ValueError as error:
            code = str(error)
            if code not in {"bot_identity_mismatch", "worker_not_configured", "unsupported_url",
                            "too_many_links", "idempotency_conflict", "job_expired", "delivery_unavailable",
                            "delivery_not_ready"}:
                code = "invalid_request"
            return web.json_response({"error": {"code": code, "message": "请求无法执行"}}, status=409)
        except web.HTTPException:
            raise
        except Exception:
            return web.json_response({"error": {"code": "worker_internal", "message": "服务处理失败"}}, status=500)

    app = web.Application(middlewares=[authenticated], client_max_size=1024 * 1024)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"protocolVersion": 2, "botId": jobs.bot_id,
                                  "ready": jobs.config is not None, "version": "0.1.0",
                                  "directDelivery": jobs.sender is not None,
                                  **(jobs.sender.health() if jobs.sender else {"deliveryReady": False}),
                                  "configSource": "platform_config.yaml"})

    async def capabilities(request: web.Request) -> web.Response:
        return web.json_response({**jobs.engine.capabilities(), "protocolVersion": 2,
                                  "directDelivery": jobs.sender is not None})

    async def read_config(request: web.Request) -> web.Response:
        if platform_config is None:
            return web.json_response({"error": {"code": "config_unavailable"}}, status=503)
        return web.json_response(platform_config.snapshot())

    async def configure(request: web.Request) -> web.Response:
        if platform_config is None:
            return web.json_response({"error": {"code": "config_unavailable"}}, status=503)
        update = await request.json()
        if not isinstance(update, dict):
            raise ValueError("invalid_config_update")
        return web.json_response(platform_config.update(update))

    async def create_job(request: web.Request) -> web.Response:
        result = jobs.create(JobInput.model_validate(await request.json()))
        return web.json_response(result, status=202)

    async def get_job(request: web.Request) -> web.Response:
        job = jobs.store.job(request.match_info["id"])
        if job is None:
            raise web.HTTPNotFound()
        return web.json_response(job)

    async def cancel_job(request: web.Request) -> web.Response:
        await jobs.cancel(request.match_info["id"])
        return web.json_response({"ok": True})

    async def renew_lease(request: web.Request) -> web.Response:
        if not jobs.store.renew(request.match_info["id"]):
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def release_lease(request: web.Request) -> web.Response:
        jobs.store.release(request.match_info["id"])
        return web.json_response({"ok": True})

    async def read_media(request: web.Request) -> web.StreamResponse:
        lease_id = request.match_info['id']
        media = jobs.store.media_file(lease_id, request.match_info['media_id'])
        if media is None:
            raise web.HTTPNotFound()
        path, mime, size = media
        jobs.store.renew(lease_id)
        response = web.StreamResponse(headers={
            'Content-Type': mime, 'Content-Length': str(size), 'Cache-Control': 'no-store',
        })
        # Open before returning control to cleanup/release; an in-flight read
        # owns this descriptor even if the caller releases the cache lease.
        with path.open('rb') as file:
            await response.prepare(request)
            while chunk := await asyncio.to_thread(file.read, 256 * 1024):
                await response.write(chunk)
            await response.write_eof()
        return response

    async def lifecycle(application: web.Application) -> AsyncIterator[None]:
        jobs.store.cleanup()

        async def cleanup_loop() -> None:
            while True:
                await asyncio.sleep(600)
                jobs.store.cleanup()

        cleaner = asyncio.create_task(cleanup_loop())
        yield
        cleaner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleaner
        await jobs.close()

    app.cleanup_ctx.append(lifecycle)
    app.add_routes([
        web.get("/api/v1/health", health), web.get("/api/v1/capabilities", capabilities),
        web.get("/api/v1/config", read_config), web.put("/api/v1/config", configure),
        web.post("/api/v1/jobs", create_job),
        web.get("/api/v1/leases/{id}/media/{media_id}", read_media),
        web.get("/api/v1/jobs/{id}", get_job), web.delete("/api/v1/jobs/{id}", cancel_job),
        web.put("/api/v1/leases/{id}", renew_lease), web.delete("/api/v1/leases/{id}", release_lease),
    ])
    return app
