# Domain and application contracts: HY-ARCH-1.2.0-R1

Normative design for implementation, not implemented endpoints or certified law.
Read alongside [architecture](../ARCHITECTURE.md) and [plan](../IMPLEMENTATION_PLAN.md).

## 1. Shared types and failure semantics

Use strict Pydantic models with `extra='forbid'` and model-level cross-field
validation. Validate omitted/defaulted fields too. Do not depend on a field
validator being called for an omitted optional reference or a default amount.

Money: `{currency: 'IRR', amount: '1500000'}`. Amount is a canonical nonnegative
base-10 integer string, without separators, exponent, signs or leading zeroes
except `0`. Domain storage uses an integer. Signed report differences are a
separate type. Quantities/rates are finite decimal strings with an explicitly
configured scale. Reject booleans, float inputs, negative quantities and implicit
rounding. Toman conversion must be an explicit, visible user-confirmed operation.

A journal line has exactly one positive debit or credit. At least two lines are
required to post; a draft may be incomplete. A single document totals at most
9e15 IRR and has at most 1,000 lines. These are implementation limits. Validate
input size and range before constructing aggregates. Gregorian dates must pass
strict calendar validation; SQLite date checks are defense-in-depth only.

Every response contains `schema_version`, `request_id`, `status`, typed `data`,
`warnings`, `evidence_refs`, and an optional `{code, retryable, details}` error.
Statuses: OK, REVIEW_REQUIRED, PENDING, CONFLICT, ERROR. PENDING is not acceptance.
Examples of local error codes: MISSING_DATA, INVALID_MONEY, RULESET_UNAVAILABLE,
UNBALANCED_VOUCHER, PERIOD_CLOSED, APPROVAL_REQUIRED, APPROVAL_STALE,
IDEMPOTENCY_CONFLICT, UNSUPPORTED_PATTERN, PROTOCOL_UNVERIFIED, UNKNOWN_OUTCOME.
External authority codes are a separate field and retain the adapter version.

## 2. Application service surface

The following are contracts to implement, not promises of current MCP tools.
`actor` and `book` come from trusted server context, never from model assertions.

| Operation | Inputs beyond trusted context | Preconditions and result |
|---|---|---|
| validate_statements | typed statements, units, period, ruleset version | Detailed rule outcomes; cannot issue audit certification |
| prepare_journal | business date, lines, source refs, idempotency key | Save draft; return ID, revision, payload digest and preview |
| request_approval | operation, object ID, expected revision | Open trusted approval flow; returns opaque approval request ID |
| post_journal | draft ID, expected revision, approval ID, idempotency key | Atomic posting + audit; return stable posted voucher ID/number |
| reverse_journal | posted ID, reversal date, reason, approval ID, idempotency key | Exact inverse with dimensions in an open period; original unchanged |
| trial_balance | configured book, date range, cursor | Posted entries only; consistent read and bounded pagination |
| prepare_invoice | kind, pattern, subject, fields, source refs, profile version | Immutable validated revision, totals and calculation trace |
| enqueue_submission | revision ID, approval ID, idempotency key | Atomic outbox entry; never synchronous fiscal acceptance |
| get_submission | submission ID | Authorized read of local and observed remote states |
| reconcile_submission | submission ID or bounded cursor | Worker inquiry only; no reissue or automatic corrective invoice |
| analyze_tax_scenario | hypothesis inputs, evidence and rule version | Advisory result or REVIEW_REQUIRED; never auto-post expenses |

The approval-issuing endpoint is NOT exposed as a model-callable tool. It uses a
human identity authenticated by the host approval adapter. Record actual approver,
book, role, operation, approved canonical digest, revision, environment, expiry,
nonce and used/revoked status. Do not store bearer approval secrets in model output.
R1 may use an out-of-band local CLI owned by the operator; a remote approval UI is
a separately authenticated adapter. A plain tool argument `approved=true` is invalid.

Idempotency is scoped to `(book, operation, key)`: same canonical request returns
the same result, a different request returns CONFLICT. Approval consumption,
idempotency result, posting and audit commit together. The ledger SQL's unique
key is only a storage example; it does not implement this service contract.

## 3. Ledger transitions, closing and dimensions

`draft -> posted` is the only financial posting transition. Draft edits increment
revision and invalidate approvals. Posted/locked is not an editable status toggle.
Closing a period prevents new posting there; authorized reopening appends a reasoned
event without rewriting previous reports. Fiscal years and accounting periods must
not overlap; dates must lie within their configured parent. Their identity and
boundaries may not be mutated after they have been used by a posted entry.

Number allocation uses a per-fiscal-year sequence locked in the posting transaction.
Do not use an unlocked `MAX(number)+1`. Retrying a request returns its existing
number. Seeding is versioned and atomic, refuses conflicting account identities and
must not overwrite a company's chart. Codes are strings; a chart is a configurable
accounting convention, not a universal statutory coding standard.

A primary tafsili coexists with separately keyed party, project, bank, cost-center
or shareholder dimensions. Validate account/dimension compatibility before posting.
Freeze account labels/versions and the full dimension set on posting, so later
renames cannot silently change historical reports. Exact reversal compares the
full original line set including all dimensions and amounts. One full reversal
per original is the initial policy; partial correcting entries require an explicit
new operation/contract. An accepted invoice must not generate a duplicate journal:
link via `(source_type, source_id, source_revision, posting_purpose)` uniqueness.

## 4. Logical records still to implement beyond the foundation SQL

| Record | Required semantics |
|---|---|
| schema_migrations, seed_versions | Immutable applied version + checksum; reject drift; backup before migration |
| year_sequences | Atomic per-year next-number allocation; no number reuse |
| account_snapshots, journal_sources | Immutable posting-time references and labels |
| approvals | Human-issued, hash-bound, one-operation, expiring and revocable |
| idempotency_results | Unique operation/key; request digest and stable result |
| source_documents | Content digest, origin, extraction policy, access/retention metadata |
| rule_versions | Immutable effective-dated rules, primary sources, reviewer and test evidence |
| invoice_revisions, invoice_lines | Immutable approved payload versions; kind/pattern/subject distinct |
| submission_outbox | Unique book/invoice revision/environment, digest, stable remote IDs, lease, attempts |
| submission_events | Append-only attempt/inquiry/response history; no cascade delete |
| authority_observations | Source/environment/as-of timestamp, raw evidence ref, mapped state |
| sales_cap_observations | Authority observation distinguished from local estimate; freshness explicit |
| cap_reservations | Unique per submitted revision; exactly-once reservation/release bookkeeping |
| tax_positions, tax_position_events | Candidate/claimed/accepted/utilized transitions with evidence |
| tax_credit_allocations | Unique claim allocation, remaining balance, no duplicate utilization |

Use foreign keys and database constraints where applicable, application invariants
where cross-record authorization is required. No production schema may be generated
by copying the old nine-table DDL. The new reference ledger is intentionally narrower.

## 5. Submission state machine and crash recovery

Local invoice: DRAFT -> VALIDATED -> APPROVAL_PENDING -> APPROVED -> QUEUED.
A material edit creates a new revision, never mutates approved/queued bytes.
Outbox: READY -> LEASED -> AWAITING_AUTHORITY -> ACCEPTED or REJECTED.
Transport ambiguity from LEASED/AWAITING_AUTHORITY -> UNKNOWN_OUTCOME.
An inquiry may resolve UNKNOWN_OUTCOME to AWAITING_AUTHORITY/ACCEPTED/REJECTED.
Exhausted retry/reconciliation budgets require MANUAL_REVIEW, not silent discard.

Separate authority processing status from buyer approval, cap status and payment.
Preserve unknown remote codes; do not map them to success. The worker has a bounded
lease and records intent before leaving the transaction. No database write lock is
held during network I/O. A crashed/expired sending lease is ambiguous: reconcile
using the same identifiers before any resend. Retry only if the verified protocol
allows safe replay; reuse the approved payload, serial, TaxID and idempotency key.
Never allocate a new invoice to recover a lost response.

Atomic enqueue verifies authority/profile/source readiness, approval digest and
revision, reserves the stable identifier and local capacity policy, records audit
and inserts the outbox row in one transaction. Timeouts do not free a reservation
until the absence of acceptance is known or an operator resolves it with evidence.
Authority observations may already include local sends: reconcile by identifiers
and observation watermark to prevent double-counting reservations. A local number
cannot be advertised as the live authoritative remaining sales cap.

Cancellation before a worker claims READY may cancel a local job with an audit
record. Cancellation once send may have begun is a reconciliation request, not
proof that the fiscal invoice has been cancelled. A legal cancellation/corrective
invoice is a separate approved document under a verified subject-specific schema.

## 6. Regulatory applicability and tax accounting

Resolve rules by jurisdiction, transaction date, tax period, taxpayer/entity type,
activity and applicable product classification. Retain source/document/version/
paragraph/hash and reviewer. A rule without an authoritative source/effective
interval/review/tests is not active. Ambiguous scope, overlapping active versions,
stale catalog or absent period coverage returns REVIEW_REQUIRED.

Evidence collection does not prove eligibility. Tax positions move through
CANDIDATE -> EVIDENCE_COMPLETE -> REVIEWED -> CLAIMED -> ACCEPTED -> UTILIZED with
explicit transitions; REJECTED, EXPIRED and CLAWBACK_REVIEW remain possible. Track
requested, accepted, allocated and remaining amounts separately. No constant 25%
value, 23% expense or 100% recovery is implied. Book-to-tax reconciliation must
separate statutory books, assessed/claimed tax treatment and managerial scenarios.

The original legal text is preserved for review, not retrieval as verified policy.
The rule loader must exclude archive paths and unverified research by default.
Exact country-specific corrections belong in independently reviewed rule versions.

## 7. Required negative/chaos tests

Omitted optional reference on corrective subject; all-zero default journal line;
float/NaN/bool/overrange money; invalid/leap dates; missing applicability; unavailable
rule files; division by zero; unsupported AST constructs; large integer precision;
unauthorized book and invoice reads; approval replay/expiry/payload changes;
concurrent posting/sequence allocation; exact reversal/dimensions; post+audit rollback;
worker crash before send, after send and before receipt commit; conflicting retries;
stale cap observations; concurrent reservations; rule gaps/overlaps; malformed or
prompt-injecting documents; key/PII redaction; backup restore and migration rollback.

The SQL tests cover only selected database invariants. Application, conformance,
authorization, worker and deployment tests above are required implementation work.
