"""Cursor pagination and allowlisted filtering/sorting (spec §9.1).

List endpoints paginate with opaque cursors over the in-memory list the
use case returned (the company list and a company's chart are bounded
lists from the engine — there is no SQL to paginate against, and faking
offset pagination would lie about stability). A cursor encodes the sort
key and tiebreaker of the last item on the previous page, so page
boundaries stay stable when items are added or removed between pages.

Filtering and sorting are restricted to an allowlist of declared fields
per endpoint: unknown query parameters and unknown sort fields are
rejected with a typed 400 instead of being silently ignored.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Collection, Sequence

from fastapi import Request

from hesabyar.interfaces.rest.errors import ApiError

#: Default page size when the client sends no ``limit``.
DEFAULT_PAGE_SIZE = 50
#: Maximum page size a client may request.
MAX_PAGE_SIZE = 100

_CURSOR_VERSION = 1


def encode_cursor(key: str, tie: str) -> str:
    """Encode a (sort key, tiebreaker) position as an opaque cursor."""
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "k": key, "t": tie}, separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Decode a cursor into its (sort key, tiebreaker) position.

    Raises a typed 400 (``INVALID_CURSOR``) for anything that is not a
    cursor this API issued — malformed base64, bad JSON, wrong version
    or non-string fields.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if (
            not isinstance(payload, dict)
            or payload.get("v") != _CURSOR_VERSION
            or not isinstance(payload.get("k"), str)
            or not isinstance(payload.get("t"), str)
        ):
            raise ValueError("not a HesabYar pagination cursor")
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ApiError(
            status_code=400,
            code="INVALID_CURSOR",
            detail="The 'cursor' parameter is not a valid pagination cursor.",
        ) from exc
    return str(payload["k"]), str(payload["t"])


def paginate[T](
    items: Sequence[T],
    *,
    key_fn: Callable[[T], str],
    tie_fn: Callable[[T], str],
    limit: int,
    cursor: str | None,
    reverse: bool = False,
) -> tuple[list[T], str | None]:
    """Sort ``items`` and return one page plus the next cursor (or None).

    Items sort by ``(key_fn(item), tie_fn(item))`` — the tiebreaker keeps
    the order total and stable even when sort keys repeat. When
    ``cursor`` is given, the page starts after the item the cursor
    points at; a cursor pointing at an item that no longer exists still
    resumes at the right position (keyset semantics).
    """
    ordered = sorted(items, key=lambda item: (key_fn(item), tie_fn(item)), reverse=reverse)
    if cursor is not None:
        cursor_key, cursor_tie = decode_cursor(cursor)
        if reverse:
            ordered = [
                item
                for item in ordered
                if (key_fn(item), tie_fn(item)) < (cursor_key, cursor_tie)
            ]
        else:
            ordered = [
                item
                for item in ordered
                if (key_fn(item), tie_fn(item)) > (cursor_key, cursor_tie)
            ]
    page = list(ordered[: limit + 1])
    if len(page) > limit:
        last = page[limit - 1]
        return page[:limit], encode_cursor(key_fn(last), tie_fn(last))
    return page, None


def reject_unknown_params(request: Request, allowed: Collection[str]) -> None:
    """Reject query parameters outside the endpoint's allowlist (400).

    FastAPI silently ignores unknown query parameters; §9.1 restricts
    filtering to declared fields, so a typo like ``?curreny_code=IRR``
    must fail loudly instead of returning unfiltered results.
    """
    unknown = sorted(set(request.query_params) - set(allowed))
    if unknown:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=(
                f"Unknown query parameter(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(allowed))}."
            ),
        )


def parse_sort_param(
    sort: str | None, allowed_fields: Collection[str], default: str
) -> tuple[str, bool]:
    """Parse a ``sort`` query value into (field, reverse).

    ``sort=name`` sorts ascending, ``sort=-name`` descending. An empty
    or absent value selects ``default``. Unknown fields are rejected
    with a typed 400 (``INVALID_SORT``).
    """
    if sort is None or sort == "":
        return default, False
    reverse = sort.startswith("-")
    field = sort[1:] if reverse else sort
    if field not in allowed_fields:
        raise ApiError(
            status_code=400,
            code="INVALID_SORT",
            detail=(
                f"Unknown sort field {field!r}. "
                f"Allowed: {', '.join(sorted(allowed_fields))}."
            ),
        )
    return field, reverse
