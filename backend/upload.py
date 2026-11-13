"""Authenticate at the route, then bound streaming form input before spooling."""
import asyncio
from contextlib import asynccontextmanager

import anyio

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser

from . import config


async def run_file_operation(func, *args, **kwargs):
    """Keep the form's spool alive until its worker finishes, even on cancellation."""
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(run_in_threadpool(func, *args, **kwargs))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError() from None
                raise
        if cancelled:
            raise asyncio.CancelledError()
        return result


@asynccontextmanager
async def bounded_form(request, *, limit: int, message: str, max_fields: int,
                       max_part_size: int = 64 * 1024, allow_urlencoded: bool = False, check_disk=None):
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, message)
    error = None

    async def bounded_stream():
        nonlocal error
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            try:
                if total > limit:
                    raise HTTPException(413, message)
                if check_disk is not None:
                    await run_in_threadpool(check_disk, len(chunk))
            except HTTPException as exc:
                error = exc
                # Starlette closes partial files on MultiPartException.
                raise MultiPartException(exc.detail) from exc
            yield chunk

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "multipart/form-data":
        parser = MultiPartParser(request.headers, bounded_stream(), max_files=1,
                                 max_fields=max_fields, max_part_size=max_part_size)
    elif content_type == "application/x-www-form-urlencoded" and allow_urlencoded:
        parser = FormParser(request.headers, bounded_stream())
    else:
        raise HTTPException(400, "Нужна форма с текстом или файлом")
    try:
        try:
            form = await parser.parse()
        except MultiPartException as exc:
            if error:
                raise error
            raise HTTPException(400, exc.message) from exc
    except BaseException:
        # Disconnect/cancellation is not a MultiPartException; close partial
        # spool files in those paths too (including disk/write failures).
        for file in getattr(parser, "_files_to_close_on_error", []):
            file.close()
        raise
    try:
        if not isinstance(parser, MultiPartParser) and len(form.multi_items()) > max_fields:
            raise HTTPException(400, "Слишком много полей формы")
        yield form
    finally:
        await form.close()


@asynccontextmanager
async def audio_form(request, check_disk):
    async with bounded_form(request, limit=config.MAX_UPLOAD_BYTES + 64 * 1024,
                            message="Файл превышает допустимый размер", max_fields=2,
                            check_disk=check_disk) as form:
        yield form
