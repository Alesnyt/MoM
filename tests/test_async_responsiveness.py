import asyncio
import threading
from pathlib import Path

try:
    import httpx2 as httpx
except ModuleNotFoundError:
    import httpx
import pytest

from backend import calendar_service, config, keys, main, store, transcript_import


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    for name in ('UPLOAD_DIR','AUDIO_DIR','WHISPER_DIR'):
        monkeypatch.setattr(config,name,tmp_path/name)
    monkeypatch.setattr(main,'UPLOAD_DIR',config.UPLOAD_DIR)
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'mom.db')
    monkeypatch.setattr(config,'MIN_FREE_BYTES',0)
    store.init_db()
    monkeypatch.setitem(main.app.dependency_overrides,main.require_user,lambda: {'user_id':'u','email':'u@example.com'})
    monkeypatch.setitem(main.app.dependency_overrides,main.require_admin,lambda: {'username':'admin'})
    monkeypatch.setattr(main,'_health',lambda **kw: {'ok':True})
    monkeypatch.setattr(keys,'snapshot',lambda: {'connected':True})
    monkeypatch.setattr(main,'ffmpeg_available',lambda: True)
    monkeypatch.setattr(main,'looks_like_audio',lambda p: True)


async def responsive_call(call, entered, release):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://testserver') as client:
        request = asyncio.create_task(call(client))
        try:
            for _ in range(200):
                if entered.is_set(): break
                await asyncio.sleep(.005)
            assert entered.is_set()
            health = await asyncio.wait_for(client.get('/api/health'), .75)
            assert health.status_code == 200
            release.set()
            result = await asyncio.wait_for(request,3)
            assert result.status_code == 200, result.text
        finally:
            release.set()
            await asyncio.gather(request,return_exceptions=True)


@pytest.mark.parametrize('stage', ['calendar','disk','parse','database','audio-copy','key-save'])
def test_slow_upload_and_setting_work_does_not_block_http(app_db,monkeypatch,stage):
    entered,release=threading.Event(),threading.Event()
    def wait():
        entered.set()
        assert release.wait(3), 'Blocking operation stalled the HTTP event loop'
    call=lambda client: client.post('/api/meetings/transcript',data={'text':'Анна: План утверждён.'})
    if stage == 'calendar':
        original=main._calendar_selection
        def slow(*args): wait(); return original(*args)
        monkeypatch.setattr(main,'_calendar_selection',slow)
    elif stage == 'disk':
        monkeypatch.setattr(main,'_disk_or_507',lambda *a: wait())
        call=lambda client: client.post('/api/meetings',files={'file':('record.wav',b'RIFF'+b'x'*100)})
    elif stage == 'parse':
        original=transcript_import.parse_transcript
        def slow(*args): wait(); return original(*args)
        monkeypatch.setattr(transcript_import,'parse_transcript',slow)
    elif stage == 'database':
        original=store.create_text_meeting
        def slow(*args,**kw): wait(); return original(*args,**kw)
        monkeypatch.setattr(store,'create_text_meeting',slow)
    elif stage == 'audio-copy':
        original=Path.open
        def slow(path,*args,**kw):
            if path.parent == config.UPLOAD_DIR and args and args[0] == 'wb': wait()
            return original(path,*args,**kw)
        monkeypatch.setattr(Path,'open',slow)
        call=lambda client: client.post('/api/meetings',files={'file':('record.wav',b'RIFF'+b'x'*100)})
    else:
        monkeypatch.setattr(config,'set_openai_api_key',lambda key: wait())
        call=lambda client: client.post('/api/settings?verify=false',json={'openai_api_key':'local-test-key-with-enough-length'})
    asyncio.run(responsive_call(call,entered,release))


def test_calendar_cleanup_does_not_block_loop(app_db,monkeypatch):
    entered,release=threading.Event(),threading.Event()
    def cleanup(): entered.set(); assert release.wait(3)
    monkeypatch.setattr(calendar_service,'cleanup',cleanup)
    async def exercise():
        stop=asyncio.Event()
        task=asyncio.create_task(calendar_service.run_cleanup(stop))
        try:
            for _ in range(200):
                if entered.is_set(): break
                await asyncio.sleep(.005)
            assert entered.is_set()
            release.set(); stop.set()
            await asyncio.wait_for(task,2)
        finally:
            release.set(); stop.set()
            await asyncio.gather(task,return_exceptions=True)
    asyncio.run(exercise())


def test_cancelled_upload_keeps_spool_open_until_worker_finishes():
    import tempfile
    from backend import upload
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    file = tempfile.SpooledTemporaryFile()
    file.write(b'audio'); file.seek(0)
    def copy():
        entered.set()
        assert release.wait(3)
        assert file.read() == b'audio'
        finished.set()
    async def request():
        try: await upload.run_file_operation(copy)
        finally: file.close()
    async def exercise():
        task = asyncio.create_task(request())
        try:
            for _ in range(200):
                if entered.is_set(): break
                await asyncio.sleep(.005)
            assert entered.is_set()
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(.01)
                assert not file.closed
            release.set()
            with pytest.raises(asyncio.CancelledError): await task
            assert finished.is_set() and file.closed
        finally:
            release.set()
            await asyncio.gather(task,return_exceptions=True)
    asyncio.run(exercise())


@pytest.mark.parametrize('stage', ['prompts', 'read', 'progress', 'complete'])
def test_pipeline_database_and_word_work_does_not_block_http(app_db, monkeypatch, stage):
    from backend import pipeline, prompts
    user = store.create_user('background@example.test', 'unused')
    store.create_meeting('background', 'Встреча', 'test.wav', user['id'])
    store.update_meeting('background', status='analyzing', transcript='Анна: План утверждён.')
    monkeypatch.setattr(config, 'get_api_key', lambda: 'test')
    monkeypatch.setattr(pipeline, 'make_client', lambda key: object())
    async def analyze(*args, **kwargs):
        return {'summary': 'План утверждён.', 'participants': []}
    monkeypatch.setattr(pipeline, '_analyze_with_heartbeat', analyze)
    entered, release = threading.Event(), threading.Event()
    module, name = {
        'prompts': (prompts, 'snapshot'), 'read': (store, 'get_meeting'),
        'progress': (store, 'update_meeting'), 'complete': (pipeline, 'complete_protocol'),
    }[stage]
    original = getattr(module, name)
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3), 'Meeting work blocked HTTP requests'
        return original(*args, **kwargs)
    monkeypatch.setattr(module, name, slow)
    async def call(client):
        await pipeline.process_meeting('background')
        return await client.get('/api/health')
    asyncio.run(responsive_call(call, entered, release))
    assert store.get_meeting('background')['status'] == 'done'


@pytest.mark.parametrize('valid', [True, False])
def test_audio_upload_invalidates_recording_size_on_success_and_rejection(app_db, monkeypatch, valid):
    from backend import disk
    payload = b'RIFF' + b'x' * 100
    disk.invalidate_recordings()
    assert disk._recordings_bytes() == 0
    def inspect_file(path):
        # Monitoring may observe the complete file just before validation.
        disk.invalidate_recordings()
        assert disk._recordings_bytes() == len(payload)
        return valid
    monkeypatch.setattr(main, 'looks_like_audio', inspect_file)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver') as client:
            response = await client.post('/api/meetings', files={'file': ('record.wav', payload)})
            assert response.status_code == (200 if valid else 400), response.text
    asyncio.run(scenario())
    assert disk._recordings_bytes() == (len(payload) if valid else 0)
