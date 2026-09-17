"""Worker input and stable error boundaries."""

import ipaddress
import re
from urllib.parse import urlsplit

# Decimal, octal and hex forms ("127.1", "0x7f000001", "2130706433") resolve to
# addresses that ipaddress.ip_address() cannot parse; never treat them as hostnames.
_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*$")


class EngineError(Exception):
    """Only stable error codes cross the authenticated worker API."""

    def __init__(self, code: str, stage: str = 'parse'):
        self.code = code
        self.stage = stage
        super().__init__(code)

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
        if _NUMERIC_HOST.match(host):
            raise EngineError('unsupported_url', 'identify') from None
    return host
