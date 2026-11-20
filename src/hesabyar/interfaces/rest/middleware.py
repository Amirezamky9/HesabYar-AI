"""Request-ID middleware (spec §9.1: ``X-Request-ID`` for correlation).

Every response carries ``X-Request-ID``. A client-supplied id is echoed
back (when it is a sane length); otherwise a UUID is generated. The id
is also stored in a context variable so logging and the error envelope
can correlate with it.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
MAX_CLIENT_REQUEST_ID_LENGTH = 128

request_id_var: ContextVar[str | None] = ContextVar("hesabyar_request_id", default=None)


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
