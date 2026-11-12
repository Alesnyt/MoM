"""Corporate directory, glossary and term discovery administration routes."""
import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.datastructures import UploadFile as StarletteUploadFile

from . import corporate_context, directory_scope, glossary, ldap_auth, store, term_discovery, upload
from .api_schemas import (
    ContextSettingsIn, DirectorySettingsIn, DirectoryPolicyIn, SpeakerSearchIn, GlossaryImportIn, GlossaryEnabledIn, TermScheduleIn, TermReviewIn
)


def create_router(require_admin) -> APIRouter:
    """Inject the shared authentication dependency without importing the app."""
    router = APIRouter()

    @router.get("/api/admin/context")
    def get_context(q: str = Query(default="", max_length=200), _admin: dict = Depends(require_admin)) -> dict:
        return corporate_context.snapshot(q)


    @router.post("/api/admin/context/settings")
    def save_context(payload: ContextSettingsIn, admin: dict = Depends(require_admin)) -> dict:
        return corporate_context.save_settings(payload.enabled, payload.sync_ad, admin["username"],
                                               sync_time=payload.sync_time, use_ad=payload.use_ad)


    @router.get("/api/admin/directory/cache")
    def get_directory(_admin: dict = Depends(require_admin)) -> dict:
        return corporate_context.snapshot()


    @router.post("/api/admin/directory/settings")
    def save_directory_settings(payload: DirectorySettingsIn, admin: dict = Depends(require_admin)) -> dict:
        return corporate_context.save_settings(None, payload.sync_ad, admin["username"], sync_time=payload.sync_time)


    @router.post("/api/admin/directory/sync", status_code=202)
    @router.post("/api/admin/context/sync", status_code=202)
    def sync_context(admin: dict = Depends(require_admin)) -> dict:
        try:
            return corporate_context.request_sync(admin["username"])
        except (ldap_auth.LdapError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


    @router.post("/api/admin/directory/policy")
    @router.post("/api/admin/context/directory")
    def save_context_directory(payload: DirectoryPolicyIn, admin: dict = Depends(require_admin)) -> dict:
        try:
            policy = directory_scope.save_sync_policy(payload.fields, payload.domains)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.record_audit(admin["username"], "context.directory", detail=json.dumps(policy, ensure_ascii=False))
        return corporate_context.snapshot()


    @router.post("/api/admin/directory/search")
    @router.post("/api/admin/context/search")
    def save_speaker_search(payload: SpeakerSearchIn, admin: dict = Depends(require_admin)) -> dict:
        try:
            return corporate_context.save_search_mode(payload.mode, admin["username"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


    @router.post("/api/admin/context/import")
    async def import_context(request: Request, admin: dict = Depends(require_admin)) -> dict:
        async with upload.bounded_form(request, limit=corporate_context.MAX_FILE_BYTES + 65536,
                                       message="Словарь больше 2 МБ", max_fields=0) as form:
            file = form.get("file")
            if not isinstance(file, StarletteUploadFile):
                raise HTTPException(status_code=400, detail="Выберите Excel или CSV со словарём")
            content = await file.read(corporate_context.MAX_FILE_BYTES + 1)
            try:
                return await asyncio.to_thread(corporate_context.import_dictionary, file.filename or "", content, admin["username"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc


    @router.get("/api/admin/glossaries")
    def get_glossaries(q: str = Query(default="", max_length=200), glossary_id: str = Query(default="", max_length=64),
                       offset: int = Query(default=0, ge=0, le=100000), _admin: dict = Depends(require_admin)) -> dict:
        return glossary.catalog(q, glossary_id, offset)


    @router.post("/api/admin/glossaries/preview")
    async def preview_glossary(request: Request, admin: dict = Depends(require_admin)) -> dict:
        async with upload.bounded_form(request, limit=glossary.MAX_FILE_BYTES + 65536,
                                       message="Глоссарий больше 25 МБ", max_fields=2) as form:
            file = form.get("file")
            if not isinstance(file, StarletteUploadFile):
                raise HTTPException(status_code=400, detail="Выберите Excel или CSV")
            content = await file.read(glossary.MAX_FILE_BYTES + 1)
            try:
                return await asyncio.to_thread(glossary.preview, file.filename or "", content, admin["username"],
                                               glossary_id=str(form.get("glossary_id") or ""), sheet=str(form.get("sheet") or ""))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc


    @router.delete("/api/admin/glossaries/preview/{upload_id}")
    def discard_glossary_preview(upload_id: str, _admin: dict = Depends(require_admin)) -> dict:
        with store.connect() as conn:
            conn.execute("DELETE FROM glossary_imports WHERE id=? AND status='preview'", (upload_id,))
        return {"ok": True}


    @router.post("/api/admin/glossaries/import", status_code=202)
    def import_glossary(payload: GlossaryImportIn, admin: dict = Depends(require_admin)) -> dict:
        try:
            return glossary.queue_import(payload.upload_id, payload.name, payload.mapping, payload.sheet, admin["username"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


    @router.post("/api/admin/glossaries/{glossary_id}/enabled")
    def enable_glossary(glossary_id: str, payload: GlossaryEnabledIn, admin: dict = Depends(require_admin)) -> dict:
        try:
            return glossary.set_enabled(glossary_id, payload.enabled, admin["username"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


    @router.get('/api/admin/term-discovery')
    def get_term_discovery(state: Literal['pending','approved','rejected','deferred'] = 'pending',
                           offset: int = Query(default=0, ge=0, le=100000), _admin: dict = Depends(require_admin)):
        return term_discovery.snapshot(state,offset)


    @router.post('/api/admin/term-discovery/settings')
    def save_term_schedule(payload: TermScheduleIn, admin: dict = Depends(require_admin)):
        term_discovery.settings(**payload.model_dump(),actor=admin['username'])
        return {'ok':True}


    @router.post('/api/admin/term-discovery/scan', status_code=202)
    def scan_terms(admin: dict = Depends(require_admin)):
        try:
            term_discovery.queue(admin['username'])
            return {'status':'queued'}
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc


    @router.post('/api/admin/term-discovery/stop')
    def stop_term_scan(admin: dict = Depends(require_admin)):
        with store.connect() as conn:
            conn.execute("UPDATE term_discovery SET status='idle',owner=NULL,lease=0 WHERE status IN ('queued','running','paused')")
        store.record_audit(admin['username'],'terms.stop')
        return {'ok':True}


    @router.post('/api/admin/term-discovery/review')
    def review_terms(payload: TermReviewIn, admin: dict = Depends(require_admin)):
        try:
            term_discovery.review(**payload.model_dump(),actor=admin['username'])
            return {'ok':True}
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc


    @router.get('/api/admin/term-discovery/evidence/{evidence_id}/audio')
    def term_audio(evidence_id: int, _admin: dict = Depends(require_admin)):
        import math
        import subprocess
        with store.connect() as conn:
            row = conn.execute('''SELECT e.*,m.filename FROM term_evidence e JOIN meetings m ON m.id=e.meeting_id
                WHERE e.id=?''',(evidence_id,)).fetchone()
        if not row or row['start'] is None:
            raise HTTPException(404,'Для этой реплики нет аудиозаписи или таймкода')
        source = term_discovery.audio_source(row['meeting_id'],row['filename'])
        if not source or not math.isfinite(row['start']):
            raise HTTPException(404,'Запись уже удалена из архива')
        start = max(0,row['start']-2)
        end = row['end'] if row['end'] is not None and math.isfinite(row['end']) else start+30
        try:
            result = subprocess.run(['ffmpeg','-v','error','-nostdin','-ss',str(start),'-i',str(source),
                                     '-t',str(min(60,max(5,end-start+2))),'-vn','-ac','1','-ar','16000',
                                     '-f','mp3','pipe:1'],capture_output=True,timeout=20)
        except (OSError,subprocess.TimeoutExpired) as exc:
            raise HTTPException(503,'Не удалось подготовить фрагмент записи') from exc
        if result.returncode or not result.stdout:
            raise HTTPException(503,'Не удалось прочитать аудиозапись')
        return Response(result.stdout,media_type='audio/mpeg',headers={'Cache-Control':'no-store'})


    return router
