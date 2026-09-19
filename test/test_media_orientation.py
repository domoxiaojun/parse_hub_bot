"""Image orientation is baked into pixels before Telegram media assembly."""

import asyncio
import subprocess
from pathlib import Path

import pytest
from parsehub.types import DownloadResult, ImageFile, LivePhotoFile
from PIL import Image, ImageChops, ImageOps, ImageStat

from delivery.preparation import prepare_motion, probe_motion
from services.media import ProcessedMedia, process_media_files, resolve_media_info
from utils.media_processing_unit import MediaProcessingUnit


def test_oriented_jpeg_is_transposed_before_downscale(tmp_path: Path) -> None:
    source = tmp_path / "large-oriented.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (3000, 1500), "blue").save(source, "JPEG", quality=80, exif=exif)

    result = asyncio.run(MediaProcessingUnit(tmp_path / "processed").process_image(source))
    output = result.output_paths[0]

    with Image.open(output) as image:
        assert image.size == (1500, 3000)
        assert ImageOps.exif_transpose(image).size == image.size
        assert image.getexif().get(274) is None
    processed = ProcessedMedia(ImageFile(path=source, width=3000, height=1500), [output])
    assert resolve_media_info(processed, str(output)) == (1500, 3000, 0)


def test_oriented_webp_conversion_keeps_display_direction(tmp_path: Path) -> None:
    source = tmp_path / "cover.webp"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (80, 40), "green").save(source, "WEBP", exif=exif)

    result = asyncio.run(MediaProcessingUnit(tmp_path / "processed").process_image(source))
    output = result.output_paths[0]

    with Image.open(output) as image:
        assert output.suffix == ".jpg"
        assert image.size == (40, 80)
        assert image.getexif().get(274) is None


def test_live_photo_cover_is_never_split_into_multiple_images(tmp_path: Path) -> None:
    folder = tmp_path / "download"
    folder.mkdir()
    cover = folder / "cover.jpg"
    motion = folder / "motion.mp4"
    Image.new("RGB", (300, 2000), "purple").save(cover, "JPEG")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(motion)],
        check=True,
    )
    download = DownloadResult(
        media=LivePhotoFile(path=cover, video_path=motion, width=300, height=2000, duration=3),
        output_dir=folder,
    )

    processed = asyncio.run(process_media_files(download))

    assert len(processed) == 1
    assert processed[0].output_paths == [cover]


@pytest.mark.parametrize("orientation", [2, 3, 4, 5, 6, 7, 8])
def test_mirrors_and_rotations_are_applied_exactly_once(tmp_path: Path, orientation: int) -> None:
    source = tmp_path / "oriented.webp"
    image = Image.new("RGB", (80, 40), "red")
    image.paste("blue", (0, 0, 40, 20))
    exif = Image.Exif()
    exif[274] = orientation
    image.save(source, "WEBP", lossless=True, exif=exif)
    with Image.open(source) as decoded:
        expected = ImageOps.exif_transpose(decoded).convert("RGB")
    processor = MediaProcessingUnit(tmp_path / "processed")
    result = asyncio.run(processor.process_image(source))
    second = asyncio.run(processor.process_image(result.output_paths[0]))
    assert result.output_paths == second.output_paths
    with Image.open(result.output_paths[0]) as actual:
        assert actual.size == expected.size
        assert actual.getexif().get(274, 1) == 1
        assert max(ImageStat.Stat(ImageChops.difference(actual, expected)).mean) < 10


def test_heif_cover_uses_decoded_orientation(tmp_path: Path) -> None:
    source = tmp_path / "cover.heic"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (120, 80), "red").save(source, "HEIF", exif=exif)
    with Image.open(source) as before:
        expected_size = ImageOps.exif_transpose(before).size
    processed = asyncio.run(MediaProcessingUnit(tmp_path / "processed").process_image(source))
    with Image.open(processed.output_paths[0]) as after:
        assert after.format == "JPEG" and after.size == expected_size
        assert after.getexif().get(274, 1) == 1


def test_motion_conversion_and_long_preview_are_not_truncated(tmp_path: Path) -> None:
    source = tmp_path / "motion.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x96:d=11",
                    "-c:v", "libx264", "-pix_fmt", "yuv444p", "-y", str(source)], check=True)
    directory = tmp_path / "processed"
    directory.mkdir()
    prepared = asyncio.run(prepare_motion(source, directory))
    stream, info = asyncio.run(probe_motion(prepared.path))
    assert stream["codec_name"] == "h264" and stream["pix_fmt"] == "yuv420p"
    assert info.duration >= 11 and info.native_error == "live_photo_native_limit"
    assert source.is_file() and prepared.path != source


def test_motion_rotation_reports_display_dimensions(tmp_path: Path) -> None:
    source, rotated = tmp_path / "source.mp4", tmp_path / "rotated.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(source)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-display_rotation:v", "90", "-i", str(source),
                    "-c", "copy", "-y", str(rotated)], check=True)
    _, info = asyncio.run(probe_motion(rotated))
    assert (info.width, info.height) == (90, 160)
