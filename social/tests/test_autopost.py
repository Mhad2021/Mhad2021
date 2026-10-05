"""The guarantees that matter: nothing publishes malformed, and TikTok never pretends."""
from __future__ import annotations

import pytest

from autopost.caption import AI_DISCLOSURE, Caption, validate
from autopost.platforms import AssetKind, Platform, publishable, spec
from autopost.queue import PostQueue, PostState, ScheduledPost

IMG = ["https://cdn.example.com/reel-still.jpg"]
VID = ["https://cdn.example.com/reel.mp4"]


def post(platform, kind, assets, text="Three lit shopfronts. One dark.", **kw):
    return ScheduledPost(
        platform=platform, caption=Caption(text=text, **kw),
        asset_kind=kind, assets=list(assets),
    )


# --- TikTok ------------------------------------------------------------------

def test_tiktok_is_never_publishable():
    assert Platform.TIKTOK not in publishable()
    assert not spec(Platform.TIKTOK).can_publish


def test_tiktok_post_lands_in_manual_only_not_blocked():
    p = post(Platform.TIKTOK, AssetKind.VIDEO, VID)
    p.check()
    assert p.state is PostState.MANUAL_ONLY
    assert "cannot be published" in p.problems[0]


def test_publishable_is_exactly_instagram_and_facebook():
    assert set(publishable()) == {Platform.INSTAGRAM, Platform.FACEBOOK}


# --- AI disclosure -----------------------------------------------------------

def test_ai_disclosure_is_added_when_the_asset_is_generated():
    c = Caption(text="Three lit shopfronts. One dark.", ai_generated=True)
    assert AI_DISCLOSURE in c.render(Platform.INSTAGRAM)


def test_ai_disclosure_is_not_duplicated_when_already_written():
    c = Caption(text="Made with AI, obviously.", ai_generated=True)
    assert c.render(Platform.INSTAGRAM).count("AI") == 1


def test_no_disclosure_added_for_a_real_photo():
    c = Caption(text="Our van, this morning.", ai_generated=False)
    assert AI_DISCLOSURE not in c.render(Platform.FACEBOOK)


# --- mechanical limits -------------------------------------------------------

def test_instagram_rejects_a_text_only_post():
    problems = validate(Caption(text="hello"), platform=Platform.INSTAGRAM,
                        asset_kind=AssetKind.TEXT, assets=[])
    assert any("no text-only post" in p for p in problems)


def test_facebook_allows_a_text_only_post():
    assert validate(Caption(text="Open as usual today."), platform=Platform.FACEBOOK,
                    asset_kind=AssetKind.TEXT, assets=[]) == []


def test_caption_over_the_instagram_limit_is_caught():
    problems = validate(Caption(text="x" * 2300), platform=Platform.INSTAGRAM,
                        asset_kind=AssetKind.IMAGE, assets=IMG)
    assert any("limit is 2200" in p for p in problems)


def test_hashtag_spam_is_caught():
    c = Caption(text="Three shopfronts.", hashtags=tuple(f"tag{i}" for i in range(9)))
    problems = validate(c, platform=Platform.INSTAGRAM,
                        asset_kind=AssetKind.IMAGE, assets=IMG)
    assert any("hashtags" in p for p in problems)


def test_unsupported_video_format_is_caught_per_platform():
    # .asf is on Instagram's list but not Facebook's.
    assert validate(Caption(text="x"), platform=Platform.INSTAGRAM,
                    asset_kind=AssetKind.VIDEO,
                    assets=["https://cdn.example.com/a.asf"]) == []
    problems = validate(Caption(text="x"), platform=Platform.FACEBOOK,
                        asset_kind=AssetKind.VIDEO,
                        assets=["https://cdn.example.com/a.asf"])
    assert any("does not accept .asf" in p for p in problems)


def test_a_local_path_is_rejected_because_zapier_needs_a_public_url():
    problems = validate(Caption(text="x"), platform=Platform.INSTAGRAM,
                        asset_kind=AssetKind.IMAGE, assets=["/tmp/reel.jpg"])
    assert any("not a public URL" in p for p in problems)


def test_too_many_assets_is_caught():
    problems = validate(Caption(text="x"), platform=Platform.INSTAGRAM,
                        asset_kind=AssetKind.IMAGE, assets=IMG * 11)
    assert any("limit is 10" in p for p in problems)


# --- the Zapier call ---------------------------------------------------------

def test_instagram_reel_builds_the_publish_video_call():
    p = post(Platform.INSTAGRAM, AssetKind.VIDEO, VID, ai_generated=True)
    p.check()
    call = p.zapier_call("17841400000000000")
    assert call["tool_name"] == "instagram_for_business_publish_video"
    assert call["params"]["instagramPageId"] == "17841400000000000"
    assert call["params"]["video"] == VID[0]          # single, not a list
    assert AI_DISCLOSURE in call["params"]["caption"]


def test_instagram_photo_passes_a_list_because_the_action_is_a_carousel():
    p = post(Platform.INSTAGRAM, AssetKind.IMAGE, IMG)
    p.check()
    params = p.zapier_params("17841400000000000")
    assert params["media"] == IMG                      # list
    assert "caption" in params


def test_facebook_video_uses_description_and_gets_a_title():
    p = post(Platform.FACEBOOK, AssetKind.VIDEO, VID)
    p.check()
    params = p.zapier_params("1234567890")
    assert params["page"] == "1234567890"
    assert params["source"] == VID[0]
    assert "description" in params and "caption" not in params
    assert params["title"]


def test_facebook_photo_post_uses_message():
    p = post(Platform.FACEBOOK, AssetKind.IMAGE, IMG)
    p.check()
    params = p.zapier_params("1234567890")
    assert "message" in params
    assert params["source"] == IMG


def test_params_refuse_to_build_without_a_resolved_account_id():
    p = post(Platform.INSTAGRAM, AssetKind.VIDEO, VID)
    p.check()
    with pytest.raises(ValueError, match="dynamic enum"):
        p.zapier_params("")


def test_params_refuse_to_build_while_problems_remain():
    p = post(Platform.INSTAGRAM, AssetKind.IMAGE, ["/tmp/local.jpg"])
    p.check()
    with pytest.raises(ValueError, match="unresolved problems"):
        p.zapier_params("17841400000000000")


# --- the queue ---------------------------------------------------------------

def test_queue_is_dry_run_by_default(tmp_path):
    assert PostQueue(tmp_path / "q.jsonl").dry_run is True


def test_only_approved_posts_come_due(tmp_path):
    q = PostQueue(tmp_path / "q.jsonl")
    a = q.add(post(Platform.INSTAGRAM, AssetKind.VIDEO, VID))
    q.add(post(Platform.TIKTOK, AssetKind.VIDEO, VID))
    q.add(post(Platform.INSTAGRAM, AssetKind.IMAGE, ["/tmp/x.jpg"]))
    assert q.due() == []
    a.state = PostState.APPROVED
    assert q.due() == [a]
    assert len(q.manual_only()) == 1
    assert len(q.blocked()) == 1


def test_recording_a_publish_appends_a_row(tmp_path):
    q = PostQueue(tmp_path / "q.jsonl")
    p = q.add(post(Platform.FACEBOOK, AssetKind.IMAGE, IMG))
    q.record(p, PostState.PUBLISHED, published_id="fb_1")
    lines = (tmp_path / "q.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1 and '"dry_run": true' in lines[0]
