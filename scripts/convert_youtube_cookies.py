#!/usr/bin/env python3
"""Convert a yt-dlp Netscape cookie export to a ParseHub cookie value.

ParseHub accepts a cookie header string or a JSON object string.  This script
keeps only cookies whose domain is ``youtube.com`` or one of its subdomains,
then writes a JSON object by default.  The output is deliberately written to
an explicit file with mode 0600 and is never printed to stdout.

Run with the repository environment, for example::

    uv run python scripts/convert_youtube_cookies.py \
        .secrets/youtube-cookies.txt \
        --output .secrets/youtube-cookie.json

The resulting file contains one JSON object.  Paste its contents as one value
under ``platforms.youtube.cookies`` in the Worker configuration, then restart
the Worker.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

YOUTUBE_DOMAIN = "youtube.com"


def _is_comment(line: str) -> bool:
    """Return whether a Netscape cookie line is a comment."""

    return line.startswith("#") and not line.startswith("#HttpOnly_")


def _youtube_host(domain: str) -> str:
    """Normalize a Netscape domain, including its HttpOnly marker."""

    return domain.removeprefix("#HttpOnly_").lstrip(".").lower()


def convert_netscape(text: str) -> dict[str, str]:
    """Return YouTube cookies from Netscape-formatted *text*.

    The Worker passes the resulting mapping to ParseHub, which materializes
    it as a Netscape cookie file for yt-dlp with a ``.youtube.com`` domain.
    Cookie values are kept as strings, including empty values.

    ``ValueError`` is raised for a non-comment, non-empty line that is not a
    valid seven-column Netscape row.  This avoids silently producing a partial
    credential when an export is malformed.
    """

    cookies: dict[str, str] = {}

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip("\r")
        if not line or _is_comment(line):
            continue

        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(f"第 {line_number} 行不是有效的 Netscape Cookie 记录")

        domain, _include_subdomains, _path, _secure, _expires, name, value = fields
        host = _youtube_host(domain)
        if host != YOUTUBE_DOMAIN and not host.endswith(f".{YOUTUBE_DOMAIN}"):
            continue
        if not name:
            raise ValueError(f"第 {line_number} 行的 Cookie 名称为空")

        # A cookie header cannot represent duplicate names from different
        # domains.  ParseHub also normalizes to a mapping, so keep the last
        # occurrence, matching the usual browser-export order.
        cookies[name] = value

    if not cookies:
        raise ValueError("没有找到 youtube.com 或其子域名的 Cookie")

    return cookies


def serialize_cookies(cookies: dict[str, str], output_format: str) -> str:
    """Serialize cookies as ParseHub-compatible JSON or header text."""

    if output_format == "json":
        return json.dumps(cookies, ensure_ascii=False, separators=(",", ":")) + "\n"
    if output_format == "header":
        return "; ".join(f"{name}={value}" for name, value in cookies.items()) + "\n"
    raise ValueError(f"不支持的输出格式: {output_format}")


def write_private(path: Path, content: str) -> None:
    """Atomically write *content* to *path* with private permissions."""

    path = path.expanduser()
    if not path.parent.exists():
        raise FileNotFoundError(f"输出目录不存在: {path.parent}；请先创建它")

    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o600)
            temporary_path = temporary.name
        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, 0o600)
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将 yt-dlp Netscape Cookie 导出转换为 ParseHub Cookie 值")
    parser.add_argument("input", type=Path, help="yt-dlp 导出的 Netscape Cookie 文件")
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        type=Path,
        help="输出文件；内容不会打印到终端，文件权限设为 0600",
    )
    parser.add_argument(
        "--format",
        choices=("json", "header"),
        default="json",
        help="输出格式，默认 json；header 输出 name=value; name=value",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        text = args.input.expanduser().read_text(encoding="utf-8")
        cookies = convert_netscape(text)
        write_private(args.output, serialize_cookies(cookies, args.format))
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))

    print(f"已转换 {len(cookies)} 个 YouTube Cookie，输出文件：{args.output.expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
