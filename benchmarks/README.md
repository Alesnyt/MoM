# Corporate context: CPU validation, 2026-11-11

## Result

Apple M1 Pro, macOS arm64, Python 3.12. CPU only, 4 inference threads, warmed models. Each sample was run twice in both modes; order was baseline/context on the first repeat and context/baseline on the second. Timings include ASR, hint selection and alias normalization, excluding model download/load, audio preparation, AD network sync and protocol generation.

| Engine / dataset | Audio | Word error rate, baseline → context | Median total processing time, baseline → context |
| --- | --- | --- | --- |
| faster-whisper small / 50 real Russian clips | 621.24 s | 9.84% → 9.18% | 100.64 s → 104.97 s (+4.30%) |
| GigaAM CTC / first 12 real Russian clips | 157.98 s | 9.88% → 9.88% | 8.27 s → 8.12 s |
| GigaAM CTC / one synthetic corporate example | 13.90 s | 40% → 36% | 0.671 s → 0.666 s |

Whisper improved aggregate WER by 0.66 percentage points (6.73% relative), with 11 improved clips, 8 worse clips and 31 unchanged clips by word-error count. The two repeats produced the same error counts. This is a small public speech benchmark, **not a guarantee of improvement on every phrase or on corporate meetings**. The first 12 clips were an initial smoke measurement; the remaining 38 extended the check without changing the dictionary.

GigaAM's real-speech transcripts were identical with and without context on all 12 clips. The selected dictionary had no exact alias to replace in those outputs. Its measured timing difference is small run-to-run variation, not an ASR speed improvement. On the separate synthetic example only `труконф` was corrected to `TrueConf`; other misheard spellings were intentionally left untouched. That example checks the conservative postprocessing path, not real-meeting recognition quality. GigaAM's acoustic inference/decoder is unchanged in this version.

WER uses word-level edit distance after case folding, punctuation removal and `ё → е`. Numbers are not expanded; inflections and different Latin/Cyrillic spellings count as differences. Repeats are not additional unique samples. Raw per-run measurements are in [context_cpu_results.csv](context_cpu_results.csv); sample IDs and hashes are in [context_samples.json](context_samples.json). No corporate recordings or directory data are included.

## Context cost

A synthetic catalog of 5,000 terms (one alias each), 50,000 employees and a transcript of 1,000 turns was tested ten times. Median CPU wall time:

- Build the per-task lookup index: **238.54 ms**.
- Select the initial bounded hints: **0.75 ms**.
- Select context and normalize all 1,000 turns: **13.80 ms**.

These are in-memory costs, excluding SQLite loading and LDAP. Index construction and transcript selection run outside the async web/worker event loop. See [context_scale_results.json](context_scale_results.json) and `python benchmarks/context_scale.py`.

## Reproduction

Source: [Google FLEURS, ru_ru test](https://huggingface.co/datasets/google/fleurs), CC BY 4.0. The first 50 rows of the Russian test Parquet were used in source order, without filtering by results. Download:

```sh
curl -L 'https://huggingface.co/datasets/google/fleurs/resolve/refs%2Fconvert%2Fparquet/ru_ru/test/0000.parquet' -o ru-test.parquet
```

Parquet SHA-256: `1e5bbee150c05a219c6c318e95d765886d34b4d179e8ab06dbecdb2791ac9a5d`. Extraction requires `pyarrow==21.0.0`. The model environment used faster-whisper 1.2.1, CTranslate2 4.9.0, PyAV 18.0.0, Torch 2.8.0 and Transformers 4.57.6. Whisper used int8 and one worker. GigaAM used its existing float32 CPU inference path.

Whisper: download `config.json`, `model.bin`, `tokenizer.json` and `vocabulary.txt` from `Systran/faster-whisper-small` at commit `536b0662742c02347bc0e980a01041f333bce120` into a local model directory. GigaAM uses the repository-pinned `ctc` commit `2f8a57144e6ec3adfd32fe0484d9ea9913305bc8` and verified vendored code. The dictionary below was fixed before the first run and retained for the extended sample.

```sh
python benchmarks/prepare_context_audio.py ru-test.parquet /tmp/mom-fleurs --count 50
python benchmarks/context_cpu.py --model /path/to/whisper-small \
  --manifest /tmp/mom-fleurs/manifest.json --dictionary benchmarks/context_dictionary.csv \
  --output /tmp/whisper-context-results.json

python benchmarks/prepare_context_audio.py ru-test.parquet /tmp/mom-fleurs-pcm --count 12 --pcm16
python benchmarks/context_cpu.py --engine gigaam --cache /tmp/mom-context-models \
  --manifest /tmp/mom-fleurs-pcm/manifest.json --dictionary benchmarks/context_dictionary.csv \
  --output /tmp/gigaam-context-results.json
```

Whisper reads the original float WAV files. GigaAM receives mono 16 kHz PCM16 converted with ffmpeg, as required by the application's input path. Within each engine, baseline and context use the same audio bytes and model settings. The synthetic example was generated using macOS Milena at 150 words/minute from phonetic spellings of the eight dictionary terms; its result is separate from FLEURS.

Tests cover atomic CSV/XLSX replacement, invalid files, ambiguous aliases, disabled context, employee selection, LDAP pagination and partial failure, directory scope changes, cross-process sync ownership, authenticated admin endpoints, context propagation from recognition to protocol, and bounded LLM input. The admin interface and substring search were also checked in a local browser. Actual production AD and protocol quality on the deployed LLM were not benchmarked locally.

The first real Whisper run exposed an existing dependency incompatibility: PyAV 19 removed `metadata_errors`, still passed by faster-whisper 1.2.1. `av>=11,<19` now prevents that runtime failure; model/decoder settings remain unchanged.

## Локальный поиск спикеров в AD-кэше

`python benchmarks/directory_lookup.py` создаёт изолированную временную SQLite-базу с 50 000 вымышленными сотрудниками, вызывает реальный поиск подсказок и запрещает сетевые LDAP-запросы. Проверяются широкие и частичные запросы по имени/email и отсутствие совпадений.

Локальный CPU-прогон на Mac 11.11.2026 (100 запросов): первое обращение с построением индекса — 162,58 мс, медиана последующих — 25,00 мс, p95 — 73,81 мс; LDAP-запросов — 0. Это время серверного поиска, без задержки ввода 250 мс и сети браузера. Корпоративный AD в замере не участвовал.
