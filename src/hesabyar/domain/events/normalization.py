"""Normalize an event payload (§10.3 step 3): money to whole Rial.

The one money-conversion implementation every path uses: the §10.3
pipeline (via the application layer's ``normalize_event``) and the
store connectors (via :class:`CommerceConnectorPort.normalize_event`).
The raw envelope is untouched — normalization produces a new payload
with converted amounts plus the rounding evidence for every converted
field, so the conversion is auditable and never silent (spec §19
rule 7, ADR 0003 decision 4).

Money fields are converted per event type
(``MONEY_FIELDS_BY_EVENT_TYPE``, dot paths, ``[]`` iterates lists). A
missing or null money field is skipped — null means "unknown" (e.g.
an unreported gateway fee), never zero.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from hesabyar.domain.events.currency import convert_store_amount
from hesabyar.domain.events.envelope import CanonicalEventType
from hesabyar.domain.events.models import MONEY_FIELDS_BY_EVENT_TYPE, AmountConversion


def _convert_segments(
    node: Any,
    segments: list[str],
    *,
    prices_in_toman: bool,
    conversions: list[AmountConversion],
    prefix: str = "",
) -> None:
    if not segments:
        return
    head, *rest = segments
    if head.endswith("[]"):
        key = head[:-2]
        if not isinstance(node, dict) or key not in node:
            return
        items = node[key]
        if not isinstance(items, list):
            return
        for index, item in enumerate(items):
            _convert_segments(
                item,
                rest,
                prices_in_toman=prices_in_toman,
                conversions=conversions,
                prefix=f"{prefix}{key}[{index}].",
            )
        return
    if not isinstance(node, dict) or head not in node:
        return
    field_path = f"{prefix}{head}"
    if not rest:
        value = node[head]
        if value is None:
            return
        converted = convert_store_amount(value, prices_in_toman=prices_in_toman)
        node[head] = converted.amount_rial.amount
        conversions.append(AmountConversion(field_path=field_path, converted=converted))
        return
    _convert_segments(
        node[head],
        rest,
        prices_in_toman=prices_in_toman,
        conversions=conversions,
        prefix=f"{field_path}.",
    )


def _convert_path(
    payload: dict[str, Any],
    path: str,
    *,
    prices_in_toman: bool,
    conversions: list[AmountConversion],
) -> None:
    """Convert one money field (dot path, ``[]`` iterates a list) in place."""
    segments = path.split(".")
    _convert_segments(payload, segments, prices_in_toman=prices_in_toman, conversions=conversions)


def normalize_payload(
    payload: Mapping[str, Any],
    event_type: CanonicalEventType,
    *,
    prices_in_toman: bool,
) -> tuple[dict[str, Any], tuple[AmountConversion, ...]]:
    """Convert a payload's money fields to whole Rial (new dict, input untouched).

    Returns the converted payload plus the rounding evidence for every
    converted field. The JSON round-trip deep-copies the payload and
    normalizes non-JSON-native values (``Decimal`` → string) exactly
    as the pipeline has always done.
    """
    converted_payload: dict[str, Any] = json.loads(json.dumps(dict(payload), default=str))
    conversions: list[AmountConversion] = []
    for path in MONEY_FIELDS_BY_EVENT_TYPE.get(event_type, ()):
        _convert_path(
            converted_payload,
            path,
            prices_in_toman=prices_in_toman,
            conversions=conversions,
        )
    return converted_payload, tuple(conversions)
