"""ParseHub 全注册表适配器，不导入 Bot、插件或全局配置。"""

import asyncio
import copy
import importlib.metadata
import json
import logging
import random
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from parsehub import ParseHub
from parsehub.utils.helpers import match_url

from utils.helpers import pack_dir_to_tar_gz
from worker.media import prepare_file
from worker.security import EngineError, validate_url

Progress = Callable[[str], Awaitable[None]]
logger = logging.getLogger('parsehub.worker')


class ParseHubEngine:
    def __init__(self, root: Path, parser: Any = None):
        self.root = root
        self.parser = parser or ParseHub()
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

    async def cache_identity(self, url: str, config: dict | None = None) -> str:
        """Use the original bot's get_raw_url retries; never replace the parse input."""
        platform = self.identify(url)
        snapshot = self.config if config is None else config
        settings = snapshot.get('platforms', {}).get(platform, {})
        defaults = snapshot.get('defaults', {})
        proxies = settings.get('parser_proxies', defaults.get('parser_proxies', []))
        for attempt in range(1, 4):
            try:
                resolved = str(await self.parser.get_raw_url(url, proxy=self._choose(proxies), clean_all=False))
                if self.identify(resolved) != platform:
                    raise EngineError('unsupported_url', 'redirect')
                provider = self.parser.get_parser(resolved)
                if provider is None:
                    raise EngineError('unsupported_url', 'redirect')
                return str(provider._clean_params(resolved, provider.__after_clean_parameters__))
            except Exception as exc:
                self._log_failure('redirect', platform, attempt, exc)
                if attempt == 3:
                    raise EngineError('upstream_http', 'redirect') from exc
        raise AssertionError('unreachable')

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
        result: dict = {
            'platform': platform, 'sourceUrl': url, 'canonicalUrl': canonical,
            'title': getattr(parsed, 'title', ''),
            'content': getattr(parsed, 'markdown_content', None) or getattr(parsed, 'content', ''),
            'contentType': 'article' if getattr(getattr(parsed, 'type', None), 'value', '') == 'richtext' else 'post',
            # Legacy API access=public means deliverable, not an independent visibility attestation.
            'contentFormat': 'markdown', 'access': 'public', 'truncated': False,
            'externalPublicationAllowed': not used_cookie and getattr(parsed, 'access', None)
            not in ('private', 'restricted', 'paid', 'unknown'),
            'media': [], 'mediaFailureCount': 0, 'mediaFailures': [], '_files': [], '_mediaFiles': {},
        }

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
        for source, target in (('author', 'author'), ('published_at', 'publishedAt')):
            value = getattr(parsed, source, None)
            if isinstance(value, str | dict):
                result[target] = {'name': value} if source == 'author' and isinstance(value, str) else value
        if mode == 'read_only':
            return result
        download_root = (directory or self.root).resolve()
        refs = getattr(parsed, 'media', None)
        refs = list(refs) if isinstance(refs, list | tuple) else ([refs] if refs else [])
        if not refs:
            return result
        result['_expectedMedia'] = len(refs)
        await report('download')
        downloaded: list[tuple[Path, bool, str | None]] = []
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
                paths = [Path(file.path)]
                if getattr(file, 'video_path', None):
                    paths.append(Path(file.video_path))
                for path in paths:
                    if not path.resolve().is_relative_to(directory) or path.is_symlink():
                        raise EngineError('media_processing_failed', 'download')
                    animation = type(refs[index]).__name__ == 'AniRef' if index < len(refs) else False
                    downloaded.append((path, animation, pair))
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
            for index, (path, animation, pair) in enumerate(downloaded):
                try:
                    items = await prepare_file(path, directory / 'processed', output_mode, animation)
                    for item in items:
                        if pair is not None:
                            item['pairedMediaId'] = pair
                        prepared.append(item)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    media_failed('convert', index, error)
        # Only publish prepared local files. The caller owns Telegram uploading,
        # sending and delivery receipts; no cross-process file_id construction.
        for item in prepared:
            path = Path(item['path']).resolve()
            if (not path.is_relative_to(directory.resolve()) and path != archive) or not path.is_file():
                raise EngineError('media_processing_failed', 'convert')
            media_id = uuid.uuid4().hex
            descriptor = {k: v for k, v in item.items() if k not in ('path', 'thumbnailPath')}
            descriptor.update(mediaId=media_id, sizeBytes=path.stat().st_size)
            result['media'].append(descriptor)
            result['_mediaFiles'][media_id] = {'path': str(path), 'sizeBytes': path.stat().st_size,
                                              'mimeType': item['mimeType']}
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
        for attempt in range(1, 4):
            cookie = self._choose(cookies)
            proxy = self._choose(proxies)
            try:
                parsed = await self.parser.parse(url, proxy=proxy, cookie=cookie)
                return parsed, bool(cookie)
            except Exception as exc:
                status = self._log_failure('parse', platform, attempt, exc)
                if attempt == 3:
                    raise EngineError('upstream_http' if status else 'upstream_contract') from exc
        raise AssertionError('unreachable')

    @staticmethod
    def _choose(values: list[str]) -> str | None:
        return random.choice(values) if values else None

    @staticmethod
    def _log_failure(stage: str, platform: str, attempt: int, error: Exception) -> int | None:
        # ParseHub wraps HTTP errors; retain only numeric status, never request URLs or headers.
        seen: set[int] = set()
        cause: BaseException | None = error
        status = None
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            candidate = getattr(getattr(cause, 'response', None), 'status_code', None)
            if isinstance(candidate, int) and 100 <= candidate <= 599:
                status = candidate
                break
            cause = cause.__cause__ or cause.__context__
        logger.warning('event=upstream.failed stage=%s platform=%s attempt=%s http_status=%s',
                       stage, platform, attempt, status or 'none')
        return status


Engine = ParseHubEngine
