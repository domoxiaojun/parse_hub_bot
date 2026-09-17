"""Edit the original ParseHub YAML without importing Bot configuration singletons."""

import copy
import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from worker.models import ConfigInput


class ConfigConflict(ValueError):
    pass


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    if len(values) > 256 or any(not isinstance(item, str) for item in values):
        raise ValueError('invalid_platform_config')
    return list(dict.fromkeys(item for item in values if item))


def proxy_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https', 'socks5', 'socks5h') or not parsed.hostname:
        raise ValueError('invalid_proxy')
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('invalid_proxy')
    if any(ord(char) < 32 or ord(char) == 127 for char in unquote(value)):
        raise ValueError('invalid_proxy')
    if parsed.port is not None and not 0 < parsed.port <= 65535:
        raise ValueError('invalid_proxy')
    return value


def proxy_summary(values: list[str]) -> list[dict[str, Any]]:
    summaries = []
    for index, value in enumerate(values):
        parsed = urlsplit(proxy_url(value))
        hostname = parsed.hostname or ''
        if ':' in hostname:
            hostname = '[' + hostname + ']'
        port = f':{parsed.port}' if parsed.port else ''
        summaries.append({'index': index, 'label': f'{parsed.scheme}://{hostname}{port}',
                          'authenticated': bool(parsed.username or parsed.password)})
    return summaries


def cookie_value(value: str) -> str:
    # The Admin already normalizes copied headers/JSON; never accept injected lines.
    if not value or len(value) > 32768 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('invalid_cookie')
    if any('=' not in part or not part.split('=', 1)[0].strip() for part in value.split(';') if part.strip()):
        raise ValueError('invalid_cookie')
    return value


def apply_edits(current: list[str], edits: Any, *, proxy: bool) -> list[str]:
    if not isinstance(edits, list) or len(edits) > 32:
        raise ValueError('invalid_edits')
    output = []
    kept = set()
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError('invalid_edits')
        if set(edit) == {'keep'}:
            index = edit['keep']
            if type(index) is not int or index < 0 or index >= len(current) or index in kept:
                raise ValueError('invalid_edits')
            kept.add(index)
            value = current[index]
        elif set(edit) == {'value'} and isinstance(edit['value'], str):
            value = proxy_url(edit['value']) if proxy else cookie_value(edit['value'])
        else:
            raise ValueError('invalid_edits')
        if value not in output:
            output.append(value)
    return output


class PlatformConfigFile:
    def __init__(self, path: Path, platforms: list[dict[str, Any]]):
        self.path = path
        self.platforms = platforms
        self._lock = threading.Lock()
        self._active_version, self._active = self._read()
        self._normalize(self._active, self._active_version)

    def _read_bytes(self) -> bytes:
        return self.path.read_bytes() if self.path.exists() else b''

    @staticmethod
    def _version(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _read(self) -> tuple[str, dict[str, Any]]:
        content = self._read_bytes()
        if len(content) > 1024 * 1024:
            raise ValueError('platform_config_too_large')
        try:
            raw = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            raise ValueError('invalid_platform_config') from exc
        if not isinstance(raw, dict) or not isinstance(raw.get('platforms') or {}, dict):
            raise ValueError('invalid_platform_config')
        return self._version(content), raw

    def _normalize(self, raw: dict[str, Any], version: str) -> ConfigInput:
        defaults = {f'{stage}_proxies': string_list(raw.get(f'default_{stage}_proxies'))
                    for stage in ('parser', 'downloader')}
        platforms = {}
        for platform in self.platforms:
            entry = (raw.get('platforms') or {}).get(platform['id']) or {}
            if not isinstance(entry, dict):
                raise ValueError('invalid_platform_config')
            resolved: dict[str, Any] = {'cookies': string_list(entry.get('cookies'))}
            for stage in ('parser', 'downloader'):
                custom = string_list(entry.get(f'{stage}_proxies'))
                values = [] if entry.get(f'disable_{stage}_proxy') else custom or defaults[f'{stage}_proxies']
                resolved[f'{stage}_proxies'] = [proxy_url(value) for value in values]
            platforms[platform['id']] = resolved
        return ConfigInput.model_validate({'version': version, 'defaults': defaults, 'platforms': platforms})

    def active_config(self) -> ConfigInput:
        return self._normalize(copy.deepcopy(self._active), self._active_version)

    def snapshot(self) -> dict[str, Any]:
        version, raw = self._read()
        saved_config = self._normalize(raw, version)
        active_config = self.active_config()
        platforms = []
        for platform in self.platforms:
            platform_id = platform['id']
            saved = raw.get('platforms', {}).get(platform_id) or {}
            active = self._active.get('platforms', {}).get(platform_id) or {}
            summary = {'id': platform_id, 'name': platform.get('name', platform_id),
                       'cookieCount': len(string_list(saved.get('cookies'))),
                       'activeCookieCount': len(string_list(active.get('cookies'))),
                       'requiresRestart': saved != active
                       or saved_config.platforms[platform_id] != active_config.platforms[platform_id]}
            for stage in ('parser', 'downloader'):
                proxies = string_list(saved.get(f'{stage}_proxies'))
                mode = 'direct' if saved.get(f'disable_{stage}_proxy') else 'custom' if proxies else 'inherit'
                effective = [] if mode == 'direct' else proxies or string_list(raw.get(f'default_{stage}_proxies'))
                summary[stage] = {'mode': mode, 'proxies': proxy_summary(proxies if mode == 'custom' else []),
                                  'effectiveCount': len(effective)}
            platforms.append(summary)
        writable = os.access(self.path if self.path.exists() else self.path.parent, os.W_OK)
        return {'sha256': version, 'writable': writable, 'requiresRestart': raw != self._active,
                'defaults': {stage: proxy_summary(string_list(raw.get(f'default_{stage}_proxies')))
                             for stage in ('parser', 'downloader')}, 'platforms': platforms}

    def update(self, update: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            version, raw = self._read()
            if update.get('baseSha256') != version:
                raise ConfigConflict('config_conflict')
            changed = copy.deepcopy(raw)
            defaults = update.get('scope') == 'defaults'
            allowed = {'baseSha256', 'scope', 'parser', 'downloader'} if defaults else {
                'baseSha256', 'platform', 'cookies', 'parser', 'downloader',
            }
            if set(update) - allowed:
                raise ValueError('invalid_config_update')
            if defaults:
                for stage in ('parser', 'downloader'):
                    if stage in update:
                        key = f'default_{stage}_proxies'
                        changed[key] = apply_edits(string_list(changed.get(key)), update[stage], proxy=True)
            else:
                platform = update.get('platform')
                if platform not in {item['id'] for item in self.platforms}:
                    raise ValueError('unsupported_platform')
                platforms = changed.setdefault('platforms', {})
                entry = platforms.get(platform) or {}
                if not isinstance(entry, dict):
                    raise ValueError('invalid_platform_config')
                platforms[platform] = entry
                if 'cookies' in update:
                    entry['cookies'] = apply_edits(string_list(entry.get('cookies')), update['cookies'], proxy=False)
                for stage in ('parser', 'downloader'):
                    if stage not in update:
                        continue
                    edit = update[stage]
                    if not isinstance(edit, dict) or edit.get('mode') not in ('inherit', 'direct', 'custom'):
                        raise ValueError('invalid_proxy_edit')
                    mode = edit['mode']
                    if set(edit) != ({'mode', 'proxies'} if mode == 'custom' else {'mode'}):
                        raise ValueError('invalid_proxy_edit')
                    entry[f'disable_{stage}_proxy'] = mode == 'direct'
                    key = f'{stage}_proxies'
                    if mode == 'custom':
                        entry[key] = apply_edits(string_list(entry.get(key)), edit['proxies'], proxy=True)
                        if not entry[key]:
                            raise ValueError('invalid_proxy_edit')
                    else:
                        entry.pop(key, None)
            content = yaml.safe_dump(changed, allow_unicode=True, sort_keys=False).encode()
            self._normalize(changed, self._version(content))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix='.platform-config-')
            try:
                with os.fdopen(fd, 'wb') as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                if self._version(self._read_bytes()) != version:
                    raise ConfigConflict('config_conflict')
                os.replace(temp, self.path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            return {'sha256': self._version(content), 'requiresRestart': changed != self._active}
