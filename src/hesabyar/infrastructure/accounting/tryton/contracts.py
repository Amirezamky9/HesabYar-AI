"""Tryton REST wire shapes: JSON encoding of Tryton values and query params.

The Tryton 8.2 REST protocol (``trytond/protocols/rest.py``) uses the same
JSON value encoding as JSON-RPC (``trytond/protocols/jsonrpc.py``):

- ``Decimal`` <-> ``{"__class__": "Decimal", "decimal": "<str>"}``
- ``date`` <-> ``{"__class__": "date", "year": y, "month": m, "day": d}``
- ``datetime`` <-> ``{"__class__": "datetime", ...}``

Search domains and orderings travel as base64url-encoded JSON in the ``d``
and ``o`` query params; fields as repeated ``f`` params; ``s`` is the limit
and ``p`` the offset.

One2Many / Many2Many values in create/write payloads use Tryton's command
syntax, e.g. ``"lines": [["create", [{...}, {...}]]`` -- a plain list of
record dicts is rejected by the server (``KeyError: 0`` in
``one2many.set``).
"""

from __future__ import annotations

import base64
import datetime
import json
from decimal import Decimal
from typing import Any

JsonValue = Any
Record = dict[str, Any]
Domain = list[Any]


def encode_wire(value: JsonValue) -> JsonValue:
    """Encode a Python value to its Tryton REST JSON representation."""
    if isinstance(value, Decimal):
        return {"__class__": "Decimal", "decimal": str(value)}
    if isinstance(value, datetime.datetime):
        return {
            "__class__": "datetime",
            "year": value.year,
            "month": value.month,
            "day": value.day,
            "hour": value.hour,
            "minute": value.minute,
            "second": value.second,
            "microsecond": value.microsecond,
        }
    if isinstance(value, datetime.date):
        return {
            "__class__": "date",
            "year": value.year,
            "month": value.month,
            "day": value.day,
        }
    if isinstance(value, dict):
        return {key: encode_wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_wire(item) for item in value]
    return value


def decode_wire(value: JsonValue) -> JsonValue:
    """Decode a Tryton REST JSON value back to a Python value."""
    if isinstance(value, dict):
        klass = value.get("__class__")
        if klass == "Decimal":
            return Decimal(value["decimal"])
        if klass == "date":
            return datetime.date(value["year"], value["month"], value["day"])
        if klass == "datetime":
            return datetime.datetime(
                value["year"],
                value["month"],
                value["day"],
                value["hour"],
                value["minute"],
                value["second"],
                value["microsecond"],
            )
        if klass == "time":
            return datetime.time(
                value["hour"], value["minute"], value["second"], value["microsecond"]
            )
        return {key: decode_wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_wire(item) for item in value]
    return value


def _b64url_json(payload: JsonValue) -> str:
    raw = json.dumps(encode_wire(payload)).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def encode_domain(domain: Domain) -> str:
    """Encode a Tryton search domain for the ``d`` query param."""
    return _b64url_json(domain)


def encode_order(order: list[list[str]]) -> str:
    """Encode a Tryton ordering (e.g. ``[["code", "ASC"]]``) for the ``o`` param."""
    return _b64url_json(order)


def encode_context(context: dict[str, JsonValue]) -> str:
    """Encode a transaction context for the ``X-Tryton-Context`` header."""
    return _b64url_json(context)
