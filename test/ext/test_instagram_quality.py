"""Offline Instagram response, real-media and downloader orchestration regressions."""

import asyncio
import copy
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from parsehub.errors import DownloadError, ParseError
from parsehub.parsers.parser.instagram import InstagramParser
from parsehub.provider_api.instagram import InstagramAPI, InstagramPost
from parsehub.types import ImageFile, VideoFile
from parsehub.utils.helpers import SecretCookie
from PIL import Image

from ext import instagram_download as downloads
from ext.hooks import install_instagram_quality
from ext.instagram_quality import AuthenticatedYtParser, candidates, graph_media, inline_recipe, v1_media


def image_item(media_id: str = "1") -> dict[str, Any]:
    return {"pk": media_id, "media_type": 1, "original_width": 4000, "original_height": 3000,
            "image_versions2": {"candidates": [
                {"url": "https://cdn.example/low.jpg", "width": 640, "height": 360},
                {"url": "https://cdn.example/high.jpg", "width": 1920, "height": 1080},
            ]}}


def video_item(media_id: str = "2", *, missing: bool = False) -> dict[str, Any]:
    return {**image_item(media_id), "media_type": 2, "video_duration": 0.2, "video_versions": [] if missing else [
        {"url": "https://cdn.example/low.mp4", "width": 640, "height": 360},
        {"url": "https://cdn.example/high.mp4", "width": 1920, "height": 1080},
    ]}


def post(items: list[dict[str, Any]]) -> InstagramPost:
    if len(items) == 1:
        return InstagramPost.from_v1({**items[0], "code": "fixture"})
    return InstagramPost.from_v1({"code": "fixture", "media_type": 8, "carousel_media": items})


@pytest.fixture(autouse=True)
def offline(monkeypatch: Any) -> None:
    install_instagram_quality()
    monkeypatch.setattr(
        "parsehub.parsers.base.ytdlp._run_ytdlp_json", AsyncMock(side_effect=ParseError("offline extraction")),
    )
    monkeypatch.setattr(downloads, "_run_ytdlp_download", AsyncMock(side_effect=AssertionError("unexpected download")))
    monkeypatch.setattr(downloads, "download_file", AsyncMock(side_effect=AssertionError("unexpected image download")))


@pytest.fixture(scope="module")
def files(tmp_path_factory: Any) -> dict[str, Path]:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    root = tmp_path_factory.mktemp("instagram-media")
    result = {}
    for name, width, height in (("low", 640, 360), ("high", 1920, 1080)):
        image = root / f"{name}.jpg"
        Image.new("RGB", (width, height), "red").save(image)
        result[f"{name}.jpg"] = image
        video = root / f"{name}.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=red:s={width}x{height}:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "+faststart", str(video),
        ], capture_output=True, check=True)
        result[f"{name}.mp4"] = video
    return result


def test_candidates_keep_identity_type_and_selected_dimensions() -> None:
    raw = video_item("123_456")
    original = copy.deepcopy(raw)
    media = v1_media(raw)
    assert media.media_id == "123" and media.is_video
    assert media.video_url.endswith("high.mp4") and (media.width, media.height) == (1920, 1080)
    assert len(media.images) == len(media.videos) == 2 and raw == original
    assert v1_media(video_item(missing=True)).is_video
    assert graph_media({"__typename": "GraphVideo", "id": "1"}).is_video
    image = graph_media({"__typename": "GraphImage", "display_url": "https://cdn.example/low.jpg",
                         "dimensions": {"width": 4000, "height": 3000}, "display_resources": [
                             {"src": "https://cdn.example/high.jpg", "config_width": 1920, "config_height": 1080},
                         ]})
    assert image.display_url.endswith("high.jpg") and (image.width, image.height) == (1920, 1080)
    assert candidates([None, {}, {"url": "file:///tmp/a"}]) == []
    assert candidates([{"url": "https://cdn.example/a", "width": "nan", "height": -1}])[0]["width"] == 0


def test_missing_carousel_videos_recover_by_id_not_embed_order(monkeypatch: Any) -> None:
    raw = post([video_item("1", missing=True), video_item("2", missing=True)])
    nodes = [{"node": {"__typename": "GraphVideo", "id": media_id, "video_duration": 0.2,
                       "video_url": f"https://cdn.example/{media_id}.mp4",
                       "dimensions": {"width": 1920, "height": 1080}}} for media_id in ("2", "1")]
    fetch = AsyncMock(return_value={"__typename": "GraphSidecar", "edge_sidecar_to_children": {"edges": nodes}})
    monkeypatch.setattr(InstagramAPI, "_fetch_embed", fetch)
    recovered = asyncio.run(InstagramAPI()._fill_missing_video_fields(None, raw, "fixture"))
    assert [m.video_url for m in recovered.media] == ["https://cdn.example/1.mp4", "https://cdn.example/2.mp4"]
    assert all(m.is_video for m in recovered.media) and fetch.await_count == 1


def test_unrecoverable_video_does_not_become_a_cover(monkeypatch: Any) -> None:
    raw = post([image_item("1"), video_item("2", missing=True)])
    monkeypatch.setattr(InstagramParser, "_parse", AsyncMock(return_value=raw))
    with pytest.raises(ParseError, match="instagram_video_unavailable"):
        asyncio.run(InstagramParser()._do_parse("https://www.instagram.com/p/fixture/"))


def test_cookie_reaches_single_video_extraction_and_playlist_is_rejected(monkeypatch: Any) -> None:
    value = {"id": "2", "title": "fixture", "duration": 0.2, "width": 1920, "height": 1080,
             "url": "https://cdn.example/high.mp4"}
    extract = AsyncMock(return_value=value)
    monkeypatch.setattr("parsehub.parsers.base.ytdlp._run_ytdlp_json", extract)
    monkeypatch.setattr(InstagramParser, "_parse", AsyncMock(return_value=post([video_item()])))
    parser = InstagramParser(cookie=SecretCookie({"sessionid": "SYNTHETIC_SECRET"}), proxy="http://proxy.invalid:8080")
    parsed = asyncio.run(parser._do_parse("https://www.instagram.com/reel/fixture/"))
    assert ".instagram.com\tTRUE" in extract.call_args.kwargs["cookie_text"]
    assert extract.call_args.kwargs["proxy"] == parser.proxy
    assert "SYNTHETIC_SECRET" not in json.dumps(parsed.to_dict())
    extract.return_value = {"_type": "playlist", "entries": [value, value]}
    with pytest.raises(ParseError, match="instagram_unexpected_playlist"):
        asyncio.run(AuthenticatedYtParser()._extract_info("https://www.instagram.com/p/fixture/"))


def test_inline_dash_preserves_video_and_audio_formats() -> None:
    media = v1_media(video_item())
    media.manifest = '''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
        mediaPresentationDuration="PT1S" minBufferTime="PT1S"><Period duration="PT1S">
        <AdaptationSet mimeType="video/mp4" contentType="video"><Representation id="4k" bandwidth="8000000"
        width="3840" height="2160" frameRate="30" codecs="avc1.640033">
        <BaseURL>https://cdn.example/4k.mp4</BaseURL></Representation></AdaptationSet>
        <AdaptationSet mimeType="audio/mp4" contentType="audio"><Representation id="audio"
        bandwidth="128000" codecs="mp4a.40.2" audioSamplingRate="48000">
        <BaseURL>https://cdn.example/audio.m4a</BaseURL></Representation></AdaptationSet></Period></MPD>'''
    recipe = inline_recipe(media)
    assert recipe and (recipe["width"], recipe["height"]) == (3840, 2160)
    assert len(recipe["requested_formats"]) == 2


def test_real_files_preserve_carousel_order_bytes_and_dimensions(
    tmp_path: Path, monkeypatch: Any, files: dict[str, Path],
) -> None:
    raw = post([image_item(), video_item()])
    monkeypatch.setattr(InstagramParser, "_parse", AsyncMock(return_value=raw))

    async def image(url: str, path: Path, **kwargs: Any) -> str:
        assert "Cookie" not in kwargs["headers"]
        await asyncio.sleep(0.01)
        shutil.copyfile(files[Path(url).name], path)
        return str(path)

    async def video(recipe: dict[str, Any], args: list[str], **kwargs: Any) -> None:
        assert "--cookies" not in args
        assert recipe["width"] == 1920
        shutil.copyfile(files["high.mp4"], kwargs["outtmpl"].replace("%(ext)s", "mp4"))

    monkeypatch.setattr(downloads, "download_file", image)
    monkeypatch.setattr(downloads, "_run_ytdlp_download", video)

    async def run() -> None:
        parsed = await InstagramParser()._do_parse("https://www.instagram.com/p/fixture/")
        result = await parsed.download(tmp_path, concurrency=2)
        assert [type(m) for m in result.media] == [ImageFile, VideoFile]
        for media in result.media:
            assert (media.width, media.height) == (1920, 1080)
            assert media._instagram_quality["matches_selected"]
            assert not media._instagram_quality["highest_confirmed"]
        assert Path(result.media[0].path).read_bytes() == files["high.jpg"].read_bytes()

    asyncio.run(run())


def test_download_fallback_records_downgrade_and_cookie_file_is_temporary(
    tmp_path: Path, monkeypatch: Any, files: dict[str, Path], caplog: Any,
) -> None:
    caplog.set_level(logging.INFO, logger="parsehub.quality")
    raw_item = video_item()
    raw_item["video_versions"] = raw_item["video_versions"][:1]
    monkeypatch.setattr(InstagramParser, "_parse", AsyncMock(return_value=post([raw_item])))
    monkeypatch.setattr("parsehub.parsers.base.ytdlp._run_ytdlp_json", AsyncMock(return_value={
        "id": "2", "title": "fixture", "duration": 0.2, "width": 1920, "height": 1080,
        "url": "https://cdn.example/high.mp4",
    }))
    jars: list[Path] = []
    calls: list[str] = []

    async def video(recipe: dict[str, Any], args: list[str], **kwargs: Any) -> None:
        jar = Path(args[args.index("--cookies") + 1])
        assert "SYNTHETIC_SECRET" in jar.read_text() and jar.stat().st_mode & 0o077 == 0
        assert "Cookie" not in kwargs["headers"]
        jars.append(jar)
        calls.append(recipe["url"])
        if recipe["width"] == 1920:
            raise RuntimeError("SYNTHETIC_SECRET must not leak")
        shutil.copyfile(files["low.mp4"], kwargs["outtmpl"].replace("%(ext)s", "mp4"))

    monkeypatch.setattr(downloads, "_run_ytdlp_download", video)

    async def run() -> None:
        parser = InstagramParser(cookie=SecretCookie({"sessionid": "SYNTHETIC_SECRET"}))
        parsed = await parser._do_parse("https://www.instagram.com/p/fixture/")
        result = await parsed.download(tmp_path, save_metadata=True)
        assert isinstance(result.media, VideoFile) and result.media.width == 640
        report = result.media._instagram_quality
        assert report["fallback"] and report["downgraded"] and report["matches_selected"]
        metadata_text = (result.output_dir / "metadata.json").read_text()
        assert "SYNTHETIC_SECRET" not in metadata_text
        metadata = json.loads(metadata_text)
        assert metadata["media"]["width"] == 640 and metadata["media"]["url"].endswith("low.mp4")
        assert metadata["download_quality"][0]["downgraded"]
        # The parse result may be shared by another request: its selection must not change.
        assert parsed.media.width == 1920

    asyncio.run(run())
    assert len(calls) == 2 and not any(jar.exists() for jar in jars)
    assert "SYNTHETIC_SECRET" not in caplog.text


def test_actual_resolution_mismatch_tries_next_candidate(
    tmp_path: Path, monkeypatch: Any, files: dict[str, Path],
) -> None:
    monkeypatch.setattr(InstagramParser, "_parse", AsyncMock(return_value=post([video_item()])))
    calls = []

    async def video(recipe: dict[str, Any], _args: list[str], **kwargs: Any) -> None:
        calls.append(recipe["width"])
        # The endpoint advertises 1080p but all supplied bytes are 360p.
        shutil.copyfile(files["low.mp4"], kwargs["outtmpl"].replace("%(ext)s", "mp4"))

    monkeypatch.setattr(downloads, "_run_ytdlp_download", video)

    async def run() -> None:
        parsed = await InstagramParser()._do_parse("https://www.instagram.com/p/fixture/")
        result = await parsed.download(tmp_path)
        assert result.media.width == 640 and result.media._instagram_quality["downgraded"]

    asyncio.run(run())
    assert calls[-1] == 640 and calls[0] == 1920


def test_cancel_waits_for_children_and_removes_partial_output(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(InstagramParser, "_parse", AsyncMock(return_value=post([image_item(str(i)) for i in range(4)])))

    async def run() -> None:
        active = peak = stopped = 0
        saturated = asyncio.Event()

        async def image(_url: str, path: Path, **_kwargs: Any) -> str:
            nonlocal active, peak, stopped
            path.write_bytes(b"partial")
            active += 1
            peak = max(peak, active)
            if active == 2:
                saturated.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
                stopped += 1
            return str(path)

        monkeypatch.setattr(downloads, "download_file", image)
        parsed = await InstagramParser()._do_parse("https://www.instagram.com/p/fixture/")
        task = asyncio.create_task(parsed.download(tmp_path, concurrency=2))
        await asyncio.wait_for(saturated.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0 and peak == stopped == 2 and not list(tmp_path.iterdir())

    asyncio.run(run())


def test_advertised_fps_mismatch_is_not_confirmed(
    tmp_path: Path, monkeypatch: Any, files: dict[str, Path],
) -> None:
    monkeypatch.setattr(InstagramParser, "_parse", AsyncMock(return_value=post([video_item()])))
    monkeypatch.setattr("parsehub.parsers.base.ytdlp._run_ytdlp_json", AsyncMock(return_value={
        "id": "2", "title": "fixture", "duration": 0.2, "width": 1920, "height": 1080, "fps": 60,
        "url": "https://cdn.example/high.mp4",
    }))
    calls = []

    async def video(recipe: dict[str, Any], _args: list[str], **kwargs: Any) -> None:
        calls.append(recipe.get("fps", 0))
        shutil.copyfile(files["high.mp4"], kwargs["outtmpl"].replace("%(ext)s", "mp4"))

    monkeypatch.setattr(downloads, "_run_ytdlp_download", video)

    async def run() -> None:
        parsed = await InstagramParser()._do_parse("https://www.instagram.com/p/fixture/")
        result = await parsed.download(tmp_path)
        assert result.media._instagram_quality["downgraded"]
        assert result.media._instagram_quality["fps"] == 30

    asyncio.run(run())
    assert calls == [60, 0]


def test_exhausted_downloads_are_bounded_and_safe(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(InstagramParser, "_parse", AsyncMock(return_value=post([image_item()])))
    failed = AsyncMock(side_effect=RuntimeError("SYNTHETIC_SECRET https://private.example/x"))
    monkeypatch.setattr(downloads, "download_file", failed)

    async def run() -> None:
        parsed = await InstagramParser()._do_parse("https://www.instagram.com/p/fixture/")
        with pytest.raises(DownloadError, match="instagram_media_download_failed") as error:
            await parsed.download(tmp_path)
        assert "SYNTHETIC_SECRET" not in str(error.value)

    asyncio.run(run())
    assert failed.await_count == 2 and not list(tmp_path.iterdir())
