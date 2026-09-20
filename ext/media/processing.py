"""Fork media preparation built on top of the upstream ``MediaProcessingUnit``.

Differences from upstream, all kept here so ``utils/`` and ``services/`` stay pristine:

- EXIF/HEIF orientation is baked into pixels before any sizing decision.
- Photos are only resized/compressed when Telegram's photo limits require it
  (no blanket 2560px downscale).
- Live Photo covers are never split into segments; the motion file is prepared
  once (H.264/yuv420p, faststart) and its real display size is recorded.
- ffmpeg/ffprobe failures raise instead of silently producing empty output.
- Unrecognised media extensions are passed through untouched instead of
  discarding the whole post.
"""

import asyncio
import math
import os
from dataclasses import dataclass
from pathlib import Path

import pillow_heif
from parsehub.types import DownloadResult, LivePhotoFile
from parsehub.utils.media_info import MediaInfoReader
from PIL import Image, ImageOps
from PIL.Image import Resampling

from ext.delivery.preparation import MotionInfo, prepare_motion
from ext.media.commands import run_cmd
from log import logger
from services.media import ProcessedMedia
from utils.helpers import to_list
from utils.media_processing_unit import MediaProcessingUnit, MediaProcessResult

# Every entry point that converts media needs HEIF support, not only bot.py.
pillow_heif.register_heif_opener()


@dataclass
class PreparedMedia(ProcessedMedia):
    """Upstream ``ProcessedMedia`` plus the prepared Live Photo motion file."""

    motion: MotionInfo | None = None


def motion_of(processed: ProcessedMedia) -> MotionInfo | None:
    """Motion info when the item was prepared by this module; ``None`` for plain upstream items."""
    return processed.motion if isinstance(processed, PreparedMedia) else None


def resolve_live_photo_video_info(processed: ProcessedMedia) -> tuple[int, int, int]:
    """Read the motion file's display dimensions instead of reusing its cover dimensions."""
    source = processed.source
    if not isinstance(source, LivePhotoFile):
        raise TypeError("live_photo_required")
    if motion := motion_of(processed):
        return motion.width, motion.height, math.ceil(motion.duration)
    fallback = source.width, source.height, source.duration
    if not source.video_path:
        return fallback
    try:
        info = MediaInfoReader.read(source.video_path)
    except Exception:
        return fallback
    return info.width or fallback[0], info.height or fallback[1], info.duration or fallback[2]


class ForkMediaProcessingUnit(MediaProcessingUnit):
    TG_MAX_PHOTO_BYTES = 10 * 1000**2
    TG_MAX_PHOTO_DIMENSION_SUM = 10_000

    async def process_image(self, file_path: Path, *, split_long_images: bool = True) -> MediaProcessResult:
        image_format, is_broken, image_mode = await self._detect_image_format(file_path)
        if image_format == "GIF":
            self.logger("图片为 GIF 格式，无需处理")
            return MediaProcessResult(output_paths=[file_path])
        source = file_path
        intermediates: list[Path] = []

        try:
            if not is_broken and (oriented := await asyncio.to_thread(self._normalize_orientation, source)):
                source = oriented
                intermediates.append(source)
                image_format, image_mode = "JPEG", "RGB"

            needs_convert = image_format not in {"PNG", "JPEG"} or source.suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
            }
            if needs_convert or is_broken:
                self.logger(f"图片需转换: format={image_format}, mode={image_mode}, broken={is_broken}")
                source = await self._img2jpg(source)
                intermediates.append(source)

            if result := await asyncio.to_thread(self._adapt_image, source, split_long_images=split_long_images):
                return result

            # Resize/compress only when Telegram's photo limits require it.
            if downscaled := await asyncio.to_thread(self._downscale_image, source):
                intermediates.append(downscaled)
                source = downscaled

            intermediates = [p for p in intermediates if p != source]
            return MediaProcessResult(output_paths=[source])
        finally:
            for p in intermediates:
                if p.exists():
                    self.logger(f"删除中间文件: {p}")
                    os.remove(p)

    def _normalize_orientation(self, file_path: Path) -> Path | None:
        """Bake EXIF orientation into pixels before any sizing or Telegram upload."""
        with Image.open(file_path) as img:
            orientation = img.getexif().get(274, 1)
            if orientation not in {2, 3, 4, 5, 6, 7, 8}:
                return None
            normalized = ImageOps.exif_transpose(img)
            if normalized.mode != "RGB":
                normalized = normalized.convert("RGB")
            out_path = self.output_dir / f"{file_path.stem}_oriented.jpg"
            normalized.save(out_path, format="JPEG", quality=95)
        self.logger(f"图片方向已规范化: orientation={orientation}, output={out_path}")
        return out_path

    def _adapt_image(self, file_path: Path, *, split_long_images: bool = True) -> MediaProcessResult | None:
        """分析图片尺寸并做填充 / 切割，返回 None 表示无需处理"""
        with Image.open(file_path) as img:
            w, h = img.width, img.height

        wh_ratio = w / h
        hw_ratio = h / w
        self.logger(f"图片尺寸: {w}x{h}, wh_ratio={wh_ratio:.2f}, hw_ratio={hw_ratio:.2f}")

        if w >= h:
            if wh_ratio <= 20:
                self.logger("横图比例正常，跳过处理")
                return None
            self.logger("横图比例超限，需要填充")
            padding = self._calc_padding_horizontal(w, h)
            with Image.open(file_path) as img:
                return self._pad_image(file_path, img, padding)

        if hw_ratio <= 5 or (w < 200 and hw_ratio < 20):
            self.logger("竖图比例正常，跳过处理")
            return None
        if not split_long_images:
            if hw_ratio <= 20:
                self.logger("封面长图保持单图，跳过切割")
                return None
            self.logger("封面竖图比例超限，使用填充保持单图")
            padding = self._calc_padding_vertical(w, h)
            with Image.open(file_path) as img:
                return self._pad_image(file_path, img, padding)
        if w < 200 and hw_ratio > 20:
            self.logger("窄竖图比例超限，需要填充")
            padding = self._calc_padding_vertical(w, h)
            with Image.open(file_path) as img:
                return self._pad_image(file_path, img, padding)
        segments = h // self.segment_height
        seg_h = h // 2 if segments < self.medium_threshold else self.segment_height
        self.logger(f"长图切割: segments={segments}, seg_h={seg_h}")
        return self._split_image(file_path, seg_h)

    async def _img2jpg(self, file_path: Path) -> Path:
        output = self.output_dir / file_path.with_suffix(".jpg").name
        try:
            with Image.open(file_path) as pil_img:
                transposed = ImageOps.exif_transpose(pil_img)
                img = transposed.convert("RGB") if transposed.mode != "RGB" else transposed
                img.save(output, format="JPEG")
        except OSError as e:
            self.logger(f"Pillow 转换失败，尝试 ffmpeg: {e}")
            await run_cmd(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(file_path),
                "-frames:v",
                "1",
                "-q:v",
                "1",
                "-y",
                str(output),
                timeout=60,
                check=True,
            )
            if not output.exists() or output.stat().st_size == 0:
                raise
        self.logger(f"图片转换完成: {output}")
        return output

    def _downscale_image(self, file_path: Path, max_side: int = 10_000) -> Path | None:
        """只在 Telegram 照片边长和文件大小限制下缩放或压缩。"""
        with Image.open(file_path) as img:
            w, h = img.size
            too_large_dimensions = w + h > self.TG_MAX_PHOTO_DIMENSION_SUM
            too_large_file = file_path.stat().st_size > self.TG_MAX_PHOTO_BYTES
            if not too_large_dimensions and not too_large_file:
                return None
            scale = min(1.0, max_side / (w + h))
            new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
            self.logger(f"图片超过 Telegram 限制，处理: {w}x{h}/{file_path.stat().st_size} bytes -> {new_w}x{new_h}")
            resized = img.resize((new_w, new_h), Resampling.LANCZOS) if scale < 1 else img.copy()
            # JPEG is the fallback only for an oversized file that cannot be reduced by dimensions.
            out_path = self.output_dir / f"{file_path.stem}_telegram.jpg"
            if resized.mode not in {"RGB", "L"}:
                resized = resized.convert("RGB")
            resized.save(out_path, format="JPEG", quality=88, optimize=True)
        return out_path

    async def remux_to_mp4(self, file_path: Path) -> Path:
        out = self.output_dir / (file_path.stem + "_remux" + ".mp4")
        cmd = ["ffmpeg", "-i", str(file_path), "-c", "copy", "-movflags", "+faststart", "-y", str(out)]
        await run_cmd(*cmd, timeout=20 * 60, check=True)
        if not out.is_file() or out.stat().st_size == 0:
            raise ValueError("ffmpeg produced no output")
        return out

    async def ensure_h264(self, file_path: Path) -> Path:
        out = self.output_dir / (file_path.stem + "_h264" + file_path.suffix)
        duration = await self.get_duration(file_path)
        height = await self._get_video_height(file_path)
        cmd = self._build_sw_transcode_cmd(file_path, out, duration, height)
        self.logger(f"h264 转码: {file_path.name} -> {out.name}, duration={duration:.0f}s, encoder=SW:libx264")
        await run_cmd(*cmd, timeout=30 * 60, check=True)
        if out.exists() and out.stat().st_size > 0:
            self.logger(f"h264 转码成功: size={out.stat().st_size / 1024 / 1024:.1f}MB")
            return out
        self.logger(f"h264 转码失败，返回原文件: {file_path}")
        return file_path

    async def split_video(
        self,
        file_path: Path,
        output_dir: Path,
        size_limit: int = MediaProcessingUnit.TG_MAX_VIDEO_SIZE,
        ffmpeg_args: list[str] | None = None,
        keep_sec: float = 1.0,
    ) -> tuple[list[Path], Path]:
        if ffmpeg_args is None:
            ffmpeg_args = ["-c", "copy"]

        base = file_path.stem
        split_dir = output_dir / f"{base}_split"
        split_dir.mkdir(parents=True, exist_ok=True)
        ext = file_path.suffix.lstrip(".")
        total_duration = int(await self.get_duration(file_path))
        self.logger(
            f"视频分割: duration={total_duration}s, "
            f"size_limit={size_limit / 1000 / 1000:.0f} MB ({size_limit / 1024 / 1024:.1f} MiB)"
        )

        cur, part, output_paths = 0, 1, []
        while cur < total_duration:
            out_file = split_dir / f"{base}_part_{part:03d}.{ext}"
            output_paths.append(out_file)
            cmd = [
                "ffmpeg",
                "-ss",
                str(cur),
                "-i",
                str(file_path),
                "-fs",
                str(size_limit),
                *ffmpeg_args,
                "-y",
                str(out_file),
            ]
            await run_cmd(*cmd, timeout=20 * 60, check=True)

            new_dur = int(await self.get_duration(out_file))
            self.logger(f"分割 part {part}: offset={cur}s, duration={new_dur}s, file={out_file}")
            if new_dur <= 0:
                break
            # Always advance past the overlap; otherwise a segment shorter than keep_sec
            # (size limit hit within a second) would re-encode the same offset forever.
            advance = max(new_dur - int(keep_sec), 1) if cur + new_dur < total_duration else new_dur
            cur += advance
            part += 1

        self.logger(f"视频分割完成: {len(output_paths)} 段")
        return output_paths, split_dir


async def process_media_files(download_result: DownloadResult) -> list[ProcessedMedia]:
    """Drop-in replacement for ``services.media.process_media_files`` (installed by ``ext.hooks``)."""
    processed_dir = Path(download_result.output_dir).joinpath("processed")
    processor = ForkMediaProcessingUnit(
        processed_dir, segment_height=1920, logger=logger.bind(name="MediaProcessor").debug
    )
    media_files = to_list(download_result.media)
    logger.debug(f"开始媒体处理: 文件数={len(media_files)}, output_dir={processed_dir}")
    processed_list: list[ProcessedMedia] = []
    for media_file in media_files:
        logger.debug(f"处理文件: {media_file.path}")
        try:
            result = (
                await processor.process_image(Path(media_file.path), split_long_images=False)
                if isinstance(media_file, LivePhotoFile)
                else await processor.process(media_file.path)
            )
        except ValueError as e:
            if isinstance(media_file, LivePhotoFile):
                raise
            # An unrecognised extension must not discard the whole post; pass the file through untouched.
            logger.warning(f"跳过媒体处理, 原样发送: {type(e).__name__}: {e}")
            processed_list.append(PreparedMedia(media_file, None, None))
            continue
        logger.debug(f"处理结果: output_paths={result.output_paths}")
        motion = None
        if isinstance(media_file, LivePhotoFile):
            if not media_file.video_path:
                logger.warning("实况视频缺失，降级为静态图片")
            else:
                try:
                    motion = await prepare_motion(Path(media_file.video_path), processed_dir)
                except Exception as error:
                    logger.warning(f"实况视频准备失败，降级为静态图片: {type(error).__name__}")
        processed_list.append(PreparedMedia(media_file, result.output_paths, result.temp_dir, motion))
    logger.debug(f"媒体处理完成: 处理数={len(processed_list)}")
    return processed_list
