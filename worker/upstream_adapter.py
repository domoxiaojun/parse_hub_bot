"""Thin adapter around the original ParseHub caches and ``ParsePipeline``."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from services import CacheEntry, ParsePipeline, ParseService, PipelineResult
from services.cache import parse_cache, persistent_cache

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


class OriginalPipelineAdapter:
    """Run the same resolution, caches and pipeline used by plugins.parse.handlers."""

    def __init__(self, service: ParseService):
        self.service = service

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
        try:
            raw_url = await self.service.get_raw_url(url)
            if output_mode == "preview" and mode != "read_only" and not refresh and use_persistent_cache:
                if cached := await persistent_cache.get(raw_url):
                    return UpstreamOutput(raw_url=raw_url, cached=cached)

            parsed = None if refresh else await parse_cache.get(raw_url)
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
            # The interactive bot replaces skipped GIF galleries with link buttons; the
            # Worker has no such fallback, so it must always download.
            gif_only_skip_download_count_threshold=0,
            richtext_skip_download=True,
            save_metadata=output_mode == "zip",
            download_dir=directory,
            t=cast(Any, lambda value: value),
        )
        try:
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


__all__ = ["OriginalPipelineAdapter", "OriginalPipelineError", "UpstreamOutput"]
