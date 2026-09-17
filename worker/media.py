"""本地媒体准备；复用图片适配/视频切片，补充实际编码验证。"""

import asyncio
import json
import math
import mimetypes
from pathlib import Path
from typing import Any, cast

import pillow_heif
from PIL import Image

from utils.media_processing_unit import MediaProcessingUnit
from worker.security import EngineError

MAX_FILE_SIZE = 2_000_000_000

# The interactive Bot registers this in bot.py. Worker is an independent entrypoint,
# so register HEIC/HEIF (and pillow-heif's AVIF opener) in the media module it uses.
pillow_heif.register_heif_opener()


async def run_process(*args: str, timeout: float = 1800) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode != 0:
            raise EngineError('media_processing_failed', 'convert')
        return stdout
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise


async def probe(path: Path) -> dict:
    data = await run_process(
        'ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_format', '-show_streams', '-of', 'json', str(path),
    )
    return cast(dict, json.loads(data))


def compatible(info: dict) -> bool:
    videos = [s for s in info.get('streams', []) if s.get('codec_type') == 'video']
    audio = [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']
    return (len(videos) == 1 and videos[0].get('codec_name') == 'h264'
            and videos[0].get('pix_fmt') == 'yuv420p'
            and all(s.get('codec_name') == 'aac' for s in audio)
            and 'mp4' in info.get('format', {}).get('format_name', '').split(','))


class WorkerMediaProcessor(MediaProcessingUnit):
    """原处理组件的目录与命名，子进程仅补取消和严格编码检查。"""

    @staticmethod
    async def get_container(file_path: Path) -> str:
        return str((await probe(file_path)).get('format', {}).get('format_name', ''))

    async def remux_to_mp4(self, file_path: Path) -> Path:
        out = self.output_dir / (file_path.stem + '_remux.mp4')
        await run_process(
            'ffmpeg', '-nostdin', '-v', 'error', '-y', '-protocol_whitelist', 'file,pipe', '-i', str(file_path),
            '-map', '0:v:0', '-map', '0:a?', '-c', 'copy', '-movflags', '+faststart', str(out),
        )
        return out

    async def ensure_h264(self, file_path: Path) -> Path:
        # Native suffix convention; use MP4 when the source container cannot hold H.264/AAC.
        suffix = file_path.suffix if file_path.suffix.lower() in ('.mp4', '.mkv', '.mov') else '.mp4'
        out = self.output_dir / (file_path.stem + '_h264' + suffix)
        info = await probe(file_path)
        duration = float(info.get('format', {}).get('duration', 0))
        video: dict[str, Any] = next(
            (stream for stream in info.get('streams', []) if stream.get('codec_type') == 'video'), {},
        )
        command = self._build_sw_transcode_cmd(file_path, out, duration, int(video.get('height', 0)))
        # Keep native preset/quality decisions while correcting pixel format and odd dimensions.
        if '-vf' not in command:
            command[1:1] = ['-protocol_whitelist', 'file,pipe']
        command[-1:-1] = ['-vf', 'scale=ceil(iw/2)*2:ceil(ih/2)*2']
        command[1:1] = ['-protocol_whitelist', 'file,pipe']
        command[-1:-1] = [
            '-map', '0:v:0', '-map', '0:a?', '-map_metadata', '-1', '-sn', '-dn',
            '-pix_fmt', 'yuv420p', '-nostdin', '-v', 'error',
        ]
        await run_process(*command)
        return out

    async def split_video(
        self, file_path: Path, output_dir: Path, size_limit: int = MAX_FILE_SIZE,
        ffmpeg_args: list[str] | None = None, keep_sec: float = 1.0,
    ) -> tuple[list[Path], Path]:
        info = await probe(file_path)
        parts = await split_video(file_path, output_dir, float(info.get('format', {}).get('duration', 0)))
        return parts, output_dir / f'{file_path.stem}_split'


async def prepare_video(path: Path, output: Path) -> list[Path]:
    processor = WorkerMediaProcessor(output, logger=lambda _: None)
    processor.TG_MAX_VIDEO_SIZE = MAX_FILE_SIZE
    info = await probe(path)
    streams = info.get('streams', [])
    video = [stream for stream in streams if stream.get('codec_type') == 'video']
    audio = [stream for stream in streams if stream.get('codec_type') == 'audio']
    codecs_compatible = (len(video) == 1 and video[0].get('codec_name') == 'h264'
                         and video[0].get('pix_fmt') == 'yuv420p'
                         and all(stream.get('codec_name') == 'aac' for stream in audio))
    target = path if codecs_compatible else await processor.ensure_h264(path)
    # Native process_video owns remuxing and splitting, including its output filenames.
    paths = (await processor.process_video(target)).output_paths
    for part in paths:
        if part.stat().st_size > MAX_FILE_SIZE or not compatible(await probe(part)):
            raise EngineError('media_processing_failed', 'convert')
    return paths


async def split_video(path: Path, output: Path, duration: float) -> list[Path]:
    """沿用原组件按大小切片策略，补充取消回收和不前进保护。"""
    if not math.isfinite(duration) or duration <= 0:
        raise EngineError('media_processing_failed', 'convert')
    split_dir = output / f'{path.stem}_split'
    split_dir.mkdir(parents=True, exist_ok=True)
    position = 0.0
    result: list[Path] = []
    while position < duration:
        if len(result) >= 128:
            raise EngineError('media_too_large', 'convert')
        part = split_dir / f'{path.stem}_part_{len(result) + 1:03d}{path.suffix}'
        await run_process(
            'ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', str(position),
            '-protocol_whitelist', 'file,pipe', '-i', str(path),
            '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
            '-fs', str(MAX_FILE_SIZE - min(1_000_000, MAX_FILE_SIZE // 100)),
            '-movflags', '+faststart', str(part),
        )
        elapsed = float((await probe(part)).get('format', {}).get('duration', 0))
        if not math.isfinite(elapsed) or elapsed <= 0 or part.stat().st_size > MAX_FILE_SIZE:
            raise EngineError('media_processing_failed', 'convert')
        result.append(part)
        if position + elapsed >= duration - 0.05:
            break
        advance = elapsed - min(1.0, elapsed / 10)
        if advance <= 0.001:
            raise EngineError('media_processing_failed', 'convert')
        position += advance
    return result


async def prepare_file(path: Path, output: Path, mode: str, animation: bool = False) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    if mode == 'raw':
        paths, kind = [path], 'document'
    elif mime.startswith('image/'):
        with Image.open(path) as image:
            gif = image.format == 'GIF'
        if gif:
            paths, kind = [path], 'animation'
        else:
            processor = MediaProcessingUnit(output, segment_height=1920, logger=lambda _: None)
            paths = (await processor.process_image(path)).output_paths
            kind = 'photo'
    elif mime.startswith('video/'):
        paths = await prepare_video(path, output)
        kind = 'animation' if animation else 'video'
    elif mime.startswith('audio/'):
        paths, kind = [path], 'audio'
    else:
        paths, kind = [path], 'document'
    results = []
    for part in paths:
        if not part.is_file() or not 0 < part.stat().st_size <= MAX_FILE_SIZE:
            raise EngineError('media_too_large', 'convert')
        item: dict[str, Any] = {'path': part, 'type': kind, 'filename': part.name,
                'mimeType': mimetypes.guess_type(part.name)[0] or 'application/octet-stream'}
        if kind == 'photo' or item['mimeType'] == 'image/gif':
            with Image.open(part) as image:
                item.update(width=image.width, height=image.height)
                if kind == 'animation':
                    duration = 0
                    for frame in range(getattr(image, 'n_frames', 1)):
                        image.seek(frame)
                        duration += image.info.get('duration', 0)
                    item['durationSeconds'] = math.ceil(duration / 1000)
        elif kind in ('video', 'animation', 'audio'):
            info = await probe(part)
            streams = info.get('streams', [])
            video: dict[str, Any] = next((s for s in streams if s.get('codec_type') == 'video'), {})
            item.update(width=video.get('width', 0), height=video.get('height', 0),
                        durationSeconds=math.ceil(float(info.get('format', {}).get('duration', 0))))
        results.append(item)
    return results
