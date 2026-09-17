"""Deterministic Rich cards. Source text is always literal, never executable markup."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from pyrogram import types

MAX_MEDIA = 50
MAX_RICH_TEXT_BYTES = 32768
MAX_RICH_BLOCKS = 500
COLLAGE_MAX_ITEMS = 4
DETAILS_THRESHOLD_BYTES = 2000
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


@dataclass
class LivePhotoFrame:
    photo: Path | str
    video: Path | str
    width: int
    height: int
    result_indices: list[int]
    text: str = ""
    media_count: int = 1
    completed_result_indices: list[int] = field(default_factory=list)


DeliveryFrame = RichFrame | LivePhotoFrame


@dataclass
class _LivePhotoUnit:
    photo: Path | str
    video: Path | str
    width: int
    height: int


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
    return sum(
        rich_text_size(getattr(block, "text", ""))
        + rich_text_size(getattr(block, "summary", ""))
        + rich_text_size(getattr(block, "caption", None))
        + blocks_size(getattr(block, "blocks", []) or [])
        for block in blocks
    )


def rich_blocks_text(blocks: list[Any]) -> str:
    return "".join(
        rich_text(getattr(block, "text", ""))
        + rich_text(getattr(block, "summary", ""))
        + rich_text(getattr(block, "caption", None))
        + rich_blocks_text(getattr(block, "blocks", []) or [])
        for block in blocks
    )


def block_count(blocks: list[Any]) -> int:
    """Count nested Rich blocks as part of Telegram's document limit."""
    return sum(1 + block_count(getattr(block, "blocks", []) or []) for block in blocks)


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


def source_markdown_frame(item: dict[str, Any], index: int, platform: str, url: str | None) -> RichFrame | None:
    """Preserve ParseHub's Markdown article when it fits one Rich Message."""
    if item.get("contentFormat") != "markdown" or item.get("media"):
        return None
    content = str(item.get("markdownContent") or item.get("content") or "")
    title = str(item.get("title") or "")
    media_count = len(re.findall(r"!\[[^\]]*\]\(https?://", content, flags=re.IGNORECASE))
    media_count += len(re.findall(r"<\s*(?:img|video|audio)\b[^>]*\bsrc\s*=\s*['\"]https?://",
                                  content, flags=re.IGNORECASE))
    if media_count > MAX_MEDIA:
        return None
    footer = escape(platform)
    if url:
        footer += f'  ·  <a href="{escape(url, quote=True)}">查看原文</a>'
    safe_title = re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", escape(title))
    document = "\n\n".join(filter(None, [f"# {safe_title}" if title else "", content,
                                             f"<footer>{footer}</footer>"]))
    if not document or len(document.encode("utf-8")) > MAX_RICH_TEXT_BYTES:
        return None
    plain = str(item.get("plainContent") or item.get("content") or "")
    return RichFrame(
        types.InputRichMessage(markdown=document, skip_entity_detection=True), [index],
        "\n".join(filter(None, [title, plain, url])), media_count, [index],
    )


def integer(value: Any, *, duration: bool = False) -> int:
    if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return 0
    return min(2147483647, math.ceil(value) if duration else round(value))


def media_block(media: dict[str, Any], source: Path | str) -> Any:
    kind = media["type"]
    source_name = source.name if isinstance(source, Path) else "media"
    filename = Path(str(media.get("filename") or source_name)).name
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)[:180] or "media"
    dimensions = {"width": integer(media.get("width")), "height": integer(media.get("height")),
                  "duration": integer(media.get("durationSeconds"), duration=True)}
    caption = (types.RichBlockCaption(text=literal("实况片段"))
               if media.get("pairedMediaId") and kind == "video" else None)
    if kind == "photo":
        return types.InputRichBlockPhoto(types.InputMediaPhoto(source))
    if kind == "video":
        return types.InputRichBlockVideo(types.InputMediaVideo(
            source, file_name=filename, supports_streaming=True, width=dimensions["width"],
            height=dimensions["height"], duration=dimensions["duration"],
            video_cover=media.get("telegramCoverFileId"),
        ), caption=caption)
    if kind == "animation":
        return types.InputRichBlockAnimation(types.InputMediaAnimation(
            source, file_name=filename, width=dimensions["width"], height=dimensions["height"],
            duration=dimensions["duration"],
        ))
    if kind == "audio":
        return types.InputRichBlockAudio(types.InputMediaAudio(
            source, file_name=filename, duration=dimensions["duration"],
        ))
    if kind == "voice":
        return types.InputRichBlockVoiceNote(types.InputMediaVoiceNote(
            source, duration=dimensions["duration"],
        ))
    if kind == "document":
        return types.InputRichBlockDocument(types.InputMediaDocument(source, file_name=filename))
    raise ValueError("unsupported_media_type")


def logical_media_count(media: list[dict[str, Any]]) -> int:
    return len(media)


def inline_attachment_count(media: list[dict[str, Any]]) -> int:
    return sum(2 if item.get("type") == "live_photo" else 1 for item in media)


def media_units(
    item: dict[str, Any], resolve_media: Callable[[str, str], Path], *, inline: bool,
) -> list[tuple[list[Any], int] | _LivePhotoUnit]:
    units: list[tuple[list[Any], int] | _LivePhotoUnit] = []

    def source(media: dict[str, Any], media_id: str, telegram_key: str = "telegramFileId") -> Path | str:
        file_id = media.get(telegram_key)
        if isinstance(file_id, str) and file_id:
            return file_id
        return resolve_media(item["leaseId"], media_id)

    for media in item.get("media", []):
        if media.get("type") == "live_photo":
            photo = source(media, media["mediaId"])
            video = source(media, media["videoMediaId"], "telegramVideoFileId")
            if inline:
                video_name = video.name if isinstance(video, Path) else "live-photo.mp4"
                units.append(([
                    types.InputRichBlockPhoto(types.InputMediaPhoto(photo)),
                    types.InputRichBlockVideo(
                        types.InputMediaVideo(
                            video,
                            file_name=Path(str(media.get("videoFilename") or video_name)).name,
                            supports_streaming=True,
                            width=integer(media.get("width")),
                            height=integer(media.get("height")),
                            duration=integer(media.get("durationSeconds"), duration=True),
                        ),
                        caption=types.RichBlockCaption(text=literal("实况视频")),
                    ),
                ], 1))
            else:
                units.append(_LivePhotoUnit(
                    photo=photo,
                    video=video,
                    width=integer(media.get("width")),
                    height=integer(media.get("height")),
                ))
            continue
        file_id = media.get("mediaId")
        if not isinstance(file_id, str):
            file_id = ""
        units.append(([media_block(media, source(media, file_id))], 1))
    return units


def collate_photos(blocks: list[Any]) -> list[Any]:
    """Use Bot API Rich Collage for consecutive photos when the block budget permits."""
    remaining_wrappers = max(0, MAX_RICH_BLOCKS - block_count(blocks))
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
        run = blocks[cursor:end]
        if len(run) > 1 and remaining_wrappers:
            container = types.InputRichBlockCollage if len(run) <= COLLAGE_MAX_ITEMS else types.InputRichBlockSlideshow
            output.append(container(run))
            remaining_wrappers -= 1
        else:
            output.extend(run)
        cursor = end
    return output


def append_rich_frame(
    frames: list[DeliveryFrame], blocks: list[Any], media_count: int, result_index: int, url: str | None,
) -> None:
    collated = collate_photos(blocks)
    frames.append(RichFrame(
        types.InputRichMessage(blocks=collated, skip_entity_detection=True), [result_index],
        "\n".join(filter(None, [rich_blocks_text(collated), url])), media_count,
    ))


def build_frames(
    results: list[dict[str, Any]], resolve_media: Callable[[str, str], Path], *, inline: bool = False,
) -> list[DeliveryFrame]:
    # Inline editing has one target: never replace an earlier batch with a later batch.
    if inline and sum(inline_attachment_count(item.get("media", [])) for item in results
                      if "error" not in item and item.get("access") == "public") > MAX_MEDIA:
        raise ValueError("inline_media_limit")
    frames: list[DeliveryFrame] = []
    for index, item in enumerate(results):
        if "error" in item or item.get("access") != "public":
            text = f"第 {index + 1} 个链接暂时无法解析，请稍后重试。"
            frames.append(RichFrame(types.InputRichMessage(blocks=[
                types.InputRichBlockParagraph(bold("暂时无法解析")),
                types.InputRichBlockParagraph(literal(text)),
            ], skip_entity_detection=True), [index], text, completed_result_indices=[index]))
            continue
        platform = PLATFORM_NAMES.get(item.get("platform", ""), item.get("platform") or "解析结果")
        title = str(item.get("title") or "")
        url = source_url(item.get("canonicalUrl"))
        if len(results) == 1 and (markdown_frame := source_markdown_frame(item, index, platform, url)):
            frames.append(markdown_frame)
            continue
        content = str(item.get("plainContent") or item.get("content") or "")
        # Platform badge lives in the footer beside the source link, never duplicated in title/meta.
        links: list[Any] = [platform]
        if url:
            links.extend(["  ·  ", types.RichTextUrl(text=literal("查看原文"), url=url)])
        reading = source_url(item.get("telegraphUrl"))
        if reading and urlsplit(reading).hostname == "telegra.ph":
            links.extend(["  ·  ", types.RichTextUrl(text=literal("阅读版"), url=reading)])
        failures = integer(item.get("mediaFailureCount"))
        if failures:
            links.extend(["  ·  " if links else "", f"{failures} 项媒体未能处理"])
        tail = [types.InputRichBlockFooter(literal(links))] if links else []
        # Reserve room for repeated source links and a small continuation marker.
        budget = MAX_RICH_TEXT_BYTES - blocks_size(tail) - 128
        units: list[tuple[list[Any], int] | _LivePhotoUnit] = []
        for text, kind in ((title, "title"), (content, "body")):
            if not text:
                continue
            details = kind == "body" and len(content.encode("utf-8")) > DETAILS_THRESHOLD_BYTES
            chunk_limit = budget - 32 if details else budget
            for chunk_index, chunk in enumerate(split_text(text, chunk_limit)):
                block: Any
                if kind == "title":
                    block = types.InputRichBlockSectionHeading(literal(chunk), size=3)
                elif details:
                    summary = "正文" if chunk_index == 0 else "正文（续）"
                    block = types.InputRichBlockDetails(
                        literal(summary), [types.InputRichBlockParagraph(literal(chunk))], is_open=False,
                    )
                else:
                    block = types.InputRichBlockParagraph(literal(chunk))
                units.append(([block], 0))
        units.extend(media_units(item, resolve_media, inline=inline))
        # The source footer is a real unit so a valid text-only result can never
        # collapse to zero frames.
        units.append((tail, 0))
        if inline:
            blocks = collate_photos([
                block for unit in units if isinstance(unit, tuple) for block in unit[0]
            ])
            frames.append(RichFrame(
                types.InputRichMessage(blocks=blocks, skip_entity_detection=True), [index],
                "\n".join(filter(None, [title, content, url])),
                logical_media_count(item.get("media", [])), [index],
            ))
            continue
        item_frames: list[DeliveryFrame] = []
        pending: list[Any] = []
        count = 0
        for unit in units:
            if isinstance(unit, _LivePhotoUnit):
                if pending:
                    append_rich_frame(item_frames, pending, count, index, url)
                    pending, count = [], 0
                item_frames.append(LivePhotoFrame(
                    photo=unit.photo, video=unit.video, width=unit.width, height=unit.height,
                    result_indices=[index],
                ))
                continue
            group_blocks, group_media = unit
            if (blocks_size(group_blocks) > budget or group_media > MAX_MEDIA
                    or block_count(group_blocks) + 2 > MAX_RICH_BLOCKS):
                raise ValueError("rich_atomic_limit")
            if pending and (blocks_size(pending + group_blocks) > budget or count + group_media > MAX_MEDIA
                            or block_count(pending + group_blocks) + 2 > MAX_RICH_BLOCKS):
                append_rich_frame(item_frames, pending, count, index, url)
                pending, count = [], 0
            pending.extend(group_blocks)
            count += group_media
        if pending:
            append_rich_frame(item_frames, pending, count, index, url)
        if not item_frames:
            raise ValueError("empty_delivery_plan")
        item_frames[-1].completed_result_indices = [index]
        if len(item_frames) > 1:
            total = len(item_frames)
            for part_index, frame in enumerate(item_frames):
                if isinstance(frame, RichFrame):
                    blocks = [types.InputRichBlockFooter(literal(
                        f"第 {index + 1} 个结果 · {part_index + 1}/{total}")), *(frame.payload.blocks or [])]
                    if block_count(blocks) <= MAX_RICH_BLOCKS:
                        frame.payload.blocks = blocks
        frames.extend(item_frames)
    if inline and frames:
        if len(frames) == 1 and isinstance(frames[0], RichFrame) and frames[0].payload.markdown is not None:
            return frames
        rich_frames = [frame for frame in frames if isinstance(frame, RichFrame)]
        blocks = [block for frame in rich_frames for block in frame.payload.blocks or []]
        if block_count(blocks) > MAX_RICH_BLOCKS:
            raise ValueError("inline_rich_block_limit")
        if blocks_size(blocks) > MAX_RICH_TEXT_BYTES:
            raise ValueError("inline_rich_text_limit")
        frames = [RichFrame(types.InputRichMessage(blocks=blocks, skip_entity_detection=True),
                            list(range(len(results))), "\n\n".join(frame.text for frame in rich_frames),
                            sum(frame.media_count for frame in rich_frames), list(range(len(results))))]
    return frames


def evidence_for(results: list[dict[str, Any]], indices: list[int], media_count: int) -> dict[str, Any]:
    items = [results[i] for i in dict.fromkeys(indices) if "error" not in results[i]
             and results[i].get("access") == "public"]
    full = "\n\n".join(str(item.get("plainContent") or item.get("content") or "") for item in items)
    return {"platforms": [item["platform"] for item in items],
            "sources": [item["canonicalUrl"] for item in items],
            "titles": [str(item.get("title") or "") for item in items],
            "content": bounded(full, 8000), "truncated": len(full.encode()) > 8000,
            "mediaCount": media_count,
            "trust": "untrusted_external_data"}
