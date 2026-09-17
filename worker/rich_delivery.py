"""Deterministic Rich cards. Source text is always literal, never executable markup."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from pyrogram import types

MAX_MEDIA = 50
MAX_RICH_TEXT_BYTES = 32768
MAX_RICH_BLOCKS = 500
PLATFORM_NAMES = {
    "xhs": "小红书", "weixin": "微信公众号", "twitter": "X", "douyin": "抖音",
    "bilibili": "哔哩哔哩", "youtube": "YouTube", "instagram": "Instagram",
    "weibo": "微博", "zhihu": "知乎", "threads": "Threads", "tiktok": "TikTok",
}


@dataclass
class RichFrame:
    payload: types.InputRichMessage
    result_indices: list[int]
    text: str
    media_count: int = 0
    completed_result_indices: list[int] = field(default_factory=list)


def literal(value: str | list[Any]) -> types.RichText:
    # Kurigram documents/implements str and lists as RichText; its annotations omit the union.
    return cast(types.RichText, value)


def bounded(text: str, limit: int) -> str:
    """Limit UTF-8 bytes without cutting a code point, including the ellipsis."""
    if len(text.encode()) <= limit:
        return text
    return text.encode()[:max(0, limit - 3)].decode("utf-8", errors="ignore").rstrip() + "…"


def rich_text(value: Any) -> str:
    """Read literal text including nested bold/link labels, never markup or URLs."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(rich_text(item) for item in value)
    return rich_text(getattr(value, "text", "")) if value is not None else ""


def rich_text_size(value: Any) -> int:
    return len(rich_text(value).encode("utf-8"))


def blocks_size(blocks: list[Any]) -> int:
    return sum(rich_text_size(getattr(block, "text", ""))
               + rich_text_size(getattr(block, "caption", None)) for block in blocks)


def bold(value: str) -> types.RichText:
    return cast(types.RichText, types.RichTextBold(literal(value)))


def split_text(text: str, limit: int) -> list[str]:
    """Split literal text on Unicode code point boundaries, preferring newlines."""
    if limit < 4:
        raise ValueError("text_budget_too_small")
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining.encode("utf-8")) <= limit:
            chunks.append(remaining)
            break
        encoded = remaining.encode("utf-8")
        end = encoded[:limit].decode("utf-8", errors="ignore")
        boundary = max(end.rfind("\n"), end.rfind(" "))
        if boundary > max(1, len(end) // 2):
            end = end[:boundary + 1]
        if not end:
            end = encoded[:limit].decode("utf-8", errors="ignore")
        chunks.append(end)
        remaining = remaining[len(end):]
    return chunks


def source_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def integer(value: Any, *, duration: bool = False) -> int:
    if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return 0
    return min(2147483647, math.ceil(value) if duration else round(value))


def media_block(media: dict[str, Any], path: Path) -> Any:
    kind = media["type"]
    filename = Path(str(media.get("filename") or path.name)).name
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)[:180] or "media"
    dimensions = {"width": integer(media.get("width")), "height": integer(media.get("height")),
                  "duration": integer(media.get("durationSeconds"), duration=True)}
    caption = (types.RichBlockCaption(text=literal("实况片段"))
               if media.get("pairedMediaId") and kind == "video" else None)
    if kind == "photo":
        return types.InputRichBlockPhoto(types.InputMediaPhoto(path))
    if kind == "video":
        return types.InputRichBlockVideo(types.InputMediaVideo(
            path, file_name=filename, supports_streaming=True, width=dimensions["width"],
            height=dimensions["height"], duration=dimensions["duration"],
        ), caption=caption)
    if kind == "animation":
        return types.InputRichBlockAnimation(types.InputMediaAnimation(
            path, file_name=filename, width=dimensions["width"], height=dimensions["height"],
            duration=dimensions["duration"],
        ))
    if kind == "audio":
        return types.InputRichBlockAudio(types.InputMediaAudio(
            path, file_name=filename, duration=dimensions["duration"],
        ))
    if kind == "document":
        return types.InputRichBlockDocument(types.InputMediaDocument(path, file_name=filename))
    raise ValueError("unsupported_media_type")


def build_frames(
    results: list[dict[str, Any]], resolve_media: Callable[[str, str], Path], *, inline: bool = False,
) -> list[RichFrame]:
    # Inline editing has one target: never replace an earlier batch with a later batch.
    if inline and sum(len(item.get("media", [])) for item in results
                      if "error" not in item and item.get("access") == "public") > MAX_MEDIA:
        raise ValueError("inline_media_limit")
    frames: list[RichFrame] = []
    for index, item in enumerate(results):
        if "error" in item or item.get("access") != "public":
            text = f"第 {index + 1} 个链接暂时无法解析，请稍后重试。"
            frames.append(RichFrame(types.InputRichMessage(blocks=[
                types.InputRichBlockParagraph(bold("暂时无法解析")),
                types.InputRichBlockParagraph(literal(text)),
            ], skip_entity_detection=True), [index], text, completed_result_indices=[index]))
            continue
        platform = PLATFORM_NAMES.get(item.get("platform", ""), item.get("platform") or "解析结果")
        title = str(item.get("title") or platform)
        author = item.get("author") or {}
        meta = " · ".join(str(v) for v in (
            platform, author.get("name") if isinstance(author, dict) else author, item.get("publishedAt"),
        ) if v)
        content = str(item.get("content") or "")
        links: list[Any] = []
        url = source_url(item.get("canonicalUrl"))
        if url:
            links.append(types.RichTextUrl(text=literal("查看原文"), url=url))
        reading = source_url(item.get("telegraphUrl"))
        if reading and urlsplit(reading).hostname == "telegra.ph":
            links.extend(["  ·  ", types.RichTextUrl(text=literal("阅读版"), url=reading)])
        failures = integer(item.get("mediaFailureCount"))
        if failures:
            links.extend(["  ·  " if links else "", f"{failures} 项媒体未能处理"])
        tail = [types.InputRichBlockFooter(literal(links))] if links else []
        # Reserve room for repeated source links and a small continuation marker.
        budget = MAX_RICH_TEXT_BYTES - blocks_size(tail) - 128
        units: list[tuple[list[Any], int]] = []
        for text, kind in ((title, "title"), (meta, "meta"), (content, "body")):
            if not text:
                continue
            for chunk in split_text(text, budget):
                block = (types.InputRichBlockParagraph(bold(chunk)) if kind == "title"
                         else types.InputRichBlockFooter(literal(chunk)) if kind == "meta"
                         else types.InputRichBlockParagraph(literal(chunk)))
                units.append(([block], 0))
        media = item.get("media", [])
        cursor = 0
        while cursor < len(media):
            end = cursor + 1
            pair = media[cursor].get("pairedMediaId")
            while pair is not None and end < len(media) and media[end].get("pairedMediaId") == pair:
                end += 1
            group = media[cursor:end]
            units.append(([media_block(m, resolve_media(item["leaseId"], m["mediaId"])) for m in group], len(group)))
            cursor = end
        if inline:
            blocks = [block for group, _ in units for block in group] + tail
            frames.append(RichFrame(
                types.InputRichMessage(blocks=blocks, skip_entity_detection=True), [index],
                "\n".join(filter(None, [title, meta, content, url])), len(media), [index],
            ))
            continue
        parts: list[tuple[list[Any], int]] = []
        pending: list[Any] = []
        count = 0
        for group_blocks, group_media in units:
            if (blocks_size(group_blocks) > budget or group_media > MAX_MEDIA
                    or len(group_blocks) + len(tail) + 1 > MAX_RICH_BLOCKS):
                raise ValueError("rich_atomic_limit")
            if pending and (blocks_size(pending + group_blocks) > budget or count + group_media > MAX_MEDIA
                            or len(pending + group_blocks) + len(tail) + 1 > MAX_RICH_BLOCKS):
                parts.append((pending, count))
                pending, count = [], 0
            pending.extend(group_blocks)
            count += group_media
        if pending:
            parts.append((pending, count))
        for part_index, (blocks, count) in enumerate(parts):
            if len(parts) > 1:
                blocks = [types.InputRichBlockFooter(literal(
                    f"第 {index + 1} 个结果 · {part_index + 1}/{len(parts)}")), *blocks]
            blocks = [*blocks, *tail]
            frames.append(RichFrame(
                types.InputRichMessage(blocks=blocks, skip_entity_detection=True), [index],
                "\n".join(filter(None, [*(rich_text(getattr(b, "text", "")) for b in blocks), url])), count,
                [index] if part_index == len(parts) - 1 else [],
            ))
    if inline and frames:
        blocks = [block for frame in frames for block in frame.payload.blocks or []]
        if len(blocks) > MAX_RICH_BLOCKS:
            raise ValueError("inline_rich_block_limit")
        if blocks_size(blocks) > MAX_RICH_TEXT_BYTES:
            raise ValueError("inline_rich_text_limit")
        frames = [RichFrame(types.InputRichMessage(blocks=blocks, skip_entity_detection=True),
                            list(range(len(results))), "\n\n".join(frame.text for frame in frames),
                            sum(frame.media_count for frame in frames), list(range(len(results))))]
    return frames


def evidence_for(results: list[dict[str, Any]], indices: list[int], media_count: int) -> dict[str, Any]:
    items = [results[i] for i in dict.fromkeys(indices) if "error" not in results[i]
             and results[i].get("access") == "public"]
    full = "\n\n".join(str(item.get("content") or "") for item in items)
    return {"platforms": [item["platform"] for item in items],
            "sources": [item["canonicalUrl"] for item in items],
            "titles": [str(item.get("title") or "") for item in items],
            "content": bounded(full, 8000), "truncated": len(full.encode()) > 8000,
            "mediaCount": media_count,
            "trust": "untrusted_external_data"}
