"""Shared adapters from pipeline/cache objects to immutable final assets."""

import asyncio
from pathlib import Path
from typing import Any, cast

from parsehub.types import AniFile, ImageFile, LivePhotoFile, VideoFile

from delivery.models import AssetKind, DeliveryError, MediaAsset, asset_key
from services.cache import CacheMedia, CacheMediaType
from services.media import ProcessedMedia, resolve_media_info


def pipeline_assets(processed_list: list[ProcessedMedia], source_id: str, *,
                    raw: bool = False) -> tuple[MediaAsset, ...]:
    assets: list[MediaAsset] = []
    for processed in processed_list:
        source = processed.source
        paths = [Path(source.path)] if raw else processed.output_paths or [Path(source.path)]
        if raw and isinstance(source, LivePhotoFile) and source.video_path:
            paths = [*paths, Path(source.video_path)]
        if isinstance(source, LivePhotoFile) and not raw:
            if len(paths) != 1:
                raise DeliveryError("live_photo_not_prepared")
            if processed.motion is None:
                path = paths[0]
                width, height, _ = resolve_media_info(processed, str(path))
                assets.append(MediaAsset(asset_key(source_id, len(assets), [path]), "photo", path,
                                         width=width, height=height, size=path.stat().st_size))
                continue
            motion = processed.motion
            key = asset_key(source_id, len(assets), [paths[0], motion.path])
            assets.append(MediaAsset(key, "live_photo", motion.path, paths[0], motion.width, motion.height,
                                     motion.duration, motion.path.stat().st_size, motion.native_error))
            continue
        kind: AssetKind = ("document" if raw else "photo" if isinstance(source, ImageFile) else
                           "animation" if isinstance(source, AniFile) else "video" if isinstance(source, VideoFile)
                           else "document")
        for path in paths:
            width, height, duration = (0, 0, 0) if raw else resolve_media_info(processed, str(path))
            assets.append(MediaAsset(asset_key(source_id, len(assets), [path]), kind, path,
                                     width=width, height=height, duration=duration, size=path.stat().st_size))
    return tuple(assets)


async def pipeline_assets_async(processed_list: list[ProcessedMedia], source_id: str, *,
                                raw: bool = False) -> tuple[MediaAsset, ...]:
    return await asyncio.to_thread(pipeline_assets, processed_list, source_id, raw=raw)


def cached_assets(media: list[CacheMedia]) -> tuple[MediaAsset, ...]:
    return tuple(MediaAsset(
        m.asset_key or asset_key("cached", i, [m.file_id]), cast(AssetKind, m.type.value),
        m.video_file_id if m.type == CacheMediaType.LIVE_PHOTO and m.video_file_id else m.file_id,
        m.file_id if m.type == CacheMediaType.LIVE_PHOTO else m.cover_file_id,
        m.width, m.height, m.duration, m.size_bytes, m.native_error, m.references,
    ) for i, m in enumerate(media))


def cache_assets(assets: tuple[MediaAsset, ...], uploaded: list[dict[str, Any]]) -> list[CacheMedia]:
    values = {item["key"]: item for item in uploaded}
    result = []
    for asset in assets:
        value = values.get(asset.key)
        if value is None:
            raise DeliveryError("missing_asset_ack")
        refs = value["refs"]
        result.append(CacheMedia(
            type=CacheMediaType(asset.type), file_id=refs["photo"] if asset.type == "live_photo" else refs["media"],
            video_file_id=refs["media"] if asset.type == "live_photo" else None,
            cover_file_id=refs.get("photo") if asset.type != "live_photo" else None,
            asset_key=asset.key, width=asset.width, height=asset.height, duration=asset.duration,
            size_bytes=asset.size, native_error=asset.native_error,
            references={**asset.references, value["representation"]: refs},
        ))
    return result


def worker_cached_media(media: CacheMedia) -> dict[str, Any]:
    result: dict[str, Any] = {"type": media.type.value, "telegramFileId": media.file_id}
    if media.cover_file_id:
        result["telegramCoverFileId"] = media.cover_file_id
    if media.type == CacheMediaType.LIVE_PHOTO:
        result.update(telegramVideoFileId=media.video_file_id, width=media.width, height=media.height,
                      durationSeconds=media.duration, videoSizeBytes=media.size_bytes, nativeError=media.native_error)
    if media.asset_key:
        result["assetKey"] = media.asset_key
    if media.references:
        result["references"] = media.references
    return result
