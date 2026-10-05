"""Caption assembly and the checks that stop a post failing or breaking policy.

Two kinds of rule live here. Mechanical ones come from the platform specs and
would cause the publish call to fail. Policy ones would not fail the call but
would breach platform rules after the fact, which is worse: Meta and TikTok
both require AI-generated content to be disclosed, and an undisclosed AI reel
is a takedown or a strike, not an error message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from autopost.platforms import AssetKind, Platform, PlatformSpec, spec

# Short, unambiguous, and reads as a label rather than an apology.
AI_DISCLOSURE = "Made with AI."

_AI_DISCLOSURE_RX = re.compile(
    r"\b(made|created|generated)\s+(with|using|by)\s+ai\b|\bai[-\s]generated\b", re.I
)
_HASHTAG_RX = re.compile(r"(?<!\w)#\w+")
_EXT_RX = re.compile(r"\.([A-Za-z0-9]{2,5})(?:\?|$)")


@dataclass(frozen=True)
class Caption:
    text: str
    hashtags: tuple[str, ...] = ()
    ai_generated: bool = False

    def render(self, platform: Platform) -> str:
        """Final caption string, with the AI label added where it is required."""
        s = spec(platform)
        parts = [self.text.strip()]
        if (
            self.ai_generated
            and s.requires_ai_disclosure
            and not _AI_DISCLOSURE_RX.search(self.text)
        ):
            parts.append(AI_DISCLOSURE)
        if self.hashtags:
            parts.append(" ".join(f"#{h.lstrip('#')}" for h in self.hashtags))
        return "\n\n".join(p for p in parts if p)


def asset_extension(url_or_path: str) -> str:
    m = _EXT_RX.search(url_or_path)
    return m.group(1).lower() if m else ""


def validate(
    caption: Caption,
    *,
    platform: Platform,
    asset_kind: AssetKind,
    assets: list[str] | None = None,
) -> list[str]:
    """Every reason this post must not be sent. Empty list means it is safe to publish."""
    s: PlatformSpec = spec(platform)
    assets = assets or []
    problems: list[str] = []

    if not s.can_publish:
        problems.append(
            f"{platform.value} cannot be published to by this pipeline: {s.notes[0]}"
        )
        return problems

    if asset_kind not in s.actions:
        problems.append(f"{platform.value} has no action for a {asset_kind.value} post")

    rendered = caption.render(platform)
    if len(rendered) > s.caption_limit:
        problems.append(
            f"caption is {len(rendered)} characters, limit is {s.caption_limit}"
        )

    tags = _HASHTAG_RX.findall(rendered)
    if len(tags) > s.hashtag_soft_cap:
        problems.append(
            f"{len(tags)} hashtags; {platform.value} reads as spam above {s.hashtag_soft_cap}"
        )

    if (
        caption.ai_generated
        and s.requires_ai_disclosure
        and not _AI_DISCLOSURE_RX.search(rendered)
    ):
        problems.append("AI-generated asset without a disclosure label")

    if asset_kind is AssetKind.TEXT:
        if not caption.text.strip():
            problems.append("text post with an empty message")
    else:
        if not assets:
            problems.append(f"{asset_kind.value} post with no asset")
        if len(assets) > s.max_assets:
            problems.append(f"{len(assets)} assets, limit is {s.max_assets}")
        allowed = s.image_formats if asset_kind is AssetKind.IMAGE else s.video_formats
        for a in assets:
            if not (a.startswith("http://") or a.startswith("https://")):
                problems.append(f"asset is not a public URL: {a[:48]}")
                continue
            ext = asset_extension(a)
            if ext and ext not in allowed:
                problems.append(
                    f"{platform.value} does not accept .{ext} for {asset_kind.value} "
                    f"(accepts: {', '.join(sorted(allowed))})"
                )

    if platform is Platform.INSTAGRAM and asset_kind is AssetKind.TEXT:
        problems.append("Instagram has no text-only post; attach an image or video")

    return problems
