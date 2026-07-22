"""Stage 2: get text for every pulled episode.

Works down a ladder, cheapest first, and stops at the first thing that yields
usable text:

  1. a transcript the publisher already shipped in the feed   (free, instant)
  2. YouTube caption tracks                                   (free, instant)
  3. speech recognition on the audio                          (slow or paid)

Roughly a third of episodes never reach step 3, so the ordering matters more
than the speed of any single backend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import audio, text
from .state import State

log = logging.getLogger(__name__)

USER_AGENT = "transcript-project/0.1 (+personal weekly digest)"

# Below this, we assume we fetched an error page or a stub rather than a real
# transcript, and fall through to the next rung of the ladder.
MIN_USABLE_WORDS = 120


@dataclass
class TranscribeResult:
    written: list[str] = field(default_factory=list)
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    by_origin: dict[str, int] = field(default_factory=dict)


def transcribe_pending(
    state: State,
    out_dir: Path,
    audio_dir: Path,
    *,
    backend=None,
    limit: int | None = None,
    allow_asr: bool = True,
) -> TranscribeResult:
    result = TranscribeResult()
    session = requests.Session()
    pending = state.pending("transcribed")

    if limit is not None:
        pending = pending[:limit]

    for uid in pending:
        record = state.get(uid) or {}
        episode = record.get("episode")
        if not episode:
            result.failed.append((uid, "no episode payload in state"))
            continue

        dest = out_dir / f"{uid}.md"
        if dest.exists() and dest.stat().st_size > 0:
            state.set_stage(uid, "transcribed", transcript_path=str(dest))
            result.skipped += 1
            continue

        title = episode.get("title", uid)
        try:
            body, origin = _resolve_text(
                episode,
                session=session,
                audio_dir=audio_dir,
                backend=backend,
                allow_asr=allow_asr,
                priority=record.get("priority", "normal"),
            )
        except Exception as e:
            log.warning("transcribe failed for %s: %s", title[:60], e)
            result.failed.append((title, str(e)))
            continue

        if not body:
            result.skipped += 1
            continue

        _write(dest, episode, body, origin, record)
        state.set_stage(
            uid,
            "transcribed",
            transcript_path=str(dest),
            transcript_origin=origin,
            word_count=text.word_count(body),
        )
        result.written.append(title)
        result.by_origin[origin] = result.by_origin.get(origin, 0) + 1
        log.info("wrote %s (%s, %d words)", dest.name, origin, text.word_count(body))

    return result


def _resolve_text(
    episode: dict,
    *,
    session: requests.Session,
    audio_dir: Path,
    backend,
    allow_asr: bool,
    priority: str,
) -> tuple[str, str]:
    """Walk the ladder. Returns (text, origin) or ("", reason)."""
    title = episode.get("title", "?")

    # --- rung 1 & 2: text that already exists somewhere ---
    for ref in episode.get("transcripts", []):
        origin = ref.get("origin")
        try:
            if origin == "youtube_captions":
                body = _youtube_captions(episode)
            else:
                body = _fetch_text(ref, session=session)
        except Exception as e:
            log.debug("%s: %s ref failed: %s", title[:40], origin, e)
            continue

        if body and text.word_count(body) >= MIN_USABLE_WORDS:
            return body, origin
        if body:
            log.debug(
                "%s: %s gave only %d words, trying next",
                title[:40],
                origin,
                text.word_count(body),
            )

    # --- rung 3: speech recognition ---
    if not allow_asr:
        log.info("skip (ASR disabled): %s", title[:60])
        return "", "skipped"
    if priority == "low":
        # Low-priority sources are followed for breadth, not depth — not worth
        # the CPU time or the API spend when no free transcript exists.
        log.info("skip (priority=low, no free transcript): %s", title[:60])
        return "", "skipped"
    if backend is None:
        return "", "skipped"

    path = _ensure_audio(episode, audio_dir)
    log.info("transcribing %s with %s", title[:60], backend.name)
    return backend.transcribe(path), backend.provenance


def _fetch_text(ref: dict, *, session: requests.Session) -> str:
    r = session.get(ref["url"], headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    # The declared type in the feed is often wrong, so prefer what the server
    # actually returned and let the sniffer settle it.
    served = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    return text.to_text(r.content, served or ref.get("mime"))


def _youtube_captions(episode: dict) -> str:
    from .sources.youtube import fetch_captions

    return fetch_captions(episode["guid"]) or ""


def _ensure_audio(episode: dict, audio_dir: Path) -> Path:
    """Get a local audio file, downloading it if needed."""
    uid = episode["uid"]
    url = episode.get("audio_url")

    if url:
        return audio.download(url, audio_dir / f"{uid}.mp3")

    if episode.get("source_kind") == "youtube":
        return _download_youtube_audio(episode, audio_dir)

    raise audio.AudioError(f"no audio URL for {episode.get('title', uid)}")


def _download_youtube_audio(episode: dict, audio_dir: Path) -> Path:
    """Fetch YouTube audio via yt-dlp, without transcoding.

    Deliberately no `-x`/postprocessing: extraction would invoke ffmpeg, which
    isn't installed. PyAV decodes the native m4a/webm stream fine.
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise audio.AudioError(
            "yt-dlp is needed for YouTube audio. Run: pip install yt-dlp"
        ) from e

    audio_dir.mkdir(parents=True, exist_ok=True)
    template = str(audio_dir / f"{episode['uid']}.%(ext)s")
    options = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "sleep_interval_requests": 1,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(episode["page_url"], download=True)
        return Path(ydl.prepare_filename(info))


def _write(dest: Path, episode: dict, body: str, origin: str, record: dict) -> None:
    """Write the transcript with front matter recording where the text came from.

    Provenance is not decoration: auto-captions, publisher transcripts and ASR
    have very different error profiles, and stage 3 (and you) should know which
    one is being read.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tags = record.get("tags") or []

    front = [
        "---",
        f"title: {_yaml(episode.get('title', ''))}",
        f"show: {_yaml(episode.get('show', ''))}",
        f"published: {episode.get('published') or 'unknown'}",
        f"source_kind: {episode.get('source_kind', '')}",
        f"url: {episode.get('page_url') or ''}",
        f"transcript_origin: {origin}",
        f"word_count: {text.word_count(body)}",
        f"tags: [{', '.join(tags)}]",
        f"transcribed_at: {datetime.now(timezone.utc).isoformat()}",
        "---",
        "",
        f"# {episode.get('title', '')}",
        "",
    ]
    dest.write_text("\n".join(front) + body + "\n", encoding="utf-8")


def _yaml(value: str) -> str:
    """Quote a scalar so colons and quotes in titles can't break the front matter."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_report(result: TranscribeResult) -> str:
    lines = [
        f"{len(result.written)} transcript(s) written | "
        f"{result.skipped} skipped | {len(result.failed)} failed"
    ]
    if result.by_origin:
        lines.append("  by origin: " + ", ".join(f"{k}={v}" for k, v in result.by_origin.items()))
    if result.failed:
        lines.append("Failures:")
        for name, msg in result.failed[:10]:
            lines.append(f"  {name[:60]}: {msg[:90]}")
    return "\n".join(lines)
