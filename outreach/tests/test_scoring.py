"""The timing guarantees the system is built around."""
from __future__ import annotations

from datetime import timedelta

from linkedin_engage.models import Author, Post, Verdict, utcnow
from linkedin_engage.scoring import freshness, headroom, rank, saturation, score_post

NOW = utcnow()


def make_post(minutes_old=30, comments=8, reactions=40, icp=0.9, relevance=0.8, urn="p1",
              author_urn="a1", text="We rebuilt our onboarding and churn fell."):
    return Post(
        urn=urn,
        author=Author(urn=author_urn, name="Dana Reyes", icp_fit=icp),
        text=text,
        posted_at=NOW - timedelta(minutes=minutes_old),
        comment_count=comments,
        reaction_count=reactions,
        relevance=relevance,
    )


def test_freshness_full_inside_the_reach_window():
    assert freshness(0) == 1.0
    assert freshness(89) == 1.0


def test_freshness_halves_after_the_window():
    assert freshness(90 + 150) == 0.5
    assert freshness(90 + 300) == 0.25


def test_freshness_never_hits_zero_but_gets_close():
    assert 0 < freshness(60 * 24) < 0.05


def test_saturation_peaks_in_the_sweet_spot():
    assert saturation(8) == 1.0
    assert saturation(3) == 1.0
    assert saturation(25) == 1.0


def test_saturation_penalises_both_extremes():
    assert saturation(0) < saturation(8)
    assert saturation(80) < saturation(0)


def test_headroom_rewards_many_eyes_few_comments():
    assert headroom(reaction_count=40, comment_count=8) == 1.0
    assert headroom(reaction_count=600, comment_count=4) > 1.0


def test_a_fresh_post_outranks_an_identical_stale_one():
    fresh = score_post(make_post(minutes_old=40), now=NOW)
    stale = score_post(make_post(minutes_old=600), now=NOW)
    assert fresh.total > stale.total * 2


def test_a_post_past_its_useful_window_is_excluded_not_merely_downranked():
    """The guarantee that matters: late comments are dropped, not deprioritised."""
    stale = score_post(make_post(minutes_old=600), now=NOW)
    assert stale.verdict is Verdict.BELOW_THRESHOLD
    assert not stale.engageable


def test_a_saturated_post_loses_to_a_quiet_one():
    quiet = score_post(make_post(comments=6), now=NOW)
    mobbed = score_post(make_post(comments=90), now=NOW)
    assert quiet.total > mobbed.total


def test_posts_past_the_age_limit_are_excluded_not_just_downranked():
    s = score_post(make_post(minutes_old=60 * 30), now=NOW)
    assert s.verdict is Verdict.TOO_OLD
    assert not s.engageable
    assert "30.0h old" in s.note


def test_author_cooldown_blocks_a_second_comment():
    s = score_post(
        make_post(),
        now=NOW,
        author_last_engaged={"a1": NOW - timedelta(days=3)},
        author_cooldown_days=14,
    )
    assert s.verdict is Verdict.AUTHOR_COOLDOWN
    assert "Dana Reyes" in s.note


def test_author_cooldown_expires():
    s = score_post(
        make_post(),
        now=NOW,
        author_last_engaged={"a1": NOW - timedelta(days=30)},
        author_cooldown_days=14,
    )
    assert s.engageable


def test_already_engaged_post_is_excluded():
    s = score_post(make_post(urn="p1"), now=NOW, engaged_post_urns={"p1"})
    assert s.verdict is Verdict.ALREADY_ENGAGED


def test_engagement_bait_is_excluded():
    s = score_post(make_post(text="Agree? Comment YES below and I'll send the template 👇"), now=NOW)
    assert s.verdict is Verdict.ENGAGEMENT_BAIT


def test_off_icp_post_falls_below_threshold():
    s = score_post(make_post(icp=0.05, relevance=0.05), now=NOW, min_score=25.0)
    assert s.verdict is Verdict.BELOW_THRESHOLD
    assert not s.engageable


def test_rank_drops_excluded_posts_and_orders_by_score():
    posts = [
        make_post(urn="old", minutes_old=60 * 30),
        make_post(urn="mobbed", comments=120),
        make_post(urn="prime", minutes_old=35, comments=7),
        make_post(urn="fading", minutes_old=240, comments=7),
    ]
    ranked = rank(posts, now=NOW)
    urns = [p.urn for p, _ in ranked]
    assert "old" not in urns
    assert urns[0] == "prime"
    assert urns.index("prime") < urns.index("fading")
