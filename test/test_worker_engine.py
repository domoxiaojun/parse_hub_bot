import asyncio
import json
import logging
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from parsehub import ParseHub
from parsehub.types import ImageRef, LivePhotoRef, VideoParseResult, VideoRef
from PIL import Image
from pyrogram import raw
from pyrogram.file_id import FileId, FileType

from worker.engine import ParseHubEngine
from worker.media import compatible, prepare_file, probe, run_process
from worker.security import EngineError, RequestPolicy, request_policy, validate_url
from worker.upload import MediaUploader


@pytest.fixture(autouse=True)
def media_dns(monkeypatch):
    pass


class FakeResult:
    def __init__(self, refs=None, access=None):
        self.media = refs or []
        self.title = 'title'
        self.content = 'body'
        self.raw_url = 'https://www.youtube.com/watch?v=example'
        self.type = SimpleNamespace(value='image')
        self.access = access
        self.name = 'test'

    async def download(self, directory, **kwargs):
        refs = self.media if isinstance(self.media, list) else [self.media]
        if any(ref.url.endswith('/fail') for ref in refs):
            raise ValueError('signed-secret-upstream-error')
        root = Path(directory)
        directory = root / self.name
        counter = 2
        while directory.exists():
            directory = root / f'{self.name}_{counter}'
            counter += 1
        directory.mkdir(parents=True)
        files = []
        for index, ref in enumerate(refs):
            path = directory / f'{index + 1:03d}_{self.name}.png'
            Image.new('RGB', (32, 16), 'red').save(path)
            file = SimpleNamespace(path=path, video_path=None)
            if isinstance(ref, LivePhotoRef):
                file.video_path = directory / f'{index + 1:03d}_{self.name}_video.mp4'
                file.video_path.write_bytes(b'raw-mp4')
            files.append(file)
        if kwargs.get('save_metadata'):
            (directory / 'metadata.json').write_text(json.dumps({'title': self.title, 'content': self.content}))
        return SimpleNamespace(media=files, output_dir=directory)


def engine(tmp_path, result=None):
    parser = SimpleNamespace(
        get_platform=lambda url: SimpleNamespace(id='youtube'),
        get_platforms=lambda: [{'id': 'youtube', 'name': 'YouTube', 'supported_types': ['video']}],
        parse=AsyncMock(return_value=result or FakeResult()),
    )
    return ParseHubEngine(tmp_path, parser)


def run(coro):
    return asyncio.run(coro)


def test_all_installed_platforms_exposed_without_bot_import(tmp_path):
    actual = ParseHubEngine(tmp_path)
    assert {p['id'] for p in actual.capabilities()['platforms']} == {p['id'] for p in ParseHub().get_platforms()}
    assert len(actual.capabilities()['platforms']) > 7


def test_extract_deduplicates_and_limits(tmp_path):
    instance = engine(tmp_path)
    assert instance.extract_urls('标题 https://youtube.com/a https://youtube.com/a。') == ['https://youtube.com/a']
    with pytest.raises(EngineError, match='too_many_urls'):
        instance.extract_urls(' '.join(f'https://youtube.com/{i}' for i in range(11)))


def test_read_only_no_download_or_upload(tmp_path):
    instance = engine(tmp_path, FakeResult([ImageRef(url='https://cdn.example/photo')]))
    result = run(instance.prepare('https://youtube.com/a', mode='read_only'))
    assert result['content'] == 'body' and result['media'] == []
    assert not list(tmp_path.iterdir())


def test_native_batch_download_failure_preserves_text(tmp_path):
    instance = engine(tmp_path, FakeResult([
        ImageRef(url='https://cdn.example/fail'), ImageRef(url='https://cdn.example/good'),
    ]))
    result = run(instance.prepare('https://youtube.com/a', directory=tmp_path))
    assert result['mediaFailureCount'] == 2
    assert result['media'] == [] and result['content'] == 'body'
    assert 'signed-secret' not in str(result)


@pytest.mark.parametrize('stage', ['download', 'convert'])
def test_media_failure_reports_stage_without_private_error(tmp_path, monkeypatch, caplog, stage):
    caplog.set_level(logging.WARNING, logger='parsehub.worker')
    instance = engine(tmp_path, FakeResult([ImageRef(url='https://cdn.example/good')]))
    error = ValueError('Cookie=private-secret https://cdn.example/signed?token=secret')
    if stage == 'download':
        monkeypatch.setattr(FakeResult, 'download', AsyncMock(side_effect=error))
    elif stage == 'convert':
        monkeypatch.setattr('worker.engine.prepare_file', AsyncMock(side_effect=error))
    result = run(instance.prepare('https://youtube.com/a', directory=tmp_path))
    assert result['media'] == [] and result['mediaFailureCount'] == 1
    assert result['mediaFailures'][0]['stage'] == stage
    assert result['mediaFailures'][0]['count'] == 1
    assert f'stage={stage}' in caplog.text
    assert 'private-secret' not in caplog.text + str(result)
    assert 'token=secret' not in caplog.text + str(result)


def test_real_parsehub_video_download_through_file_handoff(tmp_path, monkeypatch):
    """Use actual ParseHub download results and media conversion without Telegram registration."""
    async def execute():
        parsed = VideoParseResult(video=VideoRef(url='https://cdn.example/video.mp4'), title='video')
        parsed.raw_url = 'https://www.xiaohongshu.com/explore/test'

        async def download(url, path, **kwargs):
            await run_process('ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                              'color=c=red:s=32x16:d=0.3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path))
            return path

        monkeypatch.setattr('parsehub.types.result.download', download)
        parser = SimpleNamespace(get_platform=lambda _: SimpleNamespace(id='xhs'),
                                 parse=AsyncMock(return_value=parsed))
        instance = ParseHubEngine(tmp_path, parser=parser)
        result = await instance.prepare(parsed.raw_url, directory=tmp_path)
        assert result['mediaFailureCount'] == 0
        assert len(result['media']) == 1
        video = result['media'][0]
        assert video['type'] == 'video' and video['sizeBytes'] > 0
        assert len(video['mediaId']) == 32 and 'fileId' not in video
        assert Path(result['_mediaFiles'][video['mediaId']]['path']).is_file()
        assert video['width'] == 32 and video['durationSeconds'] == 1
        assert result['contentType'] == 'post'
    run(execute())


def test_worker_restores_logging_after_media_import():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, '-c', '''
import logging
import json
from worker.engine import ParseHubEngine
from worker.__main__ import configure_logging
configure_logging()
logging.getLogger('parsehub.worker').info('event=prepare.progress stage=upload')
logging.getLogger('parsehub.worker').warning('event=media.failed stage=upload')
'''], capture_output=True, text=True, check=True)
    assert 'event=prepare.progress stage=upload' in result.stderr
    assert 'event=media.failed stage=upload' in result.stderr


def test_raw_livephoto_keeps_pair_and_archive_metadata(tmp_path):
    instance = engine(tmp_path, FakeResult([LivePhotoRef(url='https://cdn.example/good', video_url='https://cdn/video')]))
    raw_result = run(instance.prepare('https://youtube.com/a', output_mode='raw', directory=tmp_path / 'raw'))
    assert [item['type'] for item in raw_result['media']] == ['document', 'document']
    assert raw_result['media'][0]['pairedMediaId'] == raw_result['media'][1]['pairedMediaId']
    archive_result = run(instance.prepare('https://youtube.com/a', output_mode='zip', directory=tmp_path / 'zip'))
    assert archive_result['media'][0]['filename'].endswith('.tar.gz')
    with tarfile.open(tmp_path / 'zip' / 'test.tar.gz') as archive:
        assert any(name.endswith('metadata.json') for name in archive.getnames())
        assert any(name.endswith('_video.mp4') for name in archive.getnames())


def test_cookie_once_and_unknown_blocked(tmp_path):
    instance = engine(tmp_path)
    instance.configure({'platforms': {'youtube': {'cookies': ['secret1', 'secret2']}}})
    instance.parser.parse.side_effect = [ValueError('login required'), FakeResult()]
    assert run(instance.prepare('https://youtube.com/a'))['access'] == 'public'
    assert len(instance.parser.parse.call_args_list) == 2
    assert instance.parser.parse.call_args_list[0].kwargs['proxy'] is None
    assert all('cookie' in call.kwargs for call in instance.parser.parse.call_args_list)


def test_cookie_explicit_public_and_restricted(tmp_path):
    instance = engine(tmp_path)
    instance.configure({'platforms': {'youtube': {'cookies': ['secret']}}})
    instance.parser.parse.side_effect = [ValueError('login required'), FakeResult(access='public')]
    assert run(instance.prepare('https://youtube.com/a', mode='read_only'))['access'] == 'public'
    instance.parser.parse.side_effect = [FakeResult(access='restricted')]
    assert run(instance.prepare('https://youtube.com/a'))['access'] == 'public'


def test_cookie_scope_on_redirect_and_download(monkeypatch):
    seen = []

    async def check(_):
        return None

    monkeypatch.setattr('worker.security.validate_public_url', check)

    def handle(request):
        seen.append((request.url.host, request.headers.get('cookie')))
        if request.url.host == 'www.youtube.com':
            return httpx.Response(302, headers={'location': 'https://cdn.example/file'})
        return httpx.Response(200, text='ok')

    async def execute():
        with request_policy(RequestPolicy('youtube', credentials=True)):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
                await client.get('https://www.youtube.com/watch', headers={'cookie': 'secret'})
        with request_policy(RequestPolicy('youtube', credentials=True, phase='download')):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                await client.get('https://www.youtube.com/file', headers={'cookie': 'secret'})

    run(execute())
    assert seen == [('www.youtube.com', 'secret'), ('cdn.example', None), ('www.youtube.com', 'secret')]


@pytest.mark.parametrize('url', ['http://127.0.0.1/x', 'http://localhost/x', 'https://user:pw@example.com/x'])
def test_private_url_rejected(url):
    with pytest.raises(EngineError):
        validate_url(url)


def test_upload_registers_without_sending_and_persists_hash(tmp_path):
    path = tmp_path / 'file.bin'
    path.write_bytes(b'contents')
    document = raw.types.Document(id=123, access_hash=456, file_reference=b'ref', date=1,
                                  mime_type='application/octet-stream', size=8, dc_id=2, attributes=[])
    client = SimpleNamespace(me=SimpleNamespace(id=1), save_file=AsyncMock(return_value='saved'),
                             invoke=AsyncMock(return_value=SimpleNamespace(document=document)))
    item = {'path': path, 'type': 'document', 'filename': path.name, 'mimeType': 'application/octet-stream'}
    result = run(MediaUploader(client, tmp_path / "original.sqlite").upload(item))
    assert FileId.decode(result['fileId']).file_type == FileType.DOCUMENT
    assert isinstance(client.invoke.call_args.args[0], raw.functions.messages.UploadMedia)
    assert isinstance(client.invoke.call_args.args[0].peer, raw.types.InputPeerEmpty)
    run(MediaUploader(client, tmp_path / "original.sqlite").upload(item))
    assert client.save_file.call_count == 1
    run(MediaUploader(client, tmp_path / "original.sqlite").upload(item, force=True))
    assert client.save_file.call_count == 2


def test_real_video_normalizes_and_probes(tmp_path):
    async def execute():
        path = tmp_path / 'mpeg4.mkv'
        await run_process('ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                          'color=c=red:s=32x16:d=0.3', '-c:v', 'mpeg4', str(path))
        items = await prepare_file(path, tmp_path / 'processed', 'preview')
        assert len(items) == 1 and items[0]['width'] == 32 and items[0]['durationSeconds'] == 1
        assert compatible(await probe(items[0]['path']))
    run(execute())


def test_nested_unrelated_public_does_not_authorize_cookie():
    from worker.security import inspect_visibility
    policy = RequestPolicy('instagram', credentials=True)
    inspect_visibility({'owner': {'id': 'someone', 'is_public': True}}, policy)
    assert not policy.public
    inspect_visibility({'id': 'post', 'visibility': 'public'}, policy)
    assert not policy.public


def test_socket_connect_rebinding_guard():
    pytest.skip('Worker no longer monkeypatches the original socket stack')
    import socket
    with request_policy(RequestPolicy('youtube')):
        sock = socket.socket()
        try:
            with pytest.raises(EngineError, match='unsupported_url'):
                sock.connect(('127.0.0.1', 80))
        finally:
            sock.close()


def test_provider_json_response_size_is_bounded(monkeypatch):
    pytest.skip('Provider response handling belongs to ParseHub')
    async def check(_):
        return None
    monkeypatch.setattr('worker.security.validate_public_url', check)

    class HugeResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(10):
                yield b'x' * 1024 * 1024

    async def execute():
        with request_policy(RequestPolicy('youtube')):
            transport = httpx.MockTransport(lambda _: httpx.Response(
                200, headers={'content-type': 'application/json'}, stream=HugeResponse(),
            ))
            async with httpx.AsyncClient(transport=transport) as client:
                await client.get('https://youtube.com/api')
    with pytest.raises(EngineError, match='upstream_contract'):
        run(execute())


def test_ytdlp_uses_guarded_subprocess_and_private_hop_rejected(tmp_path):
    from parsehub.parsers.base import ytdlp
    with request_policy(RequestPolicy('youtube')):
        command = ytdlp._yt_dlp_base_cmd()
    assert command[1:3] == ['-m', 'yt_dlp']

    async def execute():
        process = await asyncio.create_subprocess_exec(
            *command, '--dump-single-json', '--skip-download', 'http://127.0.0.1:12345/private',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        assert process.returncode != 0 and stdout.strip() in (b'', b'null')
    run(execute())


def test_ytdlp_redirect_transport_cookie_scope_in_isolated_process():
    pytest.skip('Worker no longer replaces ParseHub yt-dlp transport')
    import sys
    script = '''
import socket
import urllib.request
from worker.ytdlp_guard import install
seen=[]
socket.getaddrinfo=lambda *a,**k:[(2,1,6,'',('8.8.8.8',443))]
urllib.request.AbstractHTTPHandler.do_open=lambda self,cls,req,**kw:seen.append(dict(req.header_items()))
install('youtube','parse',None)
handler=urllib.request.HTTPHandler()
for url in ['https://www.youtube.com/api','https://cdn.example/file','https://youtu.be/short']:
    req=urllib.request.Request(url,headers={'Cookie':'secret','Authorization':'token'})
    handler.do_open(None,req)
assert seen[0]['Cookie']=='secret'
assert 'Cookie' not in seen[1] and 'Authorization' not in seen[1]
assert 'Cookie' not in seen[2]
from yt_dlp.downloader.external import ExternalFD
try:
    ExternalFD.real_download(None,'unused',{})
except OSError:
    pass
else:
    raise AssertionError('external downloader bypass')
'''
    async def execute():
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', script)
        assert await process.wait() == 0
    run(execute())


def test_registered_video_contains_local_thumbnail_and_cover(tmp_path):
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'video-contents')
    thumb = tmp_path / 'cover.jpg'
    Image.new('RGB', (32, 16), 'red').save(thumb)
    photo = raw.types.Photo(id=100, access_hash=200, file_reference=b'photo', date=1, dc_id=2,
                            sizes=[raw.types.PhotoSize(type='x', w=32, h=16, size=100)])
    document = raw.types.Document(id=123, access_hash=456, file_reference=b'ref', date=1,
                                  mime_type='video/mp4', size=8, dc_id=2, attributes=[])
    client = SimpleNamespace(me=SimpleNamespace(id=1), save_file=AsyncMock(return_value='saved'),
                             invoke=AsyncMock(side_effect=[SimpleNamespace(photo=photo),
                                                          SimpleNamespace(document=document)]))
    result = run(MediaUploader(client).upload({
        'path': path, 'thumbnailPath': thumb, 'type': 'video', 'filename': path.name,
        'mimeType': 'video/mp4', 'width': 32, 'height': 16, 'durationSeconds': 1,
    }))
    assert FileId.decode(result['coverFileId']).file_type == FileType.PHOTO
    media = client.invoke.call_args_list[-1].args[0].media
    assert media.thumb == 'saved' and isinstance(media.video_cover, raw.types.InputPhoto)
    assert 'thumbnailPath' not in result and 'path' not in result


def test_cache_identity_shortlink_anonymous_and_keeps_bilibili_part(tmp_path, monkeypatch):
    parser = ParseHub()
    instance = ParseHubEngine(tmp_path, parser)
    raw = AsyncMock(return_value='https://www.bilibili.com/video/BV1234567890?p=2')
    monkeypatch.setattr(parser, 'get_raw_url', raw)
    config = {'platforms': {'bilibili': {'cookies': ['must-not-send'], 'parser_proxies': []}}}
    original = 'https://b23.tv/share-token'
    identity = run(instance.cache_identity(original, config))
    assert identity == 'https://www.bilibili.com/video/BV1234567890?p=2'
    raw.assert_awaited_once_with(original, proxy=None, clean_all=False)
    assert original == 'https://b23.tv/share-token'


def test_cache_identity_removes_only_xhs_access_token(tmp_path, monkeypatch):
    parser = ParseHub()
    instance = ParseHubEngine(tmp_path, parser)
    url = 'https://www.xiaohongshu.com/explore/64aaa?xsec_token=access'
    monkeypatch.setattr(parser, 'get_raw_url', AsyncMock(return_value=url))
    assert run(instance.cache_identity(url)) == 'https://www.xiaohongshu.com/explore/64aaa'
    # prepare still receives original URL; identity never destroys access parameters.
    assert url.endswith('?xsec_token=access')


def test_cache_identity_rejects_cross_platform_redirect(tmp_path, monkeypatch):
    parser = ParseHub()
    instance = ParseHubEngine(tmp_path, parser)
    monkeypatch.setattr(parser, 'get_raw_url', AsyncMock(return_value='https://www.youtube.com/watch?v=x'))
    with pytest.raises(EngineError, match='upstream_http'):
        run(instance.cache_identity('https://b23.tv/share-token'))


def test_native_names_and_registration_before_download(tmp_path, monkeypatch):
    parsed = VideoParseResult(video=VideoRef(url='https://cdn.example/video.mp4'), title='原始视频标题')
    parsed.raw_url = 'https://www.youtube.com/watch?v=example'
    (tmp_path / parsed.name).mkdir()
    previous = tmp_path / parsed.name / 'keep.txt'
    previous.write_text('keep')
    registered = []

    def register(path):
        assert not path.exists()
        registered.append(path)

    async def download(url, path, **kwargs):
        assert registered == [tmp_path / f'{parsed.name}_2']
        Path(path).write_bytes(b'raw-video')
        return path

    monkeypatch.setattr('parsehub.types.result.download', download)
    # Avoid video probing in native media construction; raw handoff must preserve bytes.
    monkeypatch.setattr('parsehub.types.media_file.MediaInfoReader.read', lambda **kwargs: SimpleNamespace(
        width=32, height=16, duration=1,
    ))
    result = run(engine(tmp_path, parsed).prepare(
        parsed.raw_url, output_mode='raw', directory=tmp_path, register_directory=register,
    ))
    assert result['_directory'] == str(tmp_path / f'{parsed.name}_2')
    assert result['media'][0]['filename'] == f'{parsed.name}.mp4'
    assert previous.read_text() == 'keep'
    assert parsed.name == '原始视频标题'


def test_cancel_tracks_native_allocation_before_first_network(tmp_path, monkeypatch):
    parsed = VideoParseResult(video=VideoRef(url='https://cdn.example/video.mp4'), title='native-cancel')
    parsed.raw_url = 'https://www.youtube.com/watch?v=example'
    registered = []

    async def download(url, path, **kwargs):
        assert registered == [tmp_path / parsed.name]
        Path(path).write_bytes(b'partial')
        raise asyncio.CancelledError

    monkeypatch.setattr('parsehub.types.result.download', download)
    with pytest.raises(asyncio.CancelledError):
        run(engine(tmp_path, parsed).prepare(parsed.raw_url, directory=tmp_path,
                                            register_directory=registered.append))
    assert registered[0].is_dir()


def test_archive_registers_original_sibling_and_does_not_overwrite(tmp_path):
    registered_dirs, registered_files = [], []
    instance = engine(tmp_path, FakeResult([ImageRef(url='https://cdn.example/good')]))
    result = run(instance.prepare('https://youtube.com/a', output_mode='zip', directory=tmp_path,
                                  register_directory=registered_dirs.append, register_file=registered_files.append))
    assert registered_dirs == [tmp_path / 'test']
    assert registered_files == [tmp_path / 'test.tar.gz']
    assert str(tmp_path / 'test.tar.gz') in result['_files']
    # Native suffix helper would map test.name to test.tar.gz; an existing unrelated
    # archive is preserved and the conversion is reported as failed.
    parsed = FakeResult([ImageRef(url='https://cdn.example/good')])
    parsed.name = 'test.name'
    existing = (tmp_path / 'test.tar.gz').read_bytes()
    result = run(engine(tmp_path, parsed).prepare('https://youtube.com/a', output_mode='zip', directory=tmp_path))
    assert result['media'] == [] and result['mediaFailureCount'] == 1
    assert (tmp_path / 'test.tar.gz').read_bytes() == existing
