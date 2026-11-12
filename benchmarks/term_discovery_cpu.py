"""CPU/storage benchmark for discovery bookkeeping, with a deterministic model stub.

No network, production database, audio recognition or LLM inference. This measures
only the local incremental scan / evidence / review path, not model quality.
"""
import argparse
import asyncio
import json
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import config, store, glossary, term_discovery as td


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--meetings',type=int,default=100);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        config.DATA_DIR=Path(folder);store.DB_PATH=config.DATA_DIR/'mom.db';store.init_db()
        for index in range(args.meetings):
            segments=[{'text':f'В системе файн би строим отчёт. В ДВХ проверим таблицу номер {turn}.', 'start':turn*10,'end':turn*10+8} for turn in range(120)]
            store.create_text_meeting(f'm{index}',f'Встреча {index}','test.wav','benchmark',{'segments':segments})
        td.settings(False,'03:30',500,'benchmark');td.queue('benchmark')
        calls=0
        async def stub(parts):
            nonlocal calls
            calls+=1
            return td.validate_candidates({'candidates':[{'turn':p['id'],'term':'файн би'} for p in parts[:30]]},parts)
        td.extract=stub
        async def scan():
            while td.snapshot()['job']['status'] in ('queued','running','paused'):await td.step()
        start=time.perf_counter();cpu=time.process_time();asyncio.run(scan())
        elapsed=time.perf_counter()-start;cpu_seconds=time.process_time()-cpu
        state=td.snapshot();assert state['proposals'][0]['meetings']==args.meetings
        calls_first=calls;td.queue('benchmark');start=time.perf_counter();asyncio.run(scan());second=time.perf_counter()-start
        assert calls==calls_first
        latencies=[]
        for _ in range(40):
            start=time.perf_counter();td.snapshot();latencies.append((time.perf_counter()-start)*1000)
        v=state['proposals'][0]['variants'][0]
        start=time.perf_counter();td.review([{'id':v['id'],'revision':v['revision']}],'approve','FineBI','Отчётность','',False,'benchmark');approval=time.perf_counter()-start
        result=dict(meetings=args.meetings,turns=args.meetings*120,model='deterministic stub, no inference',
                    first_scan_seconds=round(elapsed,3),cpu_seconds=round(cpu_seconds,3),model_batches=calls_first,
                    second_scan_seconds=round(second,3),second_scan_model_calls=calls-calls_first,
                    snapshot_median_ms=round(statistics.median(latencies),2),snapshot_p95_ms=round(sorted(latencies)[37],2),
                    approval_ms=round(approval*1000,2),peak_rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**2 if sys.platform=='darwin' else 1024),1))
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
