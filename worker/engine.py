"""ParseHub 全注册表适配器，不导入 Bot、插件或全局配置。"""

import asyncio
import copy
import importlib.metadata
import json
import logging
import random
import re
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from parsehub.utils.helpers import match_url

from utils.helpers import pack_dir_to_tar_gz
from worker.media import prepare_file
from worker.security import EngineError, validate_url

Progress = Callable[[str], Awaitable[None]]
logger = logging.getLogger('parsehub.worker')


@dataclass(frozen=True)
class _UpstreamFailure:
    http_status: int | None
    reason: str


class ParseHubEngine:
    def __init__(
        self, root: Path, parser: Any, native_parse: Callable[[str], Awaitable[Any]],
    ):
        self.root = root
        self.parser = parser
        self.native_parse = native_parse
        self.config: dict = {}

    def configure(self, config: dict) -> None:
        self.config = copy.deepcopy(config)

    def capabilities(self) -> dict:
        return {
            'version': importlib.metadata.version('parsehub'),
            'platforms': [{'id': p['id'], 'name': p['name'], 'contentTypes': p['supported_types']}
                          for p in self.parser.get_platforms()],
            'outputModes': ['preview', 'raw', 'zip'],
        }

    def extract_urls(self, text: str) -> list[str]:
        urls = list(dict.fromkeys(
            match_url(fragment).rstrip('.,;!?:，。；！、）)]}>')
            for fragment in text.split() if match_url(fragment)
        ))
        if not urls and self.parser.get_platform(text):
            urls = [text if text.startswith(('http://', 'https://')) else 'https://' + text]
        if not urls:
            raise EngineError('unsupported_url', 'identify')
        if len(urls) > 10:
            raise EngineError('too_many_urls', 'identify')
        return urls

    def identify(self, url: str) -> str:
        validate_url(url)
        platform = self.parser.get_platform(url)
        if platform is None:
            raise EngineError('unsupported_url', 'identify')
        return str(platform.id)

    async def prepare(
        self, url: str, mode: str = 'auto', output_mode: str = 'preview', config: dict | None = None,
        progress: Progress | None = None, directory: Path | None = None, refresh: bool = False,
        register_directory: Callable[[Path], None] | None = None,
        register_file: Callable[[Path], None] | None = None,
    ) -> dict:
        platform = self.identify(url)
        if mode not in ('auto', 'read_only') or output_mode not in ('preview', 'raw', 'zip'):
            raise EngineError('unsupported_mode', 'identify')
        snapshot = copy.deepcopy(self.config if config is None else config)
        settings = snapshot.get('platforms', {}).get(platform, {})
        defaults = snapshot.get('defaults', {})
        proxies = settings.get('parser_proxies', defaults.get('parser_proxies', []))
        download_proxies = settings.get('downloader_proxies', defaults.get('downloader_proxies', []))
        cookies = settings.get('cookies', [])

        async def report(stage: str) -> None:
            if progress:
                await progress(stage)

        await report('parse')
        parsed, used_cookie = await self._parse(url, platform, proxies, cookies)
        canonical = getattr(parsed, 'raw_url', '') or url
        result_type = getattr(getattr(parsed, 'type', None), 'value', 'unknown')
        plain_content = str(getattr(parsed, 'content', '') or '')
        markdown_content = getattr(parsed, 'markdown_content', None)
        content = str(markdown_content) if isinstance(markdown_content, str) else plain_content
        result: dict = {
            'platform': platform, 'sourceUrl': url, 'canonicalUrl': canonical,
            'title': getattr(parsed, 'title', ''),
            'resultType': result_type,
            'plainContent': plain_content,
            'content': content,
            'contentType': 'article' if result_type == 'richtext' else 'post',
            # Legacy API access=public means deliverable, not an independent visibility attestation.
            'contentFormat': 'markdown' if isinstance(markdown_content, str) else 'plain',
            'access': 'public', 'truncated': False,
            'externalPublicationAllowed': not used_cookie and getattr(parsed, 'access', None)
            not in ('private', 'restricted', 'paid', 'unknown'),
            'media': [], 'mediaFailureCount': 0, 'mediaFailures': [], '_files': [], '_mediaFiles': {},
        }
        if isinstance(markdown_content, str):
            result['markdownContent'] = markdown_content

        def media_failed(stage: str, index: int, error: Exception) -> None:
            code = {'download': 'download_failed', 'convert': 'media_processing_failed',
                    'upload': 'upload_failed'}[stage]
            result['mediaFailureCount'] += 1
            failure = next((item for item in result['mediaFailures'] if item['stage'] == stage), None)
            if failure:
                failure['count'] += 1
            else:
                result['mediaFailures'].append({'stage': stage, 'code': code, 'count': 1})
            # Only class-level RPC identifiers, never exception messages, URLs or credentials.
            rpc_code = getattr(type(error), 'ID', '')
            if not isinstance(rpc_code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', rpc_code):
                rpc_code = 'none'
            logger.warning('event=media.failed platform=%s stage=%s index=%s code=%s error_type=%s rpc_code=%s',
                           platform, stage, index, code, type(error).__name__, rpc_code)
        if mode == 'read_only' or result_type == 'richtext':
            return result
        download_root = (directory or self.root).resolve()
        refs = getattr(parsed, 'media', None)
        refs = list(refs) if isinstance(refs, list | tuple) else ([refs] if refs else [])
        if not refs:
            return result
        result['_expectedMedia'] = len(refs)
        await report('download')
        downloaded: list[tuple[Path, bool, str | None, str | None]] = []
        archive: Path | None = None
        try:
            # Native ParseResult.download allocates name/name_N synchronously before
            # its first await. Register that same candidate without yielding, so a
            # cancellation during metadata/download still has an owned directory.
            for attempt in range(1, 4):
                directory = download_root / parsed.name
                counter = 2
                while directory.exists():
                    directory = download_root / f'{parsed.name}_{counter}'
                    counter += 1
                if directory.parent != download_root or directory == download_root:
                    raise EngineError('media_processing_failed', 'download')
                result['_directory'] = str(directory)
                if register_directory:
                    register_directory(directory)
                try:
                    # ParsePipeline: initial attempt plus two retries, each with its own timeout.
                    async with asyncio.timeout(1800):
                        download = await parsed.download(
                            download_root, proxy=self._choose(download_proxies), save_metadata=output_mode == 'zip',
                        )
                    break
                except Exception as exc:
                    self._log_failure('download', platform, attempt, exc)
                    if attempt == 3:
                        raise
                    await asyncio.sleep(1)
            if Path(download.output_dir).resolve() != directory:
                raise EngineError('media_processing_failed', 'download')
            files = download.media if isinstance(download.media, list | tuple) else [download.media]
            for index, file in enumerate(files):
                pair = str(index) if getattr(file, 'video_path', None) else None
                paths = [(Path(file.path), 'photo' if pair is not None else None)]
                if getattr(file, 'video_path', None):
                    paths.append((Path(file.video_path), 'video'))
                for path, pair_role in paths:
                    if not path.resolve().is_relative_to(directory) or path.is_symlink():
                        raise EngineError('media_processing_failed', 'download')
                    animation = type(refs[index]).__name__ == 'AniRef' if index < len(refs) else False
                    downloaded.append((path, animation, pair, pair_role))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Upstream's native batch downloader removes its whole output directory
            # when one download fails; preserve that contract and return text only.
            for index in range(len(refs)):
                media_failed('download', index, error)
            return result
        await report('convert')
        prepared = []
        if output_mode == 'zip' and downloaded:
            try:
                metadata_path = directory / 'metadata.json'
                if metadata_path.is_file():
                    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                    self._check_metadata_secrets(metadata, cookies)
                archive = directory.with_suffix('.tar.gz')
                if archive.exists():
                    raise EngineError('media_processing_failed', 'convert')
                if register_file:
                    register_file(archive)
                # The original helper preserves the native directory and archive
                # basename. Await cancellation until its background writer stops.
                packing = asyncio.create_task(asyncio.to_thread(pack_dir_to_tar_gz, directory))
                try:
                    archive = await asyncio.shield(packing)
                except asyncio.CancelledError:
                    await packing
                    raise
                prepared.extend(await prepare_file(archive, directory / 'processed', 'raw'))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                media_failed('convert', 0, error)
        else:
            for index, (path, animation, pair, pair_role) in enumerate(downloaded):
                try:
                    items = await prepare_file(path, directory / 'processed', output_mode, animation)
                    for item in items:
                        if pair is not None:
                            item['pairedMediaId'] = pair
                            item['pairedMediaRole'] = pair_role
                        prepared.append(item)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    media_failed('convert', index, error)
        # Only publish prepared local files. The caller owns Telegram uploading,
        # sending and delivery receipts; no cross-process file_id construction.
        def publish_media(item: dict[str, Any]) -> dict[str, Any]:
            path = Path(item['path']).resolve()
            if (not path.is_relative_to(directory.resolve()) and path != archive) or not path.is_file():
                raise EngineError('media_processing_failed', 'convert')
            media_id = uuid.uuid4().hex
            descriptor = {k: v for k, v in item.items() if k not in ('path', 'thumbnailPath')}
            descriptor.update(mediaId=media_id, sizeBytes=path.stat().st_size)
            result['_mediaFiles'][media_id] = {'path': str(path), 'sizeBytes': path.stat().st_size,
                                              'mimeType': item['mimeType']}
            return descriptor

        # Live Photo is one logical source item. Keep it atomic in the public
        # contract instead of asking every renderer to reconstruct a pair.
        cursor = 0
        while cursor < len(prepared):
            item = prepared[cursor]
            pair = item.get('pairedMediaId')
            end = cursor + 1
            while pair is not None and end < len(prepared) and prepared[end].get('pairedMediaId') == pair:
                end += 1
            group = prepared[cursor:end]
            photos = [entry for entry in group if entry.get('type') == 'photo'
                      and entry.get('pairedMediaRole') == 'photo']
            videos = [entry for entry in group if entry.get('type') == 'video'
                      and entry.get('pairedMediaRole') == 'video']
            if pair is not None and len(group) == 2 and len(photos) == 1 and len(videos) == 1:
                photo = publish_media(photos[0])
                video = publish_media(videos[0])
                native = video['sizeBytes'] <= 10 * 1024 * 1024 and int(video.get('durationSeconds') or 0) <= 10
                if native:
                    result['media'].append({
                        'type': 'live_photo',
                        'mediaId': photo['mediaId'],
                        'videoMediaId': video['mediaId'],
                        'sizeBytes': photo['sizeBytes'],
                        'videoSizeBytes': video['sizeBytes'],
                        'mimeType': photo.get('mimeType'),
                        'videoMimeType': video.get('mimeType'),
                        'filename': photo.get('filename'),
                        'videoFilename': video.get('filename'),
                        'width': photo.get('width') or video.get('width'),
                        'height': photo.get('height') or video.get('height'),
                        'durationSeconds': video.get('durationSeconds'),
                    })
                else:
                    photo.pop('pairedMediaId', None)
                    photo.pop('pairedMediaRole', None)
                    video.pop('pairedMediaId', None)
                    video.pop('pairedMediaRole', None)
                    result['media'].extend((photo, video))
            else:
                for entry in group:
                    descriptor = publish_media(entry)
                    descriptor.pop('pairedMediaId', None)
                    descriptor.pop('pairedMediaRole', None)
                    result['media'].append(descriptor)
            cursor = end
        logger.info('event=media.summary platform=%s expected=%s downloaded=%s prepared=%s available=%s failed=%s',
                    platform, len(refs), len(downloaded), len(prepared),
                    len(result['media']), result['mediaFailureCount'])
        result['_files'] = [str(path.resolve()) for path in directory.rglob('*') if path.is_file()]
        if archive and archive.is_file():
            result['_files'].append(str(archive.resolve()))
        return result

    @staticmethod
    def _check_metadata_secrets(value: Any, cookies: list[str]) -> None:
        """Native metadata is retained verbatim only when no credential fields leak."""
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in ('cookie', 'cookies', 'authorization', 'proxy_authorization'):
                    raise EngineError('media_processing_failed', 'metadata')
                ParseHubEngine._check_metadata_secrets(item, cookies)
        elif isinstance(value, list):
            for item in value:
                ParseHubEngine._check_metadata_secrets(item, cookies)
        elif isinstance(value, str) and any(cookie and cookie in value for cookie in cookies):
            raise EngineError('media_processing_failed', 'metadata')

    async def _parse(self, url: str, platform: str, proxies: list[str], cookies: list[str]) -> tuple[Any, bool]:
        try:
            # The upstream ParseService owns platform selection, Cookie/proxy choice
            # and its original three-attempt retry loop. Worker calls it exactly once.
            return await self.native_parse(url), bool(cookies)
        except Exception as exc:
            failure = self._log_failure(
                'parse', platform, 3, exc, credential_used=bool(cookies), proxy_used=bool(proxies),
            )
            raise EngineError(self._failure_code(failure, bool(cookies))) from exc

    @staticmethod
    def _failure_code(failure: _UpstreamFailure, credential_used: bool) -> str:
        if failure.reason == 'login_required':
            return 'credentials_invalid' if credential_used else 'credentials_required'
        if failure.reason in {'content_not_found', 'content_missing', 'response_data_missing'}:
            return 'content_unavailable'
        return 'upstream_http' if failure.http_status else 'upstream_contract'

    @staticmethod
    def _choose(values: list[str]) -> str | None:
        return random.choice(values) if values else None

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
            return 'http_error'
        messages = [str(item) for item in chain]
        if any('需要登录' in message or 'login required' in message.lower() for message in messages):
            return 'login_required'
        if any('不存在' in message or 'not found' in message.lower() for message in messages):
            return 'content_not_found'
        if any('No data found' in message for message in messages):
            return 'response_data_missing'
        if any('未获取到内容' in message for message in messages):
            return 'content_missing'
        if any(isinstance(item, TimeoutError) or 'Timeout' in type(item).__name__ for item in chain):
            return 'timeout'
        if any(isinstance(item, (KeyError, TypeError, ValueError)) for item in chain):
            return 'response_contract'
        return 'parse_rejected' if any(type(item).__name__ == 'ParseError' for item in chain) else 'unclassified'

    @staticmethod
    def _log_failure(
        stage: str, platform: str, attempt: int, error: Exception, *, credential_used: bool = False,
        proxy_used: bool = False,
    ) -> _UpstreamFailure:
        # ParseHub wraps HTTP errors. Log only stable classifications and source locations,
        # never exception messages, request URLs, headers, cookies, or proxy credentials.
        chain = ParseHubEngine._error_chain(error)
        status = None
        for cause in chain:
            candidate = getattr(getattr(cause, 'response', None), 'status_code', None)
            if isinstance(candidate, int) and 100 <= candidate <= 599:
                status = candidate
                break
        reason = ParseHubEngine._failure_reason(chain, status)
        logger.warning(
            'event=upstream.failed stage=%s platform=%s attempt=%s http_status=%s reason=%s '
            'error_type=%s cause_type=%s credential_used=%s proxy_used=%s',
            stage, platform, attempt, status or 'none', reason, type(error).__name__, type(chain[-1]).__name__,
            str(credential_used).lower(), str(proxy_used).lower(),
        )
        if logger.isEnabledFor(logging.DEBUG):
            frames = [
                f'{"/".join(Path(frame.filename).parts[-2:])}:{frame.name}:{frame.lineno}'
                for item in chain for frame in traceback.extract_tb(item.__traceback__)
            ]
            logger.debug(
                'event=upstream.diagnostic stage=%s platform=%s attempt=%s exception_chain=%s traceback=%s',
                stage, platform, attempt, '>'.join(type(item).__name__ for item in chain),
                '>'.join(frames[-12:]) or 'none',
            )
        return _UpstreamFailure(status, reason)


Engine = ParseHubEngine
