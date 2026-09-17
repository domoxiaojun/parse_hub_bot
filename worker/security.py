"""Worker input and error boundaries; never patch ParseHub's network stack."""

import ipaddress
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit


class EngineError(Exception):
    """Only stable error codes cross the authenticated worker API."""

    def __init__(self, code: str, stage: str = 'parse'):
        self.code = code
        self.stage = stage
        super().__init__(code)


@dataclass
class RequestPolicy:
    """Compatibility data only; Worker no longer patches provider requests."""
    platform: str
    credentials: bool = False
    phase: str = 'parse'
    proxy: str | None = None
    public: bool = False
    restricted: bool = False


@contextmanager
def request_policy(policy: RequestPolicy) -> Iterator[RequestPolicy]:
    yield policy


async def validate_public_url(url: str) -> None:
    validate_url(url)


def inspect_visibility(value: object, policy: RequestPolicy, depth: int = 0) -> None:
    """Retained as a no-op compatibility hook; provider response is authoritative."""


def is_challenge(exc: Exception, policy: RequestPolicy) -> bool:
    return False


def validate_url(url: str) -> str:
    """Validate submitted URLs, without rewriting provider requests or redirects."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower().rstrip('.')
        port = parts.port
    except ValueError:
        raise EngineError('unsupported_url', 'identify') from None
    if parts.scheme not in ('http', 'https') or not host or parts.username or parts.password:
        raise EngineError('unsupported_url', 'identify')
    if port not in (None, 80, 443):
        raise EngineError('unsupported_url', 'identify')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise EngineError('unsupported_url', 'identify')
    try:
        if not ipaddress.ip_address(host).is_global:
            raise EngineError('unsupported_url', 'identify')
    except ValueError:
        pass
    return host
