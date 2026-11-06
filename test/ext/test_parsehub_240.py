"""Offline regressions for the ParseHub 2.4 worker integration."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from parsehub import ParseHub
from parsehub.errors import (
    ContentUnavailableError,
    DownloadError,
    DownloadFallbackError,
    FallbackAttempt,
    FallbackError,
    ParseError,
)
from parsehub.types import DownloadResult, ImageParseResult, ImageRef, MusicFile, MusicParseResult, MusicRef
from pyrogram import raw
from pyrogram.file_id import FileId, FileType

from ext import hooks
from ext.delivery.assets import cache_assets, cached_assets, pipeline_assets, worker_cached_media
from ext.delivery.models import Destination
from ext.delivery.transport import TelegramTransport
from ext.media.processing import process_media_files
from ext.parsehub_compat import bounded_download, download_limit
from ext.worker.config import WorkerSettings
from ext.worker.engine import ParseHubEngine
from ext.worker.upstream_adapter import UpstreamOutput
from services import ParseService, PipelineResult


@pytest.mark.parametrize("method", ["parse", "get_raw_url"])
def test_unavailable_stops_retry_but_transient_errors_still_retry(method: str, monkeypatch: Any) -> None:
    hooks.install()
    service = ParseService()
    target = AsyncMock(side_effect=ContentUnavailableError())
    monkeypatch.setattr(service.parser, method, target)
    url = "https://www.bilibili.com/video/BV1234567890"
    with pytest.raises(ContentUnavailableError):
        asyncio.run(getattr(service, method)(url))
    assert target.await_count == 1

    target.reset_mock()
    target.side_effect = [TimeoutError(), ParseError("temporary"), "ok"]
    assert asyncio.run(getattr(service, method)(url)) == "ok"
    assert target.await_count == 3


def test_fallback_error_uses_final_attempt_and_download_status(caplog: Any) -> None:
    # A past auth failure must not hide the final timeout, even without __cause__.
    error = FallbackError([
        FallbackAttempt("cookie", ParseError("login required synthetic-private-cookie")),
        FallbackAttempt("mobile", TimeoutError()),
    ])
    result = ParseHubEngine._log_failure("parse", "xhs", error, True)
    assert ParseHubEngine._failure_code(result, True) == "upstream_timeout"
    unavailable = ContentUnavailableError()
    unavailable.__cause__ = DownloadError("synthetic", status_code=404)
    result = ParseHubEngine._log_failure("parse", "douyin", unavailable, False)
    assert ParseHubEngine._failure_code(result, False) == "content_unavailable"
    result = ParseHubEngine._log_failure("download", "douyin", DownloadFallbackError([
        FallbackAttempt("cdn", DownloadError("synthetic-private-url", status_code=403)),
    ]), False)
    assert result.http_status == 403
    assert ParseHubEngine._failure_code(result, False) == "media_failed"
    assert "synthetic-private" not in caplog.text


def test_music_preparation_worker_audio_upload_and_cache(tmp_path: Path, monkeypatch: Any) -> None:
    hooks.install()
    folder = tmp_path / "track"
    folder.mkdir()
    path = folder / "music.m4a"
    path.write_bytes(b"synthetic music fixture")
    duration = 12
    source = MusicFile(path=path, duration=duration)
    # Music is passed through; a video/image probe would be an integration regression.
    monkeypatch.setattr("ext.media.processing.ForkMediaProcessingUnit.process", AsyncMock(
        side_effect=AssertionError("music must not enter video/image processing"),
    ))

    async def run() -> None:
        processed = await process_media_files(DownloadResult(media=source, output_dir=folder))
        parsed = MusicParseResult(title="Music", music=MusicRef(url="https://cdn.example/a", duration=duration))
        parsed.raw_url = "https://www.bilibili.com/audio/au12345"
        pipeline = PipelineResult(parse_result=parsed, processed_list=processed, output_dir=folder)
        engine = ParseHubEngine(tmp_path, SimpleNamespace(parser=ParseHub()))
        monkeypatch.setattr(engine.upstream, "prepare", AsyncMock(return_value=UpstreamOutput(
            raw_url=parsed.raw_url, pipeline=pipeline,
        )))
        preview = await engine.prepare(parsed.raw_url)
        assert preview["resultType"] == "music"
        assert preview["media"][0]["type"] == "audio"
        assert preview["media"][0]["durationSeconds"] == duration
        original = await engine.prepare(parsed.raw_url, output_mode="raw")
        assert original["media"][0]["type"] == "document"
        assets = pipeline_assets(processed, "music")
        assert assets[0].type == "audio" and assets[0].duration == duration
        assert pipeline_assets(processed, "music", raw=True)[0].type == "document"

        document = raw.types.Document(
            id=12, access_hash=13, file_reference=b"audio-ref", date=0,
            mime_type="audio/mp4", size=path.stat().st_size, dc_id=2, attributes=[],
        )
        client = SimpleNamespace(
            resolve_peer=AsyncMock(return_value=raw.types.InputPeerSelf()),
            save_file=AsyncMock(return_value=raw.types.InputFile(id=1, parts=1, name="music", md5_checksum="")),
            guess_mime_type=lambda _: "audio/mp4",
            invoke=AsyncMock(return_value=raw.types.MessageMediaDocument(document=document)),
        )
        uploaded = await TelegramTransport(client, "123").upload(assets[0], Destination(chat_id=123), rich=False)
        request = client.invoke.call_args.args[0].media
        attribute = next(a for a in request.attributes if isinstance(a, raw.types.DocumentAttributeAudio))
        assert attribute.duration == duration and not attribute.voice
        assert not request.force_file
        assert FileId.decode(uploaded.refs["media"]).file_type == FileType.AUDIO
        cached = cache_assets(assets, [{"key": assets[0].key, "refs": uploaded.refs, "representation": "native"}])
        assert cached_assets(cached)[0].type == "audio"
        assert worker_cached_media(cached[0])["durationSeconds"] == duration

    asyncio.run(run())


def test_download_limits_are_task_local_and_reset_after_failure() -> None:
    async def original(_self: Any, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return kwargs

    wrapped = bounded_download(original)

    async def worker(limit: int) -> dict[str, Any]:
        with download_limit(limit):
            return await wrapped(None, concurrency=8)

    async def run() -> None:
        first, second = await asyncio.gather(worker(1), worker(2))
        assert first["concurrency"] == 1 and second["concurrency"] == 2
        with pytest.raises(RuntimeError), download_limit(1):
            raise RuntimeError("synthetic")
        assert await wrapped(None) == {}
        with download_limit(2):
            assert (await wrapped(None, concurrency=1))["concurrency"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_real_downloader_respects_limit_order_and_cancellation(
    tmp_path: Path, monkeypatch: Any, cancel: bool,
) -> None:
    hooks.install()

    async def run() -> None:
        active = peak = cancelled = 0
        saturated, release = asyncio.Event(), asyncio.Event()

        async def download(url: str, path: Path, **_kwargs: Any) -> Path:
            nonlocal active, peak, cancelled
            active += 1
            peak = max(peak, active)
            if active == 2:
                saturated.set()
            try:
                path.write_text("partial")
                await release.wait()
                if url.endswith("/0"):
                    await asyncio.sleep(0.01)
                path.write_text(url)
                return path
            except asyncio.CancelledError:
                cancelled += 1
                raise
            finally:
                active -= 1

        monkeypatch.setattr("parsehub.types.result.download", download)
        parsed = ImageParseResult(title="images", photo=[
            ImageRef(url=f"https://cdn.example/{i}", width=10, height=10) for i in range(4)
        ])

        async def execute() -> Any:
            with download_limit(2):
                return await parsed.download(tmp_path)

        task = asyncio.create_task(execute())
        await asyncio.wait_for(saturated.wait(), 2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled == 2
            assert not list(tmp_path.iterdir())
        else:
            release.set()
            result = await task
            assert [Path(f.path).read_text() for f in result.media] == [f"https://cdn.example/{i}" for i in range(4)]
        assert peak == 2 and active == 0

    asyncio.run(run())


@pytest.mark.parametrize("limit", [0, 9])
def test_worker_download_limit_rejects_invalid_settings(limit: int) -> None:
    with pytest.raises(ValueError, match="worker_download_concurrency"):
        WorkerSettings(
            _env_file=None, bot_token="123:test", api_id=123, api_hash="test",
            worker_service_key="k" * 32, worker_download_concurrency=limit,
        )
