"""Core records passed between discovery, scoring, drafting and the queue.

Plain dataclasses on purpose: every provider (CrowdReply, a vendor API, a
browser session, a CSV) normalises into these, so nothing downstream knows or
cares where a post came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Author:
    """The person who wrote the post. `icp_fit` is how well they match who we sell to."""

    urn: str
    name: str
    headline: str = ""
    company: str = ""
    profile_url: str = ""
    # 0.0 = outside the ICP, 1.0 = exactly who we want to be seen by.
    icp_fit: float = 0.0


@dataclass(frozen=True)
class Post:
    urn: str
    author: Author
    text: str
    posted_at: datetime
    url: str = ""
    comment_count: int = 0
    reaction_count: int = 0
    # 0.0 = nothing to say here, 1.0 = squarely our subject matter.
    relevance: float = 0.0

    def age_minutes(self, now: Optional[datetime] = None) -> float:
        return ((now or utcnow()) - self.posted_at).total_seconds() / 60.0


class Verdict(str, Enum):
    """Why a post is or is not in the queue."""

    QUEUED = "queued"
    TOO_OLD = "too_old"
    AUTHOR_COOLDOWN = "author_cooldown"
    ALREADY_ENGAGED = "already_engaged"
    ENGAGEMENT_BAIT = "engagement_bait"
    BELOW_THRESHOLD = "below_threshold"
    DAILY_CAP_REACHED = "daily_cap_reached"


@dataclass(frozen=True)
class Score:
    """A score plus the arithmetic that produced it, so it can be argued with."""

    total: float
    freshness: float
    saturation: float
    headroom: float
    base: float
    verdict: Verdict
    note: str = ""

    @property
    def engageable(self) -> bool:
        return self.verdict is Verdict.QUEUED


class DraftState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    SKIPPED = "skipped"
    POSTED = "posted"
    REJECTED_BY_FILTER = "rejected_by_filter"


@dataclass
class Draft:
    """A proposed comment. Never posted without passing through DraftState.APPROVED."""

    post: Post
    score: Score
    text: str = ""
    rationale: str = ""
    state: DraftState = DraftState.PENDING
    violations: list[str] = field(default_factory=list)
    attempts: int = 0
    created_at: datetime = field(default_factory=utcnow)

    @property
    def word_count(self) -> int:
        return len(self.text.split())
