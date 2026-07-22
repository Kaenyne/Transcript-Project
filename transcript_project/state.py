"""Tracks what we've already seen, so a weekly run only does new work.

Deliberately a single JSON file rather than a database: it's diffable, you can
open it in a text editor to see what happened, and deleting it is a clean way
to force a full re-pull.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_VERSION = 1


class State:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, Any] = {
            "version": STATE_VERSION,
            "episodes": {},  # uid -> record
            "runs": [],  # append-only log of weekly runs
            "resolved_feeds": {},  # "podcast:Odd Lots" -> feed url (lookup cache)
        }
        if path.exists():
            self._data.update(json.loads(path.read_text(encoding="utf-8")))

    # ---------------------------------------------------------------- episodes
    def is_seen(self, uid: str) -> bool:
        return uid in self._data["episodes"]

    def mark_seen(self, uid: str, **fields: Any) -> None:
        rec = self._data["episodes"].setdefault(
            uid, {"first_seen": _now(), "stage": "pulled"}
        )
        rec.update(fields)

    def get(self, uid: str) -> dict[str, Any] | None:
        return self._data["episodes"].get(uid)

    def set_stage(self, uid: str, stage: str, **fields: Any) -> None:
        """Advance an episode through: pulled -> transcribed -> summarized.

        Stored per-episode so a crash mid-run never loses completed work — the
        next run picks up exactly where this one stopped.
        """
        self.mark_seen(uid, stage=stage, **{f"{stage}_at": _now()}, **fields)

    def pending(self, stage: str) -> list[str]:
        """uids that have not yet reached `stage`."""
        order = ["pulled", "transcribed", "summarized"]
        target = order.index(stage)
        return [
            uid
            for uid, rec in self._data["episodes"].items()
            if order.index(rec.get("stage", "pulled")) < target
        ]

    # ------------------------------------------------------------ feed lookups
    def cached_feed(self, key: str) -> str | None:
        return self._data["resolved_feeds"].get(key)

    def cache_feed(self, key: str, url: str) -> None:
        self._data["resolved_feeds"][key] = url

    # ------------------------------------------------------------------- runs
    def record_run(self, summary: dict[str, Any]) -> None:
        self._data["runs"].append({"at": _now(), **summary})
        self._data["runs"] = self._data["runs"][-50:]

    @property
    def last_run(self) -> dict[str, Any] | None:
        return self._data["runs"][-1] if self._data["runs"] else None

    # ------------------------------------------------------------------- io
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # atomic; a killed run can't corrupt state


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
