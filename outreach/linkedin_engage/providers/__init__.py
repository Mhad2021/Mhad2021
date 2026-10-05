"""Pluggable access to LinkedIn. See base.py for why this seam exists."""
from __future__ import annotations

from linkedin_engage.providers.base import (
    CommentSink,
    PostSource,
    ProviderNotConfigured,
)
from linkedin_engage.providers.fixture import DryRunCommentSink, FixturePostSource

__all__ = [
    "CommentSink",
    "DryRunCommentSink",
    "FixturePostSource",
    "PostSource",
    "ProviderNotConfigured",
]
