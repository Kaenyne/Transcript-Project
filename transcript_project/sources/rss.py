"""RSS podcast feed adapter.

This is the workhorse. Apple Podcasts and Spotify are directories layered on
top of open RSS feeds, so for the large majority of shows the feed *is* the
API — it gives us episode metadata, a direct MP3 URL, and sometimes a
ready-made transcript, with no key and no rate limit.

Two-pass parse, on purpose:
  1. feedparser for the standard fields (robust, handles broken feeds well)
  2. a raw ElementTree pass for <podcast:transcript>, which feedparser does
     not surface — and which is the difference between a free transcript and
     a paid ASR run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import feedparser
import requests

from ..config import Source
from ..models import Episode, SourceKind, TranscriptOrigin, TranscriptRef

log = logging.getLogger(__name__)

PODCAST_NS = "https://podcastindex.org/namespace/1.0"
# A handful of feeds in the wild use the pre-standard http:// form.
PODCAST_NS_ALT = "http://podcastindex.org/namespace/1.0"

# Transcript formats we can actually turn into text, best first. Plain text and
# subtitle formats are trivial; HTML needs stripping; JSON is the podcast-index
# speaker-segmented format.
TRANSCRIPT_MIME_PREFERENCE = [
    "text/vtt",
    "application/x-subrip",
    "application/srt",
    "text/plain",
    "application/json",
    "text/html",
]

USER_AGENT = "transcript-project/0.1 (+personal weekly digest)"

AUDIO_TYPES = ("audio/", "video/")


def fetch(source: Source, feed_url: str, *, session: requests.Session | None = None) -> list[Episode]:
    """Pull recent episodes from one RSS feed."""
    sess = session or requests.Session()
    resp = sess.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    body = resp.content

    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"Could not parse feed {feed_url}: {parsed.bozo_exception}")

    show_name = (parsed.feed.get("title") or source.name).strip()
    transcripts_by_guid = _extract_podcast_transcripts(body)

    cutoff = datetime.now(timezone.utc) - timedelta(days=source.max_age_days)
    max_seconds = source.skip_if_longer_than_minutes * 60

    episodes: list[Episode] = []
    for entry in parsed.entries:
        published = _parse_date(entry)
        # No date means we can't tell if it's new; treat as old and skip rather
        # than re-summarizing an entire back catalogue every week.
        if published is None or published < cutoff:
            continue

        guid = _guid(entry)
        if not guid:
            continue

        duration = _duration_seconds(entry)
        if duration and duration > max_seconds:
            log.info("skip (too long, %dm): %s", duration // 60, entry.get("title"))
            continue

        refs = list(transcripts_by_guid.get(guid, []))

        episodes.append(
            Episode(
                source_id=source.id,
                source_kind=SourceKind.RSS,
                show=show_name,
                title=(entry.get("title") or "(untitled)").strip(),
                guid=guid,
                page_url=entry.get("link"),
                audio_url=_audio_url(entry),
                transcripts=refs,
                published=published,
                duration_seconds=duration,
                description=_description(entry),
            )
        )

    return episodes


# --------------------------------------------------------------------------
# podcast:transcript — the free-transcript jackpot
# --------------------------------------------------------------------------
def _extract_podcast_transcripts(body: bytes) -> dict[str, list[TranscriptRef]]:
    """Map guid -> transcript refs from <podcast:transcript> tags.

    feedparser drops these, so we re-parse the raw XML. Failures here are
    non-fatal: worst case we fall back to transcribing the audio.
    """
    out: dict[str, list[TranscriptRef]] = {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        log.debug("raw XML parse failed, no podcast:transcript tags: %s", e)
        return out

    for item in root.iter("item"):
        guid_el = item.find("guid")
        guid = (guid_el.text or "").strip() if guid_el is not None else ""
        if not guid:
            link = item.find("link")
            guid = (link.text or "").strip() if link is not None else ""
        if not guid:
            continue

        refs: list[TranscriptRef] = []
        for ns in (PODCAST_NS, PODCAST_NS_ALT):
            for el in item.findall(f"{{{ns}}}transcript"):
                url = el.get("url")
                if not url:
                    continue
                refs.append(
                    TranscriptRef(
                        url=url,
                        origin=TranscriptOrigin.RSS_TAG,
                        mime=el.get("type"),
                        language=el.get("language"),
                    )
                )

        if refs:
            refs.sort(key=_mime_rank)
            out[guid] = refs

    return out


def _mime_rank(ref: TranscriptRef) -> int:
    mime = (ref.mime or "").lower()
    for i, pref in enumerate(TRANSCRIPT_MIME_PREFERENCE):
        if mime.startswith(pref):
            return i
    return len(TRANSCRIPT_MIME_PREFERENCE)


# --------------------------------------------------------------------------
# field extraction
# --------------------------------------------------------------------------
def _guid(entry) -> str:
    for key in ("id", "guid", "link"):
        val = entry.get(key)
        if val:
            return str(val).strip()
    return ""


def _audio_url(entry) -> str | None:
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href") or enc.get("url")
        etype = (enc.get("type") or "").lower()
        if href and (not etype or etype.startswith(AUDIO_TYPES)):
            return href
    # Some feeds use media:content instead of a proper enclosure.
    for media in entry.get("media_content", []) or []:
        url = media.get("url")
        if url and (media.get("type") or "").startswith(AUDIO_TYPES):
            return url
    return None


def _parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return datetime(*tm[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _duration_seconds(entry) -> int | None:
    """itunes:duration is either seconds, MM:SS, or HH:MM:SS depending on feed."""
    raw = entry.get("itunes_duration") or entry.get("duration")
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        if ":" in raw:
            parts = [int(float(p)) for p in raw.split(":")]
            seconds = 0
            for p in parts:
                seconds = seconds * 60 + p
            return seconds
        return int(float(raw))
    except ValueError:
        return None


def _description(entry) -> str | None:
    for key in ("summary", "subtitle", "description"):
        val = entry.get(key)
        if val:
            return str(val)[:4000]
    return None
