import asyncio
import shutil
import subprocess

import httpx
import pytest

from ext.media import video_probe


@pytest.fixture(params=[False, True], ids=["moov_at_end", "faststart"])
def real_video(tmp_path, request):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required for real media verification")
    path = tmp_path / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
        "-t", "0.5", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        *(["-movflags", "+faststart"] if request.param else []), str(path),
    ], check=True, capture_output=True)
    return path


def test_real_ffprobe_with_bounded_http_ranges_and_both_moov_positions(real_video, monkeypatch):
    data = real_video.read_bytes()
    monkeypatch.setattr(video_probe, "SAMPLE_BYTES", 4096)
    served = []

    def serve(request):
        start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
        end = min(end, len(data) - 1)
        served.append(end - start + 1)
        return httpx.Response(206, headers={
            "content-range": f"bytes {start}-{end}/{len(data)}", "content-type": "video/mp4",
        }, content=data[start:end + 1])

    async def run():
        local = await video_probe.probe_video(real_video)
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            remote = await video_probe.probe_remote_video(client, "https://cdn.example/?ratio=1080p")
        # The request label says 1080p, but the actual object is 720p.
        assert (remote["width"], remote["height"]) == (1280, 720)
        assert remote["codec"] == local["codec"] == "h264"
        assert remote["fps"] == local["fps"] == 30
        assert remote["bitrate"] == local["bitrate"] > 0
        assert remote["content_length"] == len(data)
    asyncio.run(run())
    assert sum(served) <= 8192


@pytest.mark.parametrize("headers,status", [
    ({"content-range": "bytes 10-13/20"}, 206),
    ({"content-range": "bytes 0-3/*"}, 206),
    ({"content-length": "9999999999999"}, 200),
])
def test_rejects_invalid_range_or_excessive_sparse_size(headers, status):
    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(status, headers=headers, content=b"data"))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await video_probe._read_range(client, "https://cdn.example/video", 0, 3)
    asyncio.run(run())


def test_server_ignoring_tail_range_is_not_mistaken_for_video_tail():
    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"12345678"))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError, match="range_not_supported"):
                await video_probe._read_range(client, "https://cdn.example/video", 4, 7)
    asyncio.run(run())


def test_cancelled_probe_terminates_subprocess(monkeypatch, tmp_path):
    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            raise asyncio.CancelledError

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True

    process = Process()

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(video_probe.probe_video(tmp_path / "unused.mp4"))
    assert process.killed and process.waited
