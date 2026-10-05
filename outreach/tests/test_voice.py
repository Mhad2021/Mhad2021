"""The slop filter. Every case here is a comment that must never be posted."""
from __future__ import annotations

import pytest

from linkedin_engage.voice import find_violations, looks_like_engagement_bait

GOOD = "Churn fell but did activation hold? Most onboarding rebuilds move the first week and quietly lose month three."


def test_a_real_comment_passes_clean():
    assert find_violations(GOOD, author_first_name="Dana") == []


@pytest.mark.parametrize("text", [
    "Great post Dana, really valuable stuff here.",
    "Couldn't agree more with this.",
    "This really resonates with me.",
    "So true! Thanks for sharing.",
    "Spot on.",
    "Well said.",
    "100% agree.",
])
def test_agreement_slop_is_caught(text):
    assert find_violations(text), f"should have been rejected: {text}"


@pytest.mark.parametrize("text", [
    "Strong point. We help companies fix exactly this.",
    "Interesting. Our platform solves this problem.",
    "Good thread. DM me and I'll send over our deck.",
    "Happy to share what we've seen across similar accounts.",
    "Worth a look at a free audit of your funnel.",
    "Solid breakdown, book a call if you want the detail.",
])
def test_pitching_in_a_comment_is_caught(text):
    assert find_violations(text), f"should have been rejected: {text}"


@pytest.mark.parametrize("text", [
    "Your GEO footprint is the real issue here.",
    "Have you measured your AI visibility score?",
    "This is really about entity optimization.",
    "Time to leverage this properly.",
    "Total game-changer for the category.",
    "In today's fast-paced market that gap compounds.",
])
def test_jargon_and_cliche_are_caught(text):
    assert find_violations(text), f"should have been rejected: {text}"


def test_length_cap_is_enforced():
    long = " ".join(["word"] * 75)
    assert any("too long" in v for v in find_violations(long))


def test_addressing_the_author_by_name_is_caught():
    v = find_violations("Dana, that number looks off against the benchmark.", author_first_name="Dana")
    assert any("addressing the author" in x for x in v)


def test_opening_with_self_reference_is_caught():
    v = find_violations("I saw the same pattern at three different companies last year.")
    assert any("self-reference" in x for x in v)


def test_stacked_questions_are_caught():
    v = find_violations("Did activation hold? Or did month three slip? Worth checking.")
    assert any("stacked questions" in x for x in v)


def test_emoji_and_em_dash_limits():
    assert any("emoji" in v for v in find_violations("That gap costs real money 🚀🔥💯"))
    assert any("em dashes" in v for v in find_violations(
        "That number — the churn one — looks off — worth a recheck."))


def test_empty_draft_is_a_violation():
    assert find_violations("   ") == ["empty draft"]


@pytest.mark.parametrize("post", [
    "Agree? 👇",
    "Comment YES below and I'll send it over",
    "Tag someone who needs to hear this",
    "Repost if you've seen this happen",
    "Drop a comment and I'll DM you the template",
])
def test_bait_posts_are_detected(post):
    assert looks_like_engagement_bait(post), f"should be bait: {post}"


def test_a_substantive_post_is_not_bait():
    assert not looks_like_engagement_bait(
        "We rebuilt onboarding last quarter. Churn fell 18% but activation stayed flat.")
