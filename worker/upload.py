"""只上传注册，不发送聊天消息。"""

import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from pyrogram import raw, types
from pyrogram.file_id import FileId, FileType

from worker.security import EngineError


def file_hash(path: Path) -> str:
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


class MediaUploader:
    def __init__(self, client: Any, database_path: Path | None = None) -> None:
        self.client = client
        self._registered: dict[tuple, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()
        self._database = database_path
        if self._database:
            self._database.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._database) as db:
                db.execute('CREATE TABLE IF NOT EXISTS worker_upload_refs '
                           '(cache_key TEXT PRIMARY KEY, created REAL NOT NULL, result TEXT NOT NULL)')

    async def upload(self, item: dict, force: bool = False) -> dict:
        path = Path(item['path'])
        digest = await asyncio.to_thread(file_hash, path)
        bot_id = str(self.client.me.id)
        key = (bot_id, digest, item['type'])
        # Serializing only registration prevents duplicate upload for identical bytes.
        async with self._lock:
            cached = None if force else self._registered.get(key)
            db_key = json.dumps(key)
            if self._database and not force and cached is None:
                with sqlite3.connect(self._database) as db:
                    row = db.execute('SELECT created,result FROM worker_upload_refs WHERE cache_key=?',
                                     (db_key,)).fetchone()
                    if row:
                        cached = (row[0], json.loads(row[1]))
            if cached and time.time() - cached[0] < 172800:
                return {**cached[1], **{k: v for k, v in item.items() if k not in ('path', 'thumbnailPath')}}
            self._registered = {key: value for key, value in self._registered.items()
                                if time.time() - value[0] < 172800}
            saved = await self.client.save_file(str(path))
            if saved is None:
                raise EngineError('upload_failed', 'upload')
            kind = item['type']
            thumbnail = None
            cover = None
            cover_file_id = None
            if item.get('thumbnailPath') and kind == 'video':
                try:
                    thumbnail = await self.client.save_file(str(item['thumbnailPath']))
                    cover_saved = await self.client.save_file(str(item['thumbnailPath']))
                    if cover_saved:
                        cover_result = await self.client.invoke(raw.functions.messages.UploadMedia(
                            peer=raw.types.InputPeerEmpty(), media=raw.types.InputMediaUploadedPhoto(file=cover_saved),
                        ))
                        photo = cover_result.photo
                        parsed_photo = types.Photo._parse(self.client, photo)
                        if parsed_photo:
                            cover_file_id = parsed_photo.file_id
                            cover = raw.types.InputPhoto(id=photo.id, access_hash=photo.access_hash,
                                                         file_reference=photo.file_reference)
                except Exception:
                    pass  # A valid registered video remains useful without its optional cover.
            media: Any
            if kind == 'photo':
                media = raw.types.InputMediaUploadedPhoto(file=saved)
            else:
                attrs: list[Any] = [raw.types.DocumentAttributeFilename(file_name=item['filename'])]
                if kind in ('video', 'animation'):
                    attrs.append(raw.types.DocumentAttributeVideo(
                        duration=item.get('durationSeconds', 0), w=item.get('width', 0),
                        h=item.get('height', 0), supports_streaming=item['mimeType'] == 'video/mp4',
                    ))
                if kind == 'animation':
                    attrs.append(raw.types.DocumentAttributeAnimated())
                if kind == 'audio':
                    attrs.append(raw.types.DocumentAttributeAudio(duration=item.get('durationSeconds', 0)))
                media = raw.types.InputMediaUploadedDocument(
                    file=saved, mime_type=item['mimeType'], attributes=attrs,
                    force_file=kind == 'document', thumb=thumbnail, video_cover=cover,
                )
            registered = await self.client.invoke(raw.functions.messages.UploadMedia(
                peer=raw.types.InputPeerEmpty(), media=media,
            ))
            if kind == 'photo':
                photo = types.Photo._parse(self.client, registered.photo)
                if photo is None:
                    raise EngineError('upload_failed', 'upload')
                file_id = photo.file_id
            else:
                document = registered.document
                file_type = {'video': FileType.VIDEO, 'animation': FileType.ANIMATION,
                             'audio': FileType.AUDIO, 'document': FileType.DOCUMENT}[kind]
                file_id = FileId(file_type=file_type, dc_id=document.dc_id, media_id=document.id,
                                 access_hash=document.access_hash, file_reference=document.file_reference).encode()
            result = {k: v for k, v in item.items() if k not in ('path', 'thumbnailPath')}
            result.update(fileId=file_id, botId=bot_id, sha256=digest)
            if cover_file_id:
                result['coverFileId'] = cover_file_id
            self._registered[key] = (time.time(), result)
            if self._database:
                with sqlite3.connect(self._database) as db:
                    db.execute('DELETE FROM worker_upload_refs WHERE created <= ?', (time.time() - 172800,))
                    db.execute('INSERT OR REPLACE INTO worker_upload_refs VALUES (?,?,?)',
                               (db_key, time.time(), json.dumps(result)))
            return dict(result)
