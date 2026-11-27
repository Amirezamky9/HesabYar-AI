"""Security tests for the cheque scan store (US-701).

The scan URL arrives in a webhook payload, so it is attacker-influenced
input: the store must not fetch internal addresses (SSRF), must not
follow redirects (urllib's default opener follows them to any scheme,
including ``file://``), and must not let the ``sayad_id`` smuggle a
path traversal into the stored filename.

No live network is used: hostname resolution is mocked for the SSRF
cases, and the redirect cases run against a local HTTP server with
``allow_private_hosts=True`` (the test-only hook — the production
wiring never sets it).
"""

from __future__ import annotations

import http.server
import socket
import threading
from typing import Any
from unittest import mock

import pytest

from hesabyar.domain.cheques.ports import ChequeScanDownloadError
from hesabyar.infrastructure.files.local_scan_store import LocalChequeScanStore

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300000002000100e50f5d0000000049454e44ae426082"
)

SAYAD_ID = "1234567890123456"
PUBLIC_HOST = "scans.example.com"


def _addrinfo(*ips: str) -> list[tuple[Any, ...]]:
    """Fake getaddrinfo results for the given addresses."""
    results = []
    for ip in ips:
        if ":" in ip:
            results.append(
                (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443, 0, 0))
            )
        else:
            results.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)))
    return results


def _patch_dns(monkeypatch: pytest.MonkeyPatch, *ips: str) -> None:
    """Make every hostname resolve to ``ips`` (and nothing else)."""
    monkeypatch.setattr(
        "hesabyar.infrastructure.files.local_scan_store.socket.getaddrinfo",
        lambda *args, **kwargs: _addrinfo(*ips),
    )


def _forbid_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the store tries to open any URL."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the store must not fetch a refused URL")

    monkeypatch.setattr("urllib.request.build_opener", _boom)


# ---------------------------------------------------------------------------
# SSRF: non-public addresses are refused without fetching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback (local Tryton, local services)
        "10.0.0.9",  # private
        "172.16.0.9",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local: cloud metadata endpoint
        "100.64.0.1",  # CGNAT / shared address space
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "192.0.2.1",  # TEST-NET-1 (documentation)
        "198.18.0.1",  # benchmarking
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 unique-local
        "64:ff9b::127.0.0.1",  # NAT64 embedding an IPv4 loopback
        "2001::127.0.0.1",  # Teredo embedding an IPv4 loopback
    ],
)
def test_non_public_scan_host_is_refused_without_fetching(
    tmp_path, monkeypatch: pytest.MonkeyPatch, ip: str
) -> None:
    _patch_dns(monkeypatch, ip)
    _forbid_fetch(monkeypatch)
    store = LocalChequeScanStore(tmp_path / "filestore")
    with pytest.raises(ChequeScanDownloadError, match="non-public"):
        store.store_scan(sayad_id=SAYAD_ID, scan_url=f"https://{PUBLIC_HOST}/scan.png")


def test_mixed_public_and_private_addresses_are_refused(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One bad address in the answer poisons the whole host: the fetcher
    # must not be steered onto the internal one.
    _patch_dns(monkeypatch, "93.184.216.34", "10.0.0.9")
    _forbid_fetch(monkeypatch)
    store = LocalChequeScanStore(tmp_path / "filestore")
    with pytest.raises(ChequeScanDownloadError, match="non-public"):
        store.store_scan(sayad_id=SAYAD_ID, scan_url=f"https://{PUBLIC_HOST}/scan.png")


def test_unresolvable_scan_host_fails_closed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(
        "hesabyar.infrastructure.files.local_scan_store.socket.getaddrinfo", _fail
    )
    _forbid_fetch(monkeypatch)
    store = LocalChequeScanStore(tmp_path / "filestore")
    with pytest.raises(ChequeScanDownloadError, match="Cannot resolve"):
        store.store_scan(sayad_id=SAYAD_ID, scan_url="https://nonexistent.invalid/scan.png")


def test_public_scan_host_fetches(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuinely public host still works (the guard is not a blanket ban)."""
    _patch_dns(monkeypatch, "93.184.216.34")

    class _Headers:
        def get_content_type(self) -> str:
            return "image/png"

    class _Response:
        headers = _Headers()

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def read(self, n: int = -1) -> bytes:
            return _PNG if n < 0 else _PNG[:n]

    class _Opener:
        def open(self, request: Any, timeout: Any = None) -> _Response:
            return _Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *a, **k: _Opener())
    store = LocalChequeScanStore(tmp_path / "filestore")
    ref = store.store_scan(sayad_id=SAYAD_ID, scan_url=f"https://{PUBLIC_HOST}/scan.png")
    assert ref.startswith(f"cheques/{SAYAD_ID}-")
    assert ref.endswith(".png")
    assert (tmp_path / "filestore" / ref).read_bytes() == _PNG


# ---------------------------------------------------------------------------
# Redirects are refused outright (incl. file:// and private targets)
# ---------------------------------------------------------------------------


def _serve_redirect(tmp_path, target: str) -> str:
    """Local server that 302s /scan.png to ``target``."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_port}/scan.png"


def test_redirect_to_file_url_is_refused(tmp_path) -> None:
    # The redirect target is a REAL local file: the old code (urllib's
    # default opener) would have read it into the filestore.
    secret = tmp_path / "local.png"
    secret.write_bytes(_PNG)
    url = _serve_redirect(tmp_path, f"file://{secret}")
    store = LocalChequeScanStore(tmp_path / "filestore", allow_private_hosts=True)
    # The stdlib refuses file:// redirects itself ("Redirection to url
    # ... is not allowed") before our _NoRedirectHandler runs — only
    # http(s)->http(s) redirects reach our "redirects" message. The
    # behavior is correct either way; match the stdlib wording here.
    with pytest.raises(ChequeScanDownloadError, match="Redirection to url"):
        store.store_scan(sayad_id=SAYAD_ID, scan_url=url)
    cheques_dir = tmp_path / "filestore" / "cheques"
    assert not cheques_dir.exists() or not list(cheques_dir.iterdir())


def test_redirect_to_private_host_is_refused(tmp_path) -> None:
    # A redirect target skips the private-IP check, so redirects are
    # refused regardless of where they point — even a public one.
    url = _serve_redirect(tmp_path, "http://127.0.0.1:9/scan.png")
    store = LocalChequeScanStore(tmp_path / "filestore", allow_private_hosts=True)
    with pytest.raises(ChequeScanDownloadError, match="redirects"):
        store.store_scan(sayad_id=SAYAD_ID, scan_url=url)


# ---------------------------------------------------------------------------
# Filename safety + URL shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sayad_id", ["../evil", "12/3456", "1234 5678", "", "a/../../b"])
def test_unsafe_sayad_id_is_refused(tmp_path, monkeypatch: pytest.MonkeyPatch, sayad_id: str) -> None:
    # Upstream validation already requires 16 digits; the store refuses
    # anything else so a separator can never smuggle a traversal in.
    # The refusal happens before any DNS or fetch.
    _forbid_fetch(monkeypatch)
    monkeypatch.setattr(
        "hesabyar.infrastructure.files.local_scan_store.socket.getaddrinfo",
        mock.Mock(side_effect=AssertionError("DNS must not be consulted")),
    )
    store = LocalChequeScanStore(tmp_path / "filestore")
    with pytest.raises(ChequeScanDownloadError, match="sayad_id"):
        store.store_scan(sayad_id=sayad_id, scan_url=f"https://{PUBLIC_HOST}/scan.png")


def test_invalid_port_is_refused(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_fetch(monkeypatch)
    store = LocalChequeScanStore(tmp_path / "filestore")
    with pytest.raises(ChequeScanDownloadError, match="invalid port"):
        store.store_scan(sayad_id=SAYAD_ID, scan_url=f"https://{PUBLIC_HOST}:99999/scan.png")
