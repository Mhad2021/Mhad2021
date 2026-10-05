"""The approval queue and the rate limits that keep the account alive.

Two jobs. First, remember what has already been engaged, so scoring can apply
author cooldowns and never comment on the same post twice -- that memory is
what stops the system looking like a stalker. Second, enforce the caps. The
daily cap and the minimum gap between comments are not politeness; automated
commenting at volume is the thing that gets LinkedIn profiles restricted.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from linkedin_engage.models import Draft, DraftState, utcnow


class RateLimited(RuntimeError):
    """Publishing was refused by a cap. Message says which one and when it clears."""


class Queue:
    """Append-only JSONL. Durable, diffable, and trivially inspected with `tail`."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._rows: list[dict] = []
        if self.path.exists():
            self._rows = [
                json.loads(line) for line in self.path.read_text().splitlines() if line.strip()
            ]

    # --- memory, fed back into scoring ------------------------------------
    def engaged_post_urns(self) -> set[str]:
        return {
            r["post_urn"] for r in self._rows
            if r.get("state") in (DraftState.POSTED.value, DraftState.APPROVED.value)
        }

    def author_last_engaged(self) -> dict[str, datetime]:
        latest: dict[str, datetime] = {}
        for r in self._rows:
            if r.get("state") != DraftState.POSTED.value:
                continue
            urn, when = r.get("author_urn"), r.get("posted_at") or r.get("created_at")
            if not urn or not when:
                continue
            ts = datetime.fromisoformat(when)
            if urn not in latest or ts > latest[urn]:
                latest[urn] = ts
        return latest

    # --- caps --------------------------------------------------------------
    def posted_since(self, since: datetime) -> list[dict]:
        out = []
        for r in self._rows:
            if r.get("state") != DraftState.POSTED.value or not r.get("posted_at"):
                continue
            if datetime.fromisoformat(r["posted_at"]) >= since:
                out.append(r)
        return out

    def check_rate_limits(
        self, *, daily_cap: int, min_gap_seconds: int, now: Optional[datetime] = None
    ) -> None:
        """Raise RateLimited if publishing now would breach a cap."""
        now = now or utcnow()
        today = self.posted_since(now - timedelta(days=1))
        if len(today) >= daily_cap:
            raise RateLimited(
                f"daily cap reached: {len(today)}/{daily_cap} comments in the last 24h"
            )
        if today:
            last = max(datetime.fromisoformat(r["posted_at"]) for r in today)
            gap = (now - last).total_seconds()
            if gap < min_gap_seconds:
                wait = int(min_gap_seconds - gap)
                raise RateLimited(f"last comment was {int(gap)}s ago; wait {wait}s")

    # --- writes ------------------------------------------------------------
    def add(self, draft: Draft) -> dict:
        row = {
            "post_urn": draft.post.urn,
            "post_url": draft.post.url,
            "author_urn": draft.post.author.urn,
            "author_name": draft.post.author.name,
            "author_profile_url": draft.post.author.profile_url,
            "post_excerpt": draft.post.text.strip()[:280],
            "score": round(draft.score.total, 1),
            "freshness": round(draft.score.freshness, 3),
            "saturation": round(draft.score.saturation, 3),
            "age_minutes": round(draft.post.age_minutes(), 1),
            "comment_count": draft.post.comment_count,
            "comment": draft.text,
            "rationale": draft.rationale,
            "state": draft.state.value,
            "violations": draft.violations,
            "attempts": draft.attempts,
            "created_at": draft.created_at.isoformat(),
            "posted_at": None,
            "published_id": None,
        }
        self._append(row)
        return row

    def set_state(
        self, post_urn: str, state: DraftState, *, published_id: Optional[str] = None
    ) -> Optional[dict]:
        """Append a new row recording the transition. History is never rewritten."""
        current = self.find(post_urn)
        if current is None:
            return None
        row = dict(current)
        row["state"] = state.value
        if state is DraftState.POSTED:
            row["posted_at"] = utcnow().isoformat()
            row["published_id"] = published_id
        self._append(row)
        return row

    def find(self, post_urn: str) -> Optional[dict]:
        """Latest row for a post."""
        for r in reversed(self._rows):
            if r.get("post_urn") == post_urn:
                return r
        return None

    def pending(self) -> list[dict]:
        seen, out = set(), []
        for r in reversed(self._rows):
            urn = r.get("post_urn")
            if urn in seen:
                continue
            seen.add(urn)
            if r.get("state") == DraftState.PENDING.value and r.get("comment"):
                out.append(r)
        return sorted(out, key=lambda r: r.get("score", 0), reverse=True)

    def _append(self, row: dict) -> None:
        self._rows.append(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")


SHEET_COLUMNS = [
    "score", "age_minutes", "comment_count", "author_name", "author_profile_url",
    "post_excerpt", "comment", "rationale", "state", "post_url", "created_at",
]


def sheet_header() -> list[str]:
    """Header row for the Google Sheet queue tab."""
    return [c.replace("_", " ").title() for c in SHEET_COLUMNS]


def to_sheet_rows(rows: Iterable[dict]) -> list[list[object]]:
    """Flatten queue rows into the Sheet's column order, ready for an append."""
    return [[r.get(c, "") if r.get(c) is not None else "" for c in SHEET_COLUMNS] for r in rows]
