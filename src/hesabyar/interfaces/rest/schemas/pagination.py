"""Shared paginated-list response envelope (spec §9.1).

List endpoints return ``PaginatedResponse``: the page of items plus an
opaque ``next_cursor``. ``next_cursor`` is ``null`` on the last page;
pass it back as the ``cursor`` query parameter to fetch the next page.
There is no offset parameter — cursor pagination keeps page boundaries
stable while the underlying list changes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PaginatedResponse[T](BaseModel):
    """One page of a cursor-paginated list."""

    items: list[T] = Field(description="Items on this page")
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque cursor for the next page; null when this is the last "
            "page. Pass it back as the 'cursor' query parameter."
        ),
    )
