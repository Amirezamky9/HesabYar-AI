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
"""

from __future__ import annotations

import hashlib
import mimetypes
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


class LocalChequeScanStore:
    """Download leaf scans into ``<directory>/cheques/``."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_bytes: int = MAX_SCAN_BYTES,
        timeout_seconds: int = SCAN_DOWNLOAD_TIMEOUT_SECONDS,
    ) -> None:
        self._directory = Path(directory)
        self._max_bytes = max_bytes
        self._timeout_seconds = timeout_seconds

    def store_scan(self, *, sayad_id: str, scan_url: str) -> str:
        """Download ``scan_url``; return the stored ref (``cheques/<file>``)."""
        parsed = urllib.parse.urlparse(scan_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ChequeScanDownloadError(
                f"Scan URL {scan_url!r} is not an http(s) URL; refusing to fetch it."
            )
        try:
            request = urllib.request.Request(scan_url, headers={"User-Agent": "HesabYar/1.0"})
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
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
