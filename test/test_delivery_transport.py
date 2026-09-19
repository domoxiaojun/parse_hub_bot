"""Exercise actual MTProto and Rich Message serialization without contacting Telegram."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pyrogram import raw

from delivery.models import DeliveryEnvelope, Destination, MediaAsset, SendBatch
from delivery.transport import TelegramTransport, decode


def test_live_upload_binds_document_not_input_file_and_reuses_pair(tmp_path: Path) -> None:
    async def run():
        motion, still = tmp_path / "a.mp4", tmp_path / "a.jpg"
        motion.write_bytes(b"video")
        still.write_bytes(b"photo")
        document = raw.types.Document(id=12, access_hash=13, file_reference=b"video-ref", date=0,
                                      mime_type="video/mp4", size=5, dc_id=2, attributes=[])
        photo = raw.types.Photo(id=22, access_hash=23, file_reference=b"photo-ref", date=0, dc_id=2,
                                sizes=[raw.types.PhotoSize(type="x", w=80, h=120, size=5)])
        client = SimpleNamespace(
            resolve_peer=AsyncMock(return_value=raw.types.InputPeerSelf()),
            save_file=AsyncMock(return_value=raw.types.InputFile(id=1, parts=1, name="media", md5_checksum="")),
            guess_mime_type=lambda _: "video/mp4",
            invoke=AsyncMock(side_effect=[raw.types.MessageMediaDocument(document=document),
                                          raw.types.MessageMediaPhoto(photo=photo, video=document, live_photo=True)]),
        )
        transport = TelegramTransport(client, "123")
        asset = MediaAsset("a", "live_photo", motion, still, 80, 120, 2, 5)
        uploaded = await transport.upload(asset, Destination(chat_id=123), rich=False)
        calls = client.invoke.call_args_list
        assert isinstance(calls[0].args[0].media, raw.types.InputMediaUploadedDocument)
        static = calls[1].args[0].media
        assert isinstance(static, raw.types.InputMediaUploadedPhoto) and static.live_photo
        assert isinstance(static.video, raw.types.InputDocument) and static.video.id == 12
        assert uploaded.media.live_photo and uploaded.media.id.id == 22 and uploaded.media.video.id == 12
        assert decode(uploaded.refs["photo"]).id == 22 and decode(uploaded.refs["media"]).id == 12
        assert client.save_file.await_count == 2
        again = await transport.upload(asset, Destination(chat_id=123), rich=False)
        assert again.refs == uploaded.refs and client.save_file.await_count == 2
    asyncio.run(run())


def test_inline_upload_cover_is_assigned_and_kept_in_final_json(tmp_path: Path) -> None:
    async def run():
        video, cover = tmp_path / "video.mp4", tmp_path / "cover.jpg"
        video.write_bytes(b"video")
        cover.write_bytes(b"cover")
        photo = raw.types.Photo(id=2, access_hash=3, file_reference=b"photo-ref", date=0, dc_id=2,
                                sizes=[raw.types.PhotoSize(type="x", w=80, h=120, size=5)])
        document = raw.types.Document(id=4, access_hash=5, file_reference=b"video-ref", date=0,
                                      mime_type="video/mp4", size=5, dc_id=2, attributes=[])
        client = SimpleNamespace(
            save_file=AsyncMock(return_value=raw.types.InputFile(id=1, parts=1, name="file", md5_checksum="")),
            guess_mime_type=lambda _: "video/mp4",
            invoke=AsyncMock(side_effect=[raw.types.MessageMediaPhoto(photo=photo),
                                          raw.types.MessageMediaDocument(document=document, video_cover=photo)]),
            edit_inline_text=AsyncMock(return_value=True),
        )
        transport = TelegramTransport(client, "123")
        asset = MediaAsset("pair", "live_photo", video, cover, 80, 120, 2, 5)
        target = Destination(surface="inline", inline_message_id="opaque")
        prepared = await transport.upload(asset, target, rich=True)
        uploaded_video = client.invoke.call_args_list[1].args[0].media
        assert uploaded_video.video_cover.id == 2
        assert not any(isinstance(a, raw.types.DocumentAttributeAnimated) for a in uploaded_video.attributes)
        envelope = DeliveryEnvelope(target, "source", body="caption", source_url="https://example.com",
                                    media=(asset,), platform="xhs", rich_preview=True)
        await transport.rich(target, [(SendBatch("rich", (asset,), "caption", envelope), [prepared])])
        message = client.edit_inline_text.call_args.kwargs["rich_message"]
        block = next(block.video for block in message.blocks if hasattr(block, "video"))
        assert decode(block.video_cover).id == 2 and decode(block.media).id == 4
        cached = await transport.upload(asset, target, rich=True)
        assert cached.refs == prepared.refs and client.invoke.await_count == 2
    asyncio.run(run())

