"""Worker adapter tests; parsing and media preparation stay in original services."""

import ast
import asyncio
import logging
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from parsehub import DownloadResult, ParseHub
from parsehub.errors import ParseError
from parsehub.types import AniFile, ImageFile, LivePhotoFile, PostType, VideoFile

from services import CacheEntry, CacheMedia, CacheMediaType, CacheParseResult, PipelineResult
from services.media import ProcessedMedia
from worker.engine import ParseHubEngine
from worker.security import EngineError, validate_url
from worker.upstream_adapter import UpstreamOutput


def run(coro):
    return asyncio.run(coro)


def parsed(*, kind: str = "image", markdown: str | None = None):
    value = SimpleNamespace(
        title="title",
        content="body",
        raw_url="https://www.xiaohongshu.com/explore/canonical",
        type=SimpleNamespace(value=kind),
    )
    if markdown is not None:
        value.markdown_content = markdown
    return value


def service(result=None):
    parser = SimpleNamespace(
        get_platform=lambda _url: SimpleNamespace(id="xhs"),
        get_platforms=lambda: [{"id": "xhs", "name": "小红书", "supported_types": ["image", "video"]}],
    )
    return SimpleNamespace(
        parser=parser,
        get_raw_url=AsyncMock(return_value="https://www.xiaohongshu.com/explore/canonical"),
        parse=AsyncMock(return_value=result or parsed()),
    )


def engine(tmp_path: Path, result=None) -> ParseHubEngine:
    return ParseHubEngine(tmp_path, service(result))


def test_all_installed_platforms_are_exposed(tmp_path: Path) -> None:
    upstream = SimpleNamespace(parser=ParseHub())
    actual = ParseHubEngine(tmp_path, upstream)
    assert {p["id"] for p in actual.capabilities()["platforms"]} == {
        p["id"] for p in upstream.parser.get_platforms()
    }
    assert len(actual.capabilities()["platforms"]) > 7


def test_extract_deduplicates_limits_and_validates_public_input(tmp_path: Path) -> None:
    instance = engine(tmp_path)
    assert instance.extract_urls("标题 https://xhslink.cn/a https://xhslink.cn/a。") == ["https://xhslink.cn/a"]
    with pytest.raises(EngineError, match="too_many_urls"):
        instance.extract_urls(" ".join(f"https://xhslink.cn/{i}" for i in range(11)))
    for url in ("http://127.0.0.1/x", "http://localhost/x", "https://user:pw@example.com/x"):
        with pytest.raises(EngineError):
            validate_url(url)


def test_original_persistent_cache_hit_skips_parse_and_pipeline(tmp_path: Path, monkeypatch) -> None:
    upstream_service = service()
    instance = ParseHubEngine(tmp_path, upstream_service)
    cached = CacheEntry(
        parse_result=CacheParseResult(title="cached", content="cached body"),
        media=[
            CacheMedia(type=CacheMediaType.PHOTO, file_id="photo-file-id"),
            CacheMedia(type=CacheMediaType.VIDEO, file_id="video-file-id", cover_file_id="cover-file-id"),
        ],
    )
    persistent_get = AsyncMock(return_value=cached)
    parse_get = AsyncMock(side_effect=AssertionError("parse cache must not run after persistent hit"))
    monkeypatch.setattr("worker.upstream_adapter.persistent_cache.get", persistent_get)
    monkeypatch.setattr("worker.upstream_adapter.parse_cache.get", parse_get)

    result = run(instance.prepare("https://xhslink.cn/a", use_persistent_cache=True))

    persistent_get.assert_awaited_once_with("https://www.xiaohongshu.com/explore/canonical")
    upstream_service.parse.assert_not_awaited()
    assert result["canonicalUrl"] == "https://www.xiaohongshu.com/explore/canonical"
    assert result["media"] == [
        {"type": "photo", "telegramFileId": "photo-file-id"},
        {"type": "video", "telegramFileId": "video-file-id", "telegramCoverFileId": "cover-file-id"},
    ]
    assert result["_files"] == [] and result["_mediaFiles"] == {}


def test_refresh_bypasses_both_original_caches(tmp_path: Path, monkeypatch) -> None:
    instance = engine(tmp_path)
    persistent_get = AsyncMock(side_effect=AssertionError("refresh must bypass persistent cache"))
    parse_get = AsyncMock(side_effect=AssertionError("refresh must bypass parse cache"))
    monkeypatch.setattr("worker.upstream_adapter.persistent_cache.get", persistent_get)
    monkeypatch.setattr("worker.upstream_adapter.parse_cache.get", parse_get)
    created = []

    class FakePipeline:
        def __init__(self, *_args, **kwargs):
            created.append(kwargs)

        async def run(self):
            return PipelineResult(parse_result=parsed())

        def finish(self):
            pass

    monkeypatch.setattr("worker.upstream_adapter.ParsePipeline", FakePipeline)
    result = run(instance.prepare("https://xhslink.cn/a", refresh=True, use_persistent_cache=True))

    persistent_get.assert_not_awaited()
    parse_get.assert_not_awaited()
    assert created[0]["parse_result"] is None
    assert result["plainContent"] == "body"


@pytest.mark.parametrize(
    ("output_mode", "skip_processing", "save_metadata"),
    [("preview", False, False), ("raw", True, False), ("zip", True, True)],
)
def test_original_pipeline_decides_download_and_processing(
    tmp_path: Path, monkeypatch, output_mode: str, skip_processing: bool, save_metadata: bool,
) -> None:
    instance = engine(tmp_path)
    monkeypatch.setattr("worker.upstream_adapter.parse_cache.get", AsyncMock(return_value=None))
    created = []

    class FakePipeline:
        def __init__(self, *_args, **kwargs):
            created.append(kwargs)

        async def run(self):
            return PipelineResult(parse_result=parsed())

        def finish(self):
            pass

    monkeypatch.setattr("worker.upstream_adapter.ParsePipeline", FakePipeline)
    run(instance.prepare("https://xhslink.cn/a", output_mode=output_mode))

    assert created[0]["skip_media_processing"] is skip_processing
    assert created[0]["save_metadata"] is save_metadata
    assert created[0]["download_dir"] == tmp_path


def test_actual_original_pipeline_owns_download_behavior(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "original-title"
    folder.mkdir()
    path = folder / "original.mp4"
    path.write_bytes(b"original bytes")
    media_file = VideoFile(path=path, width=32, height=16, duration=1)
    source = SimpleNamespace(
        title="original-title",
        content="body",
        raw_url="https://www.xiaohongshu.com/explore/canonical",
        type=PostType.VIDEO,
        media=[SimpleNamespace()],
        download=AsyncMock(return_value=DownloadResult(media=media_file, output_dir=folder)),
    )
    instance = engine(tmp_path)
    monkeypatch.setattr("worker.upstream_adapter.parse_cache.get", AsyncMock(return_value=source))
    monkeypatch.setattr("worker.upstream_adapter.parse_cache.set", AsyncMock())

    result = run(instance.prepare("https://xhslink.cn/a", output_mode="raw"))

    source.download.assert_awaited_once()
    assert source.download.call_args.args[0] == tmp_path
    assert source.download.call_args.kwargs["save_metadata"] is False
    assert result["media"][0]["filename"] == "original.mp4"


def test_read_only_uses_original_parse_cache_key_and_never_downloads(tmp_path: Path, monkeypatch) -> None:
    upstream_service = service()
    instance = ParseHubEngine(tmp_path, upstream_service)
    parse_get = AsyncMock(return_value=None)
    parse_set = AsyncMock()
    monkeypatch.setattr("worker.upstream_adapter.parse_cache.get", parse_get)
    monkeypatch.setattr("worker.upstream_adapter.parse_cache.set", parse_set)

    result = run(instance.prepare("https://xhslink.cn/a", mode="read_only"))

    parse_get.assert_awaited_once_with("https://www.xiaohongshu.com/explore/canonical")
    parse_set.assert_awaited_once_with(
        "https://www.xiaohongshu.com/explore/canonical", upstream_service.parse.return_value,
    )
    upstream_service.parse.assert_awaited_once_with("https://xhslink.cn/a")
    assert result["media"] == [] and result["plainContent"] == "body"
    assert not list(tmp_path.iterdir())


def pipeline_output(instance: ParseHubEngine, value: PipelineResult) -> None:
    instance.upstream.prepare = AsyncMock(
        return_value=UpstreamOutput(
            raw_url="https://www.xiaohongshu.com/explore/canonical",
            pipeline=value,
        )
    )


def test_preview_adapts_original_processed_media_and_live_photo(tmp_path: Path) -> None:
    folder = tmp_path / "title"
    folder.mkdir()
    photo = folder / "photo.jpg"
    photo.write_bytes(b"photo")
    animation = folder / "move.gif"
    animation.write_bytes(b"animation")
    video = folder / "clip.mp4"
    video.write_bytes(b"video")
    live_photo = folder / "live.jpg"
    live_photo.write_bytes(b"still")
    live_video = folder / "live.mp4"
    live_video.write_bytes(b"live-video")
    processed = [
        ProcessedMedia(ImageFile(path=photo, width=20, height=10)),
        ProcessedMedia(AniFile(path=animation, width=20, height=10, duration=2)),
        ProcessedMedia(VideoFile(path=video, width=20, height=10, duration=3)),
        ProcessedMedia(LivePhotoFile(path=live_photo, video_path=live_video, width=20, height=10, duration=3)),
    ]
    instance = engine(tmp_path)
    pipeline_output(instance, PipelineResult(parse_result=parsed(kind="multimedia"), processed_list=processed,
                                               output_dir=folder))
    registered = []

    result = run(instance.prepare("https://xhslink.cn/a", register_directory=registered.append))

    assert registered == [folder]
    assert [item["type"] for item in result["media"]] == ["photo", "animation", "video", "live_photo"]
    live = result["media"][-1]
    assert live["durationSeconds"] == 3 and live["mediaId"] != live["videoMediaId"]
    assert set(result["_mediaFiles"]) == {
        item["mediaId"] for item in result["media"][:-1]
    } | {live["mediaId"], live["videoMediaId"]}


def test_raw_and_zip_preserve_original_pipeline_outputs(tmp_path: Path) -> None:
    folder = tmp_path / "title"
    folder.mkdir()
    still = folder / "live.jpg"
    still.write_bytes(b"still")
    video = folder / "live.mov"
    video.write_bytes(b"video")
    (folder / "metadata.json").write_text('{"title":"original"}')
    processed = ProcessedMedia(LivePhotoFile(path=still, video_path=video, width=20, height=10, duration=3), [still])

    raw_engine = engine(tmp_path)
    pipeline_output(raw_engine, PipelineResult(parse_result=parsed(), processed_list=[processed], output_dir=folder))
    raw = run(raw_engine.prepare("https://xhslink.cn/a", output_mode="raw"))
    assert [item["type"] for item in raw["media"]] == ["document", "document"]
    assert [item["filename"] for item in raw["media"]] == ["live.jpg", "live.mov"]

    zip_root = tmp_path / "zip"
    zip_root.mkdir()
    zip_folder = zip_root / "title"
    zip_folder.mkdir()
    (zip_folder / "metadata.json").write_text('{"title":"original"}')
    zip_engine = engine(zip_root)
    pipeline_output(zip_engine, PipelineResult(parse_result=parsed(), processed_list=[], output_dir=zip_folder))
    registered_files = []
    archive = run(zip_engine.prepare(
        "https://xhslink.cn/a", output_mode="zip", register_file=registered_files.append,
    ))
    assert registered_files == [zip_root / "title.tar.gz"]
    assert archive["media"][0]["type"] == "document"
    with tarfile.open(zip_root / "title.tar.gz") as bundle:
        assert "title/metadata.json" in bundle.getnames()


def test_richtext_preserves_source_markdown_without_download(tmp_path: Path) -> None:
    instance = engine(tmp_path)
    pipeline_output(instance, PipelineResult(parse_result=parsed(kind="richtext", markdown="## source")))
    result = run(instance.prepare("https://xhslink.cn/a"))
    assert result["resultType"] == "richtext" and result["contentType"] == "article"
    assert result["content"] == "## source" and result["plainContent"] == "body"
    assert result["media"] == []


def test_upstream_failure_has_safe_diagnostics(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="parsehub.worker")
    instance = engine(tmp_path)
    instance.configure({"platforms": {"xhs": {"cookies": ["secret"]}}})
    instance.service.get_raw_url.side_effect = ParseError(
        "login required Cookie=private https://example.test/?token=secret"
    )
    with pytest.raises(EngineError, match="credentials_invalid"):
        run(instance.prepare("https://xhslink.cn/a"))
    assert "reason=login_required" in caplog.text and "credential_used=true" in caplog.text
    assert "event=upstream.diagnostic" in caplog.text
    assert "private" not in caplog.text and "token=secret" not in caplog.text


def test_engine_has_no_custom_media_or_upload_dependency() -> None:
    source = Path(__file__).parents[1].joinpath("worker", "engine.py").read_text()
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "worker.media" not in imports and "worker.upload" not in imports
