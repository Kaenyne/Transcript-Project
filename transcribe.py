#!/usr/bin/env python3
"""Transcribe an audio file to text using faster-whisper (CPU)."""
import sys
import time
import datetime
from faster_whisper import WhisperModel

AUDIO = sys.argv[1] if len(sys.argv) > 1 else "audio.mp3"
MODEL_SIZE = sys.argv[2] if len(sys.argv) > 2 else "small"
OUT_PREFIX = sys.argv[3] if len(sys.argv) > 3 else "transcript"


def fmt_ts(seconds: float) -> str:
    td = datetime.timedelta(seconds=round(seconds))
    return str(td)


def main() -> None:
    start = time.time()
    print(f"Loading model '{MODEL_SIZE}' ...", flush=True)
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=4)

    print(f"Transcribing {AUDIO} ...", flush=True)
    segments, info = model.transcribe(AUDIO, beam_size=5, vad_filter=True)
    print(f"Detected language: {info.language} (p={info.language_probability:.2f}), "
          f"duration={fmt_ts(info.duration)}", flush=True)

    plain_lines = []
    ts_lines = []
    for seg in segments:
        text = seg.text.strip()
        ts_lines.append(f"[{fmt_ts(seg.start)} -> {fmt_ts(seg.end)}] {text}")
        plain_lines.append(text)
        # progress heartbeat
        print(f"  {fmt_ts(seg.end)} / {fmt_ts(info.duration)}", flush=True)

    with open(f"{OUT_PREFIX}.txt", "w") as f:
        f.write("\n".join(plain_lines) + "\n")
    with open(f"{OUT_PREFIX}_timestamped.txt", "w") as f:
        f.write("\n".join(ts_lines) + "\n")

    elapsed = time.time() - start
    print(f"DONE in {elapsed:.0f}s. Wrote {OUT_PREFIX}.txt and {OUT_PREFIX}_timestamped.txt", flush=True)


if __name__ == "__main__":
    main()
