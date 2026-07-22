"""Turns whatever the user wrote in sources.yaml into something fetchable.

  podcast: "Odd Lots"      -> RSS feed URL   (via iTunes Search)
  spotify: <show URL>      -> RSS feed URL   (via title -> iTunes Search)
  youtube: "@handle"       -> channel RSS feed URL
  rss:     <URL>           -> itself

Results are cached in state.json, so the lookup happens once per show rather
than every week.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

import requests

from ..config import Source

log = logging.getLogger(__name__)

ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
YT_FEED = "https://www.youtube.com/feeds/videos.xml"
USER_AGENT = "transcript-project/0.1 (+personal weekly digest)"

# iTunes Search is undocumented-but-public and throttles around ~20 calls/min.
# Caching keeps us far below that.
MATCH_THRESHOLD = 0.55


class ResolutionError(Exception):
    pass


def resolve(source: Source, *, session: requests.Session | None = None) -> str:
    """Return the URL we should actually fetch for this source."""
    sess = session or requests.Session()

    if source.kind == "rss":
        return source.spec
    if source.kind == "podcast":
        return resolve_podcast_name(source.spec, session=sess)
    if source.kind == "spotify":
        return resolve_spotify(source.spec, session=sess)
    if source.kind == "youtube":
        return resolve_youtube(source.spec, session=sess)
    raise ResolutionError(f"Unknown source kind: {source.kind}")


def cache_key(source: Source) -> str:
    return f"{source.kind}:{source.spec}"


# --------------------------------------------------------------------------
# Apple / iTunes — the public podcast directory
# --------------------------------------------------------------------------
def resolve_podcast_name(name: str, *, session: requests.Session) -> str:
    """Look up a show by name and return its RSS feed URL.

    Apple Podcasts has no real API, but the iTunes Search endpoint is public,
    keyless, and returns `feedUrl` — the underlying RSS feed. That feed, not
    Apple, is where we actually get episodes.
    """
    r = session.get(
        ITUNES_SEARCH,
        params={"term": name, "media": "podcast", "limit": 8, "country": "US"},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    r.raise_for_status()
    results = [x for x in r.json().get("results", []) if x.get("feedUrl")]
    if not results:
        raise ResolutionError(
            f'No podcast feed found for "{name}". '
            f"Fix: search podcasts.apple.com, then use an explicit `rss:` entry."
        )

    best = max(results, key=lambda x: _similarity(name, x.get("collectionName", "")))
    score = _similarity(name, best.get("collectionName", ""))
    if score < MATCH_THRESHOLD:
        options = ", ".join(f'"{x.get("collectionName")}"' for x in results[:4])
        raise ResolutionError(
            f'No confident match for "{name}" (best: "{best.get("collectionName")}"). '
            f"Did you mean one of: {options}? Or use an explicit `rss:` entry."
        )

    log.info('resolved "%s" -> "%s"', name, best.get("collectionName"))
    return best["feedUrl"]


# --------------------------------------------------------------------------
# Spotify — discovery only; audio and transcripts come from RSS
# --------------------------------------------------------------------------
def resolve_spotify(url: str, *, session: requests.Session) -> str:
    """Map a Spotify show URL to its open RSS feed, if one exists.

    Spotify's API exposes episode metadata but never audio or transcripts, and
    Spotify-exclusive shows have no feed at all. So we scrape only the public
    show title from the page's OpenGraph tag and re-find the show on Apple.
    """
    title = _spotify_show_title(url, session=session)
    if not title:
        raise ResolutionError(
            f"Could not read a show title from {url}. "
            f"Fix: find the show's RSS feed and use an explicit `rss:` entry."
        )
    try:
        return resolve_podcast_name(title, session=session)
    except ResolutionError as e:
        raise ResolutionError(
            f'Spotify show "{title}" has no open RSS feed — it is likely a '
            f"Spotify exclusive, which cannot be pulled. ({e})"
        ) from e


def _spotify_show_title(url: str, *, session: requests.Session) -> str | None:
    r = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    if r.status_code != 200:
        return None
    m = re.search(
        r'<meta\s+property="og:title"\s+content="([^"]+)"', r.text, re.IGNORECASE
    )
    return m.group(1).strip() if m else None


# --------------------------------------------------------------------------
# YouTube — channel RSS, no API key needed
# --------------------------------------------------------------------------
def resolve_youtube(spec: str, *, session: requests.Session) -> str:
    """Return the channel's RSS feed URL.

    YouTube publishes a keyless per-channel Atom feed. It only carries the ~15
    most recent uploads, which is plenty for a weekly run and costs no quota.
    """
    channel_id = _youtube_channel_id(spec, session=session)
    return f"{YT_FEED}?channel_id={channel_id}"


def _youtube_channel_id(spec: str, *, session: requests.Session) -> str:
    spec = spec.strip()

    # Already a channel id.
    if re.fullmatch(r"UC[\w-]{22}", spec):
        return spec

    m = re.search(r"/channel/(UC[\w-]{22})", spec)
    if m:
        return m.group(1)

    # Handle or vanity URL: fetch the page and read the canonical channel id.
    if spec.startswith("@"):
        page_url = f"https://www.youtube.com/{spec}"
    elif spec.startswith("http"):
        page_url = spec
    else:
        page_url = f"https://www.youtube.com/@{spec}"

    r = session.get(page_url, headers={"User-Agent": USER_AGENT}, timeout=20)
    if r.status_code != 200:
        raise ResolutionError(f"YouTube page {page_url} returned {r.status_code}")

    for pattern in (
        r'"channelId":"(UC[\w-]{22})"',
        r'<meta itemprop="identifier" content="(UC[\w-]{22})"',
        r"/channel/(UC[\w-]{22})",
    ):
        m = re.search(pattern, r.text)
        if m:
            return m.group(1)

    raise ResolutionError(
        f"Could not find a channel id for {spec}. "
        f"Fix: open the channel, view source, search for 'channelId'."
    )


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
