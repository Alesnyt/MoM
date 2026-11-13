import asyncio
import json
from types import SimpleNamespace

import pytest
from openai import BadRequestError

from backend import analyze, config, context_budget, prompts


def response(data, finish_reason='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
        message=SimpleNamespace(content=json.dumps(data)))])


def client_for(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def rejected(message='maximum context length exceeded', code='context_length_exceeded'):
    return BadRequestError(message, body={'error': {'code': code}},
        response=SimpleNamespace(request=SimpleNamespace(), status_code=400, headers={}))


@pytest.fixture(autouse=True)
def no_local_configuration(monkeypatch):
    monkeypatch.setattr(config, 'PROTOCOL_TOKENIZER_PATH', '')


@pytest.mark.parametrize('window', [8192, 16384])
def test_default_prompt_fits_small_window_without_discarding_russian_transcript(monkeypatch, window):
    monkeypatch.setattr(config, 'PROTOCOL_CONTEXT_TOKENS', window)
    monkeypatch.setattr(prompts, 'snapshot', lambda: {'protocol_prompt': prompts.PROTOCOL_PROMPT})
    text = '[реплика 1] Анна: Согласовали запуск, Борис подготовит план завтра.'
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        assert sum(context_budget.estimate_tokens(m['content']) for m in kwargs['messages']) + kwargs['max_tokens'] + 64 <= window
        assert text in kwargs['messages'][1]['content']
        return response({'decisions': ['Согласовали запуск'], 'decision_sources': [{'decision': 'Согласовали запуск', 'turns': [1]}]})
    result = asyncio.run(analyze.analyze_long_transcript(client_for(create), text, 'Запуск'))
    assert len(calls) == 1
    assert result['decision_sources'][0]['turns'] == [1]


def test_chunks_preserve_every_character_and_repeat_source_for_long_turn():
    label = '[реплика 12] '
    text = 'Анна: ' + 'абв 中文 😀 word\t' * 300 + '\n'
    chunks = analyze._transcript_chunks(label + text, 200)
    assert len(chunks) > 2
    assert all(c.startswith(label) and context_budget.estimate_tokens(c) <= 200 for c in chunks)
    assert ''.join(c.removeprefix(label) for c in chunks) == text
    plain = 'abc\r\nДлинная фраза\n\n' * 20
    assert ''.join(analyze._transcript_chunks(plain, 50)) == plain


def test_local_tokenizer_never_truncates_or_downloads_and_reloads_updated_file(tmp_path, monkeypatch):
    from tokenizers import Tokenizer, models, pre_tokenizers
    tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'слово': 1}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.enable_truncation(max_length=2)
    tokenizer.enable_padding(length=10)
    path = tmp_path / 'tokenizer.json'
    tokenizer.save(str(path))
    monkeypatch.setattr(config, 'PROTOCOL_TOKENIZER_PATH', str(path))
    count = context_budget.token_counter()
    assert count('слово') == 1
    assert count(' '.join(['слово'] * 20)) == 20
    assert context_budget._local_tokenizer.cache_info().currsize == 1
    tokenizer.pre_tokenizer = None
    tokenizer.save(str(path))
    assert context_budget.token_counter()('слово слово') == 1
    monkeypatch.setattr(config, 'PROTOCOL_TOKENIZER_PATH', str(path) + '.missing')
    with pytest.raises(ValueError, match='локальный tokenizer'):
        context_budget.token_counter()


def test_provider_context_rejection_retries_only_failed_parts_and_retains_sources(monkeypatch):
    monkeypatch.setattr(config, 'PROTOCOL_CONTEXT_TOKENS', 4096)
    monkeypatch.setattr(prompts, 'snapshot', lambda: {'protocol_prompt': 'Return JSON'})
    first = '[реплика 1] Анна: ' + 'А' * 1600 + '\n'
    second = '[реплика 2] Борис: ' + 'Б' * 1600
    calls = []
    rejected_once = False
    async def create(**kwargs):
        nonlocal rejected_once
        body = kwargs['messages'][1]['content']
        calls.append(body)
        if first in body:
            return response({'decisions': ['Запуск'], 'decision_sources': [{'decision': 'Запуск', 'turns': [1]}]})
        if second in body and not rejected_once:
            rejected_once = True
            raise rejected()
        if '[реплика 2]' in body:
            return response({'action_items': [{'task': 'План', 'assignee': 'Борис', 'source_turns': [2]}]})
        return response({'summary': 'Итоги'})
    result = asyncio.run(analyze.analyze_long_transcript(client_for(create), first + second, None))
    assert rejected_once
    assert sum(first in call for call in calls) == 1
    assert result['decision_sources'] == [{'decision': 'Запуск', 'turns': [1]}]
    assert result['action_items'][0]['source_turns'] == [2]
    pieces = [body.split('[реплика 2] ', 1)[1] for body in calls if '[реплика 2] ' in body and second not in body]
    assert ''.join(pieces) == second.removeprefix('[реплика 2] ')


def test_merge_context_rejection_preserves_all_intermediate_results(monkeypatch):
    monkeypatch.setattr(config, 'PROTOCOL_CONTEXT_TOKENS', 4096)
    monkeypatch.setattr(prompts, 'snapshot', lambda: {'protocol_prompt': 'Return JSON'})
    calls = []
    async def create(**kwargs):
        body = kwargs['messages'][1]['content']
        calls.append(body)
        if analyze._MERGE_INSTRUCTIONS in body:
            raise rejected()
        turn = 1 if '[реплика 1]' in body else 2
        return response({'decisions': [f'Решение {turn}'], 'decision_sources': [{'decision': f'Решение {turn}', 'turns': [turn]}]})
    text = '\n'.join(f'[реплика {i}] ' + 'Я' * 1600 for i in (1, 2))
    result = asyncio.run(analyze.analyze_long_transcript(client_for(create), text, None))
    assert len(calls) == 3
    assert result['decisions'] == ['Решение 1', 'Решение 2']
    assert [s['turns'] for s in result['decision_sources']] == [[1], [2]]


def test_context_retries_are_bounded_and_other_errors_not_retried(monkeypatch):
    monkeypatch.setattr(config, 'PROTOCOL_CONTEXT_TOKENS', 4096)
    monkeypatch.setattr(prompts, 'snapshot', lambda: {'protocol_prompt': 'Return JSON'})
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        raise rejected()
    with pytest.raises(ValueError, match='контекст'):
        asyncio.run(analyze.analyze_long_transcript(client_for(create), 'Я' * 1500, None))
    assert len(calls) <= 7
    calls.clear()
    async def bad_model(**kwargs):
        calls.append(kwargs)
        raise rejected('unknown model', 'model_not_found')
    with pytest.raises(BadRequestError):
        asyncio.run(analyze.analyze_long_transcript(client_for(bad_model), 'Я' * 1500, None))
    assert len(calls) == 1


def test_truncated_protocol_is_not_published(monkeypatch):
    monkeypatch.setattr(prompts, 'snapshot', lambda: {'protocol_prompt': 'Return JSON'})
    async def create(**kwargs):
        return response({'summary': 'Valid JSON but incomplete result'}, 'length')
    with pytest.raises(ValueError, match='лимит ответа'):
        asyncio.run(analyze.analyze_long_transcript(client_for(create), 'Текст', None))


def test_local_tokenizer_counts_full_requests_with_default_prompt_in_4096_window(tmp_path, monkeypatch):
    from tokenizers import Tokenizer, models, pre_tokenizers
    tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    path = tmp_path / 'tokenizer.json'
    tokenizer.save(str(path))
    monkeypatch.setattr(config, 'PROTOCOL_TOKENIZER_PATH', str(path))
    monkeypatch.setattr(config, 'PROTOCOL_CONTEXT_TOKENS', 4096)
    monkeypatch.setattr(prompts, 'snapshot', lambda: {'protocol_prompt': prompts.PROTOCOL_PROMPT})
    text = '[реплика 1] Анна: Подготовлю план завтра.'
    async def create(**kwargs):
        used = sum(len(tokenizer.encode(m['content']).ids) for m in kwargs['messages'])
        assert used + kwargs['max_tokens'] + 64 <= 4096
        assert text in kwargs['messages'][1]['content']
        return response({'summary': 'Готово'})
    result = asyncio.run(analyze.analyze_long_transcript(client_for(create), text, 'План'))
    assert result['summary'] == 'Готово'


def test_oversized_system_prompt_fails_before_provider_call(monkeypatch):
    monkeypatch.setattr(config, 'PROTOCOL_CONTEXT_TOKENS', 4096)
    monkeypatch.setattr(prompts, 'snapshot', lambda: {'protocol_prompt': 'x' * 4096})
    with pytest.raises(ValueError, match='локальный токенизатор'):
        asyncio.run(analyze.analyze_long_transcript(object(), 'Текст', None))
