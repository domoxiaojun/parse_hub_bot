from pathlib import Path

import pytest
import yaml

from worker.platform_config import ConfigConflict, PlatformConfigFile

PLATFORMS = [{'id': 'youtube', 'name': 'YouTube'}, {'id': 'bilibili', 'name': 'Bilibili'}]


def test_missing_original_yaml_empty_and_no_write(tmp_path: Path) -> None:
    path = tmp_path / 'platform_config.yaml'
    config = PlatformConfigFile(path, PLATFORMS)
    assert config.active_config().platforms['youtube'].cookies == []
    assert not path.exists()
    assert config.snapshot()['requiresRestart'] is False


def test_original_yaml_flags_defaults_and_redaction(tmp_path: Path) -> None:
    path = tmp_path / 'platform_config.yaml'
    path.write_text('''default_parser_proxies: http://user:private@127.0.0.1:7890
platforms:
  youtube:
    cookies: ['token=secret']
    disable_parser_proxy: true
  bilibili:
    downloader_proxies: ['socks5://127.0.0.1:1080']
''')
    config = PlatformConfigFile(path, PLATFORMS)
    active = config.active_config()
    assert active.platforms['youtube'].parser_proxies == []
    assert active.platforms['bilibili'].parser_proxies == ['http://user:private@127.0.0.1:7890']
    assert active.platforms['bilibili'].downloader_proxies == ['socks5://127.0.0.1:1080']
    snapshot = config.snapshot()
    assert 'private' not in str(snapshot) and 'secret' not in str(snapshot)
    assert snapshot['defaults']['parser'][0]['authenticated'] is True


def test_atomic_edit_preserves_settings_and_requires_restart(tmp_path: Path) -> None:
    path = tmp_path / 'platform_config.yaml'
    path.write_text('''custom_setting: preserve
platforms:
  youtube:
    cookies: ['a=first', 'b=second']
    extra_setting: keep
  other:
    cookies: ['untouched=value']
''')
    config = PlatformConfigFile(path, PLATFORMS)
    before = config.snapshot()
    result = config.update({'baseSha256': before['sha256'], 'platform': 'youtube',
                            'cookies': [{'keep': 1}, {'value': 'c=new'}], 'parser': {'mode': 'direct'}})
    assert result['requiresRestart'] is True
    saved = yaml.safe_load(path.read_text())
    assert saved['custom_setting'] == 'preserve'
    assert saved['platforms']['youtube']['extra_setting'] == 'keep'
    assert saved['platforms']['other']['cookies'] == ['untouched=value']
    assert saved['platforms']['youtube']['cookies'] == ['b=second', 'c=new']
    assert config.active_config().platforms['youtube'].cookies == ['a=first', 'b=second']
    with pytest.raises(ConfigConflict):
        config.update({'baseSha256': before['sha256'], 'platform': 'youtube', 'cookies': []})
    restarted = PlatformConfigFile(path, PLATFORMS)
    assert restarted.active_config().platforms['youtube'].cookies == ['b=second', 'c=new']
    assert not restarted.snapshot()['requiresRestart']


def test_default_edits_and_invalid_keeps_are_atomic(tmp_path: Path) -> None:
    path = tmp_path / 'platform_config.yaml'
    config = PlatformConfigFile(path, PLATFORMS)
    config.update({'baseSha256': config.snapshot()['sha256'], 'scope': 'defaults',
                   'parser': [{'value': 'http://localhost:7890'}]})
    content = path.read_bytes()
    with pytest.raises(ValueError):
        config.update({'baseSha256': config.snapshot()['sha256'], 'platform': 'youtube',
                       'cookies': [{'keep': 99}]})
    assert path.read_bytes() == content
    with pytest.raises(ValueError):
        config.update({'baseSha256': config.snapshot()['sha256'], 'platform': 'youtube',
                       'cookies': [{'value': 'a=b\nInjected: yes'}]})
    assert path.read_bytes() == content
