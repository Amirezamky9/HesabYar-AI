"""Unit tests for the backup object stores (US-703).

- ``FilesystemObjectStore``: real files under a tmp dir.
- ``S3CompatibleObjectStore``: a fake ``urlopen`` transport — no
  network. The tests pin the request shape (path-style URL, SigV4
  Authorization header, payload hash), signing determinism, list
  pagination, and error mapping. They do NOT assert a memorised
  signature value (that would be a magic constant no one can verify
  without running against AWS); the signing construction is verified
  structurally instead.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from hesabyar.domain.backup.errors import BackupConfigurationError, ObjectStoreError
from hesabyar.infrastructure.config import BackupSettings
from hesabyar.infrastructure.operations.object_store import (
    FilesystemObjectStore,
    S3CompatibleObjectStore,
    build_object_store,
)

NOW = datetime.datetime(2026, 11, 21, 22, 30, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Filesystem store
# ---------------------------------------------------------------------------


def test_filesystem_round_trip_and_list(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path)
    store.put_object("a/b.dump.enc", b"bytes-1", content_type="application/octet-stream")
    store.put_object("a/c.dump.enc", b"bytes-2", content_type="application/octet-stream")
    store.put_object("other/x", b"bytes-3", content_type="application/octet-stream")
    assert store.get_object("a/b.dump.enc") == b"bytes-1"
    assert store.list_objects("a/") == ["a/b.dump.enc", "a/c.dump.enc"]
    assert store.list_objects("") == ["a/b.dump.enc", "a/c.dump.enc", "other/x"]
    store.delete_object("a/b.dump.enc")
    assert store.list_objects("a/") == ["a/c.dump.enc"]
    store.delete_object("a/b.dump.enc")  # idempotent


def test_filesystem_missing_key_is_not_found(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path)
    with pytest.raises(ObjectStoreError) as excinfo:
        store.get_object("nope")
    assert excinfo.value.not_found is True
    assert store.list_objects("x") == []  # missing root lists empty


def test_filesystem_refuses_traversal_keys(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path)
    for bad in ("../escape", "a/../../escape", "/absolute", "a//b", "", "a\\b"):
        with pytest.raises(ObjectStoreError, match="unsafe object key"):
            store.put_object(bad, b"x", content_type="application/octet-stream")
        with pytest.raises(ObjectStoreError, match="unsafe object key"):
            store.get_object(bad)


# ---------------------------------------------------------------------------
# S3 store (fake transport)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def read(self) -> bytes:
        return self._body


class FakeTransport:
    """Records requests; answers from a script of (status, body) or raises."""

    def __init__(self) -> None:
        self.requests: list[urllib.request.Request] = []
        self.script: list[bytes | BaseException] = []
        self.default_body = b""

    def __call__(self, request: urllib.request.Request, **kwargs: Any) -> FakeResponse:
        self.requests.append(request)
        if self.script:
            answer = self.script.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return FakeResponse(answer)
        return FakeResponse(self.default_body)


def _s3_store(transport: FakeTransport) -> S3CompatibleObjectStore:
    return S3CompatibleObjectStore(
        endpoint_url="https://s3.example.com",
        bucket="hesabyar-backups",
        region="us-east-1",
        access_key_id="AKIAEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        urlopen=transport,
        clock=lambda: NOW,
    )


def _http_error(code: int, message: str) -> urllib.error.HTTPError:
    """An HTTPError with real headers (urllib wants a Message, not a dict)."""
    return urllib.error.HTTPError(
        "https://s3.example.com/x", code, message, Message(), io.BytesIO(message.encode())
    )


def _header(request: urllib.request.Request, name: str) -> str:
    """Case-insensitive header lookup (urllib capitalizes stored keys)."""
    lowered = {key.lower(): value for key, value in request.headers.items()}
    return lowered.get(name.lower()) or ""


def _auth_header(request: urllib.request.Request) -> str:
    return _header(request, "Authorization")


def test_s3_put_is_path_style_and_signed() -> None:
    transport = FakeTransport()
    store = _s3_store(transport)
    store.put_object("hesabyar/2026-11-21/x.dump.enc", b"dump-bytes", content_type="application/octet-stream")
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.get_method() == "PUT"
    assert request.full_url == (
        "https://s3.example.com/hesabyar-backups/hesabyar/2026-11-21/x.dump.enc"
    )
    auth = _auth_header(request)
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/20261121/us-east-1/s3/aws4_request")
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    assert "Signature=" in auth
    assert _header(request, "X-Amz-Date") == "20261121T223000Z"
    assert _header(request, "X-Amz-Content-Sha256") == hashlib.sha256(b"dump-bytes").hexdigest()
    assert request.data == b"dump-bytes"


def test_s3_signing_is_deterministic_and_payload_bound() -> None:
    transport = FakeTransport()
    store = _s3_store(transport)
    store.put_object("k", b"same-bytes", content_type="application/octet-stream")
    store.put_object("k", b"same-bytes", content_type="application/octet-stream")
    store.put_object("k", b"other-bytes", content_type="application/octet-stream")
    auths = [_auth_header(r) for r in transport.requests]
    assert auths[0] == auths[1]  # same request -> same signature
    assert auths[0] != auths[2]  # different payload -> different signature


def test_s3_get_returns_the_body() -> None:
    transport = FakeTransport()
    transport.default_body = b"stored-bytes"
    store = _s3_store(transport)
    assert store.get_object("k") == b"stored-bytes"
    assert transport.requests[0].get_method() == "GET"


def test_s3_list_parses_xml_and_paginates() -> None:
    transport = FakeTransport()
    page1 = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01">'
        b"<IsTruncated>true</IsTruncated>"
        b"<Contents><Key>a/1</Key></Contents>"
        b"<Contents><Key>a/2</Key></Contents>"
        b"<NextContinuationToken>tok-1</NextContinuationToken>"
        b"</ListBucketResult>"
    )
    page2 = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01">'
        b"<IsTruncated>false</IsTruncated>"
        b"<Contents><Key>a/3</Key></Contents>"
        b"</ListBucketResult>"
    )
    transport.script = [page1, page2]
    store = _s3_store(transport)
    assert store.list_objects("a/") == ["a/1", "a/2", "a/3"]
    assert len(transport.requests) == 2
    assert "list-type=2" in transport.requests[0].full_url
    assert "prefix=a%2F" in transport.requests[0].full_url
    assert "continuation-token=tok-1" in transport.requests[1].full_url


def test_s3_list_rejects_bad_xml() -> None:
    transport = FakeTransport()
    transport.script = [b"not xml at all"]
    store = _s3_store(transport)
    with pytest.raises(ObjectStoreError, match="Could not parse the S3 list response"):
        store.list_objects("a/")


def test_s3_errors_are_mapped() -> None:
    transport = FakeTransport()
    store = _s3_store(transport)

    transport.script = [_http_error(404, "Not Found")]
    with pytest.raises(ObjectStoreError) as excinfo:
        store.get_object("x")
    assert excinfo.value.not_found is True

    transport.script = [_http_error(403, "Forbidden")]
    with pytest.raises(ObjectStoreError) as excinfo:
        store.get_object("x")
    assert excinfo.value.not_found is False

    transport.script = [urllib.error.URLError("connection refused")]
    with pytest.raises(ObjectStoreError, match="Could not reach the S3 endpoint"):
        store.get_object("x")


def test_s3_delete_is_idempotent_on_404() -> None:
    transport = FakeTransport()
    transport.script = [_http_error(404, "Not Found")]
    store = _s3_store(transport)
    store.delete_object("x")  # must not raise


def test_s3_rejects_a_bad_endpoint_url() -> None:
    with pytest.raises(BackupConfigurationError, match="Invalid S3 endpoint URL"):
        S3CompatibleObjectStore(
            endpoint_url="not-a-url",
            bucket="b",
            region="r",
            access_key_id="a",
            secret_access_key="s",
        )


# ---------------------------------------------------------------------------
# build_object_store
# ---------------------------------------------------------------------------


def test_build_object_store_picks_the_configured_backend(tmp_path: Path) -> None:
    local = build_object_store(BackupSettings(local_directory=str(tmp_path)))
    assert isinstance(local, FilesystemObjectStore)
    s3 = build_object_store(
        BackupSettings(
            endpoint_url="https://s3.example.com",
            bucket="b",
            region="r",
            access_key_id="a",
            secret_access_key="s",
        )
    )
    assert isinstance(s3, S3CompatibleObjectStore)


def test_build_object_store_fails_closed_with_no_backend() -> None:
    with pytest.raises(BackupConfigurationError, match="No backup destination"):
        build_object_store(BackupSettings())
