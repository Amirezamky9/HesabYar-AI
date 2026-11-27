"""MCP health prober (US-704).

GETs the MCP server's own ``/health`` endpoint (added to the
Streamable HTTP app in ``hesabyar.interfaces.mcp.http``). The URL
derives from ``settings.mcp.public_url`` (the ``/mcp`` path is
stripped to the origin, then ``/health`` is appended) — no second
URL to configure, and a misconfigured public URL fails the probe
instead of probing the wrong server.

Never raises: every failure becomes an ERROR/UNKNOWN
:class:`ProcessHealth`, so a dead MCP server degrades the
monitoring snapshot instead of breaking it. ``transport`` exists
for tests (``httpx.MockTransport``).
"""

from __future__ import annotations

import datetime
import logging
from urllib.parse import urlparse, urlunparse

import httpx

from hesabyar.domain.monitoring.models import ProcessHealth, ProcessStatus

logger = logging.getLogger(__name__)


def mcp_health_url(public_url: str) -> str:
    """Derive the MCP ``/health`` URL from the server's public URL.

    ``http://127.0.0.1:8002/mcp`` → ``http://127.0.0.1:8002/health``.
    A URL without a scheme/host is returned unchanged (the probe
    will fail honestly instead of probing a guessed host).
    """
    parsed = urlparse(public_url)
    if not parsed.scheme or not parsed.netloc:
        return public_url
    return urlunparse(parsed._replace(path="/health", params="", query="", fragment=""))


class HttpxMcpHealthProber:
    """Probe the MCP ``/health`` endpoint over HTTP."""

    def __init__(
        self,
        public_url: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = mcp_health_url(public_url)
        self._timeout = timeout_seconds
        self._transport = transport

    def probe(self) -> ProcessHealth:
        """GET the MCP /health endpoint; ERROR/UNKNOWN on any failure."""
        now = datetime.datetime.now(datetime.UTC)
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport, trust_env=False) as client:
                response = client.get(self._url)
        except Exception as exc:  # noqa: BLE001 - any probe failure is a health result, not a crash
            logger.warning("MCP health probe failed: %s", exc)
            return ProcessHealth(
                name="mcp",
                status=ProcessStatus.ERROR,
                detail="MCP server is unreachable",
                checked_at=now,
            )
        if response.status_code != 200:
            return ProcessHealth(
                name="mcp",
                status=ProcessStatus.ERROR,
                detail=f"MCP health endpoint returned HTTP {response.status_code}",
                checked_at=now,
            )
        try:
            payload = response.json()
        except ValueError:
            return ProcessHealth(
                name="mcp",
                status=ProcessStatus.UNKNOWN,
                detail="MCP health endpoint returned a non-JSON response",
                checked_at=now,
            )
        if isinstance(payload, dict) and payload.get("status") == "ok":
            return ProcessHealth(name="mcp", status=ProcessStatus.OK, detail=None, checked_at=now)
        return ProcessHealth(
            name="mcp",
            status=ProcessStatus.UNKNOWN,
            detail="MCP health endpoint returned an unexpected body",
            checked_at=now,
        )
