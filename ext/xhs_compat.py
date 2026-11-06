"""Small XHS response compatibility fix; leave requests and media preparation upstream-owned."""

import math
from typing import Any


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if math.isfinite(number) and number > 0 else 0


def _rank(item: dict[str, Any]) -> tuple[float, float, float, float]:
    width, height = _number(item.get("width")), _number(item.get("height"))
    return (
        min(width, height), width * height, _number(item.get("fps")),
        max(_number(item.get("videoBitrate")), _number(item.get("bitrate"))),
    )


def select_stream(stream: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Choose highest resolution across codecs; use codec preference only for equal ranks."""
    if not isinstance(stream, dict):
        return None
    preferred = ("h264", "av1", "h265", "h266")
    keys = [*preferred, *(key for key in stream if key not in preferred)]
    valid: list[dict[str, Any]] = []
    for key in keys:
        candidates = stream.get(key)
        if not isinstance(candidates, list):
            continue
        valid.extend(
            item
            for item in candidates
            if isinstance(item, dict)
            and isinstance(item.get("masterUrl"), str)
            and item["masterUrl"].startswith(("https://", "http://"))
        )
    return [max(valid, key=_rank)] if valid else None
