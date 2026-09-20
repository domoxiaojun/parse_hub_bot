from pyrogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message

from services import CacheMedia, CacheMediaType


def cache_media_from_message(m: Message) -> CacheMedia | None:
    """从已发送的 Telegram Message 提取 CacheMedia。"""
    if m.photo:
        return CacheMedia(type=CacheMediaType.PHOTO, file_id=m.photo.file_id)
    if m.video:
        return CacheMedia(
            type=CacheMediaType.VIDEO,
            file_id=m.video.file_id,
            cover_file_id=m.video.video_cover.file_id if m.video.video_cover else None,
        )
    if m.animation:
        return CacheMedia(type=CacheMediaType.ANIMATION, file_id=m.animation.file_id)
    if m.document:
        return CacheMedia(type=CacheMediaType.DOCUMENT, file_id=m.document.file_id)
    return None


def build_cached_media_group(
    media: list[CacheMedia], *, video_cover: bool
) -> list[InputMediaPhoto | InputMediaVideo | InputMediaDocument]:
    """从 CacheMedia 列表构建 Telegram media group。"""
    group: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument] = []
    for m in media:
        match m.type:
            case CacheMediaType.PHOTO:
                group.append(InputMediaPhoto(media=m.file_id))
            case CacheMediaType.VIDEO:
                if m.cover_file_id:
                    group.append(
                        InputMediaVideo(
                            media=m.file_id,
                            supports_streaming=True,
                            video_cover=m.cover_file_id if video_cover else None,
                        )
                    )
                else:
                    group.append(InputMediaVideo(media=m.file_id, supports_streaming=True))
            case CacheMediaType.DOCUMENT:
                group.append(InputMediaDocument(media=m.file_id))
    return group
