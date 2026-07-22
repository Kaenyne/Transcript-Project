"""Downloading and preparing audio, with no system ffmpeg required.

faster-whisper decodes via PyAV (which bundles the FFmpeg libraries), so local
transcription can read a podcast MP3 straight off disk. The only real work here
is for the cloud backends, which cap upload size — those need the audio
downsampled to speech-grade mono and split into chunks.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import requests

log = logging.getLogger(__name__)

USER_AGENT = "transcript-project/0.1 (+personal weekly digest)"

# Speech recognition gains nothing above 16 kHz mono; Whisper resamples to
# exactly this internally. 32 kbps mono MP3 is ~14 MB/hour, so a typical
# episode lands comfortably under any provider's cap.
TARGET_RATE = 16_000
TARGET_BITRATE = 32_000
CHUNK_SECONDS = 30 * 60


class AudioError(Exception):
    pass


def download(url: str, dest: Path, *, max_mb: int = 500) -> Path:
    """Stream an episode to disk. Returns the path, skipping an existing file."""
    if dest.exists() and dest.stat().st_size > 0:
        log.debug("audio already present: %s", dest.name)
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    with requests.get(
        url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=60
    ) as r:
        r.raise_for_status()
        declared = int(r.headers.get("Content-Length") or 0)
        if declared and declared > max_mb * 1024 * 1024:
            raise AudioError(f"{url} is {declared / 1e6:.0f} MB, over the {max_mb} MB cap")

        written = 0
        with tmp.open("wb") as fh:
            for block in r.iter_content(chunk_size=1 << 16):
                if not block:
                    continue
                written += len(block)
                if written > max_mb * 1024 * 1024:
                    tmp.unlink(missing_ok=True)
                    raise AudioError(f"{url} exceeded the {max_mb} MB cap mid-download")
                fh.write(block)

    if written == 0:
        tmp.unlink(missing_ok=True)
        raise AudioError(f"{url} returned an empty body")

    tmp.replace(dest)
    log.info("downloaded %.1f MB -> %s", written / 1e6, dest.name)
    return dest


def duration_seconds(path: Path) -> float | None:
    import av

    try:
        with av.open(str(path)) as container:
            if container.duration:
                return container.duration / 1_000_000
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream and stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
    except Exception as e:
        log.debug("could not read duration of %s: %s", path.name, e)
    return None


def compress_for_upload(src: Path, dest: Path) -> Path:
    """Re-encode to 16 kHz mono MP3 — typically a 5-10x size reduction.

    Cloud ASR endpoints cap request size, and a 128 kbps stereo episode blows
    through that. Uses PyAV directly so no ffmpeg binary is needed.
    """
    import av

    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    with av.open(str(src)) as inp, av.open(str(dest), mode="w") as out:
        in_stream = next((s for s in inp.streams if s.type == "audio"), None)
        if in_stream is None:
            raise AudioError(f"{src.name} contains no audio stream")

        out_stream = out.add_stream("libmp3lame", rate=TARGET_RATE)
        out_stream.bit_rate = TARGET_BITRATE
        out_stream.layout = "mono"

        resampler = av.audio.resampler.AudioResampler(
            format="s16p", layout="mono", rate=TARGET_RATE
        )

        for frame in inp.decode(in_stream):
            frame.pts = None
            for resampled in resampler.resample(frame):
                for packet in out_stream.encode(resampled):
                    out.mux(packet)

        for packet in out_stream.encode(None):
            out.mux(packet)

    log.info(
        "compressed %.1f MB -> %.1f MB (%s)",
        src.stat().st_size / 1e6,
        dest.stat().st_size / 1e6,
        dest.name,
    )
    return dest


def split(src: Path, out_dir: Path, seconds: int = CHUNK_SECONDS) -> list[Path]:
    """Split audio into fixed-length parts, returning them in order.

    Only used when a compressed episode still exceeds the provider's cap.
    Chunks are cut on time, not on silence, so a sentence can straddle a
    boundary — acceptable for summarization, and why local ASR (which never
    chunks) stays the default.
    """
    import av

    total = duration_seconds(src)
    if total is None:
        raise AudioError(f"cannot determine duration of {src.name} to split it")

    count = max(1, math.ceil(total / seconds))
    if count == 1:
        return [src]

    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []

    for index in range(count):
        part = out_dir / f"{src.stem}.part{index:02d}.mp3"
        parts.append(part)
        if part.exists() and part.stat().st_size > 0:
            continue

        start = index * seconds
        end = min(total, start + seconds)

        with av.open(str(src)) as inp, av.open(str(part), mode="w") as out:
            in_stream = next(s for s in inp.streams if s.type == "audio")
            out_stream = out.add_stream("libmp3lame", rate=TARGET_RATE)
            out_stream.bit_rate = TARGET_BITRATE
            out_stream.layout = "mono"

            resampler = av.audio.resampler.AudioResampler(
                format="s16p", layout="mono", rate=TARGET_RATE
            )
            inp.seek(int(start * 1_000_000))

            for frame in inp.decode(in_stream):
                if frame.time is None:
                    continue
                if frame.time < start:
                    continue
                if frame.time >= end:
                    break
                frame.pts = None
                for resampled in resampler.resample(frame):
                    for packet in out_stream.encode(resampled):
                        out.mux(packet)

            for packet in out_stream.encode(None):
                out.mux(packet)

    log.info("split %s into %d parts", src.name, len(parts))
    return parts
