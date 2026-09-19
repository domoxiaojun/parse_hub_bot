"""Deterministic Rich preview cards built from already-uploaded Telegram references."""

import math
import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from pyrogram import types

from delivery.models import DeliveryEnvelope, DeliveryError

MAX_RICH_TEXT_BYTES = 32_768
DETAILS_THRESHOLD_BYTES = 2_000
TEXT_BLOCK_BYTES = 8_000
PLATFORM_NAMES = {
    "xhs": "小红书",
    "weixin": "微信公众号",
    "twitter": "X",
    "douyin": "抖音",
    "bilibili": "哔哩哔哩",
    "youtube": "YouTube",
    "instagram": "Instagram",
    "weibo": "微博",
    "zhihu": "知乎",
    "threads": "Threads",
    "tiktok": "TikTok",
}


def literal(value: str | list[Any]) -> types.RichText:
    return cast(types.RichText, value)


def safe_url(value: str) -> str | None:
    if not value or len(value) > 2_048 or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def platform_label(platform: str) -> str:
    return PLATFORM_NAMES.get(platform, platform or "解析结果")


def truncate_utf8(value: str, limit: int) -> str:
    if len(value.encode("utf-8")) <= limit:
        return value
    if limit <= 3:
        return "…" if limit else ""
    return value.encode("utf-8")[: limit - 3].decode("utf-8", errors="ignore").rstrip() + "…"


def split_utf8(value: str, limit: int = TEXT_BLOCK_BYTES) -> list[str]:
    chunks: list[str] = []
    remaining = value
    while remaining:
        if len(remaining.encode("utf-8")) <= limit:
            chunks.append(remaining)
            break
        prefix = remaining.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
        boundary = max(prefix.rfind("\n"), prefix.rfind(" "))
        if boundary > len(prefix) // 2:
            prefix = prefix[: boundary + 1]
        if not prefix:
            raise DeliveryError("rich_text_limit")
        chunks.append(prefix)
        remaining = remaining[len(prefix):]
    return chunks


def _filename(value: Path | str, fallback: str) -> str:
    name = value.name if isinstance(value, Path) else fallback
    return re.sub(r"[\x00-\x1f\x7f]", "", Path(name).name)[:180] or fallback


def _integer(value: float | int, *, duration: bool = False) -> int:
    if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return 0
    return min(2_147_483_647, math.ceil(value) if duration else round(value))


def _media_block(uploaded: Any) -> Any:
    asset = uploaded.asset
    media = uploaded.refs["media"]
    cover = uploaded.refs.get("photo")
    width = _integer(asset.width)
    height = _integer(asset.height)
    duration = _integer(asset.duration, duration=True)
    if asset.type == "photo":
        return types.InputRichBlockPhoto(types.InputMediaPhoto(media))
    if asset.type in {"video", "live_photo"}:
        return types.InputRichBlockVideo(types.InputMediaVideo(
            media,
            file_name=_filename(asset.media, "video.mp4"),
            supports_streaming=True,
            width=width,
            height=height,
            duration=duration,
            video_cover=cover,
        ))
    if asset.type == "animation":
        return types.InputRichBlockAnimation(types.InputMediaAnimation(
            media,
            file_name=_filename(asset.media, "animation.gif"),
            width=width,
            height=height,
            duration=duration,
        ))
    if asset.type == "audio":
        return types.InputRichBlockAudio(types.InputMediaAudio(
            media,
            file_name=_filename(asset.media, "audio"),
            duration=duration,
        ))
    if asset.type == "voice":
        return types.InputRichBlockVoiceNote(types.InputMediaVoiceNote(media, duration=duration))
    if asset.type == "document":
        return types.InputRichBlockDocument(types.InputMediaDocument(
            media,
            file_name=_filename(asset.media, "document"),
        ))
    raise DeliveryError("unsupported_media_type")


def _collate_photos(blocks: list[Any]) -> list[Any]:
    output: list[Any] = []
    cursor = 0
    while cursor < len(blocks):
        if not isinstance(blocks[cursor], types.InputRichBlockPhoto):
            output.append(blocks[cursor])
            cursor += 1
            continue
        end = cursor + 1
        while end < len(blocks) and isinstance(blocks[end], types.InputRichBlockPhoto):
            end += 1
        photos = blocks[cursor:end]
        if len(photos) == 1:
            output.extend(photos)
        elif len(photos) <= 4:
            output.append(types.InputRichBlockCollage(photos))
        else:
            output.append(types.InputRichBlockSlideshow(photos))
        cursor = end
    return output


def _footer(envelope: DeliveryEnvelope) -> types.InputRichBlockFooter:
    links = envelope.footer_links or ((envelope.platform, envelope.source_url),)
    content: list[Any] = []
    for index, (platform, raw_url) in enumerate(links):
        if index:
            content.append("  ·  ")
        content.append(platform_label(platform))
        source = safe_url(raw_url)
        if source:
            content.extend(["  ·  ", types.RichTextUrl(literal("查看原文"), source)])
    reading = safe_url(envelope.reading_url)
    if reading:
        content.extend(["  ·  ", types.RichTextUrl(literal("阅读版"), reading)])
    return types.InputRichBlockFooter(literal(content))


def build_rich_message(envelope: DeliveryEnvelope, uploaded: list[Any]) -> types.InputRichMessage:
    """Build one Rich card without interpreting source text as HTML or Markdown."""
    footer = _footer(envelope)
    footer_budget = len(("".join(platform_label(platform) + url for platform, url in
                         (envelope.footer_links or ((envelope.platform, envelope.source_url),)))
                        + "查看原文阅读版").encode("utf-8")) + 128
    title = truncate_utf8(envelope.title, 8_000)
    body_budget = max(0, MAX_RICH_TEXT_BYTES - len(title.encode("utf-8")) - footer_budget)
    body = truncate_utf8(envelope.body, body_budget)
    blocks: list[Any] = []
    for chunk in split_utf8(title):
        blocks.append(types.InputRichBlockSectionHeading(literal(chunk), size=4))
    body_chunks = split_utf8(body)
    details = len(body.encode("utf-8")) > DETAILS_THRESHOLD_BYTES
    for index, chunk in enumerate(body_chunks):
        paragraph = types.InputRichBlockParagraph(literal(chunk))
        if details:
            summary = "正文" if index == 0 else "正文（续）"
            blocks.append(types.InputRichBlockDetails(literal(summary), [paragraph], is_open=False))
        else:
            blocks.append(paragraph)
    blocks.extend(_collate_photos([_media_block(value) for value in uploaded]))
    blocks.append(footer)
    if len(blocks) > 500:
        raise DeliveryError("rich_block_limit")
    return types.InputRichMessage(blocks=blocks, skip_entity_detection=True)
