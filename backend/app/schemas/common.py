"""Shared response envelopes."""
from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class Message(BaseModel):
    detail: str


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    per_page: int = 50

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))


class ActionResult(BaseModel):
    ok: bool = True
    detail: Optional[str] = None
    data: Optional[dict[str, Any]] = None
