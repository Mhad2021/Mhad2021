"""Hard platform limits, read off the live Zapier action schemas rather than memory.

Every constraint here was taken from `inspect_zapier_actions` against the
connected account on 2026-10-05. That matters: these are the limits that decide
whether a scheduled post publishes or fails at 6am, and the published docs for
these APIs disagree with each other often enough that guessing is not safe.

Re-verify with:
    inspect_zapier_actions(selected_api=..., action=...)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"


class AssetKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    TEXT = "text"


@dataclass(frozen=True)
class ZapierAction:
    """Exactly how to publish. `account_param` needs a dynamic enum resolved first."""

    selected_api: str
    action: str
    tool_name: str
    account_param: str
    asset_param: str
    text_param: str


@dataclass(frozen=True)
class PlatformSpec:
    platform: Platform
    caption_limit: int
    # Formats the action's own help text lists. Anything else is rejected upstream.
    image_formats: frozenset[str]
    video_formats: frozenset[str]
    max_assets: int
    # Meta requires AI-generated content to be disclosed.
    requires_ai_disclosure: bool
    # More than this reads as spam even where the platform permits more.
    hashtag_soft_cap: int
    max_image_bytes: int | None
    actions: dict[AssetKind, ZapierAction] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def can_publish(self) -> bool:
        return bool(self.actions)


_IG_IMAGE_FORMATS = frozenset({"jpg", "jpeg", "gif", "png", "ico", "bmp"})
_IG_VIDEO_FORMATS = frozenset(
    {"mp4", "mov", "avi", "wmv", "flv", "webm", "mkv", "m4v", "3gp", "3g2", "asf"}
)
_FB_IMAGE_FORMATS = frozenset({"jpg", "jpeg", "bmp", "png", "gif", "tiff"})

INSTAGRAM = PlatformSpec(
    platform=Platform.INSTAGRAM,
    caption_limit=2200,
    image_formats=_IG_IMAGE_FORMATS,
    video_formats=_IG_VIDEO_FORMATS,
    max_assets=10,
    requires_ai_disclosure=True,
    hashtag_soft_cap=5,
    max_image_bytes=None,
    actions={
        AssetKind.VIDEO: ZapierAction(
            selected_api="InstagramBusinessCLIAPI",
            action="publish_video",
            tool_name="instagram_for_business_publish_video",
            account_param="instagramPageId",
            asset_param="video",
            text_param="caption",
        ),
        AssetKind.IMAGE: ZapierAction(
            selected_api="InstagramBusinessCLIAPI",
            action="publish_media_v2",
            tool_name="instagram_for_business_publish_photo_s",
            account_param="instagramPageId",
            asset_param="media",
            text_param="caption",
        ),
    },
    notes=(
        "Business account ONLY. The action's own help text excludes Personal AND "
        "Creator accounts. A Creator account must be switched to Business first.",
        "The account must be linked to a Facebook Page you administer.",
        "publish_video posts as a Reel.",
        "No text-only posts: Instagram always needs an asset.",
    ),
)

FACEBOOK = PlatformSpec(
    platform=Platform.FACEBOOK,
    caption_limit=63206,
    image_formats=_FB_IMAGE_FORMATS,
    video_formats=frozenset({"mp4", "mov", "avi", "wmv", "flv", "webm", "mkv", "m4v"}),
    max_assets=10,
    requires_ai_disclosure=True,
    hashtag_soft_cap=3,
    # The action's help text: files cannot exceed 4MB, PNG under 1MB.
    max_image_bytes=4 * 1024 * 1024,
    actions={
        AssetKind.VIDEO: ZapierAction(
            selected_api="FacebookV2CLIAPI",
            action="page_video",
            tool_name="facebook_pages_create_page_video",
            account_param="page",
            asset_param="source",
            # page_video takes title + description, not a caption.
            text_param="description",
        ),
        AssetKind.IMAGE: ZapierAction(
            selected_api="FacebookV2CLIAPI",
            action="page_stream",
            tool_name="facebook_pages_create_page_post",
            account_param="page",
            asset_param="source",
            text_param="message",
        ),
        AssetKind.TEXT: ZapierAction(
            selected_api="FacebookV2CLIAPI",
            action="page_stream",
            tool_name="facebook_pages_create_page_post",
            account_param="page",
            asset_param="",
            text_param="message",
        ),
    },
    notes=(
        "Pages only. Facebook has no API for posting to a personal profile.",
        "page_stream requires `message`; the photo is optional.",
        "PNG should stay under 1MB or Facebook repixelates it.",
    ),
)

TIKTOK = PlatformSpec(
    platform=Platform.TIKTOK,
    caption_limit=2200,
    image_formats=frozenset(),
    video_formats=frozenset({"mp4", "mov", "webm"}),
    max_assets=1,
    requires_ai_disclosure=True,
    hashtag_soft_cap=5,
    max_image_bytes=None,
    actions={},  # Deliberately empty. See below.
    notes=(
        "CANNOT BE AUTOMATED. Zapier exposes only TikTok Lead Generation (read) "
        "and TikTok Conversions (server-side ad events). Neither publishes.",
        "Publishing needs TikTok's Content Posting API, which requires your own "
        "developer app with audited access. Zapier does not broker it.",
        "The pipeline therefore exports a ready-to-upload bundle for manual posting.",
    ),
)

SPECS: dict[Platform, PlatformSpec] = {
    Platform.INSTAGRAM: INSTAGRAM,
    Platform.FACEBOOK: FACEBOOK,
    Platform.TIKTOK: TIKTOK,
}


def spec(platform: Platform) -> PlatformSpec:
    return SPECS[platform]


def publishable() -> list[Platform]:
    """Platforms this pipeline can actually post to without a human."""
    return [p for p, s in SPECS.items() if s.can_publish]
