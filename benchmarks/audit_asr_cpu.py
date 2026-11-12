"""Check the GigaAM inference path against a git baseline on existing PCM fixtures.

Requires the model environment described in benchmarks/README.md; no download.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
from backend import config, gigaam_asr
from benchmarks.context_cpu import word_errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--baseline', default='9782dfee503ac6a82f701e74bdd1d3fcf8702114')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config.DATA_DIR = args.cache
    config.WHISPER_MODEL = config.GIGAAM_MODEL
    config.TORCH_CPU_THREADS = 4
    code = subprocess.check_output(['git', 'show', f'{args.baseline}:backend/gigaam_asr.py'], cwd=ROOT, text=True)
    baseline = types.ModuleType('backend.audit_baseline')
    baseline.__package__ = 'backend'
    baseline.__file__ = str(ROOT/'backend/gigaam_asr.py')
    exec(compile(code, baseline.__file__, 'exec'), baseline.__dict__)
    model = gigaam_asr._load()
    baseline._load = lambda *args: model
    samples = json.loads(args.manifest.read_text())
    gigaam_asr.transcribe_gigaam_sync(Path(samples[0]['path']), 'ru')  # warm up
    rows = []
    for repeat in range(2):
        for sample in samples:
            path = Path(sample['path'])
            for label, module in ([('before', baseline), ('after', gigaam_asr)] if repeat == 0 else
                                  [('after', gigaam_asr), ('before', baseline)]):
                start = time.perf_counter()
                result = module.transcribe_gigaam_sync(path, 'ru')
                seconds = time.perf_counter() - start
                errors, words = word_errors(sample['reference'], result['text'])
                rows.append(dict(repeat=repeat, sample=sample['id'], mode=label, seconds=seconds,
                                 errors=errors, words=words, text=result['text'], segments=result['segments'],
                                 audio_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    summary = {}
    for label in ('before','after'):
        selected = [r for r in rows if r['mode']==label]
        summary[label] = dict(wer=sum(r['errors'] for r in selected)/sum(r['words'] for r in selected),
                             median_seconds=statistics.median(sum(r['seconds'] for r in selected if r['repeat']==n)
                                                              for n in range(2)))
    identical = all(next(r for r in rows if r['sample']==sample['id'] and r['mode']=='before' and r['repeat']==repeat)['segments'] ==
                    next(r for r in rows if r['sample']==sample['id'] and r['mode']=='after' and r['repeat']==repeat)['segments']
                    for sample in samples for repeat in range(2))
    for row in rows:
        row['transcript_sha256'] = hashlib.sha256(json.dumps(row.pop('segments'), ensure_ascii=False,
                                                           sort_keys=True).encode()).hexdigest()
        row.pop('text')
    report = dict(baseline=args.baseline, cpu_threads=4, samples=len(samples), identical=identical, summary=summary, rows=rows)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='rows'}, ensure_ascii=False), flush=True)
    if not identical:
        raise SystemExit('ASR regression: outputs differ')


if __name__ == '__main__': main()
