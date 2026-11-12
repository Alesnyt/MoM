"""Small read-only EWS client for on-premises Exchange (no autodiscovery)."""
from __future__ import annotations

from datetime import datetime, timezone
import os
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import requests
from requests_ntlm import HttpNtlmAuth
from defusedxml import ElementTree as SafeET

NS = {'s':'http://schemas.xmlsoap.org/soap/envelope/',
      'm':'http://schemas.microsoft.com/exchange/services/2006/messages',
      't':'http://schemas.microsoft.com/exchange/services/2006/types'}
for prefix, uri in NS.items(): ET.register_namespace(prefix, uri)
MAX_RESPONSE = 2 * 1024 * 1024
MAX_EVENTS = 200
_slots = threading.BoundedSemaphore(4)


class ExchangeError(ValueError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def endpoint(value: str) -> str:
    value = value.strip()
    if value and '://' not in value: value = 'https://' + value
    try: parsed = urlsplit(value)
    except ValueError as exc: raise ExchangeError("Некорректный адрес Exchange.", 400) from exc
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
        raise ExchangeError('Укажите HTTPS-адрес корпоративного OWA или EWS без логина и пароля в ссылке.', 400)
    try: parsed.port
    except ValueError as exc: raise ExchangeError('Некорректный порт Exchange.', 400) from exc
    path = parsed.path.rstrip('/')
    if path.lower() in ('', '/owa'): path = '/EWS/Exchange.asmx'
    if path.lower() != '/ews/exchange.asmx':
        raise ExchangeError('Нужен адрес вида https://mail.company.ru/EWS/Exchange.asmx или адрес OWA.', 400)
    return urlunsplit(('https', parsed.netloc, path, '', ''))


def node(parent, name: str, text=None, **attrs):
    prefix, local = name.split(':')
    child = ET.SubElement(parent, f'{{{NS[prefix]}}}{local}', attrs)
    if text is not None: child.text = str(text)
    return child


def request(operation: str, fields: list[str]):
    envelope = ET.Element(f'{{{NS["s"]}}}Envelope')
    header = node(envelope, 's:Header')
    node(header, 't:RequestServerVersion', Version='Exchange2010_SP2')
    # UTC avoids server/mailbox timezone assumptions; the UI displays local time.
    context = node(header, 't:TimeZoneContext')
    node(context, 't:TimeZoneDefinition', Id='UTC')
    op = node(node(envelope, 's:Body'), f'm:{operation}')
    shape = node(op, 'm:ItemShape')
    node(shape, 't:BaseShape', 'IdOnly')
    if operation == 'GetItem': node(shape, 't:BodyType', 'Text')
    props = node(shape, 't:AdditionalProperties')
    for field in fields: node(props, 't:FieldURI', FieldURI=field)
    return envelope, op


LIST_FIELDS = ['item:Subject', 'item:Sensitivity', 'calendar:Start', 'calendar:End',
               'calendar:Location', 'calendar:IsAllDayEvent', 'calendar:IsCancelled']
DETAIL_FIELDS = LIST_FIELDS + ['item:Body', 'calendar:UID', 'calendar:Organizer',
                               'calendar:RequiredAttendees', 'calendar:OptionalAttendees', 'calendar:Resources']


def clean(value: str | None, limit=400) -> str:
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', value or '').strip()[:limit]


def text(item, path: str, limit=400) -> str:
    return clean(item.findtext(path, default='', namespaces=NS), limit)


def person(mailbox) -> dict:
    return {'name':text(mailbox, 't:Name', 200), 'email':text(mailbox, 't:EmailAddress', 254)}


def event(item, *, details=False) -> dict:
    identity = item.find('t:ItemId', NS)
    if identity is None or not identity.get('Id'):
        raise ExchangeError('Exchange вернул событие без идентификатора.')
    data = {'ews_id':identity.get('Id'), 'change_key':identity.get('ChangeKey', ''),
            'title':text(item, 't:Subject', 200) or 'Встреча без названия',
            'start':text(item, 't:Start', 80), 'end':text(item, 't:End', 80),
            'location':text(item, 't:Location', 500), 'all_day':text(item, 't:IsAllDayEvent')=='true',
            'cancelled':text(item, 't:IsCancelled')=='true', 'private':text(item, 't:Sensitivity') in {'Private','Confidential'}}
    if details:
        organizer = item.find('t:Organizer/t:Mailbox', NS)
        data.update(uid=text(item, 't:UID', 512), agenda=text(item, 't:Body', 8000),
                    organizer=person(organizer) if organizer is not None else None,
                    invitees=[], resources=[])
        seen = set()
        for field, kind in [('RequiredAttendees','required'), ('OptionalAttendees','optional'), ('Resources','resource')]:
            for attendee in item.findall(f't:{field}/t:Attendee', NS)[:100]:
                mailbox = attendee.find('t:Mailbox', NS)
                if mailbox is None: continue
                value = person(mailbox)
                value.update(kind=kind, response=text(attendee, 't:ResponseType', 40))
                if not (value['name'] or value['email']): continue
                key = (kind=='resource', value['email'].casefold() or value['name'].casefold())
                if key in seen: continue
                seen.add(key)
                data['resources' if kind=='resource' else 'invitees'].append(value)
    return data


class EwsClient:
    def __init__(self, url: str, username: str, password: str, mode: str):
        self.url = endpoint(url)
        self.session = requests.Session()
        self.session.trust_env = False  # Do not use ambient netrc credentials or proxy redirects.
        self.session.verify = os.environ.get('SSL_CERT_FILE') or True
        self.session.auth = HttpNtlmAuth(username, password) if mode=='ntlm' else requests.auth.HTTPBasicAuth(username, password)
        self.session.headers.update({'User-Agent':'MoM-calendar/1', 'Content-Type':'text/xml; charset=utf-8'})

    def close(self):
        self.session.auth = None
        self.session.close()

    def _send(self, envelope, operation: str):
        if not _slots.acquire(blocking=False):
            raise ExchangeError('Сейчас много запросов к календарю. Повторите через несколько секунд.', 503)
        try:
            deadline = time.monotonic() + 30
            with self.session.post(self.url, data=ET.tostring(envelope, encoding='utf-8', xml_declaration=True),
                                   headers={'SOAPAction':f'"{NS["m"]}/{operation}"'},
                                   timeout=(5, 15), allow_redirects=False, stream=True) as response:
                if response.status_code in (401,403):
                    raise ExchangeError('Exchange не принял вход или запретил EWS. Проверьте домен, логин, пароль и доступ к календарю.', 401)
                if 300 <= response.status_code < 400:
                    raise ExchangeError('Сервер перенаправляет запрос. Укажите конечный HTTPS-адрес EWS в настройках Exchange.', 400)
                if response.status_code == 404:
                    raise ExchangeError('Служба EWS по этому адресу не найдена. Проверьте /EWS/Exchange.asmx.', 400)
                if response.status_code in (429,503):
                    raise ExchangeError('Exchange временно занят. Подождите и обновите календарь.', 503)
                body = bytearray()
                for chunk in response.iter_content(16384):
                    if len(body)+len(chunk)>MAX_RESPONSE or time.monotonic()>deadline:
                        raise ExchangeError('Ответ Exchange слишком большой или долгий. Выберите более короткий период.')
                    body.extend(chunk)
                try: root = SafeET.fromstring(bytes(body), forbid_dtd=True)
                except (ET.ParseError, ValueError) as exc:
                    raise ExchangeError('Вместо ответа календаря сервер вернул другую страницу. Проверьте адрес EWS.') from exc
                messages = root.findall(f'.//m:{operation}ResponseMessage', NS)
                if not messages:
                    raise ExchangeError('Exchange вернул неожиданный ответ. Проверьте доступ к службе EWS.')
                for message in messages:
                    code = text(message, 'm:ResponseCode', 100)
                    if message.get('ResponseClass') != 'Success' or code != 'NoError':
                        known = {'ErrorAccessDenied':'Нет доступа к календарю Exchange.',
                                 'ErrorItemNotFound':'Событие уже изменено или удалено. Обновите календарь.',
                                 'ErrorCalendarViewRangeTooBig':'Выберите более короткий период календаря.',
                                 'ErrorServerBusy':'Exchange занят. Повторите позже.',
                                 'ErrorInvalidServerVersion':'Версия Exchange не поддерживает выбранный протокол EWS.'}
                        raise ExchangeError(known.get(code, 'Exchange не смог прочитать календарь.') +
                                            (f' Код: {code}.' if re.fullmatch(r'[A-Za-z0-9]{1,100}',code) else ''))
                if response.status_code != 200: raise ExchangeError('Ошибка службы календаря Exchange.')
                return root
        except requests.exceptions.SSLError as exc:
            raise ExchangeError('Сертификат Exchange не прошёл проверку. Добавьте корпоративный CA в доверенные сертификаты сервера MoM.') from exc
        except requests.exceptions.Timeout as exc:
            raise ExchangeError('Exchange не ответил вовремя. Проверьте доступность EWS и попробуйте снова.', 504) from exc
        except requests.exceptions.RequestException as exc:
            raise ExchangeError('Не удалось связаться с Exchange. Проверьте адрес, сеть и доступность EWS.') from exc
        finally:
            _slots.release()

    def events(self, start: datetime, end: datetime) -> tuple[list[dict], bool]:
        envelope, op = request('FindItem', LIST_FIELDS)
        op.set('Traversal', 'Shallow')
        node(op, 'm:CalendarView', StartDate=start.astimezone(timezone.utc).isoformat(),
             EndDate=end.astimezone(timezone.utc).isoformat(), MaxEntriesReturned=str(MAX_EVENTS))
        node(node(op, 'm:ParentFolderIds'), 't:DistinguishedFolderId', Id='calendar')
        root = self._send(envelope, 'FindItem')
        folder = root.find('.//m:RootFolder', NS)
        if folder is None: raise ExchangeError('Exchange не вернул папку календаря.')
        return ([event(item) for item in folder.findall('t:Items/t:CalendarItem', NS)[:MAX_EVENTS]],
                folder.get('IncludesLastItemInRange', 'false').lower()!='true')

    def detail(self, ews_id: str) -> dict:
        envelope, op = request('GetItem', DETAIL_FIELDS)
        node(node(op, 'm:ItemIds'), 't:ItemId', Id=ews_id)
        root = self._send(envelope, 'GetItem')
        items = root.findall('.//t:CalendarItem', NS)
        if len(items)!=1: raise ExchangeError('Событие не найдено. Обновите календарь.')
        data = event(items[0], details=True)
        if data['ews_id'] != ews_id: raise ExchangeError('Exchange вернул другое событие. Обновите календарь.')
        return data
