"""Progress persistence and monitoring remain ordered under concurrent work."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import pytest

from backend import background, disk, pipeline, retention


def test_offload_keeps_context_and_reports_error():
    context = ContextVar('test-context', default='missing')
    async def scenario():
        context.set('meeting')
        assert await background.run_sync(context.get) == 'meeting'
        def fail(): raise ValueError('write failed')
        with pytest.raises(ValueError, match='write failed'):
            await background.run_sync(fail)
    asyncio.run(scenario())


def test_progress_does_not_advance_on_failed_write_or_go_backwards(monkeypatch):
    writes = []
    def write(mid, **fields):
        if not writes:
            writes.append(None)
            raise OSError('database unavailable')
        writes.append(fields['progress'])
    monkeypatch.setattr(pipeline.store, 'update_meeting', write)
    progress = pipeline._Progress('progress')
    with pytest.raises(OSError):
        progress.set(93, 'same message')
    assert progress.last_pct == -1
    progress.set(93, 'same message')
    progress.set(88, 'heartbeat', force=True)
    assert writes == [None, 93, 93]


def test_progress_writes_are_serialized(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    writes = []
    def write(mid, **fields):
        if fields['progress'] == 90:
            entered.set()
            assert release.wait(3)
        writes.append(fields['progress'])
    monkeypatch.setattr(pipeline.store, 'update_meeting', write)
    progress = pipeline._Progress('progress')
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(progress.set, 90, 'first')
        try:
            assert entered.wait(2)
            second = pool.submit(progress.set, 95, 'second')
            assert writes == []
        finally:
            release.set()
        first.result(timeout=2)
        second.result(timeout=2)
    assert writes == [90, 95]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    for name in ('UPLOAD_DIR', 'AUDIO_DIR'):
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setattr(disk.config, name, path)
    disk.invalidate_recordings()
    yield
    disk.invalidate_recordings()


def test_parallel_monitoring_shares_one_scan(archive, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    scans = []
    def scan(path):
        scans.append(path)
        entered.set()
        assert release.wait(3)
        return 5
    monkeypatch.setattr(disk, '_dir_size', scan)
    with ThreadPoolExecutor(6) as pool:
        first = pool.submit(disk._recordings_bytes)
        try:
            assert entered.wait(2)
            others = [pool.submit(disk._recordings_bytes) for _ in range(5)]
        finally:
            release.set()
        assert [f.result(timeout=2) for f in [first, *others]] == [10] * 6
    assert len(scans) == 2  # One pass per directory, including cached zero sizes.


def test_invalidation_during_scan_cannot_republish_stale_cache(archive, monkeypatch):
    scans = []
    def scan(path):
        scans.append(path)
        if len(scans) == 1:
            # A concurrent file mutation must not block behind the scan lock.
            disk.invalidate_recordings()
        return len(scans)
    monkeypatch.setattr(disk, '_dir_size', scan)
    assert disk._recordings_bytes() == 3
    assert disk._recordings_bytes() == 7
    assert disk._recordings_bytes() == 7
    assert len(scans) == 4


def test_cache_ttl_starts_after_scan_and_keys_directories(archive, monkeypatch, tmp_path):
    clock = [100.]
    scans = []
    def scan(path):
        scans.append(path)
        clock[0] += 20
        return 0
    monkeypatch.setattr(disk.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(disk, '_dir_size', scan)
    assert disk._recordings_bytes() == 0
    clock[0] += 29
    assert disk._recordings_bytes() == 0 and len(scans) == 2
    clock[0] += 1
    disk._recordings_bytes()
    assert len(scans) == 4
    monkeypatch.setattr(disk.config, 'UPLOAD_DIR', tmp_path / 'other')
    disk._recordings_bytes()
    assert len(scans) == 6


def test_archive_deletion_invalidates_sizes_immediately(archive):
    (disk.config.UPLOAD_DIR / 'meeting.webm').write_bytes(b'input')
    (disk.config.AUDIO_DIR / 'meeting.wav').write_bytes(b'prepared')
    assert disk._recordings_bytes() == 13
    retention.purge_files('meeting')
    assert disk._recordings_bytes() == 0


@pytest.mark.parametrize('failure', [None, ValueError('provider failed'), asyncio.CancelledError()])
def test_heartbeat_database_failure_cannot_replace_model_outcome(monkeypatch, failure):
    from types import SimpleNamespace
    entered = threading.Event()
    def fail_progress(*args, **kwargs):
        entered.set()
        raise OSError('temporary progress write failure')
    async def analyze(*args, **kwargs):
        assert await asyncio.to_thread(entered.wait, 2)
        if failure is not None:
            raise failure
        return {'summary': 'Успешный результат'}
    monkeypatch.setattr(pipeline, 'analyze_long_transcript', analyze)
    async def scenario():
        call = pipeline._analyze_with_heartbeat(object(), 'Текст', 'Встреча',
                                               SimpleNamespace(set=fail_progress), 'Модель')
        if failure is None:
            assert await call == {'summary': 'Успешный результат'}
        else:
            with pytest.raises(type(failure)) as caught:
                await call
            assert caught.value is failure
    asyncio.run(scenario())


@pytest.mark.parametrize('stopped', [False, True])
def test_protocol_part_progress_is_advisory_but_stop_is_not(monkeypatch, stopped):
    from types import SimpleNamespace
    from backend import control
    def report(*args, **kwargs):
        if stopped:
            raise control.ProcessingStopped()
        raise OSError('progress database unavailable')
    async def analyze(*args, on_part, **kwargs):
        await background.run_sync(on_part, 1, 2)
        return {'summary': 'Готово'}
    monkeypatch.setattr(pipeline, 'analyze_long_transcript', analyze)
    async def scenario():
        result = pipeline._analyze_with_heartbeat(object(), 'Текст', 'Встреча',
                                                 SimpleNamespace(set=report), 'Модель')
        if stopped:
            with pytest.raises(control.ProcessingStopped):
                await result
        else:
            assert await result == {'summary': 'Готово'}
    asyncio.run(scenario())
