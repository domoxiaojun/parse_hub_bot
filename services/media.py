import math
from dataclasses import dataclass
from pathlib import Path

import pillow_heif
from easy_ai18n import PreLocaleSelector
from parsehub.types import AnyMediaFile, DownloadResult, LivePhotoFile, ProgressUnit
from parsehub.utils.media_info import MediaInfoReader

from delivery.preparation import MotionInfo, prepare_motion
from log import logger
from utils.helpers import to_list
from utils.media_processing_unit import MediaProcessingUnit

# Every entry point that converts media needs HEIF support, not only bot.py.
pillow_heif.register_heif_opener()


@dataclass
class ProcessedMedia:
    source: AnyMediaFile
    output_paths: list[Path] | None = None
    output_dir: Path | None = None
    motion: MotionInfo | None = None


def resolve_media_info(processed: ProcessedMedia, file_path: str) -> tuple[int, int, int]:
    """获取媒体的宽、高、时长。若经过转码则从文件读取，否则使用源信息。"""
    if processed.output_paths:
        info = MediaInfoReader.read(file_path)
        return info.width, info.height, info.duration
    return processed.source.width, processed.source.height, getattr(processed.source, "duration", 0)


def resolve_live_photo_video_info(processed: ProcessedMedia) -> tuple[int, int, int]:
    """Read the motion file's display dimensions instead of reusing its cover dimensions."""
    source = processed.source
    if not isinstance(source, LivePhotoFile):
        raise TypeError("live_photo_required")
    if processed.motion:
        info = processed.motion
        return info.width, info.height, math.ceil(info.duration)
    fallback = source.width, source.height, source.duration
    if not source.video_path:
        return fallback
    try:
        info = MediaInfoReader.read(source.video_path)
    except Exception:
        return fallback
    return info.width or fallback[0], info.height or fallback[1], info.duration or fallback[2]


def progress(current: int, total: int, unit: ProgressUnit, _t: PreLocaleSelector) -> str | None:
    if unit == "bytes":
        if total <= 0:
            return None

        text = _t(f"下 载 中... | {current * 100 / total:.0f}%")
        if round(current * 100 / total, 1) % 25 == 0:
            return str(text)
    else:
        text = _t(f"下 载 中... | {current}/{total}")
        if (current + 1) % 3 == 0 or (current + 1) == total:
            return str(text)
    return None


async def process_media_files(download_result: DownloadResult) -> list[ProcessedMedia]:
    """对下载结果中的媒体文件进行处理，返回 ProcessedMedia 列表"""
    processed_dir = download_result.output_dir.joinpath("processed")
    processor = MediaProcessingUnit(processed_dir, segment_height=1920, logger=logger.bind(name="MediaProcessor").debug)
    media_files = to_list(download_result.media)
    logger.debug(f"开始媒体处理: 文件数={len(media_files)}, output_dir={processed_dir}")
    processed_list: list[ProcessedMedia] = []
    for media_file in media_files:
        # 对于实况图片只处理图片, 不处理视频
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
            processed_list.append(ProcessedMedia(media_file, None, None))
            continue
        logger.debug(f"处理结果: output_paths={result.output_paths}")
        motion = None
        if isinstance(media_file, LivePhotoFile):
            if not media_file.video_path:
                raise ValueError("live_photo_pair_missing")
            motion = await prepare_motion(Path(media_file.video_path), processed_dir)
        processed_list.append(ProcessedMedia(media_file, result.output_paths, result.temp_dir, motion))
    logger.debug(f"媒体处理完成: 处理数={len(processed_list)}")
    return processed_list
