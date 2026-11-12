import io
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from backend import config, corporate_context as cc, glossary as g, main, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'mom.db')
    monkeypatch.setattr(config,'ldap_configured',lambda:False)
    store.init_db()
    monkeypatch.setitem(main.app.dependency_overrides, main.require_admin, lambda:{'username':'admin'})
    return tmp_path


def csv(term='DWH',description='Хранилище данных',alias='хранилище',replacement='',area=''):
    return f'Название;Определение;Синонимы;Замены;Область\n{term};{description};{alias};{replacement};{area}\n'.encode()


def upload(content=None, name='Данные', gid='', sheet=''):
    preview=g.preview('terms.csv',content or csv(),'admin',glossary_id=gid,sheet=sheet)
    return g.queue_import(preview['upload_id'],name,preview['mapping'],preview['sheet'],'admin')


def publish(content=None,name='Данные',gid=''):
    job=upload(content,name,gid)
    assert g.run_one()
    assert g.catalog()['jobs'][0]['status']=='succeeded',g.catalog()['jobs']
    return job['glossary_id']


def test_multiple_glossaries_update_independently_remember_mapping_and_toggle(db):
    a=publish()
    b=publish(csv('SLA','Уровень обслуживания','соглашение'), 'Бизнес')
    assert g.catalog()['term_count']==2
    preview=g.preview('another.csv',csv(),'admin',glossary_id=a)
    assert preview['mapping']['term']=='Название'
    publish(csv('DLH','Озеро данных'),gid=a)
    assert {e['term'] for e in g.catalog()['terms']}=={'DLH','SLA'}
    g.set_enabled(a,False,'admin')
    assert g.catalog()['active_term_count']==1
    assert {e['term'] for e in cc.for_meeting({}).select('DLH SLA')}=={'SLA'}
    assert next(s for s in g.catalog()['glossaries'] if s['id']==b)['version']==1


def test_aliases_find_terms_but_only_explicit_replacements_change_transcript(db):
    publish(csv('TrueConf','Видеосвязь','ВКС','тру конф'))
    context=cc.for_meeting({})
    assert context.select('Обсудим ВКС')[0]['term']=='TrueConf'
    text='ВКС работает. Откройте тру конф.'
    result=context.apply({'segments':[{'text':text,'speaker':'S1','start':2,'end':5}]})
    assert result['segments'][0]['text']=='ВКС работает. Откройте TrueConf.'
    assert result['segments'][0]['context_original_text']==text
    assert result['segments'][0]['speaker']=='S1' and result['segments'][0]['end']==5


def test_ambiguous_replacements_across_glossaries_are_not_applied(db):
    publish(csv('Система А',alias='альфа',replacement='альфа'),name='Бизнес')
    publish(csv('Система Б',alias='альфа',replacement='альфа'),name='Данные')
    text='Альфа, альфавит и альфы.'
    assert cc.for_meeting({}).apply({'segments':[{'text':text}]})['segments'][0]['text']==text
    assert len(g.catalog('альфа')['terms'])==2


def test_search_partial_case_yo_multiple_words_descriptions_and_short_acronyms(db):
    publish(csv('DWH','Ёмкое корпоративное хранилище','склад|warehouse'))
    for query in ('wh','DWH','СКЛ','емкое','корп храни','house','"warehouse"'):
        assert g.catalog(query)['matched_terms']==1,query
    assert g.catalog('несуществующее')['matched_terms']==0
    assert g.catalog('DROP TABLE')['matched_terms']==0


def test_bad_late_row_keeps_published_generation_and_mapping(db):
    gid=publish()
    content='Название;Определение\n'+''.join(f'Термин {i};Описание\n' for i in range(300))+'Термин 0;дубль\n'
    upload(content.encode(),gid=gid)
    assert g.run_one()
    state=g.catalog()
    assert state['jobs'][0]['status']=='error'
    assert state['jobs'][0]['received']==250
    assert state['term_count']==1 and state['terms'][0]['term']=='DWH'
    assert state['glossaries'][0]['mapping']['aliases']=='Синонимы'


def test_reader_sees_complete_old_or_new_version_during_batches(db,monkeypatch):
    gid=publish()
    old=cc.for_meeting({})
    seen=[];insert=g._insert
    def inspect(conn,source,generation,entries):
        state=g.catalog()
        seen.append((state['term_count'],state['terms'][0]['term']))
        insert(conn,source,generation,entries)
    monkeypatch.setattr(g,'_insert',inspect)
    content='Название;Определение\n'+''.join(f'Термин {i};Новое определение\n' for i in range(501))
    upload(content.encode(),gid=gid)
    assert g.run_one()
    assert seen==[(1,'DWH')]*3
    assert g.catalog()['term_count']==501
    assert old.select('DWH')[0]['description']=='Хранилище данных'
    new=cc.for_meeting({})
    assert new.index is not old.index
    assert new.select('Термин 500')[0]['description']=='Новое определение'


def test_context_index_reused_and_definitions_only_loaded_for_selected_terms(db,monkeypatch):
    publish()
    a=cc.for_meeting({})
    def fail(): raise AssertionError('large context was reloaded')
    monkeypatch.setattr(cc,'_read',fail)
    monkeypatch.setattr(g,'compact_entries',fail)
    b=cc.for_meeting({})
    assert a is not b and a.index is b.index
    assert all(entry['description']=='' for entry in b.entries)
    assert b.select('DWH')[0]['description']=='Хранилище данных'


def test_persisted_revision_invalidates_process_cache(db):
    publish()
    a=cc.for_meeting({})
    # Simulates publication by another process: no in-memory invalidation call.
    with store.connect() as conn:
        conn.execute('UPDATE glossaries SET enabled=0')
        conn.execute('UPDATE glossary_meta SET revision=revision+1')
    assert cc.for_meeting({}).entries==[]
    assert a.select('DWH')[0]['term']=='DWH'


def test_excel_sheet_mapping_and_saved_custom_column_names(db):
    book=Workbook();book.active.title='Обложка';book.active.append(['Комментарий']);book.active.append(['Глоссарий на втором листе'])
    sheet=book.create_sheet('Бизнес');sheet.append(['Объект','Смысл','Коротко']);sheet.append(['Выручка','Доход от продаж','revenue'])
    raw=io.BytesIO();book.save(raw)
    preview=g.preview('terms.xlsx',raw.getvalue(),'admin',sheet='Бизнес')
    assert preview['sheets']==['Обложка','Бизнес'] and preview['sample'][0][0]=='Выручка'
    mapping={'term':'Объект','description':'Смысл','aliases':'Коротко'}
    job=g.queue_import(preview['upload_id'],'Бизнес',mapping,'Бизнес','admin');g.run_one()
    again=g.preview('new.xlsx',raw.getvalue(),'admin',glossary_id=job['glossary_id'])
    assert again['sheet']=='Бизнес'
    assert again['mapping']==mapping
    assert g.catalog('revenue')['terms'][0]['description']=='Доход от продаж'


@pytest.mark.parametrize('content,mapping,match',[
    (b'bad\nnope',{'term':'Missing'},'отсутствует'),
    ('Название\n=1+1'.encode(),{'term':'Название'},'формулы'),
    ('Название;Название\nA;B'.encode(),{'term':'Название'},'повторяется'),
    ('Название\n'.encode(),{'term':'Название'},'пуст'),
    (('Название\n'+'a'*241).encode(),{'term':'Название'},'240'),
])
def test_invalid_rows_and_mappings_are_rejected(content,mapping,match):
    with pytest.raises(ValueError,match=match): list(g.parse_entries('test.csv',content,mapping))


def test_global_capacity_and_duplicate_pending_update_are_rejected(db,monkeypatch):
    gid=publish()
    upload(gid=gid)
    with pytest.raises(ValueError,match='уже обновляется'): upload(gid=gid)
    assert g.run_one()
    monkeypatch.setattr(g,'MAX_TERMS',1)
    upload(csv('SLA'),name='Бизнес');g.run_one()
    state=g.catalog()
    assert state['jobs'][0]['status']=='error' and state['term_count']==1


def test_only_one_import_worker_can_claim_a_job(db):
    upload()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:g.run_one(),range(2)))
    assert sorted(results)==[False,True]
    assert g.catalog()['term_count']==1


def test_crash_recovery_unlocks_import_without_publishing_partial_data(db):
    gid=publish();job=upload(gid=gid)
    with store.connect() as conn:
        conn.execute("UPDATE glossary_imports SET status='running',updated=?,owner='dead' WHERE id=?",(time.time()-121,job['id']))
    assert not g.run_one()
    state=g.catalog()
    assert state['jobs'][0]['status']=='error' and 'прерван' in state['jobs'][0]['error']
    assert state['term_count']==1
    upload(gid=gid);assert g.run_one()


def test_legacy_migration_preserves_corrections_and_is_idempotent(db):
    store.set_app_setting('corporate_context',{'enabled':True,'terms':[{'term':'MoM','aliases':['момер'],'description':'Протоколы','department':''}]})
    store.init_db();store.init_db()
    assert g.catalog()['term_count']==1
    assert 'terms' not in store.get_app_setting('corporate_context')
    context=cc.for_meeting({})
    assert context.apply({'segments':[{'text':'момер'}]})['segments'][0]['text']=='MoM'
    publish()
    cc.import_dictionary('old.csv','Термин\nSLA'.encode(),'admin')
    assert {e['term'] for e in g.catalog()['terms']}=={'DWH','SLA'}


def test_admin_preview_queue_progress_and_permission_checks(db,monkeypatch):
    client=TestClient(main.app)
    preview=client.post('/api/admin/glossaries/preview',files={'file':('terms.csv',csv())}).json()
    assert preview['mapping']['term']=='Название'
    response=client.post('/api/admin/glossaries/import',json={'upload_id':preview['upload_id'],'name':'Данные','mapping':preview['mapping'],'sheet':''})
    assert response.status_code==202
    assert client.get('/api/admin/glossaries').json()['jobs'][0]['status']=='queued'
    g.run_one()
    state=client.get('/api/admin/glossaries?q=хран').json()
    assert state['matched_terms']==1 and state['jobs'][0]['received']==1
    with monkeypatch.context() as patch:
        patch.delitem(main.app.dependency_overrides,main.require_admin)
        for path in ('/api/admin/glossaries','/api/admin/glossaries/preview','/api/admin/glossaries/import'):
            response=client.get(path) if path.endswith('glossaries') else client.post(path)
            assert response.status_code==401


def test_cleanup_preserves_generations_while_meeting_is_processing(db):
    gid=publish();old=cc.for_meeting({})
    publish(csv('DLH'),gid=gid)
    with store.connect() as conn:
        conn.execute('UPDATE glossary_generations SET created=0')
    store.create_meeting('work','test','test.wav','test')
    store.update_meeting('work',status='transcribing')
    g.cleanup_one()
    assert old.select('DWH')[0]['description']=='Хранилище данных'
    store.update_meeting('work',status='done')
    g.cleanup_one()
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM glossary_entries').fetchone()[0]==1
    assert g.catalog()['terms'][0]['term']=='DLH'


def test_large_new_general_glossary_does_not_inject_unrelated_initial_hints(db):
    publish(csv('DWH',area=''))
    assert cc.for_meeting({}).hotwords('Запись встречи 001')==''
    assert cc.for_meeting({}).hotwords('Обсудим хранилище')=='DWH'


def test_department_views_do_not_mutate_shared_index_or_guess_anonymous_department():
    people=[{'name':'Без почты','department':'Продажи'}, {'name':'Анна','email':'a@example.com','department':'ИТ'}]
    terms=[{'term':'Кластер','aliases':[],'description':'','department':'ИТ'}]
    anonymous=cc.Context(terms,people,1,owned=True)
    assert anonymous.department=='' and '' not in anonymous.departments
    assert anonymous.select('Без почты')[0]['source']=='ad'
    assert anonymous.select()==[]


def test_sqlite_backup_contains_glossaries_and_search_index(db,monkeypatch):
    from backend import backup
    publish()
    monkeypatch.setattr(config,'DB_PATH',store.DB_PATH)
    target=db/'copy.db'
    backup._backup_db(target)
    monkeypatch.setattr(store,'DB_PATH',target)
    assert g.catalog('хран')['terms'][0]['term']=='DWH'


def test_count_cache_does_not_hide_new_results_after_import(db):
    gid=publish()
    assert g.catalog('DLH')['matched_terms']==0
    publish(csv('DLH'),gid=gid)
    assert g.catalog('DLH')['matched_terms']==1
    assert len(g.catalog()['jobs'])==1
