"""Parses sources.yaml into validated Source objects."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

Priority = Literal["high", "normal", "low"]

# The four ways a source can be declared, in the order we check for them.
SPEC_KEYS = ("rss", "podcast", "youtube", "spotify")


@dataclass
class Source:
    id: str  # slug, stable across runs — used in episode uids
    kind: str  # one of SPEC_KEYS: how the user declared it
    spec: str  # the value they gave (URL, name, or handle)
    name: str  # display name
    tags: list[str]
    priority: Priority
    max_age_days: int
    skip_if_longer_than_minutes: int
    # Title filters — essential for high-volume YouTube channels that post
    # dozens of clips and shorts a day alongside the few real segments.
    exclude: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    max_per_run: int | None = None

    def title_allowed(self, title: str) -> bool:
        if self.include and not any(
            re.search(p, title, re.IGNORECASE) for p in self.include
        ):
            return False
        return not any(re.search(p, title, re.IGNORECASE) for p in self.exclude)

    @property
    def needs_resolution(self) -> bool:
        """True if we must look up an RSS URL before we can pull."""
        return self.kind in ("podcast", "spotify")


class ConfigError(Exception):
    pass


def load_sources(path: Path) -> list[Source]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults: dict[str, Any] = raw.get("defaults") or {}
    entries = raw.get("sources") or []

    if not entries:
        raise ConfigError(f"No sources defined in {path}. Add at least one entry.")

    sources: list[Source] = []
    used_ids: set[str] = set()

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"sources[{i}] must be a mapping, got {type(entry).__name__}")

        present = [k for k in SPEC_KEYS if k in entry]
        if len(present) != 1:
            raise ConfigError(
                f"sources[{i}] must have exactly one of {SPEC_KEYS}, found {present or 'none'}"
            )

        kind = present[0]
        spec = str(entry[kind]).strip()
        name = entry.get("name") or _default_name(kind, spec)

        source_id = _slug(name)
        # Two shows with the same slug would silently share episode state.
        if source_id in used_ids:
            n = 2
            while f"{source_id}-{n}" in used_ids:
                n += 1
            source_id = f"{source_id}-{n}"
        used_ids.add(source_id)

        sources.append(
            Source(
                id=source_id,
                kind=kind,
                spec=spec,
                name=name,
                tags=list(entry.get("tags") or []),
                priority=entry.get("priority", defaults.get("priority", "normal")),
                max_age_days=int(
                    entry.get("max_age_days", defaults.get("max_age_days", 8))
                ),
                skip_if_longer_than_minutes=int(
                    entry.get(
                        "skip_if_longer_than_minutes",
                        defaults.get("skip_if_longer_than_minutes", 240),
                    )
                ),
                exclude=list(entry.get("exclude") or defaults.get("exclude") or []),
                include=list(entry.get("include") or []),
                max_per_run=entry.get("max_per_run", defaults.get("max_per_run")),
            )
        )

    return sources


def _default_name(kind: str, spec: str) -> str:
    """Best-effort display name before we've fetched the feed.

    Overwritten with the real title once the feed is actually pulled.
    """
    if kind == "podcast":
        return spec
    if kind == "youtube" and spec.startswith("@"):
        return spec.lstrip("@")
    if kind == "youtube":
        m = re.search(r"(?:@|/c/|/channel/|/user/)([^/?#]+)", spec)
        if m:
            return m.group(1)
    return re.sub(r"^https?://(www\.)?", "", spec).split("/")[0]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "source"
