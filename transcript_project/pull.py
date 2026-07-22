"""Stage 1: find new episodes across every configured source.

Produces a manifest of new episodes and records them in state.json. Downloads
nothing and transcribes nothing — that keeps this stage fast (seconds) and
safe to re-run as often as you like.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .config import Source
from .models import Episode
from .sources import resolve, rss, youtube
from .state import State

log = logging.getLogger(__name__)


@dataclass
class PullResult:
    new_episodes: list[Episode] = field(default_factory=list)
    skipped_seen: int = 0
    skipped_filtered: int = 0
    skipped_capped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (source name, msg)

    @property
    def free_transcript_count(self) -> int:
        return sum(1 for e in self.new_episodes if e.has_free_transcript)


def pull_all(sources: list[Source], state: State) -> PullResult:
    """Pull every source. One source failing never aborts the run."""
    result = PullResult()
    session = requests.Session()

    for source in sources:
        try:
            feed_url = _feed_url(source, state, session)
            episodes = _fetch(source, feed_url, session)
        except Exception as e:
            log.warning("source %s failed: %s", source.name, e)
            result.errors.append((source.name, str(e)))
            continue

        fresh = 0
        for ep in episodes:
            if not source.title_allowed(ep.title):
                result.skipped_filtered += 1
                continue
            if state.is_seen(ep.uid):
                result.skipped_seen += 1
                continue
            if source.max_per_run is not None and fresh >= source.max_per_run:
                # Feed is newest-first, so we keep the most recent N and stop.
                result.skipped_capped += 1
                continue
            # Store the whole record, not a summary of it: later stages run in
            # separate invocations and must not depend on the last pull's
            # manifest still being around.
            state.mark_seen(
                ep.uid,
                stage="pulled",
                episode=ep.to_dict(),
                title=ep.title,
                show=ep.show,
                source_id=ep.source_id,
                tags=source.tags,
                priority=source.priority,
                published=ep.published.isoformat() if ep.published else None,
                has_free_transcript=ep.has_free_transcript,
            )
            result.new_episodes.append(ep)
            fresh += 1

        log.info("%s: %d new (%d already seen)", source.name, fresh, len(episodes) - fresh)

    result.new_episodes.sort(key=lambda e: (e.published is None, e.published), reverse=True)
    return result


def _feed_url(source: Source, state: State, session: requests.Session) -> str:
    """Resolve a source to a feed URL, using the on-disk cache when possible."""
    key = resolve.cache_key(source)
    cached = state.cached_feed(key)
    if cached:
        return cached
    url = resolve.resolve(source, session=session)
    state.cache_feed(key, url)
    return url


def _fetch(source: Source, feed_url: str, session: requests.Session) -> list[Episode]:
    if source.kind == "youtube":
        return youtube.fetch(source, feed_url, session=session)
    return rss.fetch(source, feed_url, session=session)


def format_report(result: PullResult) -> str:
    """Human-readable summary of what the pull found."""
    lines: list[str] = []
    n = len(result.new_episodes)
    free = result.free_transcript_count
    lines.append(f"{n} new episode(s) | {free} with free transcripts | {n - free} need ASR")
    dropped = []
    if result.skipped_filtered:
        dropped.append(f"{result.skipped_filtered} filtered by title rules")
    if result.skipped_capped:
        dropped.append(f"{result.skipped_capped} over max_per_run")
    if dropped:
        lines.append("dropped: " + ", ".join(dropped))
    lines.append("")

    by_show: dict[str, list[Episode]] = {}
    for ep in result.new_episodes:
        by_show.setdefault(ep.show, []).append(ep)

    for show, eps in by_show.items():
        lines.append(f"  {show}")
        for ep in eps:
            date = ep.published.date().isoformat() if ep.published else "????-??-??"
            mark = "free" if ep.has_free_transcript else " ASR"
            mins = f"{ep.duration_seconds // 60}m" if ep.duration_seconds else "  ?"
            lines.append(f"    [{mark}] {date} {mins:>5}  {ep.title[:70]}")
        lines.append("")

    if result.errors:
        lines.append("Errors:")
        for name, msg in result.errors:
            lines.append(f"  {name}: {msg}")

    return "\n".join(lines)
