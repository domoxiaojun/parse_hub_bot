"""Fork persistent-cache schema: Live Photo pairs, asset keys and a media version.

Upstream ``services.cache`` only knows photo/video/animation/document file IDs.
The shared delivery core needs paired Live Photo references, content keys and
a version stamp so stale media is never replayed after a preparation change.
The upstream ``cache`` table and ``CacheRepo`` are reused as-is; only the JSON
payload model differs.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from core import bs
from db import get_session
from ext.delivery.models import MEDIA_VERSION
from repo.cache import CacheRepo
from services.cache import CacheParseResult, PersistentCache

MEDIA_CACHE_VERSION = MEDIA_VERSION


class CacheMediaType(StrEnum):
    # Superset of upstream services.cache.CacheMediaType; Enums cannot be extended in place.
    LIVE_PHOTO = "live_photo"
    AUDIO = "audio"
    VOICE = "voice"
    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"
    DOCUMENT = "document"


class CacheMedia(BaseModel):
    type: CacheMediaType
    file_id: str
    cover_file_id: str | None = None
    video_file_id: str | None = None
    asset_key: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0
    size_bytes: int = 0
    native_error: str | None = None
    references: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_pair(self) -> "CacheMedia":
        if self.type == CacheMediaType.LIVE_PHOTO and (not self.file_id or not self.video_file_id):
            raise ValueError("live_photo_pair_missing")
        return self


class CacheEntry(BaseModel):
    parse_result: CacheParseResult
    media: list[CacheMedia] | None = None
    telegraph_url: str | None = None
    rich: bool = False
    media_version: int = MEDIA_CACHE_VERSION


class ForkPersistentCache(PersistentCache):
    """Same table and eviction as upstream; validates the fork entry model and its media version."""

    async def get(self, url: str) -> CacheEntry | None:  # type: ignore[override]
        if self._disable:
            return None

        key = self._make_key(url)
        async with get_session() as session:
            repo = CacheRepo(session)
            cache = await repo.get(key)
            if cache is None:
                self.logger.debug(f"缓存未命中: key={url}")
                return None

            try:
                entry = CacheEntry.model_validate(cache.entry_json)
            except Exception as e:
                self.logger.warning(f"缓存内容无效, 已删除: key={url}, error={e}")
                await repo.remove(cache)
                return None

            if entry.media and (
                "media_version" not in entry.model_fields_set or entry.media_version != MEDIA_CACHE_VERSION
            ):
                self.logger.debug(f"媒体缓存版本过期, 已删除: key={url}")
                await repo.remove(cache)
                return None

            await repo.touch(cache, self._now())
            self.logger.debug(f"缓存命中: key={url}")
            return entry

    async def set(self, url: str, entry: CacheEntry) -> None:  # type: ignore[override]
        if self._disable:
            return

        key = self._make_key(url)
        now = self._now()
        async with get_session() as session:
            repo = CacheRepo(session)
            await repo.upsert(key=key, url=url, entry_json=entry.model_dump(mode="json"), accessed_at=now)
            removed = await self._evict_overflow(repo)
            self.logger.debug(f"缓存写入: key={url}, evicted={removed}")


persistent_cache = ForkPersistentCache(max_entries=bs.cache_max_entries, disable=bs.cache_disabled)

__all__ = [
    "MEDIA_CACHE_VERSION",
    "CacheEntry",
    "CacheMedia",
    "CacheMediaType",
    "CacheParseResult",
    "ForkPersistentCache",
    "persistent_cache",
]
