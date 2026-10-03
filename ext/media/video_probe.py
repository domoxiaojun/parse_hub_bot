"""Bounded, proxy-aware MP4 header probing and local video verification."""

import asyncio
import json
import math
import re
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

import httpx

SAMPLE_BYTES = 2 * 1024 * 1024
MAX_OBJECT_BYTES = 8 * 1024**3


class VideoProbeError(ValueError):
    """A controlled diagnostic code that is safe to include in logs."""


async def probe_video(path: Path) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,avg_frame_rate,bit_rate,duration:format=duration,bit_rate",
        "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise VideoProbeError("ffprobe_failed")
    payload = json.loads(stdout)
    stream: dict[str, Any] = next(iter(payload.get("streams", [])), {})
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    if width <= 0 or height <= 0:
        raise VideoProbeError("video_dimensions_missing")
    try:
        fps = float(Fraction(stream.get("avg_frame_rate", "0")))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    duration = float(stream.get("duration") or payload.get("format", {}).get("duration") or 0)
    if not math.isfinite(duration):
        duration = 0.0
    return {
        "width": width, "height": height, "fps": fps,
        "codec": stream.get("codec_name", "unknown"),
        "bitrate": int(stream.get("bit_rate") or 0),
        "duration": duration,
    }


async def _read_range(
    client: httpx.AsyncClient, url: str, start: int, end: int,
) -> tuple[bytes, int, str]:
    headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
    async with client.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        if response.headers.get("content-encoding", "identity") != "identity":
            raise VideoProbeError("encoded_range")
        total = int(response.headers.get("content-length") or 0)
        if response.status_code == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
            if not match or int(match[1]) != start or int(match[2]) > end:
                raise VideoProbeError("invalid_content_range")
            total = int(match[3])
        elif start:
            raise VideoProbeError("range_not_supported")
        if total <= 0 or total > MAX_OBJECT_BYTES:
            raise VideoProbeError("unknown_or_excessive_object_size")
        data = bytearray()
        limit = min(end - start + 1, total - start)
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            data.extend(chunk[:limit - len(data)])
            if len(data) >= limit:
                break
        if len(data) != limit:
            raise VideoProbeError("truncated_range")
        return bytes(data), total, str(response.url)


async def probe_remote_video(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """Read at most 4 MiB per candidate; sparse tail data supports MP4s without faststart."""
    async with asyncio.timeout(40):
        head, total, direct_url = await _read_range(client, url, 0, SAMPLE_BYTES - 1)
        with tempfile.TemporaryDirectory(prefix="parsehub-video-probe-") as folder:
            path = Path(folder) / "sample.mp4"
            with path.open("wb") as file:
                file.write(head)
                if total > len(head):
                    start = max(len(head), total - SAMPLE_BYTES)
                    tail, tail_total, _ = await _read_range(client, direct_url, start, total - 1)
                    if tail_total != total:
                        raise VideoProbeError("object_changed")
                    file.seek(start)
                    file.write(tail)
            info = await probe_video(path)
        return {**info, "content_length": total, "direct_url": direct_url}
