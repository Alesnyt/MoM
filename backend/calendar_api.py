"""Personal Exchange calendar and administrator connection settings."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from typing import Literal

from . import auth, calendar_service as calendar
from .exchange_ews import ExchangeError


class ExchangeSettingsIn(BaseModel):
    enabled: bool
    url: str = Field(default='', max_length=2000)
    auth_mode: Literal['ntlm','basic'] = 'ntlm'


class ExchangeLoginIn(BaseModel):
    username: str = Field(min_length=1,max_length=254)
    password: SecretStr = Field(min_length=1,max_length=1024)


def create_router(require_user, require_admin, rate_limit):
    router = APIRouter()

    @router.get('/api/settings/exchange')
    def settings(_admin=Depends(require_admin)):
        return calendar.settings()

    @router.post('/api/settings/exchange')
    def save(payload: ExchangeSettingsIn, admin=Depends(require_admin)):
        try: return calendar.save_settings(**payload.model_dump(),actor=admin['username'])
        except ExchangeError as exc: raise HTTPException(exc.status,str(exc)) from exc

    @router.get('/api/calendar/status')
    def status(request: Request,user=Depends(require_user)):
        return calendar.status(request.cookies.get(auth.USER_COOKIE),user['user_id'])

    @router.post('/api/calendar/connect')
    def connect(payload: ExchangeLoginIn,request: Request,user=Depends(require_user)):
        rate_limit(request,'exchange-connect')
        try:
            return calendar.connect(request.cookies.get(auth.USER_COOKIE),user['user_id'],
                                    payload.username,payload.password.get_secret_value())
        except ExchangeError as exc: raise HTTPException(exc.status,str(exc)) from exc

    @router.delete('/api/calendar/connection')
    def disconnect(request: Request,_user=Depends(require_user)):
        calendar.disconnect(request.cookies.get(auth.USER_COOKIE))
        return {'connected':False}

    @router.get('/api/calendar/events')
    def events(request: Request,start: datetime,end: datetime,refresh: bool=False,user=Depends(require_user)):
        try:
            return calendar.events(request.cookies.get(auth.USER_COOKIE),user['user_id'],start,end,refresh)
        except ExchangeError as exc: raise HTTPException(exc.status,str(exc)) from exc

    @router.get('/api/calendar/events/{event_id}')
    def event(event_id: str, request: Request,user=Depends(require_user)):
        if len(event_id)>80: raise HTTPException(400,'Некорректное событие календаря.')
        try: return calendar.detail(request.cookies.get(auth.USER_COOKIE),user['user_id'],event_id)
        except ExchangeError as exc: raise HTTPException(exc.status,str(exc)) from exc

    return router
