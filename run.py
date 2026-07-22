#!/usr/bin/env python
"""Entry point for the weekly run.

    python run.py pull              # find new episodes (stage 1)
    python run.py pull --dry-run    # ...without recording them as seen
    python run.py sources           # check every source resolves
    python run.py status            # what's pending in the pipeline
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import requests

from transcript_project.config import ConfigError, load_sources
from transcript_project.pull import format_report, pull_all
from transcript_project.sources import resolve
from transcript_project.state import State

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.yaml"
STATE_FILE = ROOT / "state.json"
MANIFEST_FILE = ROOT / "data" / "last_pull.json"


def cmd_pull(args: argparse.Namespace) -> int:
    sources = load_sources(SOURCES_FILE)
    state = State(STATE_FILE)

    print(f"Pulling {len(sources)} source(s)...\n")
    result = pull_all(sources, state)
    print(format_report(result))

    if args.dry_run:
        print("(dry run — nothing recorded)")
        return 0

    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.write_text(
        json.dumps([e.to_dict() for e in result.new_episodes], indent=2),
        encoding="utf-8",
    )
    state.record_run(
        {
            "new": len(result.new_episodes),
            "free_transcripts": result.free_transcript_count,
            "errors": len(result.errors),
        }
    )
    state.save()
    print(f"Manifest: {MANIFEST_FILE.relative_to(ROOT)}")
    return 1 if result.errors else 0


def cmd_sources(args: argparse.Namespace) -> int:
    """Verify every entry in sources.yaml resolves to a real feed."""
    sources = load_sources(SOURCES_FILE)
    state = State(STATE_FILE)
    session = requests.Session()
    failed = 0

    for src in sources:
        try:
            url = resolve.resolve(src, session=session)
            state.cache_feed(resolve.cache_key(src), url)
            print(f"  ok    {src.name[:30]:32} {url[:70]}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {src.name[:30]:32} {e}")

    state.save()
    print(f"\n{len(sources) - failed}/{len(sources)} sources resolved.")
    return 1 if failed else 0


def cmd_status(args: argparse.Namespace) -> int:
    state = State(STATE_FILE)
    last = state.last_run
    print(f"Last run: {last['at'] if last else 'never'}")
    for stage in ("transcribed", "summarized"):
        print(f"  pending {stage}: {len(state.pending(stage))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pull", help="find new episodes")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_pull)

    sub.add_parser("sources", help="verify sources.yaml").set_defaults(func=cmd_sources)
    sub.add_parser("status", help="pipeline status").set_defaults(func=cmd_status)

    args = parser.parse_args()

    # Windows consoles default to cp1252, which raises on the em-dashes and
    # smart quotes that are everywhere in episode titles.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return args.func(args)
    except ConfigError as e:
        print(f"Config error in sources.yaml: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
