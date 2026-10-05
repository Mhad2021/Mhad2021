"""The seam between this engine and whatever can actually reach LinkedIn.

Nothing above this line knows how posts are fetched or comments are published.
That matters because LinkedIn's own API exposes neither: it has no keyword post
search for standard apps, and commenting in third-party threads is not in the
member scope that integrations such as Zapier hold. Zapier's LinkedIn app is
publish-only -- Create Share Update, Create Company Update, and a raw-request
action fenced to "the app's known API endpoints".

So discovery and commenting have to come from somewhere else, and which
"somewhere else" is a business decision, not a technical one. Implement these
two protocols and the rest of the engine works unchanged.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from linkedin_engage.models import Post


class ProviderNotConfigured(RuntimeError):
    """Raised when a provider is selected but cannot run. Message says what to do."""


@runtime_checkable
class PostSource(Protocol):
    """Finds candidate posts. Must return newest-first and set posted_at in UTC."""

    name: str

    def fetch(self, *, limit: int = 50) -> list[Post]:
        ...


@runtime_checkable
class CommentSink(Protocol):
    """Publishes an approved comment. Must be idempotent per post_urn where possible."""

    name: str
    dry_run: bool

    def publish(self, *, post_urn: str, text: str) -> str:
        """Returns a provider-side id or URL for the created comment."""
        ...
