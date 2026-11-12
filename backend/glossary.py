"""Versioned Excel/CSV glossaries. Published generations are immutable.

Import batches commit separately; readers use only the published generation.
No model or network request is needed for lookup or context selection.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import secrets
import time
import threading
from collections import OrderedDict
from contextlib import contextmanager
from itertools import islice
from zipfile import BadZipFile, ZipFile
from xml.etree.ElementTree import ParseError

from . import store

log = logging.getLogger('mom.glossary')
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TERMS = 100_000
MAX_TEXT_BYTES = 100 * 1024 * 1024
_count_cache = OrderedDict()
_count_lock = threading.Lock()
FIELDS = ('term', 'aliases', 'replacements', 'description', 'department')
HEADERS = {'term': ['термин', 'term', 'наименование', 'название', 'бизнес термин'],
           'aliases': ['синонимы', 'сокращения', 'варианты', 'aliases', 'synonyms'],
           'replacements': ['замены', 'разрешенные замены', 'replacements'],
           'description': ['определение', 'описание', 'description', 'definition'],
           'department': ['область', 'подразделение', 'department', 'domain']}


def normalize(value: str) -> str:
    return ' '.join(re.findall(r'[^\W_]+', value.casefold().replace('ё', 'е'), re.UNICODE))


def init_schema(conn) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS glossary_meta (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)''')
    conn.execute('INSERT OR IGNORE INTO glossary_meta VALUES (1,0)')
    conn.execute('''CREATE TABLE IF NOT EXISTS glossaries (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        generation TEXT, version INTEGER NOT NULL DEFAULT 0, term_count INTEGER NOT NULL DEFAULT 0,
        mapping TEXT NOT NULL DEFAULT '{}', sheet TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '', updated_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS glossary_entries (
        id INTEGER PRIMARY KEY, glossary_id TEXT NOT NULL, generation TEXT NOT NULL,
        term TEXT NOT NULL, aliases TEXT NOT NULL, replacements TEXT NOT NULL, description TEXT NOT NULL,
        department TEXT NOT NULL, search_text TEXT NOT NULL, identity TEXT NOT NULL,
        UNIQUE(generation, identity))''')
    conn.execute('CREATE INDEX IF NOT EXISTS glossary_generation ON glossary_entries(generation)')
    conn.execute('CREATE TABLE IF NOT EXISTS glossary_generations (id TEXT PRIMARY KEY, created REAL NOT NULL)')
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS glossary_fts USING fts5(search_text, content='glossary_entries', content_rowid='id', tokenize='trigram')")
    conn.execute('''CREATE TRIGGER IF NOT EXISTS glossary_insert AFTER INSERT ON glossary_entries BEGIN
        INSERT INTO glossary_fts(rowid,search_text) VALUES(new.id,new.search_text); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS glossary_delete AFTER DELETE ON glossary_entries BEGIN
        INSERT INTO glossary_fts(glossary_fts,rowid,search_text) VALUES('delete',old.id,old.search_text); END''')
    conn.execute('''CREATE TABLE IF NOT EXISTS glossary_imports (
        id TEXT PRIMARY KEY, glossary_id TEXT, actor TEXT NOT NULL, filename TEXT NOT NULL,
        content BLOB, status TEXT NOT NULL, mapping TEXT NOT NULL DEFAULT '{}', sheet TEXT NOT NULL DEFAULT '',
        received INTEGER NOT NULL DEFAULT 0, error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
        owner TEXT, base_version INTEGER, finished_at TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS glossary_import_status ON glossary_imports(status,created)')
    # One-time migration preserves the former aliases-as-corrections behavior.
    row = conn.execute("SELECT payload FROM app_settings WHERE key='corporate_context'").fetchone()
    if row:
        data = json.loads(row[0])
        if data.get('terms'):
            if not conn.execute("SELECT 1 FROM glossaries WHERE id='legacy'").fetchone():
                conn.execute("INSERT INTO glossaries(id,name,generation,version,term_count,updated_at) VALUES('legacy','Корпоративный словарь','legacy',1,?,?)",
                             (len(data['terms']), data.get('imported_at') or store.utc_now()))
                _insert(conn, 'legacy', 'legacy', [dict(e, replacements=e['aliases']) for e in data['terms']])
                conn.execute('UPDATE glossary_meta SET revision=revision+1')
            data.pop('terms', None)
            conn.execute("UPDATE app_settings SET payload=? WHERE key='corporate_context'", (json.dumps(data, ensure_ascii=False),))


def revision() -> int:
    with store.connect() as conn:
        return conn.execute('SELECT revision FROM glossary_meta').fetchone()[0]


def _entry(row) -> dict:
    return {key: json.loads(row[key]) if key in ('aliases', 'replacements') else row[key]
            for key in row.keys() if key not in ('search_text', 'identity', 'generation')}


def catalog(query: str = '', glossary_id: str = '', offset: int = 0) -> dict:
    needle = normalize(query)
    with store.connect() as conn:
        conn.execute('BEGIN')
        sources = [dict(row) for row in conn.execute('SELECT * FROM glossaries ORDER BY name,id')]
        for source in sources:
            source['enabled'] = bool(source['enabled'])
            source['mapping'] = json.loads(source['mapping'])
        where, params = ['e.generation=g.generation'], []
        join = 'glossary_entries e JOIN glossaries g ON e.glossary_id=g.id'
        if glossary_id:
            where.append('g.id=?'); params.append(glossary_id)
        if needle:
            # Literal substring search. A trigram index narrows longer queries;
            # short abbreviations still work using a bounded corpus scan.
            parts = needle.split()
            indexed = [part for part in parts if len(part) >= 3]
            if indexed:
                join += ' JOIN glossary_fts ON glossary_fts.rowid=e.id'
                where.append('glossary_fts MATCH ?')
                params.append(' AND '.join('"' + part.replace('"', '""') + '"' for part in indexed))
            for part in parts:
                if len(part) >= 3: continue
                where.append('instr(e.search_text,?)>0'); params.append(part)
        clause = ' AND '.join(where)
        version = conn.execute('SELECT revision FROM glossary_meta').fetchone()[0]
        count_key = (str(store.DB_PATH), version, needle, glossary_id)
        with _count_lock:
            count = _count_cache.get(count_key)
        if not needle:
            count = sum(source['term_count'] for source in sources if not glossary_id or source['id']==glossary_id)
        elif count is None:
            count = conn.execute(f'SELECT count(*) FROM {join} WHERE {clause}', params).fetchone()[0]
            with _count_lock:
                _count_cache[count_key] = count
                _count_cache.move_to_end(count_key)
                while len(_count_cache)>128: _count_cache.popitem(last=False)
        order = 'glossary_fts.rowid' if needle and indexed else 'e.id'
        rows = conn.execute(f'SELECT e.*,g.name AS glossary_name,g.enabled FROM {join} WHERE {clause} ORDER BY {order} LIMIT 100 OFFSET ?',
                            (*params, offset)).fetchall()
        jobs = [dict(row) for row in conn.execute("SELECT j.id,j.glossary_id,j.filename,j.status,j.received,j.error,j.finished_at FROM glossary_imports j WHERE j.status!='preview' AND NOT EXISTS(SELECT 1 FROM glossary_imports newer WHERE newer.glossary_id=j.glossary_id AND newer.status!='preview' AND newer.created>j.created) ORDER BY j.created DESC LIMIT 30")]
        return {'glossaries': sources, 'terms': [_entry(row) for row in rows], 'matched_terms': count,
                'term_count': sum(s['term_count'] for s in sources),
                'active_term_count': sum(s['term_count'] for s in sources if s['enabled']),
                'revision': version, 'jobs': jobs}


@contextmanager
def rows(filename: str, content: bytes, sheet: str = '', *, preferred_sheet: str = ''):
    if len(content) > MAX_FILE_BYTES:
        raise ValueError('Файл должен быть не больше 25 МБ')
    if filename.lower().endswith('.csv'):
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = content.decode('cp1251')
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        try:
            yield csv.reader(io.StringIO(text), dialect), [], ''
        except csv.Error as exc:
            raise ValueError('Не удалось прочитать CSV. Проверьте разделители и длину полей') from exc
        return
    if not filename.lower().endswith('.xlsx'):
        raise ValueError('Выберите Excel (.xlsx) или CSV')
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_TEXT_BYTES:
                raise ValueError('Распакованный Excel превышает 100 МБ')
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        try:
            if sheet and sheet not in book.sheetnames:
                raise ValueError('Лист Excel не найден')
            ws = book[sheet] if sheet else book[preferred_sheet] if preferred_sheet in book.sheetnames else book.worksheets[0]
            if ws.max_column and ws.max_column > 100:
                raise ValueError('В таблице допускается до 100 столбцов')
            ws.reset_dimensions()
            def values():
                for row in ws.iter_rows(max_col=100):
                    yield [cell.value for cell in row]
            yield values(), book.sheetnames, ws.title
        finally:
            book.close()
    except (BadZipFile, KeyError, OSError, ParseError) as exc:
        raise ValueError('Не удалось прочитать Excel-файл') from exc


def _header(iterator) -> list[str]:
    header = [str(value or '').strip() for value in next(iterator, [])]
    while header and not header[-1]: header.pop()
    if not header or len(header) > 100:
        raise ValueError('Первая строка должна содержать названия столбцов (до 100)')
    return header


def preview(filename: str, content: bytes, actor: str, *, glossary_id: str = '', sheet: str = '') -> dict:
    if glossary_id == 'discovered-terms':
        raise ValueError('Этот справочник обновляется через подтверждение предложений терминов')
    saved = None
    if glossary_id:
        with store.connect() as conn:
            saved = conn.execute('SELECT * FROM glossaries WHERE id=?', (glossary_id,)).fetchone()
        if saved is None: raise ValueError('Глоссарий не найден')
    with rows(filename, content, sheet, preferred_sheet=saved['sheet'] if saved else '') as (iterator, sheets, selected_sheet):
        header = _header(iterator)
        sample = [[str(value or '')[:200] for value in row[:len(header)]] for row in islice(iterator, 5)]
    with store.connect() as conn:
        mapping = {}
        old = json.loads(saved['mapping']) if saved else {}
        for field in FIELDS:
            old_name = old.get(field)
            if old_name in header:
                mapping[field] = old_name
            else:
                candidates = [name for name in header if normalize(name) in HEADERS[field]]
                if len(candidates) == 1: mapping[field] = candidates[0]
        token = secrets.token_hex(16)
        # Expired previews hold no useful publication; bound abandoned uploads.
        conn.execute("DELETE FROM glossary_imports WHERE status='preview' AND created<?", (time.time()-3600,))
        if conn.execute("SELECT count(*) FROM glossary_imports WHERE status IN ('preview','queued','running')").fetchone()[0] >= 5:
            raise ValueError('Завершите текущие загрузки (не больше пяти одновременно)')
        conn.execute('INSERT INTO glossary_imports(id,glossary_id,actor,filename,content,status,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                     (token, glossary_id or None, actor, filename, content, 'preview', time.time(), time.time()))
    return {'upload_id': token, 'columns': header, 'sample': sample, 'mapping': mapping, 'sheets': sheets,
            'sheet': selected_sheet, 'name': saved['name'] if saved else filename.rsplit('.',1)[0]}


def queue_import(upload_id: str, name: str, mapping: dict, sheet: str, actor: str) -> dict:
    name = ' '.join(name.split())
    if not name or len(name)>120: raise ValueError('Укажите название глоссария до 120 символов')
    if not mapping.get('term') or any(key not in FIELDS or not isinstance(value,str) for key,value in mapping.items()):
        raise ValueError('Выберите столбец с термином')
    if len(set(mapping.values())) != len(mapping): raise ValueError('Один столбец нельзя использовать для разных полей')
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        upload = conn.execute("SELECT * FROM glossary_imports WHERE id=? AND status='preview'", (upload_id,)).fetchone()
        if not upload or upload['created'] < time.time()-3600: raise ValueError('Предпросмотр устарел. Выберите файл заново')
        gid = upload['glossary_id'] or secrets.token_hex(12)
        if gid == 'discovered-terms':
            raise ValueError('Этот справочник обновляется через подтверждение предложений терминов')
        old = conn.execute('SELECT * FROM glossaries WHERE id=?', (gid,)).fetchone()
        if conn.execute("SELECT 1 FROM glossary_imports WHERE glossary_id=? AND status IN ('queued','running')", (gid,)).fetchone():
            raise ValueError('Этот глоссарий уже обновляется')
        if conn.execute('SELECT 1 FROM glossaries WHERE name=? AND id!=?', (name,gid)).fetchone():
            raise ValueError('Такое название уже есть. Выберите обновление существующего глоссария')
        if not old:
            if conn.execute('SELECT count(*) FROM glossaries').fetchone()[0] >= 30: raise ValueError('Допускается до 30 глоссариев')
            conn.execute('INSERT INTO glossaries(id,name) VALUES(?,?)', (gid,name))
        # Do not change the visible name/mapping until the generation is ready.
        settings = {**mapping, '_name': name}
        conn.execute("UPDATE glossary_imports SET glossary_id=?,status='queued',mapping=?,sheet=?,actor=?,base_version=?,updated=? WHERE id=?",
                     (gid,json.dumps(settings,ensure_ascii=False),sheet,actor,old['version'] if old else 0,time.time(),upload_id))
    return {'id':upload_id,'status':'queued','glossary_id':gid}


def parse_entries(filename: str, content: bytes, mapping: dict, sheet: str = ''):
    size, count, seen = 0, 0, set()
    with rows(filename, content, sheet) as (iterator, _, _selected):
        header = _header(iterator)
        for field, column in mapping.items():
            if field.startswith('_'): continue
            if header.count(column)!=1: raise ValueError(f'Столбец «{column}» отсутствует или повторяется')
        indices = {field:header.index(column) for field,column in mapping.items() if field in FIELDS}
        if 'term' not in indices: raise ValueError('Выберите столбец с термином')
        for number, row in enumerate(iterator, 2):
            if number > MAX_TERMS + 1: raise ValueError('В файле допускается до 100 000 строк')
            if not any(value is not None and str(value).strip() for value in row): continue
            item = {field:' '.join(str(row[i] if i<len(row) and row[i] is not None else '').split()) for field,i in indices.items()}
            if any(value.startswith('=') for value in item.values()): raise ValueError(f'Строка {number}: замените формулы готовыми значениями')
            term, description, department = item['term'], item.get('description',''), item.get('department','')
            if not normalize(term) or len(term)>240 or len(description)>4000 or len(department)>120:
                raise ValueError(f'Строка {number}: термин до 240, определение до 4000, область до 120 символов')
            if len(normalize(term).split())>12: raise ValueError(f'Строка {number}: термин должен содержать не больше 12 слов')
            aliases, replacements = [list(dict.fromkeys(v.strip() for v in item.get(key,'').split('|') if v.strip())) for key in ('aliases','replacements')]
            if any(len(values)>20 or any(not normalize(v) or len(v)>240 or len(normalize(v).split())>12 for v in values) for values in (aliases,replacements)):
                raise ValueError(f'Строка {number}: до 20 вариантов по 240 символов и 12 слов, через |')
            identity = (normalize(term),normalize(department))
            if identity in seen: raise ValueError(f'Строка {number}: термин уже есть в этой области')
            seen.add(identity)
            size += sum(len(value.encode()) for value in item.values())
            if size>MAX_TEXT_BYTES: raise ValueError('Содержимое глоссария превышает 100 МБ')
            count += 1
            yield dict(term=term,description=description,department=department,aliases=aliases,replacements=replacements)
    if not count: raise ValueError('Глоссарий пуст')


def _insert(conn, gid: str, generation: str, entries: list[dict]) -> None:
    conn.execute("INSERT OR IGNORE INTO glossary_generations VALUES(?,?)", (generation,time.time()))
    conn.executemany('''INSERT INTO glossary_entries(glossary_id,generation,term,aliases,replacements,description,department,search_text,identity)
        VALUES(?,?,?,?,?,?,?,?,?)''', [(gid,generation,e['term'],json.dumps(e['aliases'],ensure_ascii=False),
        json.dumps(e.get('replacements',[]),ensure_ascii=False),e['description'],e['department'],
        normalize(' '.join([e['term'],*e['aliases'],*e.get('replacements',[]),e['description'],e['department']])),
        json.dumps([normalize(e['term']),normalize(e['department'])],ensure_ascii=False)) for e in entries])


def _publish(conn, job, count: int) -> None:
    other = conn.execute('SELECT coalesce(sum(term_count),0) FROM glossaries WHERE id!=?',(job['glossary_id'],)).fetchone()[0]
    if other+count>MAX_TERMS: raise ValueError('Суммарно допускается до 100 000 терминов во всех глоссариях')
    settings = json.loads(job['mapping'])
    name = settings.pop('_name')
    if conn.execute('SELECT 1 FROM glossaries WHERE name=? AND id!=?', (name,job['glossary_id'])).fetchone():
        raise ValueError('Глоссарий с таким названием уже существует')
    updated = conn.execute('''UPDATE glossaries SET name=?,generation=?,version=version+1,term_count=?,mapping=?,sheet=?,filename=?,updated_at=?
        WHERE id=? AND version=?''',(name,job['id'],count,json.dumps(settings,ensure_ascii=False),job['sheet'],job['filename'],store.utc_now(),job['glossary_id'],job['base_version']))
    if updated.rowcount!=1: raise ValueError('Глоссарий изменился во время загрузки. Повторите импорт')
    conn.execute('UPDATE glossary_meta SET revision=revision+1')
    conn.execute("UPDATE glossary_imports SET status='succeeded',content=NULL,owner=NULL,received=?,finished_at=?,updated=? WHERE id=?",(count,store.utc_now(),time.time(),job['id']))


def run_one() -> bool:
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        # A dead process never leaves a half-loaded generation visible.
        conn.execute("UPDATE glossary_imports SET status='error',error='Импорт прерван. Загрузите файл повторно; прежняя версия сохранена',content=NULL,owner=NULL,finished_at=? WHERE status='running' AND updated<?",(store.utc_now(),time.time()-120))
        job = conn.execute("SELECT * FROM glossary_imports WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not job or conn.execute("SELECT 1 FROM glossary_imports WHERE status='running'").fetchone(): return False
        job = dict(job); owner = secrets.token_hex(16)
        conn.execute("UPDATE glossary_imports SET status='running',owner=?,updated=? WHERE id=?",(owner,time.time(),job['id']))
    count = 0
    try:
        iterator = iter(parse_entries(job['filename'],job['content'],json.loads(job['mapping']),job['sheet']))
        while batch := list(islice(iterator, 250)):
            with store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if not conn.execute("SELECT 1 FROM glossary_imports WHERE id=? AND owner=? AND status='running'",(job['id'],owner)).fetchone():
                    raise ValueError('Импорт передан другому процессу')
                _insert(conn,job['glossary_id'],job['id'],batch)
                count += len(batch)
                conn.execute('UPDATE glossary_imports SET received=?,updated=? WHERE id=?',(count,time.time(),job['id']))
                active = conn.execute("SELECT 1 FROM meetings WHERE status IN ('extracting','transcribing','analyzing') LIMIT 1").fetchone()
            # Yield CPU and the SQLite writer between bounded batches.
            time.sleep(0.05 if active else 0.002)
        with store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute("SELECT 1 FROM glossary_imports WHERE id=? AND owner=? AND status='running'",(job['id'],owner)).fetchone():
                raise ValueError('Импорт передан другому процессу')
            _publish(conn,job,count)
        store.record_audit(job['actor'],'glossary.import',job['glossary_id'],f'terms={count}')
    except Exception as exc:
        log.exception('Не удалось загрузить глоссарий %s',job['id'])
        error = str(exc) if isinstance(exc,ValueError) else f'Ошибка импорта ({type(exc).__name__}). Подробности в журнале сервера'
        with store.connect() as conn:
            conn.execute("UPDATE glossary_imports SET status='error',content=NULL,owner=NULL,error=?,finished_at=?,updated=? WHERE id=? AND owner=?",(error,store.utc_now(),time.time(),job['id'],owner))
        store.record_audit(job['actor'],'glossary.error',job['glossary_id'],error)
    return True


async def run_imports(stop: asyncio.Event) -> None:
    while not stop.is_set():
        delay = 2
        try:
            if await asyncio.to_thread(run_one):
                from . import corporate_context
                await asyncio.to_thread(corporate_context.for_meeting, {})
                continue
            await asyncio.to_thread(cleanup_one)
        except Exception:
            log.exception('Ошибка фонового импорта глоссариев')
            delay = 60
        try:
            await asyncio.wait_for(stop.wait(),timeout=delay)
        except TimeoutError:
            pass


def set_enabled(gid: str, enabled: bool, actor: str) -> dict:
    with store.connect() as conn:
        if conn.execute('UPDATE glossaries SET enabled=? WHERE id=?',(int(enabled),gid)).rowcount!=1:
            raise ValueError('Глоссарий не найден')
        conn.execute('UPDATE glossary_meta SET revision=revision+1')
    store.record_audit(actor,'glossary.toggle',gid,'Включён' if enabled else 'Выключен')
    return catalog()


def import_legacy(entries: list[dict], actor: str) -> None:
    """Compatibility API replaces only the original glossary, never new ones."""
    generation = secrets.token_hex(16)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("INSERT OR IGNORE INTO glossaries(id,name) VALUES('legacy','Корпоративный словарь')")
        old = conn.execute("SELECT version FROM glossaries WHERE id='legacy'").fetchone()[0]
        _insert(conn,'legacy',generation,[dict(e,replacements=e['aliases']) for e in entries])
        job = dict(id=generation,glossary_id='legacy',mapping=json.dumps({'_name':'Корпоративный словарь'}),sheet='',filename='',base_version=old)
        _publish(conn,job,len(entries))
    store.record_audit(actor,'context.import',detail=f'terms={len(entries)}')


def compact_entries() -> tuple[int,list[dict]]:
    with store.connect() as conn:
        conn.execute('BEGIN')
        version = conn.execute('SELECT revision FROM glossary_meta').fetchone()[0]
        entries = [_entry(row) for row in conn.execute('''SELECT e.id AS entry_id,e.term,e.aliases,e.replacements,e.department,
            g.id AS glossary_id,g.name AS glossary_name,g.version AS glossary_version FROM glossary_entries e
            JOIN glossaries g ON e.glossary_id=g.id AND e.generation=g.generation WHERE g.enabled=1''')]
    shared = {}
    for entry in entries:
        for key in ('glossary_id','glossary_name','department'):
            value = entry[key]
            entry[key] = shared.setdefault(value,value)
    return version, entries


def definitions(ids: list[int]) -> dict[int,str]:
    if not ids: return {}
    with store.connect() as conn:
        return dict(conn.execute(f"SELECT id,description FROM glossary_entries WHERE id IN ({','.join('?' for _ in ids)})",ids).fetchall())


def cleanup_one() -> None:
    """Reclaim superseded generations only while no meeting is processing.

    A one-hour grace also protects short-lived API readers. Batches keep the
    writer available to the queue. Current/queued/running generations survive.
    """
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT 1 FROM meetings WHERE status IN ('extracting','transcribing','analyzing') LIMIT 1").fetchone():
            return
        conn.execute("DELETE FROM glossary_imports WHERE status='preview' AND created<?", (time.time()-3600,))
        ids = [row[0] for row in conn.execute("""SELECT e.id FROM glossary_entries e
            JOIN glossary_generations v ON v.id=e.generation
            WHERE v.created<? AND NOT EXISTS(SELECT 1 FROM glossaries g WHERE g.generation=e.generation)
            AND NOT EXISTS(SELECT 1 FROM glossary_imports j WHERE j.id=e.generation AND j.status IN ('queued','running')) LIMIT 500""",(time.time()-3600,))]
        if ids:
            conn.executemany('DELETE FROM glossary_entries WHERE id=?',[(i,) for i in ids])
        conn.execute("DELETE FROM glossary_generations WHERE created<? AND NOT EXISTS(SELECT 1 FROM glossary_entries e WHERE e.generation=glossary_generations.id)",(time.time()-3600,))
