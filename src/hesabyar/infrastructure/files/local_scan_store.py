"""Local filestore for cheque leaf scans (US-533, §5 §1.2).

The site sends a signed ``scan_url`` with the registration; the
intake downloads the scan into HesabYar's filestore so the leaf
image survives the signed URL expiring. This is the local-disk
implementation of :class:`ChequeScanStorePort
<hesabyar.domain.cheques.ports.ChequeScanStorePort>` — plain files
under ``<directory>/cheques/``, no database, no third-party
service. A later installation may swap it for object storage
behind the same port without touching the intake service.

Safety: only ``http``/``https`` URLs are fetched, the download is
capped (10 MB) and timed out (30 s), and only images + PDF are
accepted — a scan is a picture of a leaf, never an executable.
The stored name carries the ``sayad_id`` (the business key) plus a
hash of the URL, so re-downloading the same URL is idempotent and
two different scans of one leaf never collide.

US-701 — the scan URL comes from a webhook payload, so it is
attacker-influenced input, and fetching it is server-side request
forgery (SSRF) unless constrained:

- the host is resolved and EVERY address must be public — loopback,
  private, link-local (including the cloud-metadata address
  169.254.169.254), CGNAT, documentation, benchmarking, multicast
  and IPv6 transition ranges (NAT64/Teredo embed IPv4) are refused;
  an unresolvable host fails closed;
- redirects are refused outright — urllib's default opener follows
  them to ANY scheme it supports, including ``file://``, so a
  malicious scan URL could 302 the fetcher onto a local file (and a
  redirect target would skip the private-IP check);
- the ``sayad_id`` in the stored filename is restricted to safe
  characters (upstream validation already requires 16 digits; the
  store does not trust that).

Residual risk (documented in docs/security/threat-model.md): the
resolve-then-fetch gap leaves a DNS-rebinding (TOCTOU) window for
an attacker who controls the scan host's DNS. Closing it would
require fetching by pinned IP with Host/SNI handling; the scan URL
arrives in a signed webhook, so that attacker already needs
store-level compromise.
"""

from __future__ import annotations

import hashlib
import ipaddress
import mimetypes
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from hesabyar.domain.cheques.ports import ChequeScanDownloadError

#: Maximum scan size accepted (a phone photo of a leaf is ~1–3 MB).
MAX_SCAN_BYTES = 10 * 1024 * 1024

#: Download timeout, seconds.
SCAN_DOWNLOAD_TIMEOUT_SECONDS = 30

#: Content types accepted as leaf scans.
ALLOWED_SCAN_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "application/pdf",
    }
)

#: Extension per accepted content type (a URL with no usable suffix
#: falls back to these).
_EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "application/pdf": "pdf",
}

#: Networks a scan host must NOT resolve to (US-701 SSRF guard). An
#: explicit denylist, not ``ip.is_global``: the ``ipaddress``
#: predicates changed across Python versions for the shared-address
#: range (100.64.0.0/10) and the transition ranges, and this list is
#: what the tests pin. NAT64 (64:ff9b::/96) and Teredo (2001::/32)
#: are included because they embed IPv4 addresses — a hostname
#: resolving into them could reach an internal IPv4 host.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(text)
    for text in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # CGNAT / shared address space
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local (incl. cloud metadata 169.254.169.254)
        "172.16.0.0/12",  # private
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1 (documentation)
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2 (documentation)
        "203.0.113.0/24",  # TEST-NET-3 (documentation)
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved + broadcast
        "::/128",  # unspecified
        "::1/128",  # loopback
        "64:ff9b::/96",  # well-known NAT64 (embeds IPv4)
        "64:ff9b:1::/48",  # local-use NAT64 (embeds IPv4)
        "100::/64",  # discard-only
        "2001::/32",  # Teredo (embeds IPv4)
        "2001:2::/48",  # benchmarking
        "2001:10::/28",  # ORCHID
        "2001:db8::/32",  # documentation
        "fc00::/7",  # unique-local
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
    )
)

#: Characters allowed in the ``sayad_id`` part of a stored filename.
#: Upstream validation already requires exactly 16 digits; the store
#: refuses anything else so a path separator can never smuggle a
#: traversal into the filename (defence in depth).
_SAFE_SAYAD_ID_RE = re.compile(r"[A-Za-z0-9_-]+\Z")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse all redirects (US-701).

    urllib's default opener follows redirects to ANY scheme it
    supports — including ``file://`` — so a malicious scan URL could
    302 the fetcher onto a local file, and a redirect target would
    skip the private-IP check. A scan URL must be direct.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise ChequeScanDownloadError(
            f"Scan URL redirects (HTTP {code}) to {newurl!r}; refusing to follow it."
        )


def _assert_public_host(hostname: str, port: int | None) -> None:
    """Resolve ``hostname`` and refuse unless EVERY address is public.

    A host that resolves to a mix of public and non-public addresses
    is refused: the fetcher must not be steered onto an internal
    address. An unresolvable host fails closed.
    """
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ChequeScanDownloadError(
            f"Cannot resolve scan host {hostname!r}; refusing to fetch it."
        ) from exc
    if not infos:
        raise ChequeScanDownloadError(
            f"Scan host {hostname!r} resolved to no address; refusing to fetch it."
        )
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if any(address in network for network in _BLOCKED_NETWORKS):
            raise ChequeScanDownloadError(
                f"Scan host {hostname!r} resolves to non-public address "
                f"{address}; refusing to fetch it."
            )


class LocalChequeScanStore:
    """Download leaf scans into ``<directory>/cheques/``."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_bytes: int = MAX_SCAN_BYTES,
        timeout_seconds: int = SCAN_DOWNLOAD_TIMEOUT_SECONDS,
        allow_private_hosts: bool = False,
    ) -> None:
        self._directory = Path(directory)
        self._max_bytes = max_bytes
        self._timeout_seconds = timeout_seconds
        # Test hook ONLY (local HTTP servers listen on 127.0.0.1):
        # skips the private-IP check. Never True in production wiring.
        self._allow_private_hosts = allow_private_hosts

    def store_scan(self, *, sayad_id: str, scan_url: str) -> str:
        """Download ``scan_url``; return the stored ref (``cheques/<file>``)."""
        if not _SAFE_SAYAD_ID_RE.fullmatch(sayad_id):
            raise ChequeScanDownloadError(
                "Refusing to store a leaf scan: the sayad_id contains "
                "characters outside [A-Za-z0-9_-]."
            )
        parsed = urllib.parse.urlparse(scan_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ChequeScanDownloadError(
                f"Scan URL {scan_url!r} is not an http(s) URL; refusing to fetch it."
            )
        hostname = parsed.hostname
        if hostname is None:
            raise ChequeScanDownloadError(
                f"Scan URL {scan_url!r} has no host; refusing to fetch it."
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise ChequeScanDownloadError(
                f"Scan URL {scan_url!r} has an invalid port; refusing to fetch it."
            ) from exc
        if not self._allow_private_hosts:
            _assert_public_host(hostname, port)
        try:
            request = urllib.request.Request(scan_url, headers={"User-Agent": "HesabYar/1.0"})
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=self._timeout_seconds) as response:
                content_type = (response.headers.get_content_type() or "").lower()
                if content_type not in ALLOWED_SCAN_CONTENT_TYPES:
                    raise ChequeScanDownloadError(
                        f"Scan URL returned content type {content_type!r}; "
                        "only images and PDF are accepted as leaf scans."
                    )
                body = response.read(self._max_bytes + 1)
        except ChequeScanDownloadError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ChequeScanDownloadError(
                f"Could not download the leaf scan from {scan_url!r}: {exc}."
            ) from exc
        if len(body) > self._max_bytes:
            raise ChequeScanDownloadError(
                f"Leaf scan exceeds the {self._max_bytes}-byte cap; refusing to store it."
            )
        if not body:
            raise ChequeScanDownloadError("Leaf scan downloaded empty; refusing to store it.")
        extension = _extension_for(scan_url, content_type)
        digest = hashlib.sha256(scan_url.encode("utf-8")).hexdigest()[:12]
        filename = f"{sayad_id}-{digest}.{extension}"
        target_dir = self._directory / "cheques"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        target.write_bytes(body)
        return f"cheques/{filename}"


def _extension_for(scan_url: str, content_type: str) -> str:
    """Pick a file extension: the URL suffix when sane, else the content type."""
    suffix = Path(urllib.parse.urlparse(scan_url).path).suffix.lstrip(".").lower()
    if suffix in {"jpg", "jpeg", "png", "webp", "gif", "pdf"}:
        return "jpg" if suffix == "jpeg" else suffix
    by_type = _EXTENSION_BY_CONTENT_TYPE.get(content_type)
    if by_type is not None:
        return by_type
    guessed = mimetypes.guess_extension(content_type) or ".bin"
    return guessed.lstrip(".")
