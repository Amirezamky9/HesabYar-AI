"""Domain models for the Moadian e-invoicing boundary (spec §12.3, US-404).

Protocol constants and the invoice-payload validator in this module
are grounded in the official INTA technical instruction
«دستورالعمل فنی نحوه اتصال به سامانه مودیان» (instruction PDF dated
1401/07/26, INTA portal ``intamedia.ir``). The instruction's Table 7
defines the invoice items (header / body / payment); its error list
defines which missing fields reject an invoice. Anything this module
does NOT take from the instruction is marked as a documented
assumption — see ``docs/moadian.md`` §"Assumptions".

Deliberately NOT modelled here (they belong to the future production
connector, not the accounting core — spec §12.3):

- the Tax ID composition algorithm (defined in the separate official
  document «قالب شناسه یکتای حافظه مالیاتی و شماره منحصر به فرد
  مالیاتی»); only the 22-character length is enforced;
- request signing (RSA2048-SHA256 over the instruction's normalized
  JSON) and invoice encryption (AES-256-GCM + RSA-OAEP-SHA256) — the
  simulator path sends the invoice JSON in clear inside a packet
  envelope that is explicitly marked simulated, and performs no
  cryptography;
- any HTTP call to ``tp.tax.gov.ir`` — no code in this phase calls the
  real Moadian endpoints.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Any

from hesabyar.domain.standards.models import VerificationStatus

#: Base URL of the Moadian collection/processing subsystem API
#: (instruction §"آدرس API ها"). Recorded as profile metadata only —
#: no code in this phase performs HTTP calls against it.
MOADIAN_API_BASE_URL = "https://tp.tax.gov.ir/req/"

#: Packet type for an invoice packet (instruction §"سرویس درخواست
#: غیرهمگام": ``INVOICE`` + version, e.g. ``INVOICE.V01``).
INVOICE_PACKET_TYPE = "INVOICE.V01"

#: Maximum packets per async request (instruction error 5006
#: ``packet.size.is.too.large``: "در حال حاضر 100 عدد").
MAX_PACKETS_PER_REQUEST = 100

#: Version label of the official instruction this profile schema was
#: modelled from (the instruction PDF's date, 1401/07/26). Profiles
#: record it as ``protocol_version`` so a future instruction revision
#: can be distinguished.
PROTOCOL_VERSION = "1401-07-26"

#: Length of the unique tax number (``taxid``, instruction Table 7
#: row 1) and of the reference tax id (``irtaxid``, row 6).
TAXID_LENGTH = 22

#: Length of the fiscal memory unique id (instruction §"پیکربندی
#: پایانه فروشگاهی-حافظه مالیاتی": a 6-character code issued in the
#: taxpayer's کارپوشه).
FISCAL_ID_LENGTH = 6

#: Length of an economic code. Instruction Table 7 (rows 9/12) says
#: 10 digits, but the instruction's own worked example uses a
#: 14-digit code (``32652362589632``) — the new economic code
#: taxpayers must use since 1401/07/01. Both lengths are accepted;
#: see ``docs/moadian.md`` §"Assumptions".
ECONOMIC_CODE_LENGTHS: frozenset[int] = frozenset({10, 14})

#: Length of the invoice serial (``inno``, instruction Table 7 row 5).
INVOICE_SERIAL_LENGTH = 10

#: Length of a goods/service id (``sstid``, instruction Table 7 row 34).
SERVICE_STUFF_ID_LENGTH = 13


@unique
class MoadianEnvironment(StrEnum):
    """Which Moadian environment a profile targets.

    ``TEST`` profiles rehearse against the deterministic simulator.
    ``PRODUCTION`` profiles may hold drafts and recorded approvals,
    but queueing a production submission is refused until the
    production connector exists (spec §12.3: simulator first).
    """

    TEST = "test"
    PRODUCTION = "production"


@unique
class SenderMode(StrEnum):
    """Who sends the invoice (instruction §"آدرس API ها").

    ``SELF_TSP`` — the taxpayer sends directly (``self-tsp`` paths);
    ``TSP`` — a trusted tax-services company sends for the taxpayer
    (``tsp`` paths).
    """

    SELF_TSP = "self-tsp"
    TSP = "tsp"


@unique
class SubmissionStatus(StrEnum):
    """Lifecycle of a Moadian submission (HesabYar-local states).

    ``draft`` → ``approved`` → ``queued`` → ``submitted`` →
    ``accepted`` / ``rejected``. ``failed`` is a transport-level
    failure (the simulator refused the packet); a rejected invoice is
    a business outcome (``rejected``), not a transport failure.
    Draft-only creation needs no approval (spec §19 rule 11); queueing
    always requires a recorded human approval.
    """

    DRAFT = "draft"
    APPROVED = "approved"
    QUEUED = "queued"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"


@unique
class DocumentStatus(StrEnum):
    """Lifecycle of a Moadian document, mirroring its latest submission."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@unique
class OfficialStatus(StrEnum):
    """Status values returned by Moadian inquiry methods.

    ``PENDING`` ("هنوز درخواست پردازش نشده است") and ``FAILED`` are
    named in the official instruction (async flow; ``INQUIRY_BY_TIME``
    returns only ``FAILED`` packets). ``SUCCESS``, ``IN_PROGRESS``,
    ``NOT_FOUND`` and ``TIMEOUT`` are corroborated by the public
    ``moadian-sdk`` ``InvoiceStatus`` enum (PyPI ``moadian-sdk``
    0.1.0) — see ``docs/moadian.md`` §"Assumptions".
    """

    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    IN_PROGRESS = "IN_PROGRESS"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"


@unique
class SimulationScenario(StrEnum):
    """Which deterministic fixture outcome the simulator must produce.

    Test-only: ``accept`` ends in ``SUCCESS``/accepted, ``reject``
    ends in ``FAILED``/rejected with simulated error evidence. The
    scenario is stored on the submission so a rehearsal is
    reproducible from its record.
    """

    ACCEPT = "accept"
    REJECT = "reject"


@unique
class StatusEventSource(StrEnum):
    """Where a status event's evidence came from."""

    SIMULATOR = "simulator"
    OPERATOR = "operator"
    SYSTEM = "system"


@dataclass(frozen=True)
class MoadianProfile:
    """A versioned Moadian protocol profile (one fiscal memory).

    ``key_reference`` names where the signing key lives (an env-var
    name for test keys, a secret-manager path for production keys) —
    it is a reference, never key material. ``key_fingerprint`` is the
    SHA-256 hex of the *public* key, when known. No secret is ever
    stored on a profile (spec §7.4 / §12.3).
    """

    id: str
    tenant_id: str
    name: str
    slug: str
    environment: MoadianEnvironment
    sender_mode: SenderMode
    fiscal_id: str | None
    economic_code: str | None
    packet_type: str
    api_base_url: str
    protocol_version: str
    source_authority: str | None
    source_url: str | None
    snapshot_uri: str | None
    verification_status: VerificationStatus
    key_reference: str | None
    key_fingerprint: str | None
    is_active: bool
    notes: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


@dataclass(frozen=True)
class MoadianDocument:
    """One invoice prepared for Moadian submission.

    ``payload`` is the Moadian invoice JSON (``header`` / ``body`` /
    optional ``payment`` per instruction Table 7). ``invoice_id`` is
    the HesabYar/Tryton invoice this document was built from — a plain
    external reference, never a cross-database FK (spec §7.1).
    """

    id: str
    tenant_id: str
    profile_id: str
    invoice_id: str | None
    taxid: str | None
    serial: str | None
    invoice_type: int
    invoice_pattern: int
    invoice_subject: int
    payload: Mapping[str, Any]
    payload_hash: str
    status: DocumentStatus
    created_at: datetime.datetime
    updated_at: datetime.datetime


@dataclass(frozen=True)
class MoadianSubmission:
    """One submission attempt of a document to Moadian.

    ``uid`` is the client-generated unique submission id (instruction:
    uuid3 format, sent unencrypted). ``reference_number`` is the
    server-issued unique receipt id (uuid format) returned on
    acceptance into the queue. ``request_hash`` / ``response_hash``
    are SHA-256 hexes of the canonical packet / response payloads
    (spec §12.3 evidence). ``approved_by`` / ``approved_at`` /
    ``approval_note`` are the recorded human approval (spec §19
    rule 11) — queueing without them is impossible.
    """

    id: str
    tenant_id: str
    document_id: str
    profile_id: str
    uid: str
    reference_number: str | None
    correlation_id: str
    environment: MoadianEnvironment
    scenario: SimulationScenario
    status: SubmissionStatus
    retry: bool
    approved_by: str | None
    approved_at: datetime.datetime | None
    approval_note: str | None
    request_hash: str | None
    response_hash: str | None
    request_payload: Mapping[str, Any] | None
    response_payload: Mapping[str, Any] | None
    outbox_event_id: str | None
    last_error_code: str | None
    last_error_detail: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


@dataclass(frozen=True)
class MoadianStatusEvent:
    """One official status observation for a submission.

    ``evidence`` is the inquiry response item verbatim
    (``{uid, referenceNumber, status, data, packetType, fiscalId}`` —
    instruction Table 10), so the official status is always traceable
    to the evidence that reported it.
    """

    id: str
    submission_id: str
    status: OfficialStatus
    source: StatusEventSource
    evidence: Mapping[str, Any] | None
    correlation_id: str | None
    occurred_at: datetime.datetime
    created_at: datetime.datetime


# ---------------------------------------------------------------------------
# Invoice payload validation (instruction Table 7 + error list)
# ---------------------------------------------------------------------------

#: Header fields the instruction's error list requires for every
#: invoice (rows: seller economic code, invoice datetime, serial,
#: type, pattern, subject, buyer type, totals, settlement method).
_REQUIRED_HEADER_FIELDS: tuple[str, ...] = (
    "taxid",
    "indatim",
    "indati2m",
    "inty",
    "inno",
    "inp",
    "ins",
    "tins",
    "tob",
    "tprdis",
    "tdis",
    "tadis",
    "tvam",
    "todam",
    "tbill",
    "setm",
    "cap",
    "insp",
    "tvop",
)

#: Body-line fields the error list requires (goods/service id, unit
#: fee, VAT rate, quantity, amounts, line total).
_REQUIRED_BODY_FIELDS: tuple[str, ...] = (
    "sstid",
    "sstt",
    "mu",
    "am",
    "fee",
    "prdis",
    "dis",
    "adis",
    "vra",
    "vam",
    "tsstam",
)

#: Allowed values (instruction Table 7): inty 1-3 (invoice type),
#: inp 1-6 (pattern), ins 1-4 (subject), tob 1-5 (buyer type),
#: setm 1-3 (settlement method).
_ALLOWED_VALUES: dict[str, frozenset[int]] = {
    "inty": frozenset({1, 2, 3}),
    "inp": frozenset({1, 2, 3, 4, 5, 6}),
    "ins": frozenset({1, 2, 3, 4}),
    "tob": frozenset({1, 2, 3, 4, 5}),
    "setm": frozenset({1, 2, 3}),
}

#: Subjects that require a reference tax id (instruction error list
#: row 9: corrective/cancellation/return invoices — ins 2, 3, 4).
_SUBJECTS_REQUIRING_REFERENCE: frozenset[int] = frozenset({2, 3, 4})


def _is_missing(value: Any) -> bool:
    """True when a field counts as absent (None or empty string).

    The instruction allows a valueless parameter to be sent as null
    or omitted — but the error list rejects a *required* field sent
    that way, so null/"" is missing here.
    """
    return value is None or (isinstance(value, str) and value == "")


def _check_int_field(
    errors: list[str],
    where: str,
    field: str,
    value: Any,
    *,
    length: int | None = None,
    lengths: frozenset[int] | None = None,
    allowed: frozenset[int] | None = None,
) -> None:
    """Validate an integer-typed invoice field (Table 7 types)."""
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{where}.{field} must be an integer.")
        return
    if length is not None and len(str(value)) != length:
        errors.append(f"{where}.{field} must be {length} digits.")
    if lengths is not None and len(str(value)) not in lengths:
        errors.append(f"{where}.{field} must be {' or '.join(str(n) for n in sorted(lengths))} digits.")
    if allowed is not None and value not in allowed:
        errors.append(
            f"{where}.{field} must be one of {sorted(allowed)}."
        )


def _check_number_field(errors: list[str], where: str, field: str, value: Any) -> None:
    """Validate a double-typed invoice field (Table 7 types)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{where}.{field} must be a number.")


def validate_invoice_payload(payload: Mapping[str, Any]) -> list[str]:
    """Validate a Moadian invoice JSON object against Table 7.

    Returns the list of validation problems (empty when valid). The
    checks cover the fields the instruction's error list marks as
    required for a sales-pattern invoice (pattern 1); pattern-specific
    extras (foreign-currency sale, gold/jewellery, contracting,
    utility bills, flight tickets) are NOT validated here — see
    ``docs/moadian.md`` §"Assumptions". The validator never invents
    values: a missing required field is an error, never a default.
    """
    errors: list[str] = []
    header = payload.get("header")
    if not isinstance(header, Mapping):
        errors.append("header must be an object.")
        return errors
    body = payload.get("body")
    if not isinstance(body, list) or not body:
        errors.append("body must be a non-empty array of invoice lines.")
        return errors

    for field in _REQUIRED_HEADER_FIELDS:
        if _is_missing(header.get(field)):
            errors.append(f"header.{field} is required.")
    taxid = header.get("taxid")
    if not _is_missing(taxid):
        if not isinstance(taxid, str) or len(taxid) != TAXID_LENGTH:
            errors.append(f"header.taxid must be a {TAXID_LENGTH}-character string.")
    inno = header.get("inno")
    if not _is_missing(inno):
        if not isinstance(inno, str) or len(inno) != INVOICE_SERIAL_LENGTH:
            errors.append(f"header.inno must be a {INVOICE_SERIAL_LENGTH}-character string.")
    for field, allowed in _ALLOWED_VALUES.items():
        value = header.get(field)
        if not _is_missing(value):
            _check_int_field(errors, "header", field, value, allowed=allowed)
    tins = header.get("tins")
    if not _is_missing(tins):
        _check_int_field(errors, "header", "tins", tins, lengths=ECONOMIC_CODE_LENGTHS)
    tinb = header.get("tinb")
    if not _is_missing(tinb):
        _check_int_field(errors, "header", "tinb", tinb, lengths=ECONOMIC_CODE_LENGTHS)
    for field in ("tprdis", "tdis", "tadis", "tvam", "todam", "tbill", "cap", "insp", "tvop"):
        value = header.get(field)
        if not _is_missing(value):
            _check_number_field(errors, "header", field, value)
    ins = header.get("ins")
    if isinstance(ins, int) and ins in _SUBJECTS_REQUIRING_REFERENCE:
        irtaxid = header.get("irtaxid")
        if _is_missing(irtaxid):
            errors.append("header.irtaxid is required when header.ins is 2, 3 or 4.")
        elif not isinstance(irtaxid, str) or len(irtaxid) != TAXID_LENGTH:
            errors.append(f"header.irtaxid must be a {TAXID_LENGTH}-character string.")

    for index, line in enumerate(body):
        where = f"body[{index}]"
        if not isinstance(line, Mapping):
            errors.append(f"{where} must be an object.")
            continue
        for field in _REQUIRED_BODY_FIELDS:
            if _is_missing(line.get(field)):
                errors.append(f"{where}.{field} is required.")
        sstid = line.get("sstid")
        if not _is_missing(sstid):
            _check_int_field(errors, where, "sstid", sstid, length=SERVICE_STUFF_ID_LENGTH)
        mu = line.get("mu")
        if not _is_missing(mu):
            _check_int_field(errors, where, "mu", mu)
        am = line.get("am")
        if not _is_missing(am):
            _check_number_field(errors, where, "am", am)
        for field in ("fee", "prdis", "dis", "adis", "vra", "vam", "tsstam"):
            value = line.get(field)
            if not _is_missing(value):
                _check_number_field(errors, where, field, value)

    payment = payload.get("payment")
    if payment is not None and not isinstance(payment, list):
        errors.append("payment must be an array when present.")
    return errors


def validate_invoice_payload_or_raise(payload: Mapping[str, Any]) -> None:
    """Validate, raising ValueError listing every problem (fail-closed)."""
    errors = validate_invoice_payload(payload)
    if errors:
        raise ValueError("Invalid Moadian invoice payload: " + "; ".join(errors))
