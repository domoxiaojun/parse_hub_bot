"""Instagram per-item orchestration over existing ParseHub and yt-dlp downloaders."""

import asyncio
import copy
import json
import logging
import math
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from parsehub.errors import DownloadError
from parsehub.parsers.base.ytdlp import _materialize_cookie, _run_ytdlp_download
from parsehub.provider_api.instagram import InstagramPost
from parsehub.types import (
    DownloadResult,
    ImageFile,
    ImageParseResult,
    ImageRef,
    MultimediaParseResult,
    VideoFile,
    VideoParseResult,
    VideoRef,
)
from parsehub.utils.downloader import download as download_file
from PIL import Image

from ext.instagram_media import FORMAT_SORT, POLICY, QualityMedia, number, rank
from ext.media.video_probe import probe_video

logger = logging.getLogger("parsehub.quality")
MAX_ATTEMPTS = 8


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


class InstagramDownloads:
    # Concrete result constructors initialize upstream media, then call configure.
    # This mixin has no __init__ so it does not intercept upstream super() calls.
    media: Any  # pyright: ignore[reportUninitializedInstanceVariable]

    def configure(
        self, items: list[QualityMedia],
        recipes: dict[int, dict[str, Any]], cookie_text: str | None,
    ) -> None:
        self._items = items  # pyright: ignore[reportUninitializedInstanceVariable]
        self._recipes = recipes  # pyright: ignore[reportUninitializedInstanceVariable]
        self._cookie_text = cookie_text  # pyright: ignore[reportUninitializedInstanceVariable]

    def _attempts(self, index: int) -> list[tuple[str, dict[str, Any]]]:
        item = self._items[index]
        values: list[tuple[str, dict[str, Any]]] = []
        if index in self._recipes:
            values.append(("yt-dlp", self._recipes[index]))
        values.extend(("direct", candidate) for candidate in (item.videos if item.is_video else item.images))
        return sorted(values, key=lambda value: rank(value[1]), reverse=True)[:MAX_ATTEMPTS]

    async def _fetch_video(
        self, source: str, candidate: dict[str, Any], folder: Path, *, proxy: str | None, connections: int,
    ) -> Path:
        recipe = copy.deepcopy(candidate)
        if source == "direct":
            recipe.update(id="instagram", title="Instagram", ext="mp4", protocol="https")
        # Cookies stay in a private temporary cookie jar scoped to .instagram.com.
        # They are never put in a global Cookie header for CDN requests.
        with _materialize_cookie(self._cookie_text) as cookie_args:
            await _run_ytdlp_download(
                recipe, ["--quiet", "--no-progress", "-S", FORMAT_SORT, "--format-sort-force", *cookie_args],
                outtmpl=str(folder / "media.%(ext)s"), connections=connections, proxy=proxy,
                headers={"Referer": "https://www.instagram.com/"},
            )
        files = [p for p in folder.glob("media.*") if p.is_file() and p.suffix not in {".part", ".ytdl", ".json"}]
        if len(files) != 1:
            raise DownloadError("instagram_download_output_invalid")
        return files[0]

    async def _verify(
        self, path: Path, item: QualityMedia, candidate: dict[str, Any], *, source: str,
        attempt: int, target: dict[str, Any], candidate_count: int,
    ) -> ImageFile | VideoFile:
        actual: dict[str, Any]
        if item.is_video:
            actual = await probe_video(path)
            if number(actual.get("duration")) <= 0:
                raise DownloadError("instagram_video_duration_missing")
        else:
            width, height = await asyncio.to_thread(_image_size, path)
            actual = {"width": width, "height": height}
        expected = (int(number(candidate.get("width"))), int(number(candidate.get("height"))))
        dimensions = (int(actual["width"]), int(actual["height"]))
        matches = sorted(expected) == sorted(dimensions) if all(expected) else None
        if matches is False:
            raise DownloadError("instagram_media_dimensions_mismatch")
        if item.is_video and number(candidate.get("fps")) > number(actual.get("fps")) + 0.1:
            raise DownloadError("instagram_media_fps_mismatch")
        file: ImageFile | VideoFile
        if item.is_video:
            file = VideoFile(
                path=path, width=dimensions[0], height=dimensions[1], duration=math.ceil(actual["duration"]),
            )
        else:
            file = ImageFile(path=path, width=dimensions[0], height=dimensions[1])
        report = {
            "policy": POLICY, "source": source, "candidate_count": candidate_count, "attempts": attempt + 1,
            "width": dimensions[0], "height": dimensions[1], "fps": number(actual.get("fps")),
            "bitrate": number(actual.get("bitrate")), "matches_selected": matches,
            "fallback": attempt > 0, "downgraded": rank(actual)[:3] < rank(target)[:3],
            "highest_confirmed": False, "scope": "returned_candidates",
        }
        # A readable matching file verifies this selection, not every platform rendition.
        cast(Any, file)._instagram_quality = report
        cast(Any, file)._instagram_source_url = candidate.get("url")
        if source == "direct":
            cast(Any, file)._reader_original_url = candidate["url"]
        logger.info(
            "event=instagram.quality source=%s width=%s height=%s fallback=%s downgraded=%s matches_selected=%s",
            source, *dimensions, report["fallback"], report["downgraded"], matches,
        )
        return file

    async def _download_one(
        self, index: int, output_dir: Path, *, proxy: str | None, connections: int,
    ) -> ImageFile | VideoFile:
        item = self._items[index]
        attempts = self._attempts(index)
        if not attempts:
            raise DownloadError("instagram_media_unavailable")
        for attempt, (source, candidate) in enumerate(attempts):
            folder = output_dir / f"{index + 1:03d}"
            folder.mkdir(parents=True, exist_ok=True)
            try:
                if item.is_video:
                    path = await self._fetch_video(source, candidate, folder, proxy=proxy, connections=connections)
                else:
                    path = Path(await download_file(
                        candidate["url"], folder / "media.jpg", proxy=proxy, connections=connections,
                        headers={"Referer": "https://www.instagram.com/"},
                    ))
                return await self._verify(
                    path, item, candidate, source=source, attempt=attempt,
                    target=attempts[0][1], candidate_count=len(attempts),
                )
            except Exception as error:
                logger.info(
                    "event=instagram.download status=fallback index=%s attempt=%s source=%s reason=%s",
                    index, attempt + 1, source, type(error).__name__,
                )
                shutil.rmtree(folder, ignore_errors=True)
        # Do not expose response bodies, signed URLs or cookie details in upstream logs.
        raise DownloadError("instagram_media_download_failed") from None

    async def _do_download(
        self, *, output_dir: Path, callback: Any = None, callback_args: tuple[Any, ...] = (),
        callback_kwargs: dict[str, Any] | None = None, proxy: str | None = None, headers: dict[str, Any] | None = None,
        connections: int = 4, concurrency: int = 4,
    ) -> DownloadResult:
        semaphore = asyncio.Semaphore(max(1, concurrency))
        completed = 0

        async def run(index: int) -> ImageFile | VideoFile:
            nonlocal completed
            async with semaphore:
                result = await self._download_one(index, output_dir, proxy=proxy, connections=connections)
                completed += 1
                if callback:
                    await callback(completed, len(self._items), "count", *callback_args, **(callback_kwargs or {}))
                return result

        tasks = [asyncio.create_task(run(index)) for index in range(len(self._items))]
        try:
            media = await asyncio.gather(*tasks)
            # ParseResult writes metadata before downloading. Update only this
            # output snapshot after fallback/verification, never the shared parse result.
            metadata_path = output_dir / "metadata.json"
            if metadata_path.exists():
                metadata = cast(Any, self).to_dict()
                refs = metadata["media"] if isinstance(metadata["media"], list) else [metadata["media"]]
                for ref, file in zip(refs, media, strict=True):
                    ref.update(width=file.width, height=file.height)
                    if isinstance(file, VideoFile):
                        ref["duration"] = file.duration
                    if url := getattr(file, "_instagram_source_url", None):
                        ref["url"] = url
                metadata["download_quality"] = [cast(Any, file)._instagram_quality for file in media]
                metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            shutil.rmtree(output_dir, ignore_errors=True)
            raise
        return DownloadResult(media if isinstance(self.media, Sequence) else media[0], output_dir)


class InstagramVideoResult(InstagramDownloads, VideoParseResult):
    def __init__(
        self, post: InstagramPost, items: list[QualityMedia], refs: list[ImageRef | VideoRef],
        recipes: dict[int, dict[str, Any]], cookie_text: str | None,
    ) -> None:
        VideoParseResult.__init__(self, title=post.title, content=post.caption, video=cast(VideoRef, refs[0]))
        self.configure(items, recipes, cookie_text)


class InstagramImageResult(InstagramDownloads, ImageParseResult):
    def __init__(
        self, post: InstagramPost, items: list[QualityMedia], refs: list[ImageRef | VideoRef],
        recipes: dict[int, dict[str, Any]], cookie_text: str | None,
    ) -> None:
        ImageParseResult.__init__(self, title=post.title, content=post.caption, photo=cast(list[ImageRef], refs))
        self.configure(items, recipes, cookie_text)


class InstagramCarouselResult(InstagramDownloads, MultimediaParseResult):
    def __init__(
        self, post: InstagramPost, items: list[QualityMedia], refs: list[ImageRef | VideoRef],
        recipes: dict[int, dict[str, Any]], cookie_text: str | None,
    ) -> None:
        MultimediaParseResult.__init__(self, title=post.title, content=post.caption, media=refs)
        self.configure(items, recipes, cookie_text)
