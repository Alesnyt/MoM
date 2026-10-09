from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

meeting_id_var: ContextVar[str] = ContextVar("mom_meeting_id", default="")

_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs):
    record = _factory(*args, **kwargs)
    record.meeting = meeting_id_var.get() or "-"
    return record


logging.setLogRecordFactory(_record_factory)


@contextmanager
def meeting_scope(meeting_id: str) -> Iterator[None]:
    token = meeting_id_var.set(meeting_id or "")
    try:
        yield
    finally:
        meeting_id_var.reset(token)
