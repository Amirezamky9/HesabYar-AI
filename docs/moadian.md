# Moadian Test Profiles + Simulator (US-404)

Moadian e-invoicing preparation: versioned protocol profiles, invoice
documents, submissions with a recorded human approval step, an
outbox-driven reconciliation worker, and a deterministic fixture
simulator (design-eng spec §7.4, §12.3, §19 rule 11; ARCHITECTURE.md
§6.3).

**There is no production Moadian connectivity in this phase.** No code
performs HTTP calls to `tp.tax.gov.ir` or any real Moadian endpoint.
Production profiles may hold drafts and recorded approvals, but
queueing a production submission is refused with typed 501
`MOADIAN_PRODUCTION_NOT_CONNECTED` until the production connector
lands in a later phase.

## What lives where

| Piece | Location |
|---|---|
| Tables | Alembic `0007`: `moadian_profiles`, `moadian_documents`, `moadian_submissions`, `moadian_status_events` |
| ORM models | `src/hesabyar/infrastructure/persistence/postgres/models.py` (US-404 section) |
| Domain models + invoice validator | `src/hesabyar/domain/moadian/models.py` |
| Simulator port | `src/hesabyar/domain/moadian/ports.py` |
| Deterministic simulator | `src/hesabyar/infrastructure/moadian/simulator.py` |
| Query service | `src/hesabyar/application/queries/moadian.py` |
| Command services + reconciliation worker | `src/hesabyar/application/commands/moadian.py` |
| REST | `src/hesabyar/interfaces/rest/routers/moadian.py` (+ `schemas/moadian.py`) |
| Scopes | `moadian:read`, `moadian:submit` (`src/hesabyar/infrastructure/auth/scopes.py`) |

## Official sources

Every field/protocol claim in the profile schema points at one of
these. Researched 2026-11-17.

| # | Source | What it grounds |
|---|---|---|
| 1 | INTA portal, «پایانه‌های فروشگاهی و سامانه مودیان» → «آیین‌نامه‌ها و دستورالعمل‌ها و نرم‌افزارهای مرتبط», `https://www.intamedia.ir/The-regulation-of-store-terminals` | Canonical home of the official instruction documents (instruction, invoice-issuance instruction, fiscal-memory/tax-id template, SDK) |
| 2 | «دستورالعمل فنی نحوه اتصال به سامانه مودیان» (instruction PDF, dated 1401/07/26), `https://www.intamedia.ir/Portals/0/news/Terminals/سند دستورالعمل فنی نحوه اتصال به سامانه مودیان14010726.pdf` | The protocol: API base `https://tp.tax.gov.ir/req/`, sync/async methods, packet + request shapes, signing/encryption, invoice Table 7, error lists |
| 3 | Same instruction as reproduced at `https://limotx.ir/article/40` (read in full 2026-11-17) | Working copy of source 2 (the INTA portal PDF is not directly fetchable from this environment); all table/section citations below refer to it |
| 4 | «قالب شناسه یکتای حافظه مالیاتی و شماره منحصر به فرد مالیاتی» (`RC_IITP.IS _V6.3.pdf` on the INTA portal) | Tax ID composition — **not** reproduced here (see Assumptions) |
| 5 | `https://pypi.org/project/moadian-sdk/0.1.0/` (public Python SDK) | Corroboration only: `InvoiceStatus` enum (`SUCCESS`, `FAILED`, `IN_PROGRESS`, `NOT_FOUND`, `TIMEOUT`), serial/tax-id handling |
| 6 | `https://root.packagist.org/packages/kiankamgar/php-moadian` (public PHP SDK) | Corroboration only: `tp.tax.gov.ir` API surface, invoice header/body/payment shape |

## Protocol summary (from source 2/3)

- **Base URL**: `https://tp.tax.gov.ir/req/`. Paths split by sender:
  `api/self-tsp/...` (taxpayer sends directly) vs `api/tsp/...`
  (trusted tax-services company sends).
- **Async submit** (invoice sending is async, queue-based):
  `api/{self-tsp|tsp}/async/{normal-enqueue|fast-enqueue}/`.
  The response only confirms queueing; the processing outcome comes
  from inquiry.
- **Sync methods**: `GET_TOKEN`, `GET_FISCAL_INFORMATION`,
  `INQUIRY_BY_UID`, `INQUIRY_BY_REFERENCE_NUMBER`, `INQUIRY_BY_TIME`,
  `INQUIRY_BY_TIME_RANGE`, `GET_SERVER_INFORMATION`,
  `GET_SERVICE_STUFF_LIST`, `GET_ECONOMIC_CODE_INFORMATION`.
- **Request**: POST; headers `Authorization` (Bearer JWT token),
  `requestTraceId` (dedup id), `timestamp`; body
  `{packets: Packet[], signature, signatureKeyId}` (sync: a single
  `packet`).
- **Packet** (Tables 5–6): `{uid, packetType, retry, data,
  encryptionKeyId, symmetrickey, iv, fiscalId, dataSignature}`.
  `uid` is the client-generated unique submission id (uuid3 format,
  sent unencrypted). `packetType` is `INVOICE.V01` (INVOICE + version).
  `retry=true` marks a resubmission after a failure.
- **Async response** (Tables 8–9): `{uid, referenceNumber, errorCode,
  errorDetail}` per packet. `referenceNumber` is the server-issued
  unique receipt id (uuid format).
- **Inquiry response** (Table 10): `[{uid, referenceNumber, status,
  data, packetType, fiscalId}]`. `PENDING` means not yet processed;
  `INQUIRY_BY_TIME` / `INQUIRY_BY_TIME_RANGE` return only `FAILED`
  packets. Max 100 packets per async request (error 5006).
- **Security** (recorded, not implemented in this phase): JWT token
  via `GET_TOKEN` (signed request; `username` is the fiscal memory
  id or TSP id); request signing RSA2048-SHA256 over normalized JSON
  (`#`-separated values, keys sorted, null/`""` → `###`, `#` → `##`);
  invoice encryption AES-256-GCM (256-bit symmetric key + 128-bit IV,
  with an XOR pre-step in 256-bit blocks); the symmetric key encrypted
  with the organization's 4096-bit public key (RSA-OAEP-SHA256) from
  `GET_SERVER_INFORMATION`; taxpayer keys are 2048-bit RSA from a
  certified intermediate CA (`rca.gov.ir`).
- **Invoice** (Table 7): `header` (33 fields: `taxid` 22 chars,
  `indatim`/`indati2m` timestamps, `inty` 1–3, `inno` 10 chars,
  `irtaxid` 22 chars, `inp` 1–6, `ins` 1–4, `tins` 10 or 14 digits, `tob`
  1–5, totals, `setm` 1–3, …), `body` lines (`sstid` 13 digits,
  `sstt`, `mu`, `am`, `fee`, `prdis`, `dis`, `adis`, `vra`, `vam`,
  `tsstam`, …), optional `payment` array. The fiscal memory id is 6
  characters; the unique tax number is 22 characters.

## Lifecycle

```
profile (test|production, self-tsp|tsp)
  └─ document (draft) ── payload validated against Table 7
       └─ submission (draft)
            │  approve → recorded approval (approver, timestamp, note)
            ▼
         approved ── queue → outbox event (same transaction)
            │  production: 501 MOADIAN_PRODUCTION_NOT_CONNECTED
            ▼
         queued ── reconcile pass ──▶ simulator submit
            │  receipt (referenceNumber) + request/response hashes
            ▼
         submitted ── reconcile pass ──▶ inquiry → PENDING (1st poll)
            │  2nd poll: SUCCESS → accepted | FAILED → rejected
            ▼
         accepted / rejected   (document status mirrors it)
```

- **Draft-only creation** (document, submission) needs no approval.
- **Approval is a recorded step**: `approved_by` (API client id),
  `approved_at`, `approval_note` on the submission, plus an
  `audit_events` row (`moadian.submission.approve`). No LLM or
  automation can submit as an unreviewed side effect (spec §19
  rule 11): queueing requires the recorded approval, and the
  approval endpoint requires a human-held `moadian:submit` key.
- **Outbox** (spec §10.3): queueing writes a
  `moadian.submission.queued` event in the same transaction as the
  status change. The worker publishes it on dispatch (with an
  `integration_attempts` row) or marks it failed.
- **Evidence** (spec §12.3): every submission persists
  `request_hash`/`response_hash` (SHA-256 of the canonical packet /
  response), `request_payload`/`response_payload`, timestamps and
  `correlation_id`; every inquiry response is persisted verbatim as a
  `moadian_status_events` row (`evidence`).

## Simulator

`DeterministicMoadianSimulator` (`infrastructure/moadian/simulator.py`)
is in-memory and deterministic: no network, no signing, no encryption.

- `submit_packet` echoes the uid and issues a deterministic receipt
  (`uuid5` over the uid). Duplicate uid → instruction error 5005
  (`duplicate.request.uid`); malformed uid → 5003
  (`uid.format.is.not.valid`).
- `inquiry_by_uid` returns `PENDING` on the first poll, then the
  scenario's terminal status: `SUCCESS` (`accept`) or `FAILED` with
  simulated error evidence (`reject`). Unknown uid → `NOT_FOUND`.
- A submission therefore needs **two reconcile passes** to reach a
  terminal status — exactly like the real async lifecycle.
- The simulator is re-seeded from the database on each pass (poll
  counts come from the persisted status events), so a fresh instance
  per request still resumes the lifecycle correctly.
- Simulated payloads are marked `"simulated": true`; the rejection
  evidence says explicitly that it is simulated, not an official
  Moadian response.

## Secrets

- Profiles store **no secrets**: `key_reference` names where the
  signing key lives (an env-var name for test keys, a secret-manager
  path for production keys) and is refused when it looks like key
  material (PEM block, newline); `key_fingerprint` is the SHA-256 of
  the *public* key only.
- Test keys (if a test connector is ever configured) come from the
  environment only; production keys belong in a secret manager —
  both out of scope for this story.
- No key material appears in responses, logs, audit events or error
  details (the profile response exposes only the reference and the
  public-key fingerprint).

## Assumptions (documented, not facts)

1. **Inquiry status vocabulary**: the instruction names `PENDING`
   and `FAILED` explicitly. `SUCCESS`, `IN_PROGRESS`, `NOT_FOUND`,
   `TIMEOUT` are corroborated by the public `moadian-sdk`
   `InvoiceStatus` enum (source 5), not by the instruction text read
   here. If the official instruction lists a different vocabulary,
   `OfficialStatus` must be corrected before production use.
2. **Tax ID composition**: the 22-character tax id is composed of
   the fiscal memory id, date and serial per the separate template
   document (source 4), which was not read in full. Only the length
   is enforced; no composition algorithm is implemented. The
   simulator does not generate tax ids — documents carry the tax id
   supplied in their payload.
3. **Timestamp format**: Table 7 lists `indatim`/`indati2m` as
   14-character timestamps, but the instruction's worked example
   shows 13-digit epoch millis (`1655620821274`). Only presence is
   validated; the format is not enforced.
4. **Pattern-specific required fields**: the validator enforces the
   required fields for the common sales pattern (pattern 1). The
   instruction's error list has pattern-specific extras (foreign
   currency, gold/jewellery, contracting, utility bills, flight
   tickets); those are not validated yet and are not claimed.
5. **Signing/encryption**: the algorithms above are recorded from
   the instruction but not implemented — the simulator path sends
   the invoice JSON in clear inside an explicitly simulated packet.
   Real signing/encryption land with the production connector.
6. **Poll timing**: the real API needs a delay before inquiry (public
   SDKs wait 10–15s); the simulator has no timing — polls are
   logical, driven by reconcile passes.
7. **Economic code length**: Table 7 says 10 digits for `tins`/`tinb`,
   but the instruction's own worked example uses a 14-digit seller
   code (`32652362589632`) — the new economic code taxpayers must use
   since 1401/07/01. Both lengths are accepted (profile
   `economic_code`, payload `tins`/`tinb`).
