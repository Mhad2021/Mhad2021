"""Offline provider. Runs the full pipeline with no LinkedIn access at all.

This is not a toy: it is how the scoring, drafting and queue layers are
developed and tested before any paid vendor or browser session is wired in,
and how regressions are caught afterwards.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from linkedin_engage.models import Author, Post, utcnow


class FixturePostSource:
    name = "fixture"

    def __init__(self, path: Path):
        self.path = Path(path)

    def fetch(self, *, limit: int = 50) -> list[Post]:
        raw = json.loads(self.path.read_text())
        now = utcnow()
        posts = [
            Post(
                urn=row["urn"],
                author=Author(
                    urn=row["author"]["urn"],
                    name=row["author"]["name"],
                    headline=row["author"].get("headline", ""),
                    company=row["author"].get("company", ""),
                    profile_url=row["author"].get("profile_url", ""),
                    icp_fit=float(row["author"].get("icp_fit", 0.0)),
                ),
                text=row["text"],
                # Fixtures store age rather than a timestamp so they never go stale.
                posted_at=now - timedelta(minutes=float(row["age_minutes"])),
                url=row.get("url", ""),
                comment_count=int(row.get("comment_count", 0)),
                reaction_count=int(row.get("reaction_count", 0)),
                relevance=float(row.get("relevance", 0.0)),
            )
            for row in raw
        ]
        return posts[:limit]


class DryRunCommentSink:
    """Accepts comments and records them without touching LinkedIn."""

    name = "dry-run"
    dry_run = True

    def __init__(self):
        self.published: list[tuple[str, str]] = []

    def publish(self, *, post_urn: str, text: str) -> str:
        self.published.append((post_urn, text))
        return f"dry-run://{post_urn}"
