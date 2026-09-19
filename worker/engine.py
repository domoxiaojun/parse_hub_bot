"""Adapt original ParseHub outputs to the Worker delivery contract."""

import asyncio
import copy
import importlib.metadata
import logging
import mimetypes
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from parsehub.types import AniFile, ImageFile, LivePhotoFile, VideoFile
from parsehub.utils.helpers import match_url

from delivery.assets import worker_cached_media
from delivery.models import asset_key
from services import ParseService, PipelineResult
from services.media import ProcessedMedia, resolve_live_photo_video_info, resolve_media_info
from utils.helpers import pack_dir_to_tar_gz
from worker.security import EngineError, validate_url
from worker.upstream_adapter import OriginalPipelineAdapter, OriginalPipelineError, UpstreamOutput

Progress = Callable[[str], Awaitable[None]]
RegisterPath = Callable[[Path], None]
logger = logging.getLogger("parsehub.worker")


@dataclass(frozen=True)
class _UpstreamFailure:
    http_status: int | None
    reason: str


class ParseHubEngine:
    """Keep Worker concerns outside the original parse/download/process pipeline."""

    def __init__(self, root: Path, service: ParseService):
        self.root = root.resolve()
        self.service = service
        self.parser = service.parser
        self.upstream = OriginalPipelineAdapter(service)
        self.config: dict[str, Any] = {}

    def configure(self, config: dict[str, Any]) -> None:
        # Parsing reads the original global platform config. This snapshot is only
        # used to avoid externally publishing content fetched with credentials.
        self.config = copy.deepcopy(config)

    def capabilities(self) -> dict[str, Any]:
        return {
            "version": importlib.metadata.version("parsehub"),
            "platforms": [
                {"id": p["id"], "name": p["name"], "contentTypes": p["supported_types"]}
                for p in self.parser.get_platforms()
            ],
            "outputModes": ["preview", "raw", "zip"],
        }

    def extract_urls(self, text: str) -> list[str]:
        urls = list(
            dict.fromkeys(
                match_url(fragment).rstrip(".,;!?:，。；！、）)]}>")
                for fragment in text.split()
                if match_url(fragment)
            )
        )
        if not urls and self.parser.get_platform(text):
            urls = [text if text.startswith(("http://", "https://")) else "https://" + text]
        if not urls:
            raise EngineError("unsupported_url", "identify")
        if len(urls) > 10:
            raise EngineError("too_many_urls", "identify")
        return urls

    def identify(self, url: str) -> str:
        validate_url(url)
        platform = self.parser.get_platform(url)
        if platform is None:
            raise EngineError("unsupported_url", "identify")
        return str(platform.id)

    async def prepare(
        self,
        url: str,
        mode: str = "auto",
        output_mode: str = "preview",
        config: dict[str, Any] | None = None,
        progress: Progress | None = None,
        directory: Path | None = None,
        refresh: bool = False,
        register_directory: RegisterPath | None = None,
        register_file: RegisterPath | None = None,
        use_persistent_cache: bool = False,
    ) -> dict[str, Any]:
        platform = self.identify(url)
        if mode not in {"auto", "read_only"} or output_mode not in {"preview", "raw", "zip"}:
            raise EngineError("unsupported_mode", "identify")
        output_root = (directory or self.root).resolve()
        if output_root != self.root:
            raise EngineError("media_processing_failed", "prepare")

        try:
            upstream = await self.upstream.prepare(
                url,
                mode=mode,
                output_mode=output_mode,
                refresh=refresh,
                directory=output_root,
                progress=progress,
                use_persistent_cache=use_persistent_cache,
            )
        except OriginalPipelineError as error:
            credential_used = self._credential_used(platform, config)
            failure = self._log_failure(error.stage, platform, error.__cause__ or error, credential_used)
            raise EngineError(self._failure_code(failure, credential_used), error.stage) from error

        if upstream.cached is not None:
            return self._adapt_cache(url, platform, upstream, self._credential_used(platform, config))
        if upstream.pipeline is None:
            raise EngineError("upstream_contract", "parse")
        return await self._adapt_pipeline(
            url,
            platform,
            upstream,
            output_mode=output_mode,
            credential_used=self._credential_used(platform, config),
            register_directory=register_directory,
            register_file=register_file,
        )

    def _base_result(
        self,
        *,
        source_url: str,
        canonical_url: str,
        platform: str,
        title: str,
        plain_content: str,
        result_type: str,
        markdown_content: str | None,
        credential_used: bool,
    ) -> dict[str, Any]:
        content = markdown_content if markdown_content is not None else plain_content
        result: dict[str, Any] = {
            "platform": platform,
            "sourceUrl": source_url,
            "canonicalUrl": canonical_url,
            "title": title,
            "resultType": result_type,
            "plainContent": plain_content,
            "content": content,
            "contentType": "article" if result_type == "richtext" else "post",
            "contentFormat": "markdown" if markdown_content is not None else "plain",
            "access": "public",
            "truncated": False,
            "externalPublicationAllowed": not credential_used,
            "media": [],
            "mediaFailureCount": 0,
            "mediaFailures": [],
            "_files": [],
            "_mediaFiles": {},
        }
        if markdown_content is not None:
            result["markdownContent"] = markdown_content
        return result

    def _adapt_cache(
        self, source_url: str, platform: str, upstream: UpstreamOutput, credential_used: bool,
    ) -> dict[str, Any]:
        cached = upstream.cached
        assert cached is not None
        result_type = "richtext" if cached.rich else "unknown"
        result = self._base_result(
            source_url=source_url,
            canonical_url=upstream.raw_url,
            platform=platform,
            title=cached.parse_result.title,
            plain_content="" if cached.rich else cached.parse_result.content,
            result_type=result_type,
            markdown_content=cached.parse_result.content if cached.rich else None,
            credential_used=credential_used,
        )
        if cached.telegraph_url:
            result["telegraphUrl"] = cached.telegraph_url
        result["media"] = [worker_cached_media(media) for media in cached.media or []]
        return result

    async def _adapt_pipeline(
        self,
        source_url: str,
        platform: str,
        upstream: UpstreamOutput,
        *,
        output_mode: str,
        credential_used: bool,
        register_directory: RegisterPath | None,
        register_file: RegisterPath | None,
    ) -> dict[str, Any]:
        pipeline = upstream.pipeline
        assert pipeline is not None
        parsed = pipeline.parse_result
        plain_content = str(getattr(parsed, "content", "") or "")
        markdown = getattr(parsed, "markdown_content", None)
        markdown_content = str(markdown) if isinstance(markdown, str) else None
        result_type = str(getattr(getattr(parsed, "type", None), "value", "unknown"))
        canonical = str(getattr(parsed, "raw_url", "") or upstream.raw_url)
        result = self._base_result(
            source_url=source_url,
            canonical_url=canonical,
            platform=platform,
            title=str(getattr(parsed, "title", "") or ""),
            plain_content=plain_content,
            result_type=result_type,
            markdown_content=markdown_content,
            credential_used=credential_used,
        )
        if pipeline.output_dir is None:
            return result

        if pipeline.output_dir.is_symlink():
            raise EngineError("media_processing_failed", "convert")
        output_dir = pipeline.output_dir.resolve()
        if output_dir.parent != self.root or not output_dir.is_dir():
            raise EngineError("media_processing_failed", "convert")
        if register_directory:
            register_directory(output_dir)

        if output_mode == "zip":
            archive = output_dir.with_suffix(".tar.gz")
            if archive.exists():
                raise EngineError("media_processing_failed", "convert")
            if register_file:
                register_file(archive)
            packing = asyncio.create_task(asyncio.to_thread(pack_dir_to_tar_gz, output_dir))
            try:
                archive = await asyncio.shield(packing)
            except asyncio.CancelledError:
                await packing
                raise
            result["media"].append(self._local_media(result, archive, "document"))
        elif output_mode == "raw":
            for processed in pipeline.processed_list:
                result["media"].append(self._local_media(result, Path(processed.source.path), "document"))
                if isinstance(processed.source, LivePhotoFile) and processed.source.video_path:
                    result["media"].append(self._local_media(result, Path(processed.source.video_path), "document"))
        else:
            self._adapt_preview(result, pipeline)

        result["_directory"] = str(output_dir)
        result["_files"] = [
            str(path.resolve()) for path in output_dir.rglob("*") if path.is_file() and not path.is_symlink()
        ]
        if output_mode == "zip":
            archive_path = output_dir.with_suffix(".tar.gz")
            if archive_path.is_file():
                result["_files"].append(str(archive_path.resolve()))
        await self._attach_asset_keys(result)
        logger.info(
            "event=media.summary platform=%s expected=%s available=%s",
            platform,
            len(pipeline.processed_list),
            len(result["media"]),
        )
        return result

    def _adapt_preview(self, result: dict[str, Any], pipeline: PipelineResult) -> None:
        for processed in pipeline.processed_list:
            source = processed.source
            paths = [Path(path) for path in processed.output_paths or [source.path]]
            if isinstance(source, LivePhotoFile):
                self._adapt_live_photo(result, processed, paths)
                continue
            kind = (
                "photo"
                if isinstance(source, ImageFile)
                else "animation"
                if isinstance(source, AniFile)
                else "video"
                if isinstance(source, VideoFile)
                else "document"
            )
            for path in paths:
                width, height, duration = self._media_info(processed, path)
                result["media"].append(
                    self._local_media(
                        result,
                        path,
                        kind,
                        width=width,
                        height=height,
                        duration=duration,
                    )
                )

    def _adapt_live_photo(
        self, result: dict[str, Any], processed: ProcessedMedia, photo_paths: list[Path],
    ) -> None:
        source = processed.source
        assert isinstance(source, LivePhotoFile)
        video_path = (processed.motion.path if processed.motion
                      else Path(source.video_path) if source.video_path else None)
        photos = []
        for path in photo_paths:
            width, height, _ = self._media_info(processed, path)
            photos.append(self._local_media(result, path, "photo", width=width, height=height))
        if video_path is None:
            result["media"].extend(photos)
            return
        video_width, video_height, video_duration = resolve_live_photo_video_info(processed)
        video = self._local_media(
            result,
            video_path,
            "video",
            width=video_width,
            height=video_height,
            duration=video_duration,
        )
        # Rich delivery represents the pair as adjacent photo/video blocks, so native
        # sendLivePhoto's 10-second/10-MiB limits no longer apply to the pairing contract.
        if len(photos) == 1:
            photo = photos[0]
            result["media"].append(
                {
                    "type": "live_photo",
                    "mediaId": photo["mediaId"],
                    "videoMediaId": video["mediaId"],
                    "sizeBytes": photo["sizeBytes"],
                    "videoSizeBytes": video["sizeBytes"],
                    "mimeType": photo["mimeType"],
                    "videoMimeType": video["mimeType"],
                    "filename": photo["filename"],
                    "videoFilename": video["filename"],
                    "width": video.get("width", 0),
                    "height": video.get("height", 0),
                    "durationSeconds": (processed.motion.duration if processed.motion
                                        else video.get("durationSeconds", 0)),
                    "nativeError": processed.motion.native_error if processed.motion else None,
                }
            )
        else:
            result["media"].extend([*photos, video])

    async def _attach_asset_keys(self, result: dict[str, Any]) -> None:
        """Compute content keys off the event loop so delivery can reuse them cheaply."""
        source = str(result.get("canonicalUrl") or result.get("sourceUrl") or "")
        for index, media in enumerate(result.get("media", [])):
            paths = [Path(result["_mediaFiles"][media["mediaId"]]["path"])]
            if media.get("type") == "live_photo" and media.get("videoMediaId"):
                paths.append(Path(result["_mediaFiles"][media["videoMediaId"]]["path"]))
            media["assetKey"] = await asyncio.to_thread(asset_key, source, index, paths)

    def _local_media(
        self,
        result: dict[str, Any],
        path: Path,
        kind: str,
        *,
        width: int = 0,
        height: int = 0,
        duration: int = 0,
    ) -> dict[str, Any]:
        if path.is_symlink():
            raise EngineError("media_processing_failed", "convert")
        path = path.resolve()
        if not path.is_file() or not path.is_relative_to(self.root):
            raise EngineError("media_processing_failed", "convert")
        media_id = uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or {
            "photo": "image/jpeg",
            "video": "video/mp4",
            "animation": "image/gif",
            "document": "application/octet-stream",
        }[kind]
        size = path.stat().st_size
        descriptor: dict[str, Any] = {
            "type": kind,
            "mediaId": media_id,
            "sizeBytes": size,
            "mimeType": mime,
            "filename": path.name,
        }
        if width:
            descriptor["width"] = width
        if height:
            descriptor["height"] = height
        if duration:
            descriptor["durationSeconds"] = duration
        result["_mediaFiles"][media_id] = {"path": str(path), "sizeBytes": size, "mimeType": mime}
        return descriptor

    @staticmethod
    def _media_info(processed: ProcessedMedia, path: Path) -> tuple[int, int, int]:
        try:
            return resolve_media_info(processed, str(path))
        except Exception:
            source = processed.source
            return (
                int(getattr(source, "width", 0) or 0),
                int(getattr(source, "height", 0) or 0),
                int(getattr(source, "duration", 0) or 0),
            )

    def _credential_used(self, platform: str, config: dict[str, Any] | None) -> bool:
        snapshot = self.config if config is None else config
        return bool(snapshot.get("platforms", {}).get(platform, {}).get("cookies", []))

    @staticmethod
    def _failure_code(failure: _UpstreamFailure, credential_used: bool) -> str:
        if failure.reason == "login_required":
            return "credentials_invalid" if credential_used else "credentials_required"
        if failure.reason in {"content_not_found", "content_missing", "response_data_missing"}:
            return "content_unavailable"
        if failure.reason == "timeout":
            return "upstream_timeout"
        return "upstream_http" if failure.http_status else "upstream_contract"

    @staticmethod
    def _error_chain(error: BaseException) -> list[BaseException]:
        seen: set[int] = set()
        chain: list[BaseException] = []
        cause: BaseException | None = error
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            chain.append(cause)
            cause = cause.__cause__ or cause.__context__
        return chain

    @staticmethod
    def _failure_reason(chain: list[BaseException], status: int | None) -> str:
        if status is not None:
            return "http_error"
        messages = [str(item) for item in chain]
        if any("需要登录" in message or "login required" in message.lower() for message in messages):
            return "login_required"
        if any("不存在" in message or "not found" in message.lower() for message in messages):
            return "content_not_found"
        if any("No data found" in message for message in messages):
            return "response_data_missing"
        if any("未获取到内容" in message for message in messages):
            return "content_missing"
        if any(isinstance(item, TimeoutError) or "Timeout" in type(item).__name__ for item in chain):
            return "timeout"
        if any(isinstance(item, (KeyError, TypeError, ValueError)) for item in chain):
            return "response_contract"
        return "parse_rejected" if any(type(item).__name__ == "ParseError" for item in chain) else "unclassified"

    @staticmethod
    def _log_failure(
        stage: str, platform: str, error: BaseException, credential_used: bool,
    ) -> _UpstreamFailure:
        chain = ParseHubEngine._error_chain(error)
        status = None
        for cause in chain:
            candidate = getattr(getattr(cause, "response", None), "status_code", None)
            if isinstance(candidate, int) and 100 <= candidate <= 599:
                status = candidate
                break
        reason = ParseHubEngine._failure_reason(chain, status)
        logger.warning(
            "event=upstream.failed stage=%s platform=%s http_status=%s reason=%s "
            "error_type=%s cause_type=%s credential_used=%s",
            stage,
            platform,
            status or "none",
            reason,
            type(error).__name__,
            type(chain[-1]).__name__,
            str(credential_used).lower(),
        )
        if logger.isEnabledFor(logging.DEBUG):
            frames = [
                f'{"/".join(Path(frame.filename).parts[-2:])}:{frame.name}:{frame.lineno}'
                for item in chain
                for frame in traceback.extract_tb(item.__traceback__)
            ]
            logger.debug(
                "event=upstream.diagnostic stage=%s platform=%s exception_chain=%s traceback=%s",
                stage,
                platform,
                ">".join(type(item).__name__ for item in chain),
                ">".join(frames[-12:]) or "none",
            )
        return _UpstreamFailure(status, reason)


Engine = ParseHubEngine
