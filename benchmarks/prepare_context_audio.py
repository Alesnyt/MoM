"""Extract an ordered public FLEURS subset; never select clips by recognition result."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parquet", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--pcm16", action="store_true", help="Convert with ffmpeg for GigaAM's PCM input")
    args = parser.parse_args()
    import pyarrow.parquet as pq
    rows = pq.read_table(args.parquet).slice(0, args.count).to_pylist()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index, row in enumerate(rows):
        path = (args.output / f"ru-{index:02}.wav").resolve()
        source = path.with_suffix(".source.wav") if args.pcm16 else path
        source.write_bytes(row["audio"]["bytes"])
        if args.pcm16:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(path)], check=True)
        manifest.append({"path": str(path), "reference": row["transcription"], "id": row["id"],
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
