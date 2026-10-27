"""Authenticate at the route, then bound multipart input before spooling."""
from contextlib import asynccontextmanager

from fastapi import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser

from . import config


@asynccontextmanager
async def audio_form(request, check_disk):
    limit = config.MAX_UPLOAD_BYTES + 64 * 1024  # bounded multipart metadata
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "Файл превышает допустимый размер")
    error = None

    async def bounded_stream():
        nonlocal error
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            try:
                if total > limit:
                    raise HTTPException(413, "Файл превышает допустимый размер")
                check_disk(len(chunk))
            except HTTPException as exc:
                error = exc
                # Starlette closes all partially spooled files on this exception.
                raise MultiPartException(exc.detail) from exc
            yield chunk

    parser = MultiPartParser(request.headers, bounded_stream(), max_files=1, max_fields=1, max_part_size=64 * 1024)
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        if error:
            raise error
        raise HTTPException(400, exc.message) from exc
    try:
        yield form
    finally:
        await form.close()
