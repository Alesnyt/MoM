"""Read-only Exchange contract, personal isolation and selected-context integration."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from defusedxml.common import DefusedXmlException
from backend import auth, calendar_service as cal, config, main, store, exchange_ews as ews

LIST = '''<t:CalendarItem><t:ItemId Id="occurrence-1" ChangeKey="v1"/><t:Subject>План DWH &amp; BI</t:Subject>
<t:Start>2026-11-12T09:00:00Z</t:Start><t:End>2026-11-12T10:00:00Z</t:End>
<t:Location>Переговорка 3</t:Location><t:IsCancelled>false</t:IsCancelled><t:IsAllDayEvent>false</t:IsAllDayEvent>
<t:Sensitivity>Normal</t:Sensitivity>'''
DETAIL = LIST+'''<t:Body BodyType="Text">1. Обсудить качество DWH.\n2. План BI.</t:Body>
<t:UID>series-id</t:UID><t:Organizer><t:Mailbox><t:Name>Организатор</t:Name><t:EmailAddress>owner@example.com</t:EmailAddress></t:Mailbox></t:Organizer>
<t:RequiredAttendees><t:Attendee><t:Mailbox><t:Name>Анна Иванова</t:Name><t:EmailAddress>anna@example.com</t:EmailAddress></t:Mailbox><t:ResponseType>Accept</t:ResponseType></t:Attendee></t:RequiredAttendees>
<t:OptionalAttendees><t:Attendee><t:Mailbox><t:Name>Пётр Петров</t:Name><t:EmailAddress>peter@example.com</t:EmailAddress></t:Mailbox><t:ResponseType>NoResponseReceived</t:ResponseType></t:Attendee></t:OptionalAttendees>
<t:Resources><t:Attendee><t:Mailbox><t:Name>Переговорка 3</t:Name><t:EmailAddress>room@example.com</t:EmailAddress></t:Mailbox></t:Attendee></t:Resources></t:CalendarItem>'''


def xml(operation, content, *, code='NoError', response_class='Success'):
    return f'''<s:Envelope xmlns:s="{ews.NS['s']}" xmlns:m="{ews.NS['m']}" xmlns:t="{ews.NS['t']}">
    <s:Body><m:{operation}Response><m:ResponseMessages><m:{operation}ResponseMessage ResponseClass="{response_class}">
    <m:ResponseCode>{code}</m:ResponseCode>{content}</m:{operation}ResponseMessage></m:ResponseMessages>
    </m:{operation}Response></s:Body></s:Envelope>'''.encode()


class Response:
    def __init__(self, body, status=200): self.body=body; self.status_code=status
    def __enter__(self): return self
    def __exit__(self,*a): pass
    def iter_content(self,size):
        for i in range(0,len(self.body),size): yield self.body[i:i+size]


@pytest.mark.parametrize('value', ['mail.example.com','https://mail.example.com/owa/','https://mail.example.com/EWS/Exchange.asmx'])
def test_endpoint_normalizes_owa_without_discovery(value):
    assert ews.endpoint(value)=='https://mail.example.com/EWS/Exchange.asmx'


@pytest.mark.parametrize('value', ['http://mail.example.com','https://user:secret@mail.example.com','https://mail.example.com/owa?token=secret',
                                   'https://mail.example.com/#fragment','file:///etc/passwd','https://mail.example.com:bad/owa','https://mail.example.com/other','https://[broken'])
def test_endpoint_rejects_insecure_or_ambiguous_addresses(value):
    with pytest.raises(ews.ExchangeError): ews.endpoint(value)


def test_only_read_calendar_soap_operations_and_selected_fields(monkeypatch):
    client = ews.EwsClient('https://mail.example.com/owa','DOMAIN\\user','test-secret','ntlm')
    calls=[]
    def send(url,**kwargs):
        calls.append(kwargs)
        assert kwargs['allow_redirects'] is False and kwargs['stream']
        assert client.session.verify is not False and client.session.trust_env is False
        if len(calls)==1:
            return Response(xml('FindItem',f'<m:RootFolder IncludesLastItemInRange="true"><t:Items>{LIST}</t:CalendarItem></t:Items></m:RootFolder>'))
        return Response(xml('GetItem',f'<m:Items>{DETAIL}</m:Items>'))
    monkeypatch.setattr(client.session,'post',send)
    start=datetime(2026,11,12,tzinfo=timezone.utc)
    values, truncated = client.events(start,start+timedelta(days=7))
    assert not truncated and values[0]['title']=='План DWH & BI'
    result=client.detail(values[0]['ews_id'])
    assert [p['name'] for p in result['invitees']]==['Анна Иванова','Пётр Петров']
    assert result['resources'][0]['name']=='Переговорка 3'
    assert result['agenda'].startswith('1. Обсудить')
    first=ews.ET.fromstring(calls[0]['data'])
    assert first.find('.//t:DistinguishedFolderId',ews.NS).get('Id')=='calendar'
    assert first.find('.//m:CalendarView',ews.NS).get('MaxEntriesReturned')=='200'
    assert b'item:Body' not in calls[0]['data'] and b'test-secret' not in calls[0]['data']
    second=ews.ET.fromstring(calls[1]['data'])
    assert second.find('.//t:BodyType',ews.NS).text=='Text'
    assert second.find('.//t:ItemId',ews.NS).get('Id')=='occurrence-1'
    client.close(); assert client.session.auth is None


@pytest.mark.parametrize('status', [301,302,401,403,404,429,503])
def test_http_failures_do_not_follow_redirects_or_return_remote_body(monkeypatch,status):
    client=ews.EwsClient('mail.example.com','user','secret','basic')
    monkeypatch.setattr(client.session,'post',lambda *a,**kw:Response(b'private server details',status))
    with pytest.raises(ews.ExchangeError) as error: client.detail('id')
    assert 'private server' not in str(error.value)


def test_xml_entities_and_oversized_responses_are_rejected(monkeypatch):
    client=ews.EwsClient('mail.example.com','user','secret','basic')
    monkeypatch.setattr(client.session,'post',lambda *a,**kw:Response(b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>'))
    with pytest.raises(ews.ExchangeError): client.detail('id')
    monkeypatch.setattr(ews,'MAX_RESPONSE',16)
    monkeypatch.setattr(client.session,'post',lambda *a,**kw:Response(b'x'*20))
    with pytest.raises(ews.ExchangeError,match='большой'): client.detail('id')


class FakeClient:
    made=[]
    def __init__(self,*args): self.closed=False; self.calls=0; self.made.append(self)
    def close(self): self.closed=True
    def events(self,*args):
        self.calls+=1
        return [ews.event(ews.SafeET.fromstring(f'<root xmlns:t="{ews.NS["t"]}">{LIST}</t:CalendarItem></root>')[0])],False
    def detail(self,key):
        return ews.event(ews.SafeET.fromstring(f'<root xmlns:t="{ews.NS["t"]}">{DETAIL}</root>')[0],details=True)


@pytest.fixture
def db(tmp_path,monkeypatch):
    for name,value in dict(DATA_DIR=tmp_path,DB_PATH=tmp_path/'mom.db',ENV_PATH=tmp_path/'.env',UPLOAD_DIR=tmp_path/'uploads',AUDIO_DIR=tmp_path/'audio',WHISPER_DIR=tmp_path/'whisper').items():
        monkeypatch.setattr(config,name,value)
    monkeypatch.setattr(store,'DB_PATH',config.DB_PATH)
    monkeypatch.setattr(main,'UPLOAD_DIR',config.UPLOAD_DIR)
    monkeypatch.setattr(cal,'_connections',cal.OrderedDict())
    monkeypatch.setattr(cal,'EwsClient',FakeClient)
    FakeClient.made=[]
    store.init_db()
    owner=store.create_user('owner@example.com','unused')
    other=store.create_user('other@example.com','unused')
    store.put_session('owner-token','user',user_id=owner['id'],email=owner['email'],expires=__import__('time').time()+3600)
    store.put_session('other-token','user',user_id=other['id'],email=other['email'],expires=__import__('time').time()+3600)
    cal.save_settings(True,'https://mail.example.com/owa','ntlm','admin')
    return owner,other


def connected(db):
    user=db[0]; cal.connect('owner-token',user['id'],'DOMAIN\\owner','secret-never-persist')
    start=datetime(2026,11,12,tzinfo=timezone.utc)
    events=cal.events('owner-token',user['id'],start,start+timedelta(days=7))
    return events['events'][0]['id']


def test_personal_connection_and_selected_snapshot_do_not_leak(db):
    key=connected(db); owner,other=db
    with pytest.raises(ews.ExchangeError): cal.detail('other-token',other['id'],key)
    with pytest.raises(ews.ExchangeError): cal.detail('owner-token',other['id'],key)
    with pytest.raises(ews.ExchangeError): cal.detail('owner-token',owner['id'],'forged')
    detail=cal.detail('owner-token',owner['id'],key)
    assert 'ews_id' not in detail
    context=cal.selected_context('owner-token',owner['id'],key)
    assert 'id' not in context and 'password' not in json.dumps(context)
    with store.connect() as conn: all_data='\n'.join(conn.iterdump())
    assert 'secret-never-persist' not in all_data and 'DOMAIN\\owner' not in all_data
    context['invitees'].clear()
    assert len(cal.selected_context('owner-token',owner['id'],key)['invitees'])==2
    cal.disconnect('owner-token')
    assert FakeClient.made[0].closed and not cal.status('owner-token',owner['id'])['connected']


def test_cache_avoids_exchange_calls_and_selected_snapshot_survives_refresh(db):
    key=connected(db); owner=db[0]
    cal.detail('owner-token',owner['id'],key)
    client=FakeClient.made[-1]; before=client.calls
    start=datetime(2026,11,12,tzinfo=timezone.utc)
    for _ in range(5): cal.events('owner-token',owner['id'],start,start+timedelta(days=7))
    assert client.calls==before
    cal.events('owner-token',owner['id'],start,start+timedelta(days=7),True)
    assert client.calls==before+1
    assert cal.selected_context('owner-token',owner['id'],key)['title']=='План DWH & BI'


def test_disconnect_during_login_discards_late_response(db,monkeypatch):
    entered,release=threading.Event(),threading.Event()
    original=FakeClient.events
    def slow(self,*args): entered.set(); assert release.wait(3); return original(self,*args)
    monkeypatch.setattr(FakeClient,'events',slow)
    with ThreadPoolExecutor() as pool:
        pending=pool.submit(cal.connect,'owner-token',db[0]['id'],'user','password')
        assert entered.wait(3)
        cal.disconnect('owner-token'); release.set()
        with pytest.raises(ews.ExchangeError,match='отменено'): pending.result(3)
    assert not cal.status('owner-token',db[0]['id'])['connected'] and FakeClient.made[-1].closed


def test_settings_change_expires_connections_and_failed_save_preserves_them(db,monkeypatch):
    connected(db)
    original=store.set_app_setting
    def fail(*a): raise OSError('readonly')
    monkeypatch.setattr(store,'set_app_setting',fail)
    with pytest.raises(ews.ExchangeError, match='сохранить'): cal.save_settings(False,'','ntlm','admin')
    assert cal.status('owner-token',db[0]['id'])['connected']
    monkeypatch.setattr(store,'set_app_setting',original)
    cal.save_settings(False,'','ntlm','admin')
    assert not cal.status('owner-token',db[0]['id'])['connected'] and FakeClient.made[-1].closed


def test_expired_credentials_are_removed(db):
    connected(db)
    for connection in cal._connections.values(): connection.expires=0
    cal.cleanup()
    assert not cal._connections and FakeClient.made[-1].closed


def test_selected_event_sets_title_context_but_not_speakers_or_mail_recipients(db,monkeypatch):
    key=connected(db); cal.detail('owner-token',db[0]['id'],key)
    client=TestClient(main.app); client.cookies.set(auth.USER_COOKIE,'owner-token')
    monkeypatch.setattr(config,'MIN_FREE_BYTES',0)
    response=client.post('/api/meetings/transcript',data={'text':'Спикер 1: Обсудили план.', 'calendar_event_id':key})
    assert response.status_code==200,response.text
    meeting=response.json()
    assert meeting['title']=='План DWH & BI' and meeting['calendar_context']['resources'][0]['name']=='Переговорка 3'
    assert meeting['speakers']==[{'id':'S1','name':'Спикер 1'}]
    client.post('/api/session/logout')
    assert not cal._connections
    saved=store.get_meeting(meeting['id'])
    assert saved['calendar_context']==meeting['calendar_context']
    with store.connect() as conn: assert conn.execute('SELECT COUNT(*) FROM protocol_mail').fetchone()[0]==0


def test_forged_calendar_context_cannot_create_a_meeting(db,monkeypatch):
    connected(db)
    client=TestClient(main.app); client.cookies.set(auth.USER_COOKIE,'owner-token')
    response=client.post('/api/meetings/transcript',data={'text':'A: text','calendar_event_id':'forged'})
    assert response.status_code==409
    assert client.get('/api/meetings').json()==[]


def test_calendar_routes_require_user_and_settings_require_admin(db):
    client=TestClient(main.app)
    for path in ['/api/calendar/status','/api/calendar/events','/api/calendar/events/any','/api/settings/exchange']:
        assert client.get(path).status_code==401
    client.cookies.set(auth.USER_COOKIE,'owner-token')
    assert client.get('/api/settings/exchange').status_code==401
    assert client.get('/api/calendar/status').json()=={'enabled':True,'connected':False}


def test_context_is_bounded_and_distinguishes_invitees_from_actual_attendance():
    value={'title':'План встречи','agenda':'Большая повестка ' * 10000,'invitees':[{'name':'Анна','email':'anna@example.com'}]*100,
           'resources':[{'name':'Комната'}]}
    result=cal.context_text(value,1500)
    assert len(result.encode())<=1502
    assert 'не факты состоявшейся встречи' in result and 'не являются распознанными спикерами' in result
    assert 'invited_not_confirmed' in result and 'planned_agenda' in result
    assert cal.context_text(value,50)==''


@pytest.mark.parametrize('start,end', [(datetime(2026,1,1),datetime(2026,1,2)),
    (datetime(2026,1,2,tzinfo=timezone.utc),datetime(2026,1,1,tzinfo=timezone.utc)),
    (datetime(2026,1,1,tzinfo=timezone.utc),datetime(2026,3,1,tzinfo=timezone.utc))])
def test_calendar_window_is_bounded(start,end):
    with pytest.raises(ews.ExchangeError): cal.window(start,end)


def test_calendar_context_reaches_protocol_and_keeps_selected_title(db,monkeypatch):
    from backend import keys, pipeline, prompts
    key=connected(db); cal.detail('owner-token',db[0]['id'],key)
    client=TestClient(main.app); client.cookies.set(auth.USER_COOKIE,'owner-token')
    meeting=client.post('/api/meetings/transcript',data={'text':'Спикер 1: Обсудили план.', 'calendar_event_id':key}).json()
    store.save_llm_settings({**prompts.snapshot(), 'name_review_enabled':False}, 'admin')
    monkeypatch.setattr(keys,'snapshot',lambda:{'connected':True})
    monkeypatch.setattr(config,'get_api_key',lambda:'test-key')
    monkeypatch.setattr(config,'SMTP_HOST','')
    monkeypatch.setattr(pipeline,'make_client',lambda key:object())
    received=[]
    async def analyze(client, transcript, title, *args, **kwargs):
        received.append(kwargs['meeting_context'])
        assert 'Обсудили план.' in transcript
        return {'title':'Название от модели', 'summary':'Готово', 'participants':[], 'action_items':[], 'mom':{}}
    monkeypatch.setattr(pipeline,'_analyze_with_heartbeat',analyze)
    assert client.post(f"/api/meetings/{meeting['id']}/protocol").status_code==200
    asyncio.run(pipeline.process_meeting(meeting['id']))
    saved=store.get_meeting(meeting['id'])
    assert saved['status']=='done'
    assert saved['title']==saved['result']['title']=='План DWH & BI'
    assert received==[meeting['calendar_context']]


def test_prompt_keeps_invitation_glossary_and_speaker_roles_together(db,monkeypatch):
    from backend import analyze, prompts
    store.save_llm_settings({**prompts.snapshot(), 'protocol_prompt':'Return JSON'}, 'admin')
    monkeypatch.setattr(config,'PROTOCOL_CONTEXT_TOKENS',16384)
    calls=[]
    async def fake(client, transcript, title, **kwargs):
        calls.append(transcript)
        assert len((kwargs['system_prompt']+transcript).encode()) < 16384-4096
        assert 'приглашения из календаря' in transcript
        assert 'Приглашённые могут отсутствовать' in transcript
        assert 'План BI' in transcript and 'TrueConf' in transcript and 'Архитектор' in transcript
        assert 'Обсудили план.' in transcript
        return {'summary':'Готово'}
    monkeypatch.setattr(analyze,'analyze_transcript',fake)
    asyncio.run(analyze.analyze_long_transcript(object(),'Анна: Обсудили план.','Встреча',
        meeting_context={'title':'План BI','agenda':'План '*10000,'invitees':[]},
        corporate_terms=[{'term':'TrueConf','description':'Видеосвязь'}],
        participant_roles=[{'name':'Анна','job_title':'Архитектор'}]))
    assert len(calls)==1
