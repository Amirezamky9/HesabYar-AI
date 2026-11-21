"""Object-store backends for backups (US-703).

Two implementations of :class:`hesabyar.domain.backup.ports.ObjectStorePort`:

- :class:`FilesystemObjectStore` — a plain directory (dev/CI). The
  "local S3-compatible stub" the story allows: no server, no
  credentials, same key layout as S3.
- :class:`S3CompatibleObjectStore` — a minimal S3 client over the
  standard library only (``urllib`` + SigV4 signing with ``hmac`` /
  ``hashlib``). No boto3: the backup path needs exactly four
  operations (PUT / GET / LIST / DELETE), and a heavy dependency
  would sit in the worker for nothing. Path-style addressing
  (``{endpoint}/{bucket}/{key}``) throughout — virtual-hosted style
  breaks on most custom endpoints and on MinIO.

The S3 destination decision is UNVERIFIED (phased design §1.3
phase 7): the endpoint, bucket, region and credentials all come
from configuration (``BackupSettings``), with no default and no
provider named anywhere in this module. ``build_object_store``
fails closed when neither backend is configured — a backup with
nowhere to go is a loud failure, never a silent skip.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hesabyar.domain.backup.errors import BackupConfigurationError, ObjectStoreError
from hesabyar.infrastructure.config import BackupSettings

#: Operations that need a signed payload hash (all of them — the
#: client always sends the real SHA-256, never UNSIGNED-PAYLOAD).
_AMZ_ALGORITHM = "AWS4-HMAC-SHA256"

#: Signed headers, in canonical order.
_SIGNED_HEADERS = ("host", "x-amz-content-sha256", "x-amz-date")


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    """The SigV4 signing key (AWS4 + secret, date, region, s3, request)."""
    key = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    key = _hmac_sha256(key, region)
    key = _hmac_sha256(key, "s3")
    return _hmac_sha256(key, "aws4_request")


def _quote_segment(segment: str) -> str:
    """URI-encode one path segment (RFC 3986 unreserved chars pass through)."""
    return urllib.parse.quote(segment, safe="~-._")


class FilesystemObjectStore:
    """Object storage backed by a local directory (dev/CI backend).

    Keys map to files under ``root`` (``<root>/<key>``); writes are
    atomic (temp file + rename). Keys are validated against
    traversal — a key escaping the root is refused, never written.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path_for(self, key: str) -> Path:
        if not key or key.startswith("/") or "\\" in key:
            raise ObjectStoreError(f"Refusing unsafe object key {key!r}.")
        parts = key.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ObjectStoreError(f"Refusing unsafe object key {key!r}.")
        path = (self._root.joinpath(*parts)).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ObjectStoreError(f"Refusing object key {key!r}: escapes the store root.")
        return path

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def get_object(self, key: str) -> bytes:
        path = self._path_for(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectStoreError(
                f"Object {key!r} not found in the local store.",
                details={"key": key},
                not_found=True,
            ) from exc
        except OSError as exc:
            raise ObjectStoreError(
                f"Could not read object {key!r} from the local store: {exc}",
                details={"key": key},
            ) from exc

    def list_objects(self, prefix: str) -> list[str]:
        root = self._root.resolve()
        if not root.is_dir():
            return []
        keys: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.name.startswith(".") and path.name.endswith(f".tmp-{os.getpid()}"):
                continue  # a half-written upload, not an object
            key = path.relative_to(root).as_posix()
            if key.startswith(prefix):
                keys.append(key)
        return sorted(keys)

    def delete_object(self, key: str) -> None:
        path = self._path_for(key)
        path.unlink(missing_ok=True)


# ``urllib.request.urlopen``-compatible callable, injectable for tests.
UrlOpener = Callable[..., Any]


class S3CompatibleObjectStore:
    """Minimal S3-compatible client (stdlib only, SigV4, path-style).

    ``endpoint_url`` is the store's base URL without a trailing slash
    (``https://s3.example.com`` or ``http://localhost:9000`` for a
    local MinIO). All four operations sign with SigV4 over
    ``host`` + ``x-amz-content-sha256`` + ``x-amz-date``.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        timeout_seconds: float = 120.0,
        urlopen: UrlOpener | None = None,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._endpoint = endpoint_url.rstrip("/")
        self._bucket = bucket
        self._region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._timeout_seconds = timeout_seconds
        self._urlopen = urlopen or urllib.request.urlopen
        self._clock = clock or (lambda: datetime.datetime.now(datetime.UTC))
        parsed = urllib.parse.urlparse(self._endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BackupConfigurationError(
                f"Invalid S3 endpoint URL {endpoint_url!r}: expected http(s)://host."
            )
        self._host = parsed.netloc

    # -- signing ---------------------------------------------------------------

    def _sign(
        self,
        method: str,
        path: str,
        params: dict[str, str],
        body: bytes,
        now: datetime.datetime,
    ) -> dict[str, str]:
        """Build the SigV4 Authorization + Amz headers for one request."""
        moment = now.astimezone(datetime.UTC)
        amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = moment.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_uri = "/" + "/".join(_quote_segment(part) for part in path.split("/"))
        canonical_query = "&".join(
            f"{_quote_segment(k)}={_quote_segment(v)}"
            for k, v in sorted(params.items())
        )
        canonical_headers = (
            f"host:{self._host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = ";".join(_SIGNED_HEADERS)
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                _AMZ_ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        key = _signing_key(self._secret_access_key, date_stamp, self._region)
        signature = hmac.new(
            key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {
            "Authorization": (
                f"{_AMZ_ALGORITHM} Credential={self._access_key_id}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "X-Amz-Date": amz_date,
            "X-Amz-Content-Sha256": payload_hash,
        }

    # -- transport ---------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: bytes = b"",
        content_type: str | None = None,
    ) -> bytes:
        """One signed HTTP request; returns the response body bytes."""
        params = params or {}
        now = self._clock()
        # The wire query string MUST use the same encoding as the
        # canonical query string (RFC 3986 percent-encoding, NOT
        # urlencode's form encoding: a space is %20, never +), or the
        # signature the server recomputes will not match.
        query = (
            "&".join(
                f"{_quote_segment(k)}={_quote_segment(v)}"
                for k, v in sorted(params.items())
            )
            if params
            else ""
        )
        url = f"{self._endpoint}/{path}"
        if query:
            url += f"?{query}"
        headers = self._sign(method, path, params, body, now)
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body or None, headers=headers, method=method)
        try:
            with self._urlopen(request, timeout=self._timeout_seconds) as response:
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            if exc.code == 404:
                raise ObjectStoreError(
                    f"S3 object not found ({method} {path}): {detail}",
                    details={"method": method, "path": path, "status": exc.code},
                    not_found=True,
                ) from exc
            raise ObjectStoreError(
                f"S3 request failed ({method} {path}): HTTP {exc.code} {detail}",
                details={"method": method, "path": path, "status": exc.code},
            ) from exc
        except urllib.error.URLError as exc:
            raise ObjectStoreError(
                f"Could not reach the S3 endpoint {self._endpoint}: {exc.reason}",
                details={"method": method, "path": path},
            ) from exc
        except OSError as exc:
            raise ObjectStoreError(
                f"S3 request failed ({method} {path}): {exc}",
                details={"method": method, "path": path},
            ) from exc

    @staticmethod
    def _parse_list_xml(body: bytes) -> tuple[list[str], bool, str | None]:
        """Parse a ListObjectsV2 response: (keys, is_truncated, next_token)."""
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ObjectStoreError(f"Could not parse the S3 list response: {exc}") from exc
        keys: list[str] = []
        for contents in root.findall("./{*}Contents"):
            key = contents.findtext("./{*}Key")
            if key:
                keys.append(key)
        truncated = (root.findtext("./{*}IsTruncated") or "").strip().lower() == "true"
        token = root.findtext("./{*}NextContinuationToken") or None
        return keys, truncated, token

    # -- port --------------------------------------------------------------------

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
        self._request(
            "PUT",
            f"{self._bucket}/{key}",
            body=data,
            content_type=content_type,
        )

    def get_object(self, key: str) -> bytes:
        return self._request("GET", f"{self._bucket}/{key}")

    def list_objects(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            params = {"list-type": "2", "prefix": prefix}
            if token is not None:
                params["continuation-token"] = token
            body = self._request("GET", f"{self._bucket}/", params=params)
            page_keys, truncated, token = self._parse_list_xml(body)
            keys.extend(page_keys)
            if not truncated:
                break
        return sorted(keys)

    def delete_object(self, key: str) -> None:
        try:
            self._request("DELETE", f"{self._bucket}/{key}")
        except ObjectStoreError as exc:
            if exc.not_found:
                return  # S3 DELETE is idempotent
            raise


def build_object_store(settings: BackupSettings) -> (
    FilesystemObjectStore | S3CompatibleObjectStore
):
    """Build the configured backend (fail closed when none is configured)."""
    if settings.endpoint_url is not None:
        # The settings validator already required bucket/region/keys
        # when the endpoint is set; the asserts keep pyright honest.
        assert settings.bucket is not None
        assert settings.region is not None
        assert settings.access_key_id is not None
        assert settings.secret_access_key is not None
        return S3CompatibleObjectStore(
            endpoint_url=settings.endpoint_url,
            bucket=settings.bucket,
            region=settings.region,
            access_key_id=settings.access_key_id,
            secret_access_key=settings.secret_access_key,
        )
    if settings.local_directory is not None:
        return FilesystemObjectStore(settings.local_directory)
    raise BackupConfigurationError(
        "No backup destination is configured: set HESABYAR_BACKUP__ENDPOINT_URL "
        "(S3-compatible) or HESABYAR_BACKUP__LOCAL_DIRECTORY (local files). "
        "See docs/backup-restore.md."
    )
