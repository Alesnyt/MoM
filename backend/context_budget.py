"""Offline protocol token accounting; no model weights or remote downloads."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Callable

from . import config


def estimate_tokens(text: str) -> int:
    # A cautious estimate for Latin/Cyrillic text, NOT an exact tokenizer for an
    # arbitrary provider. Other scripts/symbols keep the UTF-8 byte upper bound.
    # analyze_long_transcript handles provider context rejections by splitting.
    return sum(1 if ord(char) < 128 or '\u0400' <= char <= '\u04ff'
               else len(char.encode('utf-8')) for char in text)


@lru_cache(maxsize=1)
def _local_tokenizer(path: str, modified: int, size: int):
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(path)
    # Exported tokenizer settings must never silently truncate a count.
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def token_counter() -> Callable[[str], int]:
    path = config.PROTOCOL_TOKENIZER_PATH
    if not path:
        return estimate_tokens
    try:
        file = Path(path).expanduser().resolve(strict=True)
        stat = file.stat()
        tokenizer = _local_tokenizer(str(file), stat.st_mtime_ns, stat.st_size)
    except Exception as exc:
        raise ValueError('Не удалось загрузить локальный tokenizer.json. Проверьте PROTOCOL_TOKENIZER_PATH.') from exc
    return lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids)


def output_tokens() -> int:
    return min(8192, config.PROTOCOL_CONTEXT_TOKENS // 4)


def context_rejected(exc: Exception) -> bool:
    body = getattr(exc, 'body', None)
    code = getattr(exc, 'code', None)
    if isinstance(body, dict):
        error = body.get('error', body)
        if isinstance(error, dict):
            code = error.get('code', code)
    if isinstance(code, str) and code in {'context_length_exceeded', 'max_context_length_exceeded'}:
        return True
    message = str(exc).lower()
    return any(term in message for term in (
        'maximum context length', 'context window exceeded', 'context length exceeded',
        'input tokens exceed', 'too many input tokens',
    ))
