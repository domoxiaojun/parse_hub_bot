"""ParseHub 2.4 policies applied outside the upstream Bot sources."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

from parsehub.errors import ContentUnavailableError
from parsehub.types import AnyParseResult

from core import pl_cfg
from services.parser import ParseService

DEFAULT_DOWNLOAD_CONCURRENCY = 2
_download_limit: ContextVar[int | None] = ContextVar("worker_download_concurrency", default=None)


async def parse(self: ParseService, url: str) -> AnyParseResult:
    """Keep cookie/proxy rotation and transient retries; unavailable content is final."""
    platform = self.get_platform(url)
    for attempt in range(3):
        try:
            cookie = pl_cfg.roll_cookie(platform.id)
            return await self.parser.parse(
                url,
                cookie=cookie.get_secret_value() if cookie else None,
                proxy=pl_cfg.roll_parser_proxy(platform.id),
            )
        except ContentUnavailableError:
            raise
        except Exception:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


async def get_raw_url(self: ParseService, url: str, clean_all: bool = True) -> str:
    platform = self.get_platform(url)
    for attempt in range(3):
        try:
            return str(await self.parser.get_raw_url(
                url, proxy=pl_cfg.roll_parser_proxy(platform.id), clean_all=clean_all,
            ))
        except ContentUnavailableError:
            raise
        except Exception:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


@contextmanager
def download_limit(concurrency: int) -> Iterator[None]:
    token = _download_limit.set(concurrency)
    try:
        yield
    finally:
        _download_limit.reset(token)


def bounded_download(original: Any) -> Any:
    @wraps(original)
    async def download(self: Any, *args: Any, **kwargs: Any) -> Any:
        if (limit := _download_limit.get()) is not None:
            kwargs["concurrency"] = min(max(1, kwargs.get("concurrency", limit)), limit)
        return await original(self, *args, **kwargs)
    return download
