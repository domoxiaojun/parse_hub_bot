"""The fork must never edit upstream directories; every override goes through ext.hooks."""

import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ext import hooks
from ext.bot import inline
from ext.bot.registry import EXCLUDED_UPSTREAM_PLUGINS, handler_names, register_module_handlers

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_OWNED = (
    "bot.py",
    "log.py",
    "i18n.py",
    "alembic.ini",
    "docker-compose.yaml",
    "Dockerfile",
    "LICENSE",
    "README.md",
    "README.en.md",
    ".github",
    "alembic",
    "core",
    "db",
    "i18n",
    "plugins",
    "repo",
    "services",
    "utils",
)


def _upstream_ref() -> str | None:
    """The upstream commit this branch is based on; newer upstream commits are not yet ours to match."""
    probe = subprocess.run(["git", "merge-base", "HEAD", "upstream/main"], cwd=ROOT, capture_output=True, text=True)
    return probe.stdout.strip() or None if probe.returncode == 0 else None


def test_upstream_owned_paths_are_pristine() -> None:
    ref = _upstream_ref()
    if ref is None:
        pytest.skip("no upstream/main ref in this clone")
    diff = subprocess.run(
        ["git", "diff", "--stat", ref, "--", *UPSTREAM_OWNED],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert diff.stdout == "", f"upstream-owned paths differ from {ref}:\n{diff.stdout}"


def test_fork_packages_do_not_leak_into_upstream_directories() -> None:
    upstream_dirs = [ROOT / name for name in ("plugins", "services", "utils", "core", "db", "repo")]
    leaked = [
        path
        for directory in upstream_dirs
        for path in directory.rglob("*.py")
        if "from ext" in path.read_text() or "import ext" in path.read_text()
    ]
    assert leaked == []


def test_hooks_install_is_idempotent_and_targets_exist() -> None:
    hooks.install(bot=True)
    hooks.install(bot=True)
    assert hooks.installed() == {"log", "douyin", "media", "bot"}
    import plugins.parse.handlers
    import services.media
    import services.pipeline
    from ext.bot.handlers import handle_parse
    from ext.media.processing import process_media_files

    assert services.pipeline.process_media_files is process_media_files
    assert services.media.process_media_files is process_media_files
    assert plugins.parse.handlers.handle_parse is handle_parse


def test_log_patcher_masks_proxy_credentials_everywhere() -> None:
    assert (
        hooks.mask_credentials("proxy=socks5://user:p%40ss@10.0.0.1:1080 ok") == "proxy=socks5://***@10.0.0.1:1080 ok"
    )
    assert (
        hooks.mask_credentials('["http://u:p@h:1", "https://x.com/a?b=c"]')
        == '["http://***@h:1", "https://x.com/a?b=c"]'
    )
    assert hooks.mask_credentials("no credentials here http://example.com") == "no credentials here http://example.com"


def test_fork_inline_module_replaces_every_excluded_upstream_handler() -> None:
    replaced: set[str] = set()
    for name in EXCLUDED_UPSTREAM_PLUGINS:
        replaced.update(handler_names(importlib.import_module(f"plugins.{name}")))
    assert replaced == set(handler_names(inline))
    added: list[tuple[object, int]] = []
    client = SimpleNamespace(add_handler=lambda handler, group: added.append((handler, group)))
    assert register_module_handlers(client, inline) == len(replaced) == len(added)


def test_douyin_hook_only_touches_douyin_results() -> None:
    import asyncio
    from unittest.mock import AsyncMock, patch

    from services.parser import ParseService

    hooks.install()
    service = ParseService()
    parsed = SimpleNamespace(raw_url="https://example.com/p")
    with (
        patch.object(service.parser, "parse", AsyncMock(return_value=parsed)),
        patch.object(service.parser, "get_platform", return_value=SimpleNamespace(id="xhs")),
        patch("ext.douyin_quality.upgrade_douyin_video", AsyncMock()) as upgrade,
    ):
        assert asyncio.run(service.parse("https://example.com/p")) is parsed
        upgrade.assert_not_awaited()
