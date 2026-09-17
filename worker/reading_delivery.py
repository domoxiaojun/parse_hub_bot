"""Complete public reading pages for single-message inline/Guest delivery."""

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from utils.ph import Telegraph
from worker.rich_delivery import RichFrame, build_frames, source_url, split_text

PAGE_CONTENT_BYTES = 48_000  # Telegraph permits 64 KiB; leave room for navigation.


def page_parts(item: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Use text nodes so external HTML/Markdown cannot create executable markup."""
    nodes: list[dict[str, Any]] = []
    meta = str(item.get("platform") or "")
    for value in (str(item.get("title") or ""), meta,
                  str(item.get("plainContent") or item.get("content") or "")):
        for chunk in split_text(value, 2048) if value else []:
            children: list[Any] = []
            for index, line in enumerate(chunk.split("\n")):
                if index:
                    children.append({"tag": "br"})
                if line:
                    children.append(line)
            nodes.append({"tag": "p", "children": children})
    parts: list[list[dict[str, Any]]] = []
    pending: list[dict[str, Any]] = []
    size = 2
    for node in nodes:
        node_size = len(json.dumps(node, ensure_ascii=False, separators=(",", ":")).encode()) + 1
        if pending and size + node_size > PAGE_CONTENT_BYTES:
            parts.append(pending)
            pending, size = [], 2
        pending.append(node)
        size += node_size
    if pending or not parts:
        parts.append(pending)
    return parts


async def reading_url(
    publisher: Telegraph, item: dict[str, Any], receipt: dict[str, Any], index: int, checkpoint: Callable[[], None],
) -> str:
    parts = page_parts(item)
    url = source_url(item.get("canonicalUrl"))
    digest = hashlib.sha256(json.dumps([parts, url], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cached = receipt.setdefault("readingPages", {}).get(str(index))
    if not isinstance(cached, dict) or cached.get("digest") != digest or len(cached.get("urls", [])) != len(parts):
        cached = {"digest": digest, "urls": [None] * len(parts)}
        receipt["readingPages"][str(index)] = cached
    # Publish backwards: every returned entry point already links to its complete continuation.
    for part_index in reversed(range(len(parts))):
        existing = source_url(cached["urls"][part_index])
        if existing and urlsplit(existing).hostname == "telegra.ph":
            continue
        nodes = list(parts[part_index])
        if part_index + 1 < len(parts):
            nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {
                "href": cached["urls"][part_index + 1]}, "children": ["继续阅读"]}]})
        if url:
            nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": url},
                                                     "children": ["查看原文"]}]})
        page = await publisher.create_page(
            f"解析结果 {index + 1} · {part_index + 1}/{len(parts)}", content=nodes,
        )
        valid = source_url(page.url)
        if not valid or urlsplit(valid).scheme != "https" or urlsplit(valid).hostname != "telegra.ph":
            raise ValueError("invalid_reading_url")
        cached["urls"][part_index] = valid
        checkpoint()
    return str(cached["urls"][0])


async def prepare_reading_frame(
    results: list[dict[str, Any]], receipt: dict[str, Any], checkpoint: Callable[[], None],
    resolve: Callable[[str, str], Path],
) -> RichFrame:
    public = [item for item in results if "error" not in item and item.get("access") == "public"]
    if any(item.get("externalPublicationAllowed") is False for item in public):
        raise ValueError("external_publication_not_allowed")
    media_count = sum(len(item.get("media", [])) for item in public)
    if any(item.get("media") and item.get("contentType") != "article" for item in public):
        raise ValueError("inline_media_overflow")
    publisher = Telegraph()
    compact = []
    try:
        for index, item in enumerate(results):
            if "error" in item or item.get("access") != "public":
                compact.append(item)
                continue
            async with asyncio.timeout(120):
                url = await reading_url(publisher, item, receipt, index, checkpoint)
            compact.append({**item, "title": f"第 {index + 1} 个解析结果", "author": None, "publishedAt": None,
                            "platform": "解析结果", "content": "完整标题和正文请打开阅读版。"
                            + ("文章媒体请在原文查看。" if item.get("media") else ""),
                            "plainContent": "完整标题和正文请打开阅读版。"
                            + ("文章媒体请在原文查看。" if item.get("media") else ""),
                            "markdownContent": None, "contentFormat": "plain",
                            "telegraphUrl": url, "media": []})
    finally:
        await publisher.close()
    frame = build_frames(compact, resolve, inline=True)[0]
    if not isinstance(frame, RichFrame):
        raise ValueError("invalid_reading_frame")
    receipt["readingMediaCount"] = media_count
    receipt["mode"] = "reading"
    return frame
