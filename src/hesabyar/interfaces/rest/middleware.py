"""REST middleware: request ids (spec §9.1), the request-body size limit (US-701)
and the request metrics counter (US-704).

``RequestIdMiddleware`` — every response carries ``X-Request-ID``. A
client-supplied id is echoed back (when it is a sane length); otherwise
a UUID is generated. The id is also stored in a context variable so
logging and the error envelope can correlate with it.

``RequestMetricsMiddleware`` — counts every response into the
in-process request metrics (total + 5xx), the source of the 5xx-rate
alert rule. It records in a ``finally`` so a response that raises
mid-stream still counts (as a 500 — the error handler's envelope is
what the client sees).

``RequestSizeLimitMiddleware`` — refuses request bodies over
``MAX_REQUEST_BODY_BYTES`` with a 413 problem-details response before
any handler (or JSON parser) sees them. FastAPI/Starlette reads
unbounded bodies by default, so without this a huge POST is a
memory-exhaustion DoS: the 10 MiB upload cap in the file readers runs
only after the whole (base64) body is parsed into memory. The limit
is 20 MiB — the largest legitimate body is a base64 file upload (the
10 MiB file cap becomes ~13.4 MiB of base64 plus JSON overhead).
"""

from __future__ import annotations

import json
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hesabyar.domain.monitoring.metrics import RequestMetrics, request_metrics
from hesabyar.interfaces.rest.errors import PROBLEM_BASE_URL, PROBLEM_CONTENT_TYPE, ProblemDetails

REQUEST_ID_HEADER = "X-Request-ID"
MAX_CLIENT_REQUEST_ID_LENGTH = 128

#: Maximum REST request body in bytes (see the module docstring).
MAX_REQUEST_BODY_BYTES = 20 * 1024 * 1024

request_id_var: ContextVar[str | None] = ContextVar("hesabyar_request_id", default=None)


class _BodyTooLarge(Exception):
    """Internal signal: the streamed body passed the size limit."""


def get_current_request_id() -> str | None:
    """Return the request id of the current request, if any."""
    return request_id_var.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach (or echo) an X-Request-ID on every request and response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        if incoming and len(incoming) <= MAX_CLIENT_REQUEST_ID_LENGTH:
            request_id = incoming
        else:
            request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class RequestMetricsMiddleware(BaseHTTPMiddleware):
    """Count every response (total + 5xx) into the request metrics (US-704).

    Wired outermost so it counts everything, including the 413s the
    size limiter returns. ``metrics`` is injectable for tests; the
    default is the process-wide instance the monitoring service reads.
    """

    def __init__(self, app: ASGIApp, metrics: RequestMetrics | None = None) -> None:
        super().__init__(app)
        self._metrics = metrics or request_metrics

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            self._metrics.record(status_code)


class RequestSizeLimitMiddleware:
    """Pure-ASGI request-body size limit (US-701).

    Enforced two ways: an early reject on ``Content-Length`` (no body
    is read at all), and a streaming cap while the body is received
    (covers chunked requests with no Content-Length). A body over the
    limit gets a 413 problem-details response; a malformed
    Content-Length is left for the server/handler to deal with.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > self._max_bytes:
                    await _send_body_too_large(send, self._max_bytes)
                    return
            except ValueError:
                pass
        state = {"response_started": False}

        async def _tracking_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["response_started"] = True
            await send(message)

        received = 0

        async def _limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request.body":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    raise _BodyTooLarge()
            return message

        try:
            await self._app(scope, _limited_receive, _tracking_send)
        except _BodyTooLarge:
            # The app was still reading the body when the cap hit, so
            # normally no response has started and the 413 goes out
            # cleanly. If a handler already started responding without
            # reading its body first, there is nothing clean to send.
            if not state["response_started"]:
                await _send_body_too_large(send, self._max_bytes)


async def _send_body_too_large(send: Send, max_bytes: int) -> None:
    """Send the 413 problem-details response for an oversized body."""
    request_id = get_current_request_id()
    problem = ProblemDetails(
        type=f"{PROBLEM_BASE_URL}/request-body-too-large",
        title="Request Body Too Large",
        status=413,
        detail=f"The request body exceeds the {max_bytes}-byte limit.",
        code="REQUEST_BODY_TOO_LARGE",
        request_id=request_id,
    )
    body = json.dumps(problem.model_dump(mode="json")).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", PROBLEM_CONTENT_TYPE.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if request_id is not None:
        headers.append((REQUEST_ID_HEADER.encode("ascii"), request_id.encode("ascii")))
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})
