# Transcript Project

Weekly digest pipeline: follow a list of podcasts and YouTube channels, pull
new episodes, get transcripts, produce summary reports.

Run it whenever you want a digest. Nothing is scheduled or autonomous.

```
python run.py sources      # check every source in sources.yaml resolves
python run.py pull         # find new episodes since last run
python run.py status       # what's pending in the pipeline
```

## The key insight

Apple Podcasts and Spotify are **directories layered on top of open RSS feeds**.
Neither has a usable content API — but you don't need one:

| What you want | Where it actually comes from |
|---|---|
| Find a show by name | iTunes Search API (public, keyless) → returns the RSS `feedUrl` |
| Episode list + MP3 URL | that RSS feed |
| A free transcript | `<podcast:transcript>` tag in the same feed |
| YouTube video list | `youtube.com/feeds/videos.xml?channel_id=…` (keyless, no quota) |
| YouTube transcript | the video's caption track |

So there is no "Apple adapter" and no "Spotify adapter". There are **two**
adapters — RSS and YouTube — and a resolver that maps whatever you typed onto
one of them. No API keys are required for stage 1.

**Measured on the starter source list: 7 of 8 new episodes already had a free,
human-quality transcript.** Transcription is the exception, not the rule —
which is why the pull stage records *where the text lives* rather than
downloading anything.

## Stage status

- [x] **1. Pull sources** — RSS + YouTube, resolution, dedup, filtering
- [ ] **2. Transcribe** — fetch free transcripts; ASR only for the remainder
- [ ] **3. Summarize** — per-episode notes + a combined weekly report

## Editing `sources.yaml`

The only file you touch week to week. Four ways to add a source:

```yaml
- podcast: "Odd Lots"                  # resolved by name via iTunes
- rss: "https://feeds.example/x.rss"   # explicit, most reliable
- youtube: "@markets"                  # handle, /channel/ URL, or UC… id
- spotify: "https://open.spotify.com/show/…"
```

Useful per-source keys:

| Key | Does |
|---|---|
| `tags: [macro, ai]` | groups the weekly report into sections |
| `priority: high\|normal\|low` | `low` = only summarize if a free transcript exists |
| `include: ["Surveillance"]` | regex allow-list on titles |
| `exclude: ["#shorts"]` | regex block-list on titles |
| `max_per_run: 5` | cap a firehose channel |
| `max_age_days: 8` | how far back to look |

High-volume YouTube channels are the one thing that needs tuning. Bloomberg
posts ~40 clips a day; `include` + `max_per_run` cuts that to the 3 real
programmes.

### The one real limitation

YouTube's channel feed returns **only the 15 most recent uploads, with no
paging**. For a channel posting 40 videos a day, a weekly run physically
cannot see the whole week — Bloomberg's 15 newest videos are all from *today*.

The pull stage detects this and warns by name rather than silently dropping
episodes:

```
WARNING Bloomberg Television: feed is capped at 15 items and its oldest entry
(2026-07-22) is still inside your 7-day window — some uploads were missed.
```

If you see that, either run more often or accept partial coverage of that
channel. Podcast RSS feeds carry full back catalogues and are unaffected.

## Spotify, honestly

**This project never calls the Spotify API, deliberately.** As of the February
2026 platform changes, Spotify Developer Mode requires a **Premium
subscription**, allows one Client ID, caps search results at 10, and removed
the batch show/episode endpoints. It has never had a transcript endpoint or
full-episode audio. It is strictly worse than the free, keyless iTunes lookup.

A `spotify:` entry therefore works without any API or credentials: it reads the
show's public page title and re-finds the show on Apple to get the RSS feed.

If a show is a **Spotify exclusive there is no feed and it cannot be pulled** —
the resolver says so by name. That tail is shrinking anyway (Rogan, Gimlet and
Call Her Daddy all went cross-platform). Mark those unavailable and move on.

## State

`state.json` tracks every episode uid you've already seen, plus cached feed
lookups, plus a run log. It's the reason a weekly run only does new work.

Delete it to force a full re-pull. It's git-ignored, as is everything in
`data/` except the manifest schema.

## Setup

```
pip install -r requirements.txt
python run.py sources
```

**No API keys, and no `ffmpeg`.** Stage 2 can stay dependency-free too:
`faster-whisper` decodes audio via bundled PyAV rather than a system ffmpeg,
podcast enclosures are already MP3, and YouTube audio can be saved in its
native format without transcoding. Install ffmpeg only if you later want
`yt-dlp`'s conversion options.
