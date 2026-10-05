"""The scheduled queue, the dry-run gate, and the exact Zapier call for each post.

Nothing publishes unless a post is APPROVED and dry_run is off. The param
builder is the point of this module: it turns a post into the precise argument
dict `execute_zapier_write_action` wants, so publishing is a filled-in call
rather than something re-derived at 6am from a half-remembered schema.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from autopost.caption import Caption, validate
from autopost.platforms import AssetKind, Platform, spec


def utcnow() -> datetime:
    return datetime.now(UTC)


class PostState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    PUBLISHED = "published"
    FAILED = "failed"
    BLOCKED = "blocked"
    MANUAL_ONLY = "manual_only"


@dataclass
class ScheduledPost:
    platform: Platform
    caption: Caption
    asset_kind: AssetKind
    assets: list[str] = field(default_factory=list)
    publish_at: Optional[datetime] = None
    title: str = ""
    state: PostState = PostState.DRAFT
    problems: list[str] = field(default_factory=list)
    published_id: str = ""

    def check(self) -> list[str]:
        """Re-run validation and move to BLOCKED / MANUAL_ONLY where warranted."""
        self.problems = validate(
            self.caption,
            platform=self.platform,
            asset_kind=self.asset_kind,
            assets=self.assets,
        )
        if not spec(self.platform).can_publish:
            self.state = PostState.MANUAL_ONLY
        elif self.problems:
            self.state = PostState.BLOCKED
        elif self.state in (PostState.BLOCKED, PostState.MANUAL_ONLY):
            self.state = PostState.DRAFT
        return self.problems

    def zapier_params(self, account_id: str) -> dict:
        """The argument dict for execute_zapier_write_action. Raises if unsafe."""
        s = spec(self.platform)
        if self.problems:
            raise ValueError(f"post has unresolved problems: {'; '.join(self.problems)}")
        action = s.actions.get(self.asset_kind)
        if action is None:
            raise ValueError(f"no {self.asset_kind.value} action for {self.platform.value}")
        if not account_id:
            raise ValueError(
                f"{action.account_param} is required and must come from the dynamic enum; "
                "resolve it with inspect_zapier_actions after the account is authorised"
            )

        params: dict[str, object] = {
            action.account_param: account_id,
            action.text_param: self.caption.render(self.platform),
        }
        if action.asset_param and self.assets:
            # publish_media_v2 and page_stream take a list; the rest take one.
            multi = self.asset_kind is AssetKind.IMAGE
            params[action.asset_param] = self.assets if multi else self.assets[0]
        if self.platform is Platform.FACEBOOK and self.asset_kind is AssetKind.VIDEO:
            params["title"] = self.title or self.caption.text.strip()[:80]
        return params

    def zapier_call(self, account_id: str) -> dict:
        """Everything needed to make the call, ready to hand to the execute tool."""
        action = spec(self.platform).actions[self.asset_kind]
        return {
            "selected_api": action.selected_api,
            "action": action.action,
            "tool_name": action.tool_name,
            "params": self.zapier_params(account_id),
        }


class PostQueue:
    """Append-only JSONL, so the publish history cannot be silently rewritten."""

    def __init__(self, path: str | Path, *, dry_run: bool = True):
        self.path = Path(path)
        self.dry_run = dry_run
        self.posts: list[ScheduledPost] = []

    def add(self, post: ScheduledPost) -> ScheduledPost:
        post.check()
        self.posts.append(post)
        return post

    def due(self, *, now: Optional[datetime] = None) -> list[ScheduledPost]:
        now = now or utcnow()
        return [
            p for p in self.posts
            if p.state is PostState.APPROVED
            and (p.publish_at is None or p.publish_at <= now)
        ]

    def manual_only(self) -> list[ScheduledPost]:
        return [p for p in self.posts if p.state is PostState.MANUAL_ONLY]

    def blocked(self) -> list[ScheduledPost]:
        return [p for p in self.posts if p.state is PostState.BLOCKED]

    def record(self, post: ScheduledPost, state: PostState, published_id: str = "") -> None:
        post.state = state
        post.published_id = published_id
        row = {
            "at": utcnow().isoformat(),
            "platform": post.platform.value,
            "asset_kind": post.asset_kind.value,
            "state": state.value,
            "published_id": published_id,
            "caption": post.caption.render(post.platform),
            "assets": post.assets,
            "problems": post.problems,
            "dry_run": self.dry_run,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
