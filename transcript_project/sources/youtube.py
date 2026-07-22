"""YouTube channel adapter.

Two keyless mechanisms, no YouTube Data API quota involved:

  discovery  -> the per-channel Atom feed (~15 most recent uploads)
  transcript -> youtube-transcript-api, which reads the caption tracks

Almost every video worth summarizing has at least auto-generated captions, so
YouTube rarely costs us an ASR run. Videos with captions disabled fall back to
audio download (yt-dlp) in the transcribe stage.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from ..config import Source
from ..models import Episode, SourceKind, TranscriptOrigin, TranscriptRef

log = logging.getLogger(__name__)

USER_AGENT = "transcript-project/0.1 (+personal weekly digest)"
WATCH_URL = "https://www.youtube.com/watch?v={vid}"


def fetch(
    source: Source, feed_url: str, *, session: requests.Session | None = None
) -> list[Episode]:
    """Pull recent uploads from one channel's Atom feed.

    The feed has no duration field, so `skip_if_longer_than_minutes` cannot be
    enforced here — it is applied later, once the transcript length is known.
    """
    sess = session or requests.Session()
    resp = sess.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()

    parsed = feedparser.parse(resp.content)
    channel = (parsed.feed.get("title") or source.name).strip()
    cutoff = datetime.now(timezone.utc) - timedelta(days=source.max_age_days)

    episodes: list[Episode] = []
    for entry in parsed.entries:
        video_id = entry.get("yt_videoid")
        if not video_id:
            continue

        published = _parse_date(entry)
        if published is None or published < cutoff:
            continue

        # A caption track may or may not exist; we record the intent to look and
        # let the transcribe stage resolve it. Keeps the pull stage fast.
        ref = TranscriptRef(
            url=WATCH_URL.format(vid=video_id),
            origin=TranscriptOrigin.YOUTUBE_CAPTIONS,
            mime="application/x-youtube-captions",
        )

        episodes.append(
            Episode(
                source_id=source.id,
                source_kind=SourceKind.YOUTUBE,
                show=channel,
                title=(entry.get("title") or "(untitled)").strip(),
                guid=video_id,
                page_url=WATCH_URL.format(vid=video_id),
                audio_url=None,  # resolved by yt-dlp only if captions are missing
                transcripts=[ref],
                published=published,
                description=_description(entry),
            )
        )

    return episodes


def fetch_captions(video_id: str, languages: tuple[str, ...] = ("en",)) -> str | None:
    """Return caption text for a video, or None if no track is available.

    Uses the instance API introduced in youtube-transcript-api 1.x; the old
    `YouTubeTranscriptApi.get_transcript` classmethod no longer exists.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        log.warning("youtube-transcript-api not installed; cannot read captions")
        return None

    try:
        transcript = YouTubeTranscriptApi().fetch(video_id, languages=list(languages))
    except Exception as e:  # library raises many distinct subclasses
        log.info("no captions for %s: %s", video_id, type(e).__name__)
        return None

    return " ".join(s.text.strip() for s in transcript.snippets if s.text.strip())


def _parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return datetime(*tm[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _description(entry) -> str | None:
    media = entry.get("media_description")
    if media:
        return str(media)[:4000]
    summary = entry.get("summary")
    return str(summary)[:4000] if summary else None
