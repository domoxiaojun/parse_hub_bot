"""Worker 保持原生下载目录/processed 命名并验证实际媒体。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from worker.media import compatible, prepare_file, prepare_video, probe, run_process, split_video


def run(coro):
    return asyncio.run(coro)


def test_compatible_video_keeps_original_filename_and_has_no_invented_cover(tmp_path):
    async def execute():
        source = tmp_path / '原视频.mp4'
        await run_process('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                          'color=c=red:s=32x16:d=0.3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source))
        result = await prepare_file(source, tmp_path / 'processed', 'preview')
        assert result[0]['path'] == source
        assert result[0]['filename'] == '原视频.mp4'
        assert result[0]['width'] == 32 and result[0]['durationSeconds'] == 1
        assert 'thumbnailPath' not in result[0]
        assert list((tmp_path / 'processed').iterdir()) == []
    run(execute())


def test_native_remux_and_transcode_filenames(tmp_path):
    async def execute():
        processed = tmp_path / 'processed'
        source = tmp_path / 'native.mkv'
        await run_process('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                          'color=c=red:s=32x16:d=0.3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source))
        paths = await prepare_video(source, processed)
        assert paths == [processed / 'native_remux.mp4']
        assert compatible(await probe(paths[0]))
        incompatible = tmp_path / 'legacy.mkv'
        await run_process('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                          'color=c=red:s=32x16:d=0.3', '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono',
                          '-t', '0.3', '-c:v', 'mpeg4', '-c:a', 'pcm_s16le', str(incompatible))
        paths = await prepare_video(incompatible, processed)
        assert paths == [processed / 'legacy_h264_remux.mp4']
        assert compatible(await probe(paths[0]))
        assert source.exists() and incompatible.exists()
    run(execute())


def test_raw_filename_unchanged_and_images_use_native_shared_processed(tmp_path):
    source = tmp_path / '原图.webp'
    Image.new('RGB', (32, 16), 'red').save(source)
    processed = tmp_path / 'processed'
    raw = run(prepare_file(source, processed, 'raw'))
    assert raw[0]['path'] == source and raw[0]['filename'] == source.name
    preview = run(prepare_file(source, processed, 'preview'))
    assert preview[0]['path'] == processed / '原图.jpg'
    assert not any(item.name.isdecimal() for item in processed.iterdir())


def test_image_split_reuses_native_segment_layout(tmp_path):
    source = tmp_path / 'tall.png'
    Image.new('RGB', (320, 4000), 'red').save(source)
    processed = tmp_path / 'processed'
    items = run(prepare_file(source, processed, 'preview'))
    assert len(items) == 3
    assert all(item['path'].parent.parent == processed for item in items)
    assert all(item['path'].parent.name.startswith('split_') for item in items)
    assert [item['filename'] for item in items] == ['segment_001.png', 'segment_002.png', 'segment_003.png']


def test_video_split_reuses_native_directory_and_segment_names(tmp_path, monkeypatch):
    async def fake_process(*args):
        Path(args[-1]).write_bytes(b'local media')
        assert '-protocol_whitelist' in args
        return b''

    monkeypatch.setattr('worker.media.run_process', fake_process)
    monkeypatch.setattr('worker.media.probe', AsyncMock(return_value={'format': {'duration': '2'}}))
    source = tmp_path / 'title_remux.mp4'
    processed = tmp_path / 'processed'
    paths = run(split_video(source, processed, 3))
    assert paths == [processed / 'title_remux_split' / f'title_remux_part_{index:03d}.mp4' for index in (1, 2)]


def test_cancelled_media_process_kills_child(monkeypatch):
    async def execute():
        entered = asyncio.Event()
        process = SimpleNamespace(returncode=None, wait=AsyncMock())

        async def communicate():
            entered.set()
            await asyncio.Event().wait()

        def kill():
            process.returncode = -9

        process.communicate = communicate
        process.kill = kill
        monkeypatch.setattr('worker.media.asyncio.create_subprocess_exec', AsyncMock(return_value=process))
        task = asyncio.create_task(run_process('ffmpeg'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode == -9
        process.wait.assert_awaited_once()
    run(execute())
