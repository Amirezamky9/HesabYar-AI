"""Translation of Tryton/HTTP failures into stable HesabYar application errors.

Every failure raised by the Tryton adapter is a ``TrytonAdapterError`` with a
stable ``code`` from the taxonomy below. Tryton stack traces, tracebacks, and
raw HTML error pages are never exposed to callers: messages are sanitized,
truncated, and stripped of anything resembling a traceback or a file path.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

import httpx

from hesabyar.domain.common.errors import HesabYarError

# Stable engine error codes (see docs/adapter.md for the full taxonomy).
ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
ENGINE_AUTHENTICATION_FAILED = "ENGINE_AUTHENTICATION_FAILED"
ENGINE_FORBIDDEN = "ENGINE_FORBIDDEN"
ENGINE_NOT_FOUND = "ENGINE_NOT_FOUND"
ENGINE_VALIDATION_FAILED = "ENGINE_VALIDATION_FAILED"
ENGINE_ERROR = "ENGINE_ERROR"

_MAX_MESSAGE_LENGTH = 300

_TRACEBACK_MARKERS = ("Traceback", 'File "', "File '", ".py\", line", ".py', line")


def _sanitize_message(message: str) -> str:
    """Strip tracebacks, HTML, and file paths from an engine error message."""
    text = message.strip()
    # Tryton 8.2 returns validation failures as a JSON body
    # {"status": 400, "message": "...", "description": "..."}; surface the
    # message, not the raw JSON envelope.
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            text = payload["message"].strip()
            description = payload.get("description")
            if isinstance(description, str) and description.strip():
                text = f"{text} {description.strip()}"
    # Werkzeug HTML error pages carry the message in a <p> element.
    match = re.search(r"<p>(.*?)</p>", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = html.unescape(match.group(1)).strip()
    else:
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
    text = " ".join(text.split())
    for marker in _TRACEBACK_MARKERS:
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    if len(text) > _MAX_MESSAGE_LENGTH:
        text = text[: _MAX_MESSAGE_LENGTH - 3].rstrip() + "..."
    return text or "Tryton engine error"


class TrytonAdapterError(HesabYarError):
    """A failure of the Tryton engine call, translated to a stable code."""

    def __init__(
        self,
        message: str,
        code: str = ENGINE_ERROR,
        status_code: int | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(_sanitize_message(message), code=code, details=details)
        self.status_code = status_code
        self.retryable = retryable


def translate_http_error(status_code: int, body: str, operation: str) -> TrytonAdapterError:
    """Map an HTTP failure from the Tryton server to a stable error."""
    detail = _sanitize_message(body)
    if status_code == 401:
        return TrytonAdapterError(
            f"Tryton authentication failed during {operation}.",
            code=ENGINE_AUTHENTICATION_FAILED,
            status_code=status_code,
        )
    if status_code == 403:
        return TrytonAdapterError(
            f"Tryton refused {operation}: access forbidden.",
            code=ENGINE_FORBIDDEN,
            status_code=status_code,
        )
    if status_code == 404:
        return TrytonAdapterError(
            f"Tryton resource not found during {operation}.",
            code=ENGINE_NOT_FOUND,
            status_code=status_code,
        )
    if status_code in (400, 422):
        return TrytonAdapterError(
            f"Tryton rejected {operation}: {detail}",
            code=ENGINE_VALIDATION_FAILED,
            status_code=status_code,
        )
    if status_code in (502, 503, 504):
        return TrytonAdapterError(
            f"Tryton engine unavailable during {operation}.",
            code=ENGINE_UNAVAILABLE,
            status_code=status_code,
            retryable=True,
        )
    return TrytonAdapterError(
        f"Tryton engine error during {operation}: {detail}",
        code=ENGINE_ERROR,
        status_code=status_code,
    )


def translate_transport_error(error: httpx.HTTPError, operation: str) -> TrytonAdapterError:
    """Map an httpx transport failure (connect/timeout/...) to a stable error."""
    if isinstance(error, httpx.TimeoutException):
        return TrytonAdapterError(
            f"Tryton engine timed out during {operation}.",
            code=ENGINE_UNAVAILABLE,
            retryable=True,
        )
    return TrytonAdapterError(
        f"Tryton engine unreachable during {operation}.",
        code=ENGINE_UNAVAILABLE,
        retryable=True,
    )
