"""Turns whatever transcript format a publisher shipped into clean prose.

Feeds are inconsistent: the same show may offer VTT, SRT, JSON and HTML, and
YouTube auto-captions arrive as a rolling window that repeats every line one to
three times. Everything downstream wants one thing — readable text — so all of
that normalizes here.
"""

from __future__ import annotations

import html
import json
import re

# "00:01:02.500 --> 00:01:05.000" plus optional cue settings after the arrow.
TIMECODE = re.compile(
    r"^\s*(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->\s*(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}.*$"
)
CUE_INDEX = re.compile(r"^\s*\d+\s*$")
# <v Speaker 1>, <00:00:01.000>, <c>, </c> — inline VTT markup.
VOICE_TAG = re.compile(r"<v\s+([^>]+)>", re.IGNORECASE)
ANY_TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"[ \t]+")


def to_text(raw: str | bytes, mime: str | None = None) -> str:
    """Convert a transcript payload to prose. Format is sniffed, not trusted.

    Publishers routinely mislabel `type=` in the feed, and error pages come
    back as HTML with a 200, so the declared MIME is only a tiebreaker.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    raw = raw.lstrip("﻿").strip()
    if not raw:
        return ""

    kind = _sniff(raw, mime)
    if kind == "json":
        return _from_json(raw)
    if kind == "html":
        return _from_html(raw)
    if kind in ("vtt", "srt"):
        return _from_cues(raw)
    return _collapse(raw)


def _sniff(raw: str, mime: str | None) -> str:
    head = raw[:400].lstrip()
    if head.startswith("WEBVTT"):
        return "vtt"
    if head[:1] in "[{":
        return "json"
    if re.match(r"^\s*<(?:!doctype|html|body|div|p)\b", head, re.IGNORECASE):
        return "html"
    if TIMECODE.search(raw[:2000]):
        return "srt"

    m = (mime or "").lower()
    if "json" in m:
        return "json"
    if "html" in m:
        return "html"
    if "vtt" in m:
        return "vtt"
    if "subrip" in m or "srt" in m:
        return "srt"
    return "plain"


def _from_cues(raw: str) -> str:
    """Strip cue numbers, timecodes and inline tags from VTT/SRT."""
    speaker = None
    lines: list[str] = []

    for line in raw.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("WEBVTT")
            or stripped.startswith(("NOTE", "STYLE", "REGION"))
            or TIMECODE.match(stripped)
            or CUE_INDEX.match(stripped)
        ):
            continue

        voice = VOICE_TAG.search(stripped)
        if voice:
            name = voice.group(1).strip()
            if name != speaker:
                speaker = name
                lines.append(f"\n[{name}]")

        text = html.unescape(ANY_TAG.sub("", stripped)).strip()
        if text:
            lines.append(text)

    return _collapse("\n".join(_dedupe_rolling(lines)))


def _dedupe_rolling(lines: list[str]) -> list[str]:
    """Drop the repeats produced by YouTube's rolling caption window.

    Auto-captions repeat each line as the window scrolls, so a naive join
    triples the transcript. A line is dropped when it exactly matches, or is
    fully contained in, one of the few lines just emitted.
    """
    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        recent = [x for x in out[-4:] if x.strip()]
        if any(line == r or (line in r and len(line) > 8) for r in recent):
            continue
        out.append(line)
    return out


def _from_json(raw: str) -> str:
    """Handle the podcast-namespace JSON transcript and common variants."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _collapse(raw)

    segments = data
    if isinstance(data, dict):
        for key in ("segments", "results", "transcript", "events", "items"):
            if isinstance(data.get(key), list):
                segments = data[key]
                break
        else:
            for key in ("text", "transcript", "body"):
                if isinstance(data.get(key), str):
                    return _collapse(data[key])
            return _collapse(json.dumps(data))

    if not isinstance(segments, list):
        return _collapse(str(segments))

    parts: list[str] = []
    speaker = None
    for seg in segments:
        if isinstance(seg, str):
            parts.append(seg)
            continue
        if not isinstance(seg, dict):
            continue

        who = seg.get("speaker") or seg.get("speaker_name")
        if who and who != speaker:
            speaker = who
            parts.append(f"\n[{who}]")

        body = seg.get("body") or seg.get("text") or seg.get("word") or ""
        # YouTube's json3 nests the words under segs[].utf8
        if not body and isinstance(seg.get("segs"), list):
            body = "".join(s.get("utf8", "") for s in seg["segs"])
        if body:
            parts.append(str(body).strip())

    return _collapse(" ".join(parts))


def _from_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|li)>", "\n", raw)
    return _collapse(html.unescape(ANY_TAG.sub(" ", raw)))


def _collapse(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = WS.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def word_count(text: str) -> int:
    return len(text.split())
