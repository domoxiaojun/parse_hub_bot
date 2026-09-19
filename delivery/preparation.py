"""Final motion-file preparation. No Telegram I/O or send-time transformations."""

import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from delivery.models import DeliveryError


@dataclass(frozen=True)
class MotionInfo:
    path: Path
    width: int
    height: int
    duration: float
    native_error: str | None


async def command(*args: str) -> bytes:
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(180):
            output, _ = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise DeliveryError("media_processing_failed")
    return output


async def probe_motion(path: Path) -> tuple[dict[str, Any], MotionInfo]:
    data = json.loads(await command(
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)))
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if stream is None:
        raise DeliveryError("live_photo_video_missing")
    duration = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise DeliveryError("live_photo_duration_invalid")
    width, height = int(stream["width"]), int(stream["height"])
    rotation = float(stream.get("tags", {}).get("rotate", 0))
    for side in stream.get("side_data_list", []):
        rotation = float(side.get("rotation", rotation))
    if round(rotation) % 180 == 90:
        width, height = height, width
    error = "live_photo_native_limit" if duration > 10 or path.stat().st_size > 10_000_000 else None
    return stream, MotionInfo(path, width, height, duration, error)


async def prepare_motion(path: Path, directory: Path) -> MotionInfo:
    stream, info = await probe_motion(path)
    needs_size = info.duration <= 10 and path.stat().st_size > 10_000_000
    if (stream.get("codec_name") == "h264" and stream.get("pix_fmt") == "yuv420p"
            and path.suffix.lower() == ".mp4" and not needs_size):
        return info
    output = directory / f"{path.stem}_h264.mp4"
    # Budget 8 MiB for video; preserve the entire clip and allow room for audio/container.
    quality = (["-b:v", str(int(8_000_000 * 8 / info.duration)), "-maxrate",
                str(int(8_000_000 * 8 / info.duration)), "-bufsize", "1000000"]
               if needs_size else ["-crf", "20"])
    await command("ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a?",
                  "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  "-preset", "medium", *quality, "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                  "-y", str(output))
    _, final = await probe_motion(output)
    return final
