"""Decides which posts are worth a comment, and in what order.

The whole point of this module is that *when* you comment matters more than
what you say. A comment on a 90-minute-old post rides the post's own
distribution; the same comment at six hours is read by nobody. Most tools in
this space rank by keyword match and ignore timing entirely, which is why they
produce busywork.

Three multipliers sit on top of a quality base:

  freshness   how much reach is left to ride
  saturation  whether your comment will be seen or buried under 200 others
  headroom    lots of eyes, few comments -> unusually cheap visibility

Hard gates (age, author cooldown, already-engaged, engagement bait) return a
verdict instead of a score, so nothing silently scores low when it should be
excluded outright.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from linkedin_engage.models import Post, Score, Verdict, utcnow
from linkedin_engage.voice import looks_like_engagement_bait

# Comments posted inside this window are treated as equally early.
FULL_REACH_MINUTES = 90.0
# After the full-reach window, remaining reach halves every this-many minutes.
REACH_HALF_LIFE_MINUTES = 150.0
# A post is never worth commenting on after this, whatever else is true.
MAX_AGE_HOURS = 24.0
# Below this the comment is effectively invisible; don't pretend otherwise.
FRESHNESS_FLOOR = 0.02

# Comment counts where a thoughtful comment still gets read.
SATURATION_SWEET_SPOT = (3, 25)
SATURATION_DECAY_SPAN = 60.0
SATURATION_FLOOR = 0.15

# Reactions-per-comment above this means the post has reach the comment
# section hasn't caught up with yet.
HEADROOM_RATIO = 20.0
HEADROOM_MAX_BONUS = 0.15

ICP_WEIGHT = 0.55
RELEVANCE_WEIGHT = 0.45


def freshness(age_minutes: float) -> float:
    """1.0 while the post is still spreading, decaying to a floor afterwards."""
    if age_minutes <= FULL_REACH_MINUTES:
        return 1.0
    elapsed = age_minutes - FULL_REACH_MINUTES
    decayed = 0.5 ** (elapsed / REACH_HALF_LIFE_MINUTES)
    return max(FRESHNESS_FLOOR, decayed)


def saturation(comment_count: int) -> float:
    """Inverted U: being first is risky, being 80th is pointless."""
    low, high = SATURATION_SWEET_SPOT
    if comment_count < low:
        # No traction yet. Still worth it -- an early comment on a post that
        # then takes off is the best seat in the house -- but it's a gamble.
        return 0.6 + 0.1 * comment_count
    if comment_count <= high:
        return 1.0
    faded = 1.0 - (comment_count - high) / SATURATION_DECAY_SPAN
    return max(SATURATION_FLOOR, faded)


def headroom(reaction_count: int, comment_count: int) -> float:
    """Bonus when a post is being seen far more than it's being discussed."""
    ratio = reaction_count / max(comment_count, 1)
    if ratio <= HEADROOM_RATIO:
        return 1.0
    over = min((ratio - HEADROOM_RATIO) / HEADROOM_RATIO, 1.0)
    return 1.0 + HEADROOM_MAX_BONUS * over


def score_post(
    post: Post,
    *,
    now: Optional[datetime] = None,
    engaged_post_urns: Optional[set[str]] = None,
    author_last_engaged: Optional[dict[str, datetime]] = None,
    author_cooldown_days: int = 14,
    min_score: float = 25.0,
    max_age_hours: float = MAX_AGE_HOURS,
) -> Score:
    """Score one post, or explain why it is excluded."""
    now = now or utcnow()
    age = post.age_minutes(now)

    def excluded(verdict: Verdict, note: str) -> Score:
        return Score(
            total=0.0, freshness=0.0, saturation=0.0, headroom=0.0,
            base=0.0, verdict=verdict, note=note,
        )

    if age > max_age_hours * 60:
        return excluded(Verdict.TOO_OLD, f"{age / 60:.1f}h old (limit {max_age_hours:.0f}h)")

    if engaged_post_urns and post.urn in engaged_post_urns:
        return excluded(Verdict.ALREADY_ENGAGED, "already commented on this post")

    if author_last_engaged:
        last = author_last_engaged.get(post.author.urn)
        if last is not None:
            days = (now - last).total_seconds() / 86400.0
            if days < author_cooldown_days:
                return excluded(
                    Verdict.AUTHOR_COOLDOWN,
                    f"commented on {post.author.name} {days:.1f}d ago "
                    f"(cooldown {author_cooldown_days}d)",
                )

    if looks_like_engagement_bait(post.text):
        return excluded(Verdict.ENGAGEMENT_BAIT, "post is engagement bait; commenting looks desperate")

    f = freshness(age)
    s = saturation(post.comment_count)
    h = headroom(post.reaction_count, post.comment_count)
    base = ICP_WEIGHT * post.author.icp_fit + RELEVANCE_WEIGHT * post.relevance
    total = base * f * s * h * 100.0

    if total < min_score:
        return Score(
            total=total, freshness=f, saturation=s, headroom=h, base=base,
            verdict=Verdict.BELOW_THRESHOLD,
            note=f"scored {total:.1f}, below {min_score:.0f}",
        )

    return Score(
        total=total, freshness=f, saturation=s, headroom=h, base=base,
        verdict=Verdict.QUEUED,
        note=f"{age:.0f}m old, {post.comment_count} comments",
    )


def rank(posts: list[Post], **kwargs) -> list[tuple[Post, Score]]:
    """Score every post, keep the engageable ones, best first."""
    scored = [(p, score_post(p, **kwargs)) for p in posts]
    engageable = [(p, s) for p, s in scored if s.engageable]
    return sorted(engageable, key=lambda ps: ps[1].total, reverse=True)
