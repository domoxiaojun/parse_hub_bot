"""Thin adapter around the original ParseHub caches and ``ParsePipeline``."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from core import bs
from ext.cache import CacheEntry, persistent_cache
from ext.douyin_quality import QUALITY_POLICY
from ext.parsehub_compat import DEFAULT_DOWNLOAD_CONCURRENCY, download_limit
from services import ParsePipeline, ParseService, PipelineResult
from services.cache import parse_cache

Progress = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class UpstreamOutput:
    raw_url: str
    cached: CacheEntry | None = None
    pipeline: PipelineResult | None = None


class OriginalPipelineError(Exception):
    def __init__(self, stage: str, error: Exception):
        self.stage = stage
        super().__init__(stage)
        self.__cause__ = error


class _Reporter:
    def __init__(self, progress: Progress | None):
        self.progress = progress
        self.error: Exception | None = None
        self.error_stage = "prepare"

    async def report(self, text: str) -> None:
        if self.progress is None:
            return
        stage = (
            "parse"
            if "解 析" in text
            else "download"
            if "下 载" in text
            else "convert"
            if "处 理" in text
            else "prepare"
        )
        await self.progress(stage)

    async def report_error(self, stage: str, error: Exception) -> None:
        self.error = error
        self.error_stage = {"解析": "parse", "下载": "download", "媒体处理": "convert"}.get(stage, "prepare")

    async def dismiss(self) -> None:
        return


def pipeline_download_dir() -> Path:
    """The upstream pipeline always downloads into ``bs.download_dir``; the Worker must share it."""
    return Path(bs.download_dir).resolve()


class OriginalPipelineAdapter:
    """Run the same resolution, caches and pipeline used by plugins.parse.handlers."""

    def __init__(self, service: ParseService, *, download_concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY):
        self.service = service
        self.download_concurrency = download_concurrency

    async def prepare(
        self,
        url: str,
        *,
        mode: str,
        output_mode: str,
        refresh: bool,
        directory: Path,
        progress: Progress | None,
        use_persistent_cache: bool,
    ) -> UpstreamOutput:
        if directory.resolve() != pipeline_download_dir():
            # Upstream ParsePipeline has no download_dir parameter; the Worker cache root must be DOWNLOAD_DIR.
            raise OriginalPipelineError("prepare", RuntimeError("download_dir_mismatch"))
        try:
            raw_url = await self.service.get_raw_url(url)
            platform = self.service.parser.get_platform(raw_url)
            douyin = platform is not None and platform.id == "douyin"
            # Telegram file-id caches do not retain selection evidence or a file to verify.
            if douyin and use_persistent_cache:
                logging.getLogger("parsehub.quality").info(
                    "event=quality.cache platform=douyin source=telegram_cache action=bypass reason=no_quality_evidence"
                )
            if output_mode == "preview" and mode != "read_only" and not refresh and use_persistent_cache and not douyin:
                if cached := await persistent_cache.get(raw_url):
                    return UpstreamOutput(raw_url=raw_url, cached=cached)

            parsed = None if refresh else await parse_cache.get(raw_url)
            if douyin and parsed is not None:
                quality = getattr(parsed, "_douyin_quality", {})
                if quality.get("policy") != QUALITY_POLICY or not quality.get("highest_confirmed"):
                    parsed = None
                else:
                    logging.getLogger("parsehub.quality").info(
                        "event=quality.cache platform=douyin source=parse_cache action=reuse_selection reprobed=false"
                    )
            if mode == "read_only":
                parsed = parsed or await self.service.parse(url)
                await parse_cache.set(raw_url, parsed)
                return UpstreamOutput(raw_url=raw_url, pipeline=PipelineResult(parse_result=parsed))
        except Exception as error:
            raise OriginalPipelineError("parse", error) from error

        reporter = _Reporter(progress)
        pipeline = ParsePipeline(
            url,
            raw_url,
            reporter,
            parse_result=parsed,
            singleflight=False,  # Jobs already provides per-URL singleflight.
            skip_media_processing=output_mode in {"raw", "zip"},
            # The shared envelope must contain every final asset, including GIF galleries.
            gif_only_skip_download_count_threshold=0,
            richtext_skip_download=False,
            save_metadata=output_mode == "zip",
            t=cast(Any, lambda value: value),
        )
        try:
            with download_limit(self.download_concurrency):
                result = await pipeline.run()
        except Exception as error:
            raise OriginalPipelineError(reporter.error_stage, error) from error
        finally:
            pipeline.finish()
        if result is None:
            raise OriginalPipelineError(
                reporter.error_stage,
                reporter.error or RuntimeError("upstream_pipeline_failed"),
            )
        await parse_cache.set(raw_url, result.parse_result)
        return UpstreamOutput(raw_url=raw_url, pipeline=result)


__all__ = ["OriginalPipelineAdapter", "OriginalPipelineError", "UpstreamOutput", "pipeline_download_dir"]
