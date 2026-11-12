"""Compare the prior context implementation with a large indexed glossary on CPU."""
import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import config, corporate_context as cc, glossary as g, local_asr, store
from context_cpu import word_errors


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline',type=Path,required=True);parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--model',required=True);parser.add_argument('--dictionary',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    old={'__name__':'backend.context_baseline','__package__':'backend'}
    exec(compile(args.baseline.read_text(),str(args.baseline),'exec'),old)
    terms=cc.parse_dictionary(args.dictionary.name,args.dictionary.read_bytes())
    baseline=old['Context'](terms,[],1)
    with tempfile.TemporaryDirectory(prefix='mom-glossary-asr-') as temp:
        config.DATA_DIR=Path(temp);store.DB_PATH=Path(temp)/'mom.db';config.LDAP_ENABLED=False;store.init_db()
        cc.import_dictionary(args.dictionary.name,args.dictionary.read_bytes(),'benchmark')
        content=('Термин;Определение\n'+''.join(f'Синтетический показатель {i:06d};Описание показателя\n' for i in range(100000-len(terms)))).encode()
        preview=g.preview('scale.csv',content,'benchmark');g.queue_import(preview['upload_id'],'Масштаб',preview['mapping'],'','benchmark');g.run_one()
        current=cc.for_meeting({});assert g.catalog()['term_count']==100000
        from faster_whisper import WhisperModel
        config.WHISPER_MODEL='local:small'
        local_asr._model=WhisperModel(args.model,device='cpu',compute_type='int8',cpu_threads=4,num_workers=1)
        local_asr._model_size=config.local_whisper_size()
        samples=json.loads(args.manifest.read_text())[:12]
        local_asr.transcribe_local_sync(Path(samples[0]['path']),language='ru')
        results=[]
        for repeat in range(2):
            for number,sample in enumerate(samples):
                assert baseline.hotwords(sample.get('title',''))==current.hotwords(sample.get('title',''))
                texts={}
                for label,context in ([('before',baseline),('after',current)] if repeat==0 else [('after',current),('before',baseline)]):
                    start=time.perf_counter()
                    document=local_asr.transcribe_local_sync(Path(sample['path']),language='ru',hotwords=context.hotwords(sample.get('title','')))
                    result=context.apply(document)
                    elapsed=time.perf_counter()-start
                    text=' '.join(segment['text'] for segment in result['segments']);texts[label]=text
                    errors,words=word_errors(sample['reference'],text)
                    results.append(dict(repeat=repeat,sample=number,mode=label,seconds=elapsed,errors=errors,words=words,
                        transcript_sha256=hashlib.sha256(text.encode()).hexdigest()))
                assert texts['before']==texts['after'],number
                print(f'repeat={repeat} sample={number} identical=True',flush=True)
        output={'engine':'faster-whisper small int8','device':'cpu','threads':4,'terms':100000,'samples':len(samples),
            'repeats':2,'exact_transcript_match':True,'samples_sha256':[hashlib.sha256(Path(s['path']).read_bytes()).hexdigest() for s in samples],
            'median_total_seconds':{mode:statistics.median(sum(row['seconds'] for row in results if row['mode']==mode and row['repeat']==repeat) for repeat in range(2)) for mode in ('before','after')},
            'wer':{mode:sum(r['errors'] for r in results if r['mode']==mode)/sum(r['words'] for r in results if r['mode']==mode) for mode in ('before','after')},'runs':results}
        args.output.write_text(json.dumps(output,indent=2)+'\n');print(json.dumps({k:v for k,v in output.items() if k not in ('runs','samples_sha256')}),flush=True)

if __name__=='__main__':main()
