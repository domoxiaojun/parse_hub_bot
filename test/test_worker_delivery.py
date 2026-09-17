"""Offline contract and delivery lifecycle tests; never connects to Telegram."""
import asyncio
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import TypeAdapter, ValidationError
from pyrogram import types
from pyrogram.errors import FloodWait, Forbidden

from worker.config import WorkerSettings
from worker.jobs import Jobs
from worker.models import DeliveryTarget, JobInput, MessageDelivery
from worker.reading_delivery import page_parts, prepare_reading_frame
from worker.rich_delivery import blocks_size, build_frames, rich_text, split_text
from worker.sender import TelegramSender, create_sender_client
from worker.store import Store


def result(count=1, **overrides):
    return {"platform": "xhs", "canonicalUrl": "https://www.xiaohongshu.com/explore/1",
            "sourceUrl": "https://www.xiaohongshu.com/explore/1", "title": "山间散步",
            "content": "<script>不要执行</script>" * 25, "access": "public", "author": {"name": "作者"},
            "contentType": "post", "contentFormat": "markdown", "mediaFailureCount": 0,
            "media": [{"mediaId": f"{i:032x}", "type": "photo", "sizeBytes": 5} for i in range(count)],
            "leaseId": "lease", **overrides}


def target(surface="message"):
    return TypeAdapter(DeliveryTarget).validate_python(
        {"surface": "message", "chatId": "-100123", "replyToMessageId": 7, "messageThreadId": 9}
        if surface == "message" else {"surface": surface, "inlineMessageId": "opaque_id"})


def job(items=None):
    return {"id": "job", "results": items if items is not None else [result()],
            "delivery": {"status": "pending", "surface": "message", "messageIds": [], "inFlight": False}}


def test_rich_layout_literal_content_and_footer():
    frames = build_frames([result()], lambda *_: Path("photo.jpg"))
    blocks = frames[0].payload.blocks
    assert isinstance(blocks[0], types.InputRichBlockParagraph)
    assert isinstance(blocks[0].text, types.RichTextBold)
    assert isinstance(blocks[1], types.InputRichBlockFooter) and "小红书 · 作者" in blocks[1].text
    assert isinstance(blocks[2], types.InputRichBlockParagraph)
    assert "<script>" in blocks[2].text  # Literal RichText, not HTML/Markdown source.
    assert isinstance(blocks[3], types.InputRichBlockPhoto)
    assert isinstance(blocks[-1].text[0], types.RichTextUrl)
    assert blocks[-1].text[0].url == result()["canonicalUrl"]
    assert frames[0].payload.markdown is None and frames[0].payload.html is None


def test_media_mapping_order_metadata_and_archive():
    media = [{"mediaId": str(i), "type": kind, "filename": "media.tar.gz" if kind == "document" else "clip.mp4",
              "width": 32.4, "height": 16, "durationSeconds": 1.1, "pairedMediaId": "pair"}
             for i, kind in enumerate(["photo", "video", "animation", "audio", "document"])]
    blocks = build_frames([result(media=media, content="短正文")], lambda *_: Path("media"))[0].payload.blocks
    assert [type(block).__name__ for block in blocks[3:-1]] == [
        "InputRichBlockPhoto", "InputRichBlockVideo", "InputRichBlockAnimation", "InputRichBlockAudio",
        "InputRichBlockDocument"]
    assert blocks[4].video.duration == 2 and blocks[4].video.width == 32
    assert blocks[-2].document.file_name == "media.tar.gz"


def test_splitting_keeps_order_and_inline_does_not_drop_media():
    frames = build_frames([result(51)], lambda *_: Path("photo.jpg"))
    assert len(frames) == 2 and "2/2" in frames[1].payload.blocks[0].text
    with pytest.raises(ValueError, match="inline_media_limit"):
        build_frames([result(51)], lambda *_: Path("photo.jpg"), inline=True)
    inline = build_frames([result(2), result(2)], lambda *_: Path("photo.jpg"), inline=True)
    assert len(inline) == 1 and inline[0].result_indices == [0, 1]


def test_error_cards_never_display_upstream_exception_or_private_url():
    frames = build_frames([{"error": {"code": "upstream_http", "message": "secret cookie=x"},
                            "sourceUrl": "https://private?token=secret"}], lambda *_: Path("unused"))
    assert "secret" not in frames[0].text
    assert "暂时无法解析" in frames[0].payload.blocks[0].text.text
    partial = build_frames([result(0, mediaFailureCount=2)], lambda *_: Path("unused"))
    assert "2 项媒体未能处理" in partial[0].payload.blocks[-1].text


@pytest.mark.parametrize("surface", ["message", "inline", "guest"])
def test_sender_acknowledges_target_and_commits_before_return(surface):
    async def run():
        client = SimpleNamespace(send_rich_message=AsyncMock(return_value=SimpleNamespace(id=21)),
                                 edit_inline_text=AsyncMock(return_value=True))
        state = job()
        saved = []
        store = SimpleNamespace(media_file=lambda *_: (Path("photo.jpg"), "image/jpeg", 5))
        await TelegramSender(client).deliver(state, target(surface), store, lambda: saved.append(copy.deepcopy(state)))
        assert saved[0]["delivery"]["inFlight"] is True
        assert state["delivery"]["status"] == "sent"
        assert state["evidence"]["trust"] == "untrusted_external_data"
        if surface == "message":
            params = client.send_rich_message.call_args.kwargs
            assert params["chat_id"] == -100123 and params["reply_parameters"].message_id == 7
            assert params["message_thread_id"] == 9 and state["delivery"]["messageIds"] == [21]
        else:
            assert client.edit_inline_text.call_args.kwargs["inline_message_id"] == "opaque_id"
            assert state["delivery"]["confirmed"] is True
            client.send_rich_message.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("error,status", [(Forbidden(), "partial"), (OSError("secret"), "unknown")])
def test_partial_or_ambiguous_send_is_not_retried(error, status):
    async def run():
        client = SimpleNamespace(send_rich_message=AsyncMock(side_effect=[SimpleNamespace(id=21), error]))
        state = job([result(51)])
        await TelegramSender(client).deliver(state, target(),
            SimpleNamespace(media_file=lambda *_: (Path("photo.jpg"), "image/jpeg", 5)), lambda: None)
        assert state["delivery"]["status"] == status and state["delivery"]["messageIds"] == [21]
        assert client.send_rich_message.call_count == 2
        assert "secret" not in str(state)
    asyncio.run(run())


def test_floodwait_and_cancel_during_send():
    async def run():
        client = SimpleNamespace(send_rich_message=AsyncMock(side_effect=[FloodWait(0), SimpleNamespace(id=21)]))
        state = job([result(0)])
        await TelegramSender(client).deliver(state, target(), None, lambda: None)
        assert state["delivery"]["status"] == "sent" and client.send_rich_message.call_count == 2
        client.send_rich_message.side_effect = asyncio.CancelledError()
        state = job([result(0)])
        with pytest.raises(asyncio.CancelledError):
            await TelegramSender(client).deliver(state, target(), None, lambda: None)
        assert state["delivery"]["status"] == "unknown"
    asyncio.run(run())


def test_delivery_job_replay_after_lease_release_and_restart(tmp_path):
    async def run():
        store = Store(tmp_path)
        client = SimpleNamespace(send_rich_message=AsyncMock(return_value=SimpleNamespace(id=21)))
        engine = SimpleNamespace(configure=lambda _: None, extract_urls=lambda _: ["https://x.com/a/status/1"],
                                 prepare=AsyncMock(return_value=result(0, _files=[])))
        jobs = Jobs(engine, store, "123", TelegramSender(client))
        jobs.configure(SimpleNamespace(model_dump=lambda: {"version": "1"}))
        request = JobInput(text="https://x.com/a/status/1", accountId="123", requestId="r", idempotencyKey="same",
                           delivery=target())
        state = jobs.create(request)
        await asyncio.gather(*list(jobs.tasks.values()))
        assert state["status"] == "ready" and state["delivery"]["status"] == "sent"
        assert store.renew(state["results"][0]["leaseId"]) is False
        assert jobs.create(request)["delivery"]["messageIds"] == [21]
        assert client.send_rich_message.call_count == 1
        await jobs.close()
        store.close()
        reopened = Store(tmp_path)
        assert reopened.job(state["id"])["delivery"]["status"] == "sent"
        reopened.close()
    asyncio.run(run())


def test_restart_keeps_uncertain_delivery_terminal(tmp_path):
    store = Store(tmp_path)
    state = job([])
    state.update(status="running")
    state["delivery"].update(status="sending", inFlight=True)
    store.save_job(state, "key", "fingerprint")
    store.close()
    store = Store(tmp_path)
    assert store.job("job")["delivery"]["status"] == "unknown"
    assert store.job("job")["status"] == "interrupted"
    store.close()


def test_target_validation_and_outbound_only_client():
    with pytest.raises(ValidationError):
        MessageDelivery(surface="message", chatId="@user", arbitrary="target")
    settings = WorkerSettings(bot_token="123:fixture", api_id=1, api_hash="fixture", _env_file=None,
                              worker_service_key="fixture-service-key-32-characters-long")
    with patch("worker.sender.Client") as client:
        create_sender_client(settings)
        assert client.call_args.args[0] == "worker_sender_123"
        assert client.call_args.kwargs["in_memory"] is False
        assert client.call_args.kwargs["workdir"] == settings.sessions_path
        assert client.call_args.kwargs["sleep_threshold"] == 0
        assert client.call_args.kwargs["no_updates"] is True
        assert client.call_args.kwargs["plugins"] is None


def test_rich_text_serializes_as_plain_nodes_not_source_markup():
    async def run():
        from pyrogram import raw
        frame = build_frames([result(0, content="<b>literal</b>")], lambda *_: Path("unused"))[0]
        serialized = await frame.payload.write(client=SimpleNamespace())
        assert isinstance(serialized.blocks[0], raw.types.PageBlockParagraph)
        assert isinstance(serialized.blocks[0].text, raw.types.TextBold)
        assert isinstance(serialized.blocks[0].text.text, raw.types.TextPlain)
        assert serialized.blocks[0].text.text.text == "山间散步"
        assert isinstance(serialized.blocks[2].text, raw.types.TextPlain)
        assert serialized.blocks[2].text.text == "<b>literal</b>"
    asyncio.run(run())


def test_long_content_is_complete_and_utf8_safe():
    content = "开头\n" + "中间内容🙂" * 5000 + "\n结尾标记"
    frames = build_frames([result(0, content=content)], lambda *_: Path("unused"))
    body = "".join(block.text for frame in frames for block in frame.payload.blocks
                  if isinstance(block, types.InputRichBlockParagraph) and isinstance(block.text, str))
    assert len(frames) > 1
    assert body == content
    assert all(blocks_size(frame.payload.blocks) <= 32768 for frame in frames)


@pytest.mark.parametrize("source", ["a" * 100000, "  中文🙂\n\n \t" * 5000, "\n" * 40000])
def test_split_preserves_every_character(source):
    chunks = split_text(source, 100)
    assert "".join(chunks) == source
    assert all(0 < len(chunk.encode()) <= 100 for chunk in chunks)


def test_long_title_and_metadata_fit_the_whole_frame_budget():
    title = "标题🙂" * 9000
    author = "作者" * 10000
    content = "正文尾部" * 2000
    frames = build_frames([result(0, title=title, author={"name": author}, content=content)],
                          lambda *_: Path("unused"))
    titles = [block.text.text for frame in frames for block in frame.payload.blocks
              if isinstance(getattr(block, "text", None), types.RichTextBold)]
    assert "".join(titles) == title
    assert all(blocks_size(frame.payload.blocks) <= 32768 for frame in frames)
    assert sum(bool(frame.completed_result_indices) for frame in frames) == 1


@pytest.mark.parametrize("surface", ["message", "inline", "guest"])
def test_multiple_results_keep_complete_text_and_source(surface):
    items = [result(0, title=f"标题-{i}", content=f"开头-{i}\n" + "中文🙂text" * 600 + f"\n末尾-{i}",
                    canonicalUrl=f"https://example.com/{i}") for i in range(2)]
    frames = build_frames(items, lambda *_: Path("unused"), inline=surface != "message")
    text = "".join(rich_text(getattr(block, "text", "")) for frame in frames for block in frame.payload.blocks)
    for item in items:
        assert item["title"] in text and item["content"] in text
        assert any(item["canonicalUrl"] in frame.text for frame in frames)
    assert all(blocks_size(frame.payload.blocks) <= 32768 for frame in frames)


def test_pairs_and_media_order_survive_batch_boundary():
    item = result(101, content="正文")
    item["media"][49]["pairedMediaId"] = "live"
    item["media"][50]["pairedMediaId"] = "live"
    frames = build_frames([item], lambda _, mid: Path(mid))
    ids = [[str(block.photo.media) for block in frame.payload.blocks
            if isinstance(block, types.InputRichBlockPhoto)] for frame in frames]
    assert [mid for group in ids for mid in group] == [m["mediaId"] for m in item["media"]]
    assert all(len(group) <= 50 for group in ids)
    assert any(item["media"][49]["mediaId"] in group and item["media"][50]["mediaId"] in group for group in ids)


def test_block_limit_accounts_for_frame_headers_and_footers(monkeypatch):
    monkeypatch.setattr("worker.rich_delivery.MAX_RICH_BLOCKS", 6)
    frames = build_frames([result(12)], lambda *_: Path("photo.jpg"))
    assert all(len(frame.payload.blocks) <= 6 for frame in frames)
    assert sum(frame.media_count for frame in frames) == 12


def test_nested_rich_labels_and_media_captions_are_counted():
    blocks = [types.InputRichBlockParagraph(types.RichTextBold("标题")),
              types.InputRichBlockFooter(["前缀", types.RichTextUrl("链接", url="https://example.com")]),
              types.InputRichBlockVideo(types.InputMediaVideo("video.mp4"),
                                        caption=types.RichBlockCaption(text="实况片段"))]
    assert blocks_size(blocks) == len("标题前缀链接实况片段".encode())


def test_sender_chains_replies_and_does_not_claim_unsent_body():
    async def run():
        client = SimpleNamespace(send_rich_message=AsyncMock(side_effect=[SimpleNamespace(id=21), Forbidden()]))
        state = job([result(0, content="正文" * 30000)])
        await TelegramSender(client).deliver(state, target(), None, lambda: None)
        assert state["delivery"]["status"] == "partial"
        calls = client.send_rich_message.call_args_list
        assert [c.kwargs["reply_parameters"].message_id for c in calls] == [7, 21]
        assert all(c.kwargs["message_thread_id"] == 9 for c in calls)
        assert state["evidence"]["sources"] == []
        assert state["delivery"]["completedFrames"] == 1
        assert state["delivery"]["totalFrames"] > 1
    asyncio.run(run())


@pytest.mark.parametrize("surface", ["inline", "guest"])
def test_inline_text_overflow_publishes_complete_reading_pages(surface):
    async def run():
        content = "<script>literal</script>\n中文🙂" * 6000
        item = result(0, title="长标题" * 1000, content=content)
        publisher = SimpleNamespace(create_page=AsyncMock(), close=AsyncMock())
        publisher.create_page.side_effect = lambda *a, **kw: SimpleNamespace(
            url=f"https://telegra.ph/page-{publisher.create_page.call_count}")
        client = SimpleNamespace(edit_inline_text=AsyncMock(return_value=True), send_rich_message=AsyncMock())
        state = job([item])
        with patch("worker.reading_delivery.Telegraph", return_value=publisher):
            await TelegramSender(client).deliver(state, target(surface), None, lambda: None)
        assert state["delivery"]["status"] == "sent"
        assert publisher.create_page.call_count > 1
        calls = list(reversed(publisher.create_page.call_args_list))
        all_text = []
        for call in calls:
            nodes = call.kwargs["content"]
            assert len(json.dumps(nodes, ensure_ascii=False).encode()) < 65536
            for node in nodes:
                for child in node["children"]:
                    if isinstance(child, str):
                        all_text.append(child)
                    elif child == {"tag": "br"}:
                        all_text.append("\n")
        combined = "".join(all_text)
        assert item["title"] in combined and content in combined
        payload = client.edit_inline_text.call_args.kwargs["rich_message"]
        assert "阅读版" in "".join(rich_text(b.text) for b in payload.blocks)
        assert state["delivery"]["mediaCount"] == 0
        publisher.close.assert_awaited_once()
        client.send_rich_message.assert_not_called()
    asyncio.run(run())


def test_reading_failure_persists_and_never_edits_partial_content():
    async def run():
        client = SimpleNamespace(edit_inline_text=AsyncMock())
        publisher = SimpleNamespace(create_page=AsyncMock(side_effect=OSError("secret upstream error")),
                                    close=AsyncMock())
        state = job([result(0, content="x" * 40000)])
        saved = []
        with patch("worker.reading_delivery.Telegraph", return_value=publisher):
            await TelegramSender(client).deliver(state, target("inline"), None,
                                                 lambda: saved.append(copy.deepcopy(state)))
        assert saved[-1]["delivery"]["status"] == "failed"
        assert state["delivery"]["error"]["code"] == "reading_page_failed"
        assert not state["delivery"]["inFlight"]
        assert "secret" not in str(state)
        client.edit_inline_text.assert_not_called()
    asyncio.run(run())


def test_reading_pages_are_reused_and_restricted_results_are_never_published():
    async def run():
        publisher = SimpleNamespace(create_page=AsyncMock(return_value=SimpleNamespace(url="https://telegra.ph/full")),
                                    close=AsyncMock())
        items = [result(0, content="公开正文"), result(0, content="PRIVATE", access="restricted")]
        receipt = {}
        with patch("worker.reading_delivery.Telegraph", return_value=publisher):
            for _ in range(2):
                await prepare_reading_frame(items, receipt, lambda: None, lambda *_: Path("unused"))
        assert publisher.create_page.call_count == 1
        assert "PRIVATE" not in str(publisher.create_page.call_args)
        assert "access_token" not in str(receipt)
    asyncio.run(run())


@pytest.mark.parametrize("count,content", [(51, "正文"), (1, "中文" * 20000)])
def test_inline_media_overflow_has_no_reading_or_archive_combination(count, content):
    async def run():
        client = SimpleNamespace(edit_inline_text=AsyncMock())
        state = job([result(count, content=content)])
        with patch("worker.reading_delivery.Telegraph") as publisher:
            await TelegramSender(client).deliver(state, target("inline"),
                SimpleNamespace(media_file=lambda *_: (Path("photo.jpg"), "image/jpeg", 1)), lambda: None)
        assert state["delivery"]["status"] == "failed"
        assert state["delivery"]["error"]["code"] == "delivery_limits"
        publisher.assert_not_called()
        client.edit_inline_text.assert_not_called()
    asyncio.run(run())


def test_article_reading_branch_never_uploads_local_media():
    async def run():
        publisher = SimpleNamespace(create_page=AsyncMock(return_value=SimpleNamespace(url="https://telegra.ph/article")),
                                    close=AsyncMock())
        state = job([result(51, contentType="article")])
        client = SimpleNamespace(edit_inline_text=AsyncMock(return_value=True))
        with patch("worker.reading_delivery.Telegraph", return_value=publisher):
            await TelegramSender(client).deliver(state, target("guest"), None, lambda: None)
        assert state["delivery"]["status"] == "sent"
        assert state["delivery"]["readingMediaCount"] == 51
        assert state["delivery"]["mediaCount"] == 0
        assert all(isinstance(b, types.InputRichBlockParagraph | types.InputRichBlockFooter)
                   for b in client.edit_inline_text.call_args.kwargs["rich_message"].blocks)
    asyncio.run(run())


def test_telegraph_json_size_with_many_linebreaks():
    content = "\n" * 100000
    parts = page_parts(result(0, title="T", content=content))
    assert all(len(json.dumps(part, ensure_ascii=False).encode()) < 60000 for part in parts)


def test_legacy_caption_keeps_body_and_honors_visibility():
    # Load the real helper without starting the bot's credential-dependent i18n setup.
    spec = importlib.util.spec_from_file_location("caption_helpers", Path("plugins/helpers.py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"i18n": SimpleNamespace(t_=lambda value: value),
                                 "repo.settings": SimpleNamespace(SettingsConfig=object)}):
        spec.loader.exec_module(module)
    body = "第一段\n\n" + "长正文🙂" * 2000 + "\n结尾"
    caption = module.build_caption_by_str("标题", body, "https://example.com", rich=True)
    assert caption.startswith("**标题**") and body in caption and "https://example.com" in caption
    assert "###" not in caption
    hidden = module.build_caption_by_str("标题", body, "https://example.com", rich=True,
                                         hide_title=True, hide_desc=True, hide_source=True)
    assert hidden == ""
    assert body in module.format_text(body)


def test_all_failed_jobs_send_error_card_but_never_report_parse_success(tmp_path):
    async def run():
        store = Store(tmp_path)
        client = SimpleNamespace(send_rich_message=AsyncMock(return_value=SimpleNamespace(id=21)))
        engine = SimpleNamespace(configure=lambda _: None, extract_urls=lambda _: ["https://x.com/a/status/1"],
                                 prepare=AsyncMock(side_effect=ValueError("private upstream error")))
        jobs = Jobs(engine, store, "123", TelegramSender(client))
        jobs.configure(SimpleNamespace(model_dump=lambda: {"version": "1"}))
        state = jobs.create(JobInput(text="https://x.com/a/status/1", accountId="123", requestId="r",
                                     idempotencyKey="failed", delivery=target()))
        await asyncio.gather(*list(jobs.tasks.values()))
        assert state["status"] == "failed"
        assert state["delivery"]["status"] == "partial" and state["delivery"]["messageIds"] == [21]
        assert state["evidence"]["sources"] == []
        assert "private upstream error" not in str(state)
        await jobs.cancel(state["id"])
        assert store.job(state["id"])["delivery"]["messageIds"] == [21]
        await jobs.close()
        store.close()
    asyncio.run(run())
