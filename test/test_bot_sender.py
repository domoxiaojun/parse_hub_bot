"""Regression tests for the interactive Bot adapter around the shared delivery core."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from delivery.models import DeliveryError
from plugins.parse.sender import MessageSender, envelope_for, send_raw, send_zip
from repo.settings import SettingsConfig
from services.pipeline import PipelineResult


class Reporter:
    def __init__(self) -> None:
        self.errors: list[tuple[str, Exception]] = []
        self.dismissed = 0

    async def report_error(self, stage: str, error: Exception) -> None:
        self.errors.append((stage, error))

    async def dismiss(self) -> None:
        self.dismissed += 1


def sender() -> MessageSender:
    message = SimpleNamespace(
        chat=SimpleNamespace(id=123), message_thread_id=None, id=456,
    )
    return MessageSender(SimpleNamespace(), message, SettingsConfig())


def parse_result() -> SimpleNamespace:
    return SimpleNamespace(raw_url="https://example.com/post", title="标题", content="正文")


def test_custom_content_is_appended_to_parsed_body() -> None:
    envelope = envelope_for(sender(), parse_result(), (), "附加说明")
    assert envelope.body == "正文\n\n附加说明"


def test_raw_delivery_failure_reports_error_and_cleans(monkeypatch) -> None:
    async def run() -> None:
        reporter = Reporter()
        result = PipelineResult(parse_result())
        failure = DeliveryError("upload_failed")
        monkeypatch.setattr("plugins.parse.sender.send_content", AsyncMock(side_effect=failure))
        assert await send_raw(sender(), result, reporter, _t=lambda value: value) is False
        assert reporter.errors == [("上传", failure)]

    asyncio.run(run())


def test_zip_pack_failure_reports_error(monkeypatch, tmp_path: Path) -> None:
    async def run() -> None:
        reporter = Reporter()
        output = tmp_path / "output"
        output.mkdir()
        result = PipelineResult(parse_result(), output_dir=output)
        failure = OSError("disk full")
        monkeypatch.setattr("plugins.parse.sender.pack_dir_to_tar_gz", lambda *_: (_ for _ in ()).throw(failure))
        assert await send_zip(sender(), result, reporter, _t=lambda value: value) is False
        assert reporter.errors == [("上传", failure)]

    asyncio.run(run())
