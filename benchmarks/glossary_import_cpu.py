"""Measure CPU ASR while a 100k-term glossary is being replaced in background."""
import argparse
import json
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import config, corporate_context as cc, glossary as g, local_asr, store


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model',required=True);parser.add_argument('--manifest',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();samples=json.loads(args.manifest.read_text());samples=[samples[0],samples[7]]
    with tempfile.TemporaryDirectory(prefix='mom-glossary-overlap-') as folder:
        config.DATA_DIR=Path(folder);store.DB_PATH=Path(folder)/'mom.db';config.LDAP_ENABLED=False;store.init_db()
        content=('Термин;Определение\n'+''.join(f'Показатель {i:06d};Корпоративный показатель для аналитики\n' for i in range(100000))).encode()
        p=g.preview('data.csv',content,'benchmark');job=g.queue_import(p['upload_id'],'Данные',p['mapping'],'','benchmark');g.run_one()
        context=cc.for_meeting({})
        from faster_whisper import WhisperModel
        config.WHISPER_MODEL='local:small';local_asr._model=WhisperModel(args.model,device='cpu',compute_type='int8',cpu_threads=4,num_workers=1)
        local_asr._model_size=config.local_whisper_size()
        local_asr.transcribe_local_sync(Path(samples[0]['path']),language='ru')
        records=[];texts={}
        for repeat in range(2):
            for mode in (['quiet','import'] if repeat==0 else ['import','quiet']):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future=None
                    if mode=='import':
                        store.create_meeting(f'active-{repeat}','benchmark','audio.wav','benchmark')
                        store.update_meeting(f'active-{repeat}',status='transcribing')
                        p=g.preview('data.csv',content,'benchmark',glossary_id=job['glossary_id'])
                        g.queue_import(p['upload_id'],'Данные',p['mapping'],'','benchmark');future=pool.submit(g.run_one)
                    for index,sample in enumerate(samples):
                        start=time.perf_counter()
                        result=local_asr.transcribe_local_sync(Path(sample['path']),language='ru',hotwords=context.hotwords())
                        result=context.apply(result);elapsed=time.perf_counter()-start
                        text=' '.join(s['text'] for s in result['segments'])
                        if index in texts:assert texts[index]==text
                        texts[index]=text
                        records.append(dict(repeat=repeat,mode=mode,sample=index,seconds=elapsed))
                    if future:
                        progress=g.catalog()['jobs'][0]
                        assert progress['status']=='running' and progress['received']>0,progress
                        store.update_meeting(f'active-{repeat}',status='done');assert future.result()
        totals={mode:statistics.median(sum(r['seconds'] for r in records if r['mode']==mode and r['repeat']==repeat) for repeat in range(2)) for mode in ('quiet','import')}
        output={'device':'cpu','threads':4,'terms':100000,'samples':2,'repeats':2,'exact_transcript_match':True,'median_total_seconds':totals,
                'difference_percent':(totals['import']/totals['quiet']-1)*100,'runs':records}
        args.output.write_text(json.dumps(output,indent=2)+'\n');print(json.dumps(output),flush=True)

if __name__=='__main__':main()
