import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest

from backend import config, keys


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(keys, '_status', {})
    monkeypatch.setattr(config, 'OPENAI_API_KEY', 'test-key')
    monkeypatch.setattr(config, 'OPENAI_BASE_URL', 'https://models.example.test/v1')
    monkeypatch.setattr(config, 'OPENAI_MODEL', 'test-model')
    client = SimpleNamespace(close=AsyncMock(), models=SimpleNamespace(list=AsyncMock(
        return_value=SimpleNamespace(data=[SimpleNamespace(id='test-model')]))))
    monkeypatch.setattr(keys, 'make_client', lambda *args, **kw: client)
    monkeypatch.setattr(keys, '_ping', AsyncMock())
    return client


@pytest.mark.parametrize('operation', ['verify', 'models'])
@pytest.mark.parametrize('fail', [False, True])
def test_provider_clients_close_on_success_and_failure(provider, monkeypatch, operation, fail):
    if fail:
        if operation == 'verify':
            monkeypatch.setattr(keys, '_ping', AsyncMock(side_effect=ValueError('provider failed')))
        else:
            provider.models.list.side_effect = ValueError('provider failed')
    async def scenario():
        if operation == 'verify':
            result = await keys.verify_key()
            assert result['connected'] is not fail
        elif fail:
            with pytest.raises(ValueError, match='provider failed'):
                await keys.list_chat_models()
        else:
            assert await keys.list_chat_models() == ['test-model']
    asyncio.run(scenario())
    provider.close.assert_awaited_once()


@pytest.mark.parametrize('operation', ['verify', 'models'])
def test_disconnect_cancellation_finishes_provider_cleanup(provider, monkeypatch, operation):
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        async def wait(*args):
            entered.set()
            await asyncio.Future()
        async def close():
            # An already cancelled AnyIO scope must not interrupt pool cleanup.
            await asyncio.sleep(0)
            closed.set()
        provider.close.side_effect = close
        monkeypatch.setattr(keys, '_ping', wait)
        provider.models.list.side_effect = wait
        async with anyio.create_task_group() as group:
            group.start_soon(keys.verify_key if operation == 'verify' else keys.list_chat_models)
            await entered.wait()
            group.cancel_scope.cancel()
        assert closed.is_set()
        assert not keys.snapshot()['connected']
        assert keys.snapshot()['checked_at'] is None
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['verify', 'models'])
def test_client_close_error_does_not_replace_success(provider, operation):
    provider.close.side_effect = OSError('close failed')
    async def scenario():
        if operation == 'verify':
            assert (await keys.verify_key())['connected']
        else:
            assert await keys.list_chat_models() == ['test-model']
    asyncio.run(scenario())
    provider.close.assert_awaited_once()


def test_direct_task_cancellation_closes_client(provider, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        async def wait(*args):
            entered.set()
            await asyncio.Future()
        monkeypatch.setattr(keys, '_ping', wait)
        task = asyncio.create_task(keys.verify_key())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        provider.close.assert_awaited_once()
        assert keys.snapshot()['checked_at'] is None
    asyncio.run(scenario())
