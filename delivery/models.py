"""Platform-independent final assets and delivery contracts."""

import asyncio
import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

AssetKind = Literal["photo", "video", "live_photo", "animation", "audio", "voice", "document"]
SendKind = Literal["message", "single", "live_photo", "album", "rich"]
MEDIA_VERSION = 2


class DeliveryError(ValueError):
    """A stable, safe code; never include upstream error text."""


@dataclass(frozen=True)
class Destination:
    surface: str = "message"
    chat_id: int | None = None
    inline_message_id: str | None = None
    thread_id: int | None = None
    reply_to: int | None = None
    silent: bool = False
    protect: bool = False


@dataclass(frozen=True)
class MediaAsset:
    key: str
    type: AssetKind
    media: Path | str
    photo: Path | str | None = None
    width: int = 0
    height: int = 0
    duration: float = 0
    size: int = 0
    native_error: str | None = None
    references: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryEnvelope:
    dest: Destination
    source_id: str
    title: str = ""
    body: str = ""
    source_url: str = ""
    reading_url: str = ""
    media: tuple[MediaAsset, ...] = ()
    platform: str = ""
    rich_preview: bool = False
    footer_links: tuple[tuple[str, str], ...] = ()


@dataclass
class SendResult:
    kind: SendKind
    status: str = "pending"
    message_ids: list[int] = field(default_factory=list)
    albums: list[dict[str, Any]] = field(default_factory=list)
    assets_cached: list[dict[str, Any]] = field(default_factory=list)
    confirmed: bool = False


@dataclass(frozen=True)
class SendBatch:
    kind: SendKind
    assets: tuple[MediaAsset, ...]
    text: str
    envelope: DeliveryEnvelope


def utf16_size(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def truncate(text: str, limit: int) -> str:
    if utf16_size(text) <= limit:
        return text
    if limit <= 1:
        return "…" if limit else ""
    return text.encode("utf-16-le")[:(limit - 1) * 2].decode("utf-16-le", errors="ignore").rstrip() + "…"


def caption(envelope: DeliveryEnvelope, limit: int) -> str:
    link = envelope.reading_url or envelope.source_url
    footer = f"来源：{link}" if link else ""
    text = "\n\n".join(filter(None, [envelope.title, envelope.body, footer]))
    if utf16_size(text) <= limit:
        return text or "解析结果"
    # Keep a complete source link; never slice an entity or a URL.
    if utf16_size(footer) > limit:
        raise DeliveryError("source_link_too_long")
    budget = max(0, limit - utf16_size(footer) - (2 if footer else 0))
    summary = truncate(envelope.body or envelope.title, budget)
    return "\n\n".join(filter(None, [summary, footer]))


def plan(envelope: DeliveryEnvelope) -> list[SendBatch]:
    assets = envelope.media
    if len(assets) > 500:
        raise DeliveryError("media_count_limit")
    for asset in assets:
        if asset.type == "live_photo" and not asset.photo:
            raise DeliveryError("live_photo_pair_missing")
        for value in (asset.media, asset.photo):
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                raise DeliveryError("media_must_be_prepared")
            if isinstance(value, Path) and (not value.is_file() or value.stat().st_size == 0):
                raise DeliveryError("media_expired")
    if envelope.dest.surface != "message" or envelope.rich_preview:
        # Covers are attachments too; account conservatively for their separate photo references.
        if sum(2 if a.type == "live_photo" or a.photo else 1 for a in assets) > 50:
            raise DeliveryError("rich_media_limit")
        return [SendBatch("rich", assets, caption(envelope, 30000), envelope)]
    if not assets:
        return [SendBatch("message", (), caption(envelope, 4096), envelope)]
    for asset in assets:
        if asset.type == "live_photo":
            if asset.native_error:
                raise DeliveryError(asset.native_error)
            if not math.isfinite(asset.duration) or not 0 < asset.duration <= 10 or not 0 < asset.size <= 10_000_000:
                raise DeliveryError("live_photo_native_limit")
    text = caption(envelope, 1024)
    if len(assets) == 1:
        return [SendBatch("live_photo" if assets[0].type == "live_photo" else "single", assets, text, envelope)]
    kinds = {a.type for a in assets}
    if not (kinds <= {"photo", "video", "live_photo"} or kinds <= {"audio"} or kinds <= {"document"}):
        if "live_photo" in kinds:
            raise DeliveryError("incompatible_live_album")
        if sum(2 if a.photo else 1 for a in assets) > 50:
            raise DeliveryError("rich_media_limit")
        return [SendBatch("rich", assets, caption(envelope, 30000), envelope)]
    batches = []
    start = 0
    while start < len(assets):
        size = 9 if len(assets) - start == 11 else min(10, len(assets) - start)
        batches.append(SendBatch("album", assets[start:start + size], text if start == 0 else "", envelope))
        start += size
    return batches


def asset_key(source: str, index: int, paths: list[Path | str]) -> str:
    digest = hashlib.sha256(f"{MEDIA_VERSION}:{source}:{index}".encode())
    for path in paths:
        if isinstance(path, Path):
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(path.encode())
    return digest.hexdigest()


async def asset_key_async(source: str, index: int, paths: list[Path | str]) -> str:
    return await asyncio.to_thread(asset_key, source, index, paths)


def from_worker(item: dict[str, Any], dest: Destination, resolve: Callable[[str, str], Path]) -> DeliveryEnvelope:
    if "error" in item or item.get("access") != "public":
        return DeliveryEnvelope(dest, "error", body="暂时无法解析，请稍后重试。", rich_preview=True)
    assets = []
    source_id = str(item.get("canonicalUrl") or "")
    for index, media in enumerate(item.get("media", [])):
        def location(id_key: str, cache_key: str, value: dict[str, Any] = media) -> Path | str:
            cached = value.get(cache_key)
            return str(cached) if cached else resolve(item["leaseId"], value[id_key])
        live = media["type"] == "live_photo"
        main = location("videoMediaId", "telegramVideoFileId") if live else location("mediaId", "telegramFileId")
        photo = location("mediaId", "telegramFileId") if live else media.get("telegramCoverFileId")
        size = (main.stat().st_size if isinstance(main, Path)
                else int(media.get("videoSizeBytes" if live else "sizeBytes", 0)))
        assets.append(MediaAsset(
            key=media.get("assetKey") or asset_key(source_id, index, [main, *([photo] if photo else [])]),
            type=media["type"], media=main, photo=photo, width=int(media.get("width") or 0),
            height=int(media.get("height") or 0), duration=float(media.get("durationSeconds") or 0), size=size,
            native_error=media.get("nativeError"), references=media.get("references") or {},
        ))
    return DeliveryEnvelope(
        dest,
        source_id,
        str(item.get("title") or ""),
        str(item.get("plainContent") or item.get("content") or ""),
        source_id,
        str(item.get("telegraphUrl") or ""),
        tuple(assets),
        str(item.get("platform") or ""),
        item.get("outputMode", "preview") == "preview",
        ((str(item.get("platform") or ""), source_id),) if source_id else (),
    )
