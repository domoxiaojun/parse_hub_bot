"""Thin Telegram transports. Input files have already been normalized."""

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pyrogram import raw, types, utils
from pyrogram.file_id import FileId, FileType

from delivery.models import DeliveryError, Destination, MediaAsset, SendBatch
from delivery.rich import build_rich_message


class ReferenceCache(Protocol):
    def get_upload(self, key: str) -> dict[str, Any] | None: ...
    def set_upload(self, key: str, value: dict[str, Any]) -> None: ...


class MemoryReferences:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def get_upload(self, key: str) -> dict[str, Any] | None:
        return self.values.get(key)

    def set_upload(self, key: str, value: dict[str, Any]) -> None:
        self.values[key] = value


class BotRejected(Exception):
    def __init__(self, code: int, retry_after: int = 0, reference_expired: bool = False):
        self.code = code
        self.retry_after = retry_after
        self.reference_expired = reference_expired
        super().__init__(f"bot_api_{code}")


class UploadFailure(DeliveryError):
    """A privacy-safe upload stage plus the original exception type/identifier."""

    def __init__(self, phase: str, cause: BaseException | None = None):
        self.phase = phase
        self.cause_type = type(cause).__name__ if cause is not None else "None"
        self.rpc = str(getattr(cause, "ID", "") or "none")
        super().__init__("upload_failed")


@dataclass
class UploadedAsset:
    asset: MediaAsset
    representation: str
    refs: dict[str, Any]
    media: Any
    cache_key: str


def decode(file_id: str) -> Any:
    return utils.get_input_media_from_file_id(file_id).id


def document_id(document: Any, kind: str) -> str:
    file_type = {"video": FileType.VIDEO, "live_photo": FileType.VIDEO, "animation": FileType.ANIMATION,
                 "audio": FileType.AUDIO, "voice": FileType.VOICE}.get(kind, FileType.DOCUMENT)
    return str(FileId(file_type=file_type, dc_id=document.dc_id, media_id=document.id,
                      access_hash=document.access_hash, file_reference=document.file_reference).encode())


class TelegramTransport:
    def __init__(self, client: Any, account_id: str, cache: ReferenceCache | None = None):
        self.client = client
        self.account_id = account_id
        self.cache = cache if cache is not None else MemoryReferences()

    async def _photo(self, source: Path | str, peer: Any, video: Any = None) -> str:
        media: Any
        if isinstance(source, str):
            if video is None:
                return source
            media = raw.types.InputMediaPhoto(id=decode(source), live_photo=True, video=video)
        else:
            try:
                uploaded = await self.client.save_file(source)
            except Exception as error:
                raise UploadFailure("save_photo", error) from error
            if uploaded is None:
                raise UploadFailure("save_photo_empty")
            media = raw.types.InputMediaUploadedPhoto(file=uploaded, live_photo=bool(video) or None, video=video)
        try:
            response = await self.client.invoke(raw.functions.messages.UploadMedia(peer=peer, media=media))
            return str(types.Photo._parse(self.client, response.photo).file_id)
        except Exception as error:
            raise UploadFailure("upload_photo", error) from error

    async def _document(self, asset: MediaAsset, peer: Any, cover: str | None) -> str:
        if isinstance(asset.media, str):
            return asset.media
        attrs: list[Any] = [raw.types.DocumentAttributeFilename(file_name=asset.media.name)]
        if asset.type in {"video", "live_photo", "animation"}:
            attrs.append(raw.types.DocumentAttributeVideo(duration=asset.duration, w=asset.width, h=asset.height,
                                                           supports_streaming=True))
        if asset.type == "animation":
            attrs.append(raw.types.DocumentAttributeAnimated())
        if asset.type in {"audio", "voice"}:
            attrs.append(raw.types.DocumentAttributeAudio(duration=math.ceil(asset.duration),
                                                           voice=asset.type == "voice"))
        try:
            uploaded = await self.client.save_file(asset.media)
        except Exception as error:
            raise UploadFailure("save_document", error) from error
        if uploaded is None:
            raise UploadFailure("save_document_empty")
        media = raw.types.InputMediaUploadedDocument(
            file=uploaded, mime_type=self.client.guess_mime_type(str(asset.media)) or "application/octet-stream",
            attributes=attrs, video_cover=decode(cover) if cover else None,
            force_file=True if asset.type == "document" else None,
        )
        try:
            response = await self.client.invoke(raw.functions.messages.UploadMedia(peer=peer, media=media))
            return document_id(response.document, asset.type)
        except Exception as error:
            raise UploadFailure("upload_document", error) from error

    async def upload(self, asset: MediaAsset, dest: Destination, *, rich: bool, force: bool = False) -> UploadedAsset:
        representation = "preview" if rich else "native"
        key = hashlib.sha256(f"{self.account_id}:{asset.key}:{representation}".encode()).hexdigest()
        refs = None if force else self.cache.get_upload(key)
        if not refs and not force:
            refs = asset.references.get(representation)
        if refs and (not refs.get("media") or (asset.type == "live_photo" and not refs.get("photo"))):
            refs = None
        if refs is None:
            try:
                peer = await self.client.resolve_peer(dest.chat_id) if dest.chat_id else raw.types.InputPeerSelf()
            except Exception as error:
                raise UploadFailure("resolve_peer", error) from error
            cover = None
            if asset.type == "live_photo" and not rich:
                main = await self._document(asset, peer, None)
                if asset.photo is None:
                    raise DeliveryError("live_photo_pair_missing")
                cover = await self._photo(asset.photo, peer, decode(main))
            elif asset.type == "photo":
                main = await self._photo(asset.media, peer)
            else:
                cover = await self._photo(asset.photo, peer) if asset.photo else None
                main = await self._document(asset, peer, cover)
            refs = {"media": main, "photo": cover}
            self.cache.set_upload(key, refs)
        media: Any
        if asset.type == "live_photo" and not rich:
            media = raw.types.InputMediaPhoto(id=decode(refs["photo"]), live_photo=True, video=decode(refs["media"]))
        elif asset.type == "photo":
            media = raw.types.InputMediaPhoto(id=decode(refs["media"]))
        else:
            media = raw.types.InputMediaDocument(id=decode(refs["media"]),
                                                video_cover=decode(refs["photo"]) if refs.get("photo") else None)
        return UploadedAsset(asset, representation, dict(refs), media, key)

    async def refresh(self, uploaded: UploadedAsset, dest: Destination) -> UploadedAsset:
        asset = uploaded.asset
        origin = uploaded.refs.get("origin")
        if origin:
            message = await self.client.get_messages(origin["chatId"], origin["messageId"])
            if asset.type == "live_photo" and uploaded.representation == "native":
                if not message.live_photo or not message.photo:
                    raise DeliveryError("live_photo_pair_missing")
                refs = {"photo": message.photo.file_id, "media": message.live_photo.file_id, "origin": origin}
            else:
                value = getattr(message, "video" if asset.type == "live_photo" else asset.type, None)
                if value is None:
                    raise DeliveryError("media_reference_expired")
                cover = getattr(value, "video_cover", None)
                refs = {"media": value.file_id, "photo": cover.file_id if cover else None, "origin": origin}
                if asset.type == "live_photo" and not refs["photo"]:
                    raise DeliveryError("live_photo_pair_missing")
            self.cache.set_upload(uploaded.cache_key, refs)
            return await self.upload(asset, dest, rich=uploaded.representation == "preview")
        if not isinstance(asset.media, Path) or (asset.type == "live_photo" and not isinstance(asset.photo, Path)):
            raise DeliveryError("media_reference_expired")
        return await self.upload(asset, dest, rich=uploaded.representation == "preview", force=True)

    async def native(self, dest: Destination, batch: SendBatch, uploaded: list[UploadedAsset],
                     random_ids: list[int]) -> list[Any]:
        peer = await self.client.resolve_peer(dest.chat_id)
        reply = types.ReplyParameters(message_id=dest.reply_to) if dest.reply_to else None
        common = {"peer": peer, "silent": dest.silent or None, "noforwards": dest.protect or None,
                  "reply_to": await utils.get_reply_to(self.client, reply, dest.thread_id, None)}
        rpc: Any
        if batch.kind == "message":
            rpc = raw.functions.messages.SendMessage(**common, message=batch.text, random_id=random_ids[0],
                                                     no_webpage=True)
        elif batch.kind == "album":
            rpc = raw.functions.messages.SendMultiMedia(**common, multi_media=[
                raw.types.InputSingleMedia(media=asset.media, random_id=random_ids[i],
                                           message=batch.text if i == 0 else "")
                for i, asset in enumerate(uploaded)
            ])
        else:
            rpc = raw.functions.messages.SendMedia(**common, media=uploaded[0].media,
                                                   message=batch.text, random_id=random_ids[0])
        response = await self.client.invoke(rpc, sleep_threshold=0)
        if isinstance(response, raw.types.UpdateShortSentMessage):
            return [types.Message(id=response.id)]
        messages = list(await utils.parse_messages(self.client, response))
        mapping = {u.random_id: u.id for u in getattr(response, "updates", [])
                   if isinstance(u, raw.types.UpdateMessageID)}
        by_id = {m.id: m for m in messages}
        if all(rid in mapping and mapping[rid] in by_id for rid in random_ids):
            return [by_id[mapping[rid]] for rid in random_ids]
        return sorted(messages, key=lambda m: m.id)

    async def rich(self, dest: Destination, batches: list[tuple[SendBatch, list[UploadedAsset]]]) -> Any:
        if len(batches) != 1:
            raise DeliveryError("rich_batch_contract")
        batch, uploaded = batches[0]
        rich_message = build_rich_message(batch.envelope, uploaded)
        if dest.surface == "message":
            reply = types.ReplyParameters(message_id=dest.reply_to) if dest.reply_to else None
            return await self.client.send_rich_message(
                chat_id=dest.chat_id,
                rich_message=rich_message,
                disable_notification=dest.silent,
                message_thread_id=dest.thread_id,
                reply_parameters=reply,
                protect_content=dest.protect,
            )
        return await self.client.edit_inline_text(
            inline_message_id=dest.inline_message_id,
            rich_message=rich_message,
        )

    def remember(self, uploaded: list[UploadedAsset], messages: list[Any], dest: Destination) -> None:
        for value, message in zip(uploaded, messages, strict=True):
            if value.asset.type == "live_photo" and value.representation == "native":
                # Live messages also expose photo for backwards compatibility; read the pair first.
                motion = getattr(message, "live_photo", None)
                photo = getattr(message, "photo", None)
                if motion and photo:
                    value.refs.update(media=motion.file_id, photo=photo.file_id)
            # Origin allows refreshing both references without downloading or sending again.
            value.refs["origin"] = {"chatId": dest.chat_id, "messageId": message.id}
            self.cache.set_upload(value.cache_key, value.refs)
