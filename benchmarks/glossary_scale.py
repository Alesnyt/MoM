"""CPU import/search/meeting-context benchmark with isolated synthetic data."""
import argparse
import json
import platform
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import config, corporate_context as cc, glossary as g, store


def stats(values):
    return {'median_ms':round(statistics.median(values),2),'p95_ms':round(sorted(values)[int((len(values)-1)*.95)],2),'max_ms':round(max(values),2)}


def timed(fn):
    start=time.perf_counter();result=fn();return (time.perf_counter()-start)*1000,result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--size',type=int,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();process=psutil.Process();rss=[];done=threading.Event()
    def monitor():
        while not done.wait(.02):rss.append(process.memory_info().rss)
    thread=threading.Thread(target=monitor);thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='mom-glossary-scale-') as temp:
            config.DATA_DIR=Path(temp);store.DB_PATH=Path(temp)/'mom.db';config.LDAP_ENABLED=False;store.init_db()
            before=process.memory_info().rss
            content=('Термин;Синонимы;Определение;Область\n'+''.join(
                f'Показатель {i:06d};метрика{i:06d}|metric{i:06d};Описание показателя для корпоративной отчётности и аналитики;Данные\n' for i in range(args.size))).encode()
            preview=g.preview('data.csv',content,'benchmark');job=g.queue_import(preview['upload_id'],'Данные',preview['mapping'],'','benchmark')
            import_ms,_=timed(g.run_one);assert g.catalog()['term_count']==args.size
            queries=[f'метрика{args.size-1:06d}','Показатель 000','корпоративной','ка000','неизвестное','ик','metric000']*5
            search=[]
            for q in queries:
                elapsed,result=timed(lambda:g.catalog(q));search.append(elapsed)
                if q=='неизвестное':assert result['matched_terms']==0
            cold_ms,context=timed(lambda:cc.for_meeting({}))
            calls=[];selection=[]
            text=' '.join(f'Обсудили метрика{i%args.size:06d} и планы отдела.' for i in range(1000))
            for _ in range(30):
                elapsed,current=timed(lambda:cc.for_meeting({}));calls.append(elapsed);assert context.index is current.index
                elapsed,result=timed(lambda:current.select(text));selection.append(elapsed);assert len(result)<=30
            def concurrent(q):return timed(lambda:g.catalog(q))[0]
            with ThreadPoolExecutor(max_workers=4) as pool:concurrency=list(pool.map(concurrent,queries))
            # Update the same source while four request threads select/read the old corpus.
            preview=g.preview('data.csv',content,'benchmark',glossary_id=job['glossary_id'])
            g.queue_import(preview['upload_id'],'Данные',preview['mapping'],'','benchmark')
            updating=[]
            with ThreadPoolExecutor(max_workers=5) as pool:
                future=pool.submit(g.run_one)
                def during(i):
                    elapsed,result=timed(lambda:(g.catalog(queries[i%len(queries)]),cc.for_meeting({}).select('метрика000001')))
                    assert result[0]['term_count']==args.size
                    return elapsed
                updating=list(pool.map(during,range(80)))
                assert future.result()
            result={'platform':platform.platform(),'cpu':platform.machine(),'terms':args.size,'csv_mb':round(len(content)/1e6,2),
                'import_seconds':round(import_ms/1000,2),'search':stats(search),'four_readers':stats(concurrency),
                'four_readers_during_import':stats(updating),'cold_context_ms':round(cold_ms,2),'warm_context':stats(calls),
                'select_1000_turns':stats(selection),'rss_start_mb':round(before/1e6,1),'rss_peak_mb':round(max(rss)/1e6,1),
                'rss_end_mb':round(process.memory_info().rss/1e6,1),'database_mb':round(store.DB_PATH.stat().st_size/1e6,1)}
            args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False),flush=True)
    finally:done.set();thread.join()

if __name__=='__main__':main()
