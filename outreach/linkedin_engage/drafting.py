"""Drafts one comment per post, then refuses to hand over anything that smells automated.

The generate-check-regenerate loop is the whole design. A model asked for a
LinkedIn comment will, unprompted, produce "Great insight, Dana!" a meaningful
fraction of the time, because that is what LinkedIn comments look like in
training data. The filter in voice.py is what stops that reaching the queue,
and a draft that cannot pass it in `max_draft_attempts` tries is dropped
rather than softened.
"""
from __future__ import annotations

import logging
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from linkedin_engage.config import Settings, get_settings
from linkedin_engage.models import Draft, DraftState, Post, Score
from linkedin_engage.voice import SYSTEM_PROMPT, find_violations

log = logging.getLogger(__name__)


class CommentDraft(BaseModel):
    """Structured output shape. An empty comment is a valid, useful answer."""

    comment: str = Field(description="The comment text, or empty if the post warrants none.")
    rationale: str = Field(description="One line: what this adds that the author lacks.")


class DraftingError(RuntimeError):
    """The model could not be reached. Distinct from 'the draft was rejected'."""


def _user_turn(post: Post, avoid: Optional[list[str]] = None) -> str:
    parts = [
        f"Author: {post.author.name}",
    ]
    if post.author.headline:
        parts.append(f"Their headline: {post.author.headline}")
    if post.author.company:
        parts.append(f"Their company: {post.author.company}")
    parts += [
        f"Posted {post.age_minutes():.0f} minutes ago, "
        f"{post.comment_count} comments so far.",
        "",
        "Their post:",
        post.text.strip(),
    ]
    if avoid:
        parts += [
            "",
            "A previous attempt was rejected for the following. Do not repeat these:",
            *(f"- {v}" for v in avoid),
        ]
    return "\n".join(parts)


def draft_comment(
    post: Post,
    score: Score,
    *,
    client: Optional[anthropic.Anthropic] = None,
    settings: Optional[Settings] = None,
) -> Draft:
    """Draft a comment for one post. Always returns a Draft; check `.state`."""
    settings = settings or get_settings()
    client = client or anthropic.Anthropic()
    draft = Draft(post=post, score=score)
    avoid: list[str] = []
    first_name = post.author.name.split()[0] if post.author.name else ""

    for attempt in range(1, settings.max_draft_attempts + 1):
        draft.attempts = attempt
        try:
            response = client.messages.parse(
                model=settings.model,
                max_tokens=settings.max_tokens,
                # Stable prefix, so it is cached across every draft in a run.
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": _user_turn(post, avoid)}],
                output_format=CommentDraft,
            )
        except anthropic.BadRequestError as exc:
            raise DraftingError(f"request rejected: {exc.message}") from exc
        except anthropic.AuthenticationError as exc:
            raise DraftingError("ANTHROPIC_API_KEY missing or invalid") from exc
        except anthropic.RateLimitError as exc:
            raise DraftingError("rate limited; the SDK already retried") from exc
        except anthropic.APIStatusError as exc:
            raise DraftingError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise DraftingError("could not reach the API") from exc

        if response.stop_reason == "refusal":
            draft.state = DraftState.REJECTED_BY_FILTER
            draft.violations = ["model declined to draft for this post"]
            return draft

        parsed = response.parsed_output
        text = (parsed.comment or "").strip()
        draft.rationale = (parsed.rationale or "").strip()

        if not text:
            # The model judged the post not worth commenting on. Respect that.
            draft.state = DraftState.SKIPPED
            draft.text = ""
            return draft

        violations = find_violations(text, author_first_name=first_name)
        if not violations:
            draft.text = text
            draft.violations = []
            draft.state = DraftState.PENDING
            return draft

        log.info("draft %d/%d rejected for %s: %s",
                 attempt, settings.max_draft_attempts, post.urn, "; ".join(violations))
        draft.text = text
        draft.violations = violations
        avoid = violations

    # Out of attempts. Dropping is the right call.
    draft.state = DraftState.REJECTED_BY_FILTER
    return draft
