"""Compare CPU ASR with/without the same corporate dictionary on reference audio."""
import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, corporate_context, local_asr


def word_errors(reference, hypothesis):
    ref = corporate_context.normalize(reference).split()
    hyp = corporate_context.normalize(hypothesis).split()
    row = list(range(len(hyp) + 1))
    for index, word in enumerate(ref, 1):
        following = [index]
        for column, actual in enumerate(hyp, 1):
            following.append(min(following[-1] + 1, row[column] + 1,
                                 row[column - 1] + (word != actual)))
        row = following
    return row[-1], len(ref)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=("whisper", "gigaam"), default="whisper")
    parser.add_argument("--model", help="Local faster-whisper model path")
    parser.add_argument("--cache", type=Path, help="GigaAM data directory containing hf/")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.engine == "whisper":
        if not args.model:
            parser.error("--model is required for Whisper")
        from faster_whisper import WhisperModel
        local_asr._model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=4, num_workers=1)
        local_asr._model_size = config.local_whisper_size()
        transcribe = local_asr.transcribe_local_sync
    else:
        if not args.cache:
            parser.error("--cache is required for GigaAM")
        from backend import gigaam_asr
        config.DATA_DIR = args.cache
        config.TORCH_CPU_THREADS = 4
        config.WHISPER_MODEL = config.GIGAAM_MODEL
        def transcribe(path, language="ru", hotwords=""):
            return gigaam_asr.transcribe_gigaam_sync(path, language=language)
    context = corporate_context.Context(corporate_context.parse_dictionary(args.dictionary.name,
                                       args.dictionary.read_bytes()), [], 1)
    samples = json.loads(args.manifest.read_text())
    transcribe(Path(samples[0]["path"]), language="ru")
    duration = 0
    import soundfile
    for sample in samples:
        duration += soundfile.info(sample["path"]).duration
    metadata = {"platform": platform.platform(), "engine": args.engine, "cpu_threads": 4,
                "audio_seconds": duration, "dictionary_sha256": hashlib.sha256(args.dictionary.read_bytes()).hexdigest(),
                "sample_ids": [sample["id"] for sample in samples]}
    results = []
    for repeat in range(args.repeats):
        for index, sample in enumerate(samples):
            for enabled in ([False, True] if repeat % 2 == 0 else [True, False]):
                start = time.perf_counter()
                payload = transcribe(Path(sample["path"]), language="ru",
                            hotwords=context.hotwords(sample.get("title", "")) if enabled else "")
                raw = payload["text"]
                if enabled:
                    payload = context.apply(payload)
                text = " ".join(segment["text"] for segment in payload["segments"])
                elapsed = time.perf_counter() - start
                errors, words = word_errors(sample["reference"], text)
                results.append({"repeat": repeat, "sample": index, "context": enabled, "seconds": elapsed,
                                "errors": errors, "words": words, "text": text, "raw": raw})
                print(json.dumps({"repeat": repeat, "sample": index, "context": enabled,
                                  "seconds": round(elapsed, 3), "errors": errors}, ensure_ascii=False), flush=True)
                args.output.write_text(json.dumps({**metadata, "results": results}, ensure_ascii=False, indent=2))
    for enabled in (False, True):
        rows = [row for row in results if row["context"] == enabled]
        totals = [sum(row["seconds"] for row in rows if row["repeat"] == repeat) for repeat in range(args.repeats)]
        print("context", enabled, "WER", sum(row["errors"] for row in rows) / sum(row["words"] for row in rows),
              "median total seconds", statistics.median(totals), flush=True)


if __name__ == "__main__":
    main()
