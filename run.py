#!/usr/bin/env python
"""Entry point for the weekly run.

    python run.py pull                    # find new episodes (stage 1)
    python run.py pull --dry-run          # ...without recording them as seen
    python run.py transcribe              # get text for them (stage 2)
    python run.py transcribe --free-only  # ...skipping anything needing ASR
    python run.py transcribe --stt groq   # ...using cloud ASR instead of CPU
    python run.py sources                 # check every source resolves
    python run.py status                  # what's pending in the pipeline
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


def _load_env(path: Path = None) -> None:
    """Read KEY=VALUE lines from .env into the environment.

    Hand-rolled rather than pulling in python-dotenv: it's a dozen lines, and
    the only secret this project ever needs is GROQ_API_KEY. Real environment
    variables always win, so a shell export overrides the file.
    """
    path = path or ROOT / ".env"
    if not path.exists():
        return
    import os

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


SOURCES_FILE = ROOT / "sources.yaml"
STATE_FILE = ROOT / "state.json"
MANIFEST_FILE = ROOT / "data" / "last_pull.json"


def cmd_pull(args: argparse.Namespace) -> int:
    sources = load_sources(SOURCES_FILE)
    state = State(STATE_FILE)

    # Backfill support: normally every source is pulled over its configured
    # window, but --source/--max-age-days let you reach back into one show's
    # archive without editing sources.yaml and having to remember to undo it.
    if args.source:
        needle = args.source.lower()
        sources = [s for s in sources if needle in s.id or needle in s.name.lower()]
        if not sources:
            print(f"No source matches {args.source!r}.", file=sys.stderr)
            return 2

    for s in sources:
        if args.max_age_days is not None:
            s.max_age_days = args.max_age_days
        if args.max_episodes is not None:
            s.max_per_run = args.max_episodes

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


def cmd_transcribe(args: argparse.Namespace) -> int:
    from transcript_project import asr
    from transcript_project.transcribe import format_report, transcribe_pending

    state = State(STATE_FILE)
    pending = state.pending("transcribed")
    if not pending:
        print("Nothing pending. Run `python run.py pull` first.")
        return 0

    free_only = args.free_only
    backend = None if free_only else asr.get_backend(args.stt, model=args.model)

    n = len(pending) if args.limit is None else min(args.limit, len(pending))
    mode = "free transcripts only" if free_only else f"free + {args.stt} ASR"
    print(f"Transcribing {n} of {len(pending)} pending ({mode})...\n")

    result = transcribe_pending(
        state,
        out_dir=ROOT / "data" / "transcripts",
        audio_dir=ROOT / "data" / "audio",
        backend=backend,
        limit=args.limit,
        allow_asr=not free_only,
    )
    state.save()
    print(format_report(result))
    return 1 if result.failed else 0


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
    p.add_argument("--source", help="only pull sources matching this id or name")
    p.add_argument(
        "--max-age-days", type=int, help="override how far back to look (backfill)"
    )
    p.add_argument(
        "--max-episodes", type=int, help="cap how many episodes to take per source"
    )
    p.set_defaults(func=cmd_pull)

    t = sub.add_parser("transcribe", help="get text for pulled episodes")
    t.add_argument(
        "--stt",
        choices=["local", "groq"],
        default="local",
        help="ASR backend for episodes with no free transcript (default: local)",
    )
    t.add_argument(
        "--free-only",
        action="store_true",
        help="only fetch transcripts that already exist; never run ASR",
    )
    t.add_argument("--limit", type=int, help="process at most N episodes")
    t.add_argument(
        "--model",
        help="override the ASR model (e.g. 'tiny' or 'small' to trade accuracy for speed)",
    )
    t.set_defaults(func=cmd_transcribe)

    sub.add_parser("sources", help="verify sources.yaml").set_defaults(func=cmd_sources)
    sub.add_parser("status", help="pipeline status").set_defaults(func=cmd_status)

    args = parser.parse_args()
    _load_env()

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
