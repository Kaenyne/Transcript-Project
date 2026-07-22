"""Core data types shared by every source adapter.

The whole pipeline is built around one idea: no matter where an episode came
from (an RSS feed, a YouTube channel, a Spotify show page), it gets normalized
into an `Episode` before anything downstream touches it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any


class SourceKind(str, Enum):
    RSS = "rss"
    YOUTUBE = "youtube"


class TranscriptOrigin(str, Enum):
    """Where the text came from. Cheapest options first."""

    RSS_TAG = "rss_tag"  # podcast:transcript tag in the feed — free, instant
    YOUTUBE_CAPTIONS = "youtube_captions"  # uploader or auto captions — free
    SHOW_NOTES = "show_notes"  # some feeds ship a full transcript inline
    ASR = "asr"  # we transcribed the audio ourselves — slow, costs money


@dataclass
class TranscriptRef:
    """A pointer to transcript text that exists *somewhere else*.

    Discovered during the pull phase, resolved during the transcribe phase, so
    that pulling stays fast and never blocks on a download.
    """

    url: str
    origin: TranscriptOrigin
    mime: str | None = None  # e.g. text/vtt, application/srt, application/json
    language: str | None = None


@dataclass
class Episode:
    """One podcast episode or one video, normalized."""

    # --- identity ---
    source_id: str  # which entry in sources.yaml produced this
    source_kind: SourceKind
    show: str  # human-readable show/channel name
    title: str
    guid: str  # stable per-episode id from the source

    # --- content pointers ---
    page_url: str | None = None  # episode/video web page
    audio_url: str | None = None  # direct media URL, if downloadable
    transcripts: list[TranscriptRef] = field(default_factory=list)

    # --- metadata ---
    published: datetime | None = None
    duration_seconds: int | None = None
    description: str | None = None

    @property
    def uid(self) -> str:
        """Globally unique, stable, filesystem-safe id.

        Namespaced by source_id so the same episode syndicated to two feeds
        the user follows doesn't collide, and hashed so weird guids (full URLs,
        UUIDs with slashes) can't break paths.
        """
        digest = hashlib.sha1(f"{self.source_id}::{self.guid}".encode()).hexdigest()
        return f"{self.source_id}-{digest[:12]}"

    @property
    def has_free_transcript(self) -> bool:
        """True if we can get text without paying for ASR."""
        return any(t.origin != TranscriptOrigin.ASR for t in self.transcripts)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["published"] = self.published.isoformat() if self.published else None
        d["source_kind"] = self.source_kind.value
        for t in d["transcripts"]:
            t["origin"] = (
                t["origin"].value if hasattr(t["origin"], "value") else t["origin"]
            )
        d["uid"] = self.uid
        return d
