"""Unified delivery regression tests. No Telegram network calls."""
import asyncio
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pyrogram import raw, types
from pyrogram.errors import FloodWait, Forbidden

from delivery.models import (
    DeliveryEnvelope,
    DeliveryError,
    Destination,
    MediaAsset,
    caption,
    from_worker,
    plan,
    utf16_size,
)
from delivery.rich import build_rich_message
from delivery.sender import send_envelope
from delivery.transport import MemoryReferences, TelegramTransport, UploadedAsset, UploadFailure
from worker.app import public_job
from worker.models import InlineDelivery, MessageDelivery
from worker.sender import TelegramSender
from worker.store import Store


def live(i=0, **overrides):
    return MediaAsset(str(i), "live_photo", f"video-{i}", f"photo-{i}", 1080, 1440, 3, 4096, **overrides)


def envelope(media=(), surface="message", **kwargs):
    return DeliveryEnvelope(Destination(surface=surface, chat_id=123 if surface == "message" else None,
                                        inline_message_id="inline" if surface != "message" else None),
                            "source", source_url="https://example.com/p", media=tuple(media), **kwargs)


class FakeTransport:
    def __init__(self):
        self.uploaded = []
        self.sent = []
        self.api = SimpleNamespace(send=AsyncMock(return_value=True))
        self.rich_messages = []
        self.fail_batch = None
        self.fail_error = Forbidden()
        self.fail_upload = False
        self.fail_upload_error = OSError("upload interrupted")

    async def upload(self, asset, dest, *, rich, force=False):
        if self.fail_upload:
            raise self.fail_upload_error
        result = UploadedAsset(asset, "preview" if rich else "native",
                               {"media": str(asset.media), "photo": str(asset.photo) if asset.photo else None},
                               None, asset.key)
        self.uploaded.append(result)
        return result

    async def native(self, dest, batch, uploaded, random_ids):
        index = len(self.sent)
        self.sent.append((batch, list(random_ids)))
        if index == self.fail_batch:
            raise self.fail_error
        return [SimpleNamespace(id=index * 10 + i + 1, media_group_id=str(index + 100))
                for i in range(max(1, len(uploaded)))]

    async def rich(self, dest, batches):
        batch, uploaded = batches[0]
        message = build_rich_message(batch.envelope, uploaded)
        self.rich_messages.append(message)
        self.sent.append(("rich", message))
        return SimpleNamespace(id=len(self.sent)) if dest.surface == "message" else True

    async def refresh(self, uploaded, dest):
        return uploaded

    def remember(self, uploaded, messages, dest):
        pass


@pytest.mark.parametrize("n,sizes", [(1, [1]), (2, [2]), (10, [10]), (11, [9, 2]), (21, [10, 9, 2])])
def test_live_album_native_routing_pairing_and_caption(n, sizes):
    async def run():
        e = envelope([live(i) for i in range(n)], title="标题", body="正文")
        batches = plan(e)
        assert [len(b.assets) for b in batches] == sizes
        assert batches[0].kind == ("live_photo" if n == 1 else "album")
        assert all(not b.text for b in batches[1:])
        t, state, snapshots = FakeTransport(), {}, []
        result = await send_envelope(e, t, state, lambda: snapshots.append(copy.deepcopy(state)))
        assert result.status == "sent" and len(result.message_ids) == n
        assert len(t.sent) == len(sizes)
        assert [(u.asset.photo, u.asset.media) for u in t.uploaded] == [(f"photo-{i}", f"video-{i}") for i in range(n)]
        assert any(s.get("inFlight") and s.get("_randomIds") for s in snapshots)
        assert len(result.albums) == (0 if n == 1 else len(sizes))
    asyncio.run(run())


@pytest.mark.parametrize("surface", ["inline", "guest"])
def test_inline_live_is_one_video_with_its_own_cover(surface):
    async def run():
        t, state = FakeTransport(), {}
        result = await send_envelope(envelope([live(0), live(1)], surface, body="正文"), t, state, lambda: None)
        assert result.confirmed and result.message_ids == []
        assert len(t.rich_messages) == 1
        blocks = t.rich_messages[0].blocks
        videos = [b.video for b in blocks if isinstance(b, types.InputRichBlockVideo)]
        assert [(v.media, v.video_cover) for v in videos] == [
            ("video-0", "photo-0"), ("video-1", "photo-1")]
        assert not any(isinstance(b, (types.InputRichBlockPhoto, types.InputRichBlockAnimation)) for b in blocks)
    asyncio.run(run())


def test_native_live_rejects_limits_and_mixed_animation_before_upload():
    for media in ([replace(live(), duration=11)], [live(), MediaAsset("a", "animation", "animation")]):
        with pytest.raises(DeliveryError):
            plan(envelope(media))
    assert plan(envelope([replace(live(), duration=11)], "inline"))[0].kind == "rich"


def test_plain_and_mixed_media_routes():
    assert plan(envelope())[0].kind == "message"
    assert plan(envelope([MediaAsset("a", "video", "video")]))[0].kind == "single"
    assert plan(envelope([MediaAsset("a", "photo", "photo"), live()]))[0].kind == "album"
    assert plan(envelope([MediaAsset("a", "animation", "gif"), MediaAsset("b", "photo", "photo")]))[0].kind == "rich"
    for kind in ("audio", "document"):
        assert plan(envelope([MediaAsset(str(i), kind, str(i)) for i in range(3)]))[0].kind == "album"


def test_caption_unicode_budget_and_source_are_preserved():
    for limit in (1024, 4096):
        text = caption(envelope(title="长标题" * 1000, body="🙂中文" * 8000), limit)
        assert utf16_size(text) <= limit and text.endswith("来源：https://example.com/p")
        assert "\ufffd" not in text


def test_rich_media_limit_counts_covers_and_does_not_drop_items():
    assert len(plan(envelope([live(i) for i in range(25)], "inline"))) == 1
    with pytest.raises(DeliveryError, match="rich_media_limit"):
        plan(envelope([live(i) for i in range(26)], "inline"))


@pytest.mark.parametrize("error,status", [(Forbidden(), "partial"), (OSError("timeout"), "unknown")])
def test_second_batch_failure_does_not_repeat_confirmed_album(error, status):
    async def run():
        t, state = FakeTransport(), {}
        t.fail_batch, t.fail_error = 1, error
        e = envelope([live(i) for i in range(11)])
        result = await send_envelope(e, t, state, lambda: None)
        assert result.status == status and len(state["messageIds"]) == 9
        assert state["completedFrames"] == 1
        await send_envelope(e, t, state, lambda: None)
        assert len(t.sent) == 2
    asyncio.run(run())


def test_upload_failure_logs_safe_actionable_stage_without_partial_send(caplog):
    async def run():
        t, state = FakeTransport(), {}
        t.fail_upload = True
        t.fail_upload_error = UploadFailure("save_document", OSError("/secret/path and token"))
        with caplog.at_level("WARNING", logger="parsehub.delivery"):
            result = await send_envelope(envelope([live()]), t, state, lambda: None)
        assert result.status == "failed" and not t.sent and not state["inFlight"]
        entry = next(record.getMessage() for record in caplog.records
                     if "event=delivery.upload_failed" in record.getMessage())
        assert all(value in entry for value in (
            "kind=live_photo", "batch=1/1", "asset=1/1", "asset_type=live_photo",
            "size_bytes=4096", "representation=native", "phase=save_document", "error_type=OSError",
        ))
        assert "secret" not in entry and "token" not in entry
    asyncio.run(run())


def test_worker_preview_restores_rich_video_platform_footer_and_source_link():
    async def run():
        transport = FakeTransport()
        source = "https://www.douyin.com/video/1"
        item = {
            "access": "public",
            "outputMode": "preview",
            "platform": "douyin",
            "canonicalUrl": source,
            "title": "标题",
            "plainContent": "正文",
            "media": [{
                "type": "video",
                "telegramFileId": "video-file-id",
                "width": 1080,
                "height": 1920,
                "durationSeconds": 24,
                "sizeBytes": 4096,
            }],
        }
        job = {"results": [item], "delivery": {"status": "pending"}}
        await TelegramSender(SimpleNamespace(), transport=transport).deliver(
            job, MessageDelivery(surface="message", chatId="123"), None, lambda: None)
        assert job["delivery"]["status"] == "sent" and job["delivery"]["kind"] == "rich"
        blocks = transport.rich_messages[0].blocks
        assert isinstance(blocks[0], types.InputRichBlockSectionHeading)
        assert blocks[0].text == "标题" and blocks[0].size == 4
        video = next(block.video for block in blocks if isinstance(block, types.InputRichBlockVideo))
        assert (video.width, video.height, video.duration) == (1080, 1920, 24)
        footer = blocks[-1]
        assert isinstance(footer, types.InputRichBlockFooter) and footer.text[0] == "抖音"
        link = next(value for value in footer.text if isinstance(value, types.RichTextUrl))
        assert link.text == "查看原文" and link.url == source
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["raw", "zip"])
def test_worker_raw_and_zip_keep_native_document_delivery(mode):
    item = {
        "access": "public", "outputMode": mode, "platform": "xhs",
        "canonicalUrl": "https://example.com/p", "telegram": "unused",
        "media": [{"type": "document", "telegramFileId": "document-file-id", "sizeBytes": 5}],
    }
    converted = from_worker(item, Destination(chat_id=123), lambda *_: Path("unused"))
    assert not converted.rich_preview and plan(converted)[0].kind == "single"


def test_floodwait_reuses_random_ids():
    async def run():
        t, state = FakeTransport(), {}
        t.fail_batch, t.fail_error = 0, FloodWait(0)
        result = await send_envelope(envelope([live()]), t, state, lambda: None)
        assert result.status == "sent"
        assert t.sent[0][1] == t.sent[1][1]
    asyncio.run(run())


def test_missing_pair_rejected_without_inventing_partner():
    with pytest.raises(DeliveryError, match="live_photo_pair_missing"):
        plan(envelope([replace(live(), photo=None)]))


def test_native_rpc_is_single_sendmultimedia_with_existing_references():
    async def run():
        client = SimpleNamespace(
            resolve_peer=AsyncMock(return_value=raw.types.InputPeerUser(user_id=123, access_hash=1)),
            invoke=AsyncMock(return_value=raw.types.Updates(updates=[], users=[], chats=[], date=0, seq=0)),
        )
        transport = TelegramTransport(client, "123")
        e = envelope([live(), MediaAsset("p", "photo", "photo")])
        batch = plan(e)[0]
        media = [
            raw.types.InputMediaPhoto(id=raw.types.InputPhoto(id=1, access_hash=1, file_reference=b"p"),
                                      live_photo=True,
                                      video=raw.types.InputDocument(id=2, access_hash=2, file_reference=b"v")),
            raw.types.InputMediaPhoto(id=raw.types.InputPhoto(id=3, access_hash=3, file_reference=b"s")),
        ]
        prepared = [UploadedAsset(a, "native", {}, m, a.key) for a, m in zip(e.media, media, strict=True)]
        await transport.native(e.dest, batch, prepared, [11, 22])
        rpc = client.invoke.call_args.args[0]
        assert isinstance(rpc, raw.functions.messages.SendMultiMedia)
        assert [m.random_id for m in rpc.multi_media] == [11, 22]
        assert rpc.multi_media[0].media.live_photo and rpc.multi_media[0].media.video.id == 2
        assert rpc.multi_media[0].message and not rpc.multi_media[1].message
    asyncio.run(run())


def test_worker_public_receipt_hides_random_ids():
    job = {"results": [{"references": "secret"}], "delivery": {"tasks": [{"_randomIds": [[42]], "messageIds": [1]}]}}
    public = public_job(job)
    assert public["results"] == []
    assert "_randomIds" not in public["delivery"]["tasks"][0]
    assert job["delivery"]["tasks"][0]["_randomIds"] == [[42]]


def test_worker_inline_and_guest_use_worker_transport():
    async def run():
        for surface in ("inline", "guest"):
            t = FakeTransport()
            item = {"access": "public", "platform": "xhs", "canonicalUrl": "https://example.com",
                    "title": "title", "plainContent": "body", "media": [
                        {"type": "live_photo", "telegramFileId": "cover", "telegramVideoFileId": "video",
                         "width": 100, "height": 200, "durationSeconds": 3, "videoSizeBytes": 500},
                    ]}
            job = {"results": [item], "delivery": {"status": "pending"}}
            await TelegramSender(SimpleNamespace(), transport=t).deliver(
                job, InlineDelivery(surface=surface, inlineMessageId="inline"), None, lambda: None)
            assert job["delivery"]["status"] == "sent" and job["delivery"]["confirmed"]
            video = next(block.video for block in t.rich_messages[0].blocks
                         if isinstance(block, types.InputRichBlockVideo))
            assert video.video_cover == "cover"
    asyncio.run(run())


def test_worker_multiple_sources_stay_separate_rich_tasks():
    async def run():
        t = FakeTransport()
        items = [{"access": "public", "platform": "xhs", "canonicalUrl": f"https://example.com/{i}",
                  "title": "title", "plainContent": "body", "media": []} for i in range(2)]
        job = {"results": items, "delivery": {"status": "pending"}}
        await TelegramSender(SimpleNamespace(), transport=t).deliver(
            job, MessageDelivery(surface="message", chatId="123"), None, lambda: None)
        assert job["delivery"]["status"] == "sent" and len(t.sent) == 2
        assert all(kind == "rich" for kind, _ in t.sent)
        assert len(job["evidence"]["sources"]) == 2
    asyncio.run(run())


def test_upload_cache_and_confirmed_receipt_survive_restart(tmp_path: Path):
    store = Store(tmp_path)
    store.set_upload("a", {"media": "video", "photo": "photo"})
    store.save_job({"id": "j", "status": "ready", "delivery": {"status": "sent", "messageIds": [1]}}, "idem", "fp")
    store.close()
    reopened = Store(tmp_path)
    assert reopened.get_upload("a") == {"media": "video", "photo": "photo"}
    assert reopened.idempotent("idem", "fp")["delivery"]["messageIds"] == [1]
    reopened.close()


def test_cache_partial_pair_never_reused():
    async def run():
        cache = MemoryReferences()
        t = TelegramTransport(SimpleNamespace(), "123", cache)
        t._document = AsyncMock(return_value="fresh-video")
        t._photo = AsyncMock(return_value="fresh-photo")
        t.client.resolve_peer = AsyncMock(return_value=raw.types.InputPeerSelf())
        from unittest.mock import patch
        with patch("delivery.transport.decode", return_value=raw.types.InputPhoto(
                id=1, access_hash=1, file_reference=b"")):
            first = await t.upload(live(), Destination(chat_id=123), rich=True)
            cache.set_upload(first.cache_key, {"media": "old-video"})
            second = await t.upload(live(), Destination(chat_id=123), rich=True)
        assert second.refs["photo"] == "fresh-photo"
        assert t._photo.await_count == 2 and t._document.await_count == 2
    asyncio.run(run())


def test_cancelled_visible_request_is_unknown_and_not_replayed():
    async def run():
        t, state = FakeTransport(), {}
        t.fail_batch, t.fail_error = 0, asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await send_envelope(envelope([live()]), t, state, lambda: None)
        assert state["status"] == "unknown"
        await send_envelope(envelope([live()]), t, state, lambda: None)
        assert len(t.sent) == 1
    asyncio.run(run())

def test_worker_cancel_after_confirmed_result_is_partial():
    async def run():
        transport = FakeTransport()
        job = {
            "id": "job",
            "results": [
                {"access": "public", "platform": "xhs", "canonicalUrl": "https://example.com/1",
                 "title": "one", "plainContent": "one", "media": []},
                {"access": "public", "platform": "xhs", "canonicalUrl": "https://example.com/2",
                 "title": "two", "plainContent": "two", "media": [
                     {"type": "video", "telegramFileId": "video", "width": 100,
                      "height": 100, "durationSeconds": 1, "sizeBytes": 10},
                 ]},
            ],
            "delivery": {"status": "pending"},
        }
        original_upload = transport.upload
        calls = 0

        async def cancel_on_second_upload(asset, dest, *, rich, force=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise asyncio.CancelledError
            return await original_upload(asset, dest, rich=rich, force=force)

        transport.upload = cancel_on_second_upload
        with pytest.raises(asyncio.CancelledError):
            await TelegramSender(SimpleNamespace(), transport=transport).deliver(
                job, MessageDelivery(surface="message", chatId="123"), None, lambda: None)
        assert job["delivery"]["status"] == "partial"
        assert job["delivery"]["messageIds"]

    asyncio.run(run())



@pytest.mark.parametrize("status,in_flight,expected", [("sent", False, "sent"),
                                                      ("sending", True, "unknown")])
def test_restart_checks_receipt_before_expired_files(tmp_path, status, in_flight, expected):
    async def run():
        t = FakeTransport()
        state = {"kind": "live_photo", "status": status, "inFlight": in_flight, "messageIds": [12]}
        expired = replace(live(), media=tmp_path / "expired.mp4", photo=tmp_path / "expired.jpg")
        result = await send_envelope(envelope([expired]), t, state, lambda: None)
        assert result.status == expected and result.message_ids == [12]
        assert not t.uploaded and not t.sent
    asyncio.run(run())


def test_reference_expired_refreshes_pair_and_reuses_request_ids():
    async def run():
        from pyrogram.errors import FileReferenceExpired
        t, state = FakeTransport(), {}
        t.fail_batch, t.fail_error = 0, FileReferenceExpired()
        t.refresh = AsyncMock(side_effect=lambda value, _: value)
        result = await send_envelope(envelope([live()]), t, state, lambda: None)
        assert result.status == "sent"
        assert t.refresh.await_count == 1
        assert t.sent[0][1] == t.sent[1][1]
        assert t.refresh.call_args.args[0].asset.photo == "photo-0"
    asyncio.run(run())


def test_cache_cleanup_preserves_acknowledged_and_uncertain_jobs(tmp_path: Path):
    store = Store(tmp_path)
    for status in ("sent", "unknown", "partial"):
        store.save_job({"id": status, "status": "ready", "delivery": {"status": status}}, status, status)
    store.db.execute("UPDATE worker_jobs SET updated=0")
    store.db.commit()
    store.cleanup()
    assert all(store.job(status) for status in ("sent", "unknown", "partial"))
    store.close()
