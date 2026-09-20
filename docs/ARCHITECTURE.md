# HesabYar-AI: canonical implementation architecture

Revision: **HY-ARCH-1.2.0-R1**  
Reviewed: **2026-09-20**  
Source baseline: **06c99b58e01f3341f981eb16b334d3f1ed84a10c**  
State: **FOUNDATION_IMPLEMENTATION_READY / PRODUCTION_BLOCKED**

This is a design and implementation contract, not a claim that the runtime exists,
that an accountant has certified the product, or that current Iranian tax law has
been fully verified. The user authorized architecture corrections. Newly selected
engineering defaults are recorded in `adr/0001-implementation-readiness.md`; they
are not misrepresented as decisions from the earlier interviews.

The earlier Persian architecture and research are preserved unchanged under
`archive/`. They are historical evidence, **not executable instructions**.

## 1. Scope, precedence and what actually exists

Preserve the product direction: Iranian accounting knowledge, deterministic
financial validation, double-entry bookkeeping with floating tafsili, a thin MCP
interface, native Moadian integration, document extraction and evidence-based tax
and inflation analysis. Use Python and SQLite for the first implementation. Keep
foreign-currency and general-sales invoice adapters in the roadmap; do not replace
the project with a generic bookkeeping chatbot.

At the source baseline the repository contains knowledge documents, YAML statement
templates, a Python financial validator, source-processing scripts and validator
CI. The proposed ledger, Moadian client, MCP server and package entry point are not
implemented merely because their names appear in a diagram. The inherited
validator has fail-open behavior; it is **not** an authorization gate for posting,
submitting or certifying statements until milestone F1 is complete.

Normative precedence for IMPLEMENTATION:
1. This architecture and `contracts/domain-contracts.md`.
2. Accepted engineering ADRs and the phase/gate matrix in `IMPLEMENTATION_PLAN.md`.
3. Executable foundation examples in `contracts/ledger.sql` and their tests, limited
   to the guarantees explicitly described below.
4. Existing code and tests are migration inputs, not automatically correct rules.
5. Archived research, examples and marketing prose cannot override the above.

For LEGAL TRUTH, a currently applicable primary source plus documented review is
required. An architecture document, an LLM answer or a passing unit test cannot
establish the law. Conflicts with a primary source create a reviewed change and a
new rule version; they must not be silently reconciled by the model.

## 2. Release boundaries

| Phase | Deliverable | External effects |
|---|---|---|
| F0 | Package boundary, dependency lock, source/rule manifest, CI | None |
| F1 | Fail-closed deterministic validator | Read-only; no certification claim |
| F2 | Local double-entry ledger and protected approval/posting | Local approved writes only |
| F3 | Read-only MCP, then approved ledger tools; preview parsing | No tax authority traffic |
| F4 | Immutable invoice drafts, submission outbox, simulator | Offline only |
| F5 | Versioned real Moadian adapter and conformance evidence | Test integration only when documented |
| F6 | Reviewed regulatory features and controlled deployment | Separate production acceptance required |

F0-F4 can be implemented without treating unverified fiscal claims as facts.
F5 needs the authoritative protocol and independently sourced examples. F6 needs
legal applicability review, operational/security acceptance and owner release
approval. These are narrow integration/release gates, not a reason to restart all
architecture discovery. No automatic production send is authorized by this file.

## 3. Module boundaries

```text
MCP / CLI / trusted approval UI
              |
      Application services ------ Authorization / Approval service
              |
  +-----------+------------+-------------+------------------+
  | Ledger    | Validation | Invoices    | Advisory analysis|
  | domain    | domain     | & outbox    | & rule registry  |
  +-----------+------------+-------------+------------------+
              |
  Ports: LedgerRepository, RuleRepository, InvoiceRepository,
         SubmissionTransport, Signer, Clock, DocumentStore, Parser
              |
  Adapters: SQLite, local filesystem, verified native Moadian,
            offline simulator, PDF text parser, optional vision
```

The domain imports no MCP, HTTP client, LLM SDK or production credentials.
Application services own transactions, authorization, approvals and idempotency.
Adapters implement storage/transport; tools only decode inputs and encode results.
The original six seams remain recognizable; authorization, rule provenance and
submission durability are added as explicit cross-cutting boundaries.

Use a `src/hesabyar/` package, not new importable modules mixed into the existing
Markdown `core/` tree. Existing knowledge paths remain stable. Package resources
must be explicitly included and tested from an installed wheel, not only a checkout.

## 4. Money, quantities, dates and identifiers

All persisted functional-currency amounts are integer IRR. At public JSON/MCP
boundaries encode money as canonical decimal strings with an IRR currency field;
clients must not lose precision through JavaScript numbers. No float, NaN,
Infinity, Boolean-as-integer or implicit toman conversion is accepted.

Quantities, rates and foreign-currency values are decimal strings parsed as
`Decimal`. SQLite `DECIMAL(p,s)` does not supply fixed-point enforcement: store
canonical text or scaled integers with explicit scale. Define rounding in a
versioned policy, including line versus invoice aggregation. A preview rounding
policy is not automatically a valid Moadian serialization policy.

The foundation contract chooses a maximum document total of 9,000,000,000,000,000
IRR and 1,000 lines as **engineering limits**, not statutory thresholds. Reject
range violations before arithmetic/storage; do not silently cast or round.

Persist canonical ISO Gregorian business dates and UTC event timestamps. Convert
Jalali only at an explicit boundary; retain the original input/date convention in
evidence. Fiscal year, accounting period and statutory tax quarter are distinct.
A company's fiscal year is configured, not inferred from the current calendar.

Keep invoice subject/lifecycle, invoice kind, invoice pattern, buyer category,
local workflow, authority processing and buyer response as separate fields.
Exact external codes belong to the selected Moadian specification, not guesses.
Identifiers remain strings so leading zeroes survive.

## 5. Ledger and persistence

R1 is single legal entity per database; configure identity at initialization and
refuse a different entity identity on reopen. This is not multi-tenant SaaS. A
future multi-entity adapter requires tenant-scoped keys, authorization and tests.

`contracts/ledger.sql` is an executable **foundation contract**, not a production
migration. It demonstrates strict storage, real line-sum checking, period checks,
per-year voucher numbers, immutable posted lines/dimensions, and append-only audit
records. The application obligations listed at the end of that SQL file are still
mandatory implementation work. Passing its tests does not imply those obligations
have already been implemented.

Posting runs in ONE bounded database write transaction:
1. Resolve authenticated actor and book; check permission and idempotency key.
2. Load draft at expected revision and approved payload digest.
3. Validate active posting accounts, required tafsili/dimensions, valid dates and
   open period; sum the actual lines with exact arithmetic.
4. Allocate the next voucher number within its fiscal year under the same lock.
5. Freeze the journal and its account/rule/source snapshots; append the audit event.
6. Commit everything together or roll everything back.

No network request or model call is allowed inside this transaction. Each SQLite
connection enables foreign keys, recursive triggers and a bounded busy timeout; WAL is enabled on the
real local database. Use local persistent storage, not a network filesystem or an
ephemeral container layer. One writer is a deliberate first-release limitation.

Posted entries are immutable. Corrections create separately approved reversing
and replacement entries. Exact reversal includes account, primary tafsili and all
analytical dimensions, not just equal totals. Period close/reopen, account changes
and migrations require explicit audited operations. Reports use a consistent read
snapshot and posted entries only.

## 6. Authorization, human confirmation and audit

The model may propose a draft; it may not confer authority upon itself. Splitting
`record_journal` into prepare/approve/post is mandatory. Approval is issued by a
trusted human interaction outside model-controlled tool arguments, bound to actor,
entity, operation, payload hash, revision, environment and expiry. Any material
change invalidates approval. Replay or role mismatch fails closed.

Roles: reader, bookkeeper, approver and administrator. All roles are scoped to the
configured book. Administrative permission does not bypass posting invariants.
Local stdio still inherits OS access and is not automatically a secure approval UI.
A remote deployment requires an authenticated identity-to-role mapping and explicit
client permissions; knowing an invoice ID does not authorize inquiry or access.

Audit events carry actor, operation, correlation ID, before/after digests, source
versions and UTC time. The original cascade-delete transmission design is removed.
Use append-only events plus mutable projections for current status. Hash chains
provide tamper evidence, not an unqualified legal non-repudiation guarantee. Keep
protected backups/checkpoints and test detection of a rewritten chain.

Private keys, access tokens, personal identifiers and full invoices must not appear
in model context, Git, ordinary logs or CI artifacts. Raw authority responses go to
an access-controlled evidence store with a hash and retention policy; tool output
contains a redacted summary and an opaque evidence reference.

## 7. Financial validation

Replace the legacy expression substitution/eval path with an allowlisted AST
interpreter or an explicit operation registry. Reject calls except a precisely
defined aggregate, attribute introspection, excessive expression complexity,
unknown paths, unsupported types and oversized inputs.

A rule result is PASS, FAIL, NOT_APPLICABLE, MISSING_DATA or ENGINE_ERROR. A skipped
or unevaluable rule is never PASS. Applicability needs explicit context; missing
context is MISSING_DATA. Missing rule files or an empty selected rule set are
configuration errors. A report with incomplete mandatory rules cannot be valid.

Use exact integer/Decimal operations and unit-aware tolerances. Distinguish mandatory
statement relationships from configurable analytical heuristics such as leverage
ratios. Quote source paragraph identifiers as strings, not YAML numeric values.
Show evaluated, failed, not-applicable and missing counts separately.

## 8. Invoice preparation and Moadian delivery

An invoice draft is validated against a versioned pattern registry. Patterns 1 and
2 are planned adapters; their exact legal meanings/applicability must be verified.
Exports must not be silently equated to currency sales. Unsupported patterns fail
explicitly; a placeholder must not serialize a plausible-looking fiscal invoice.

Prepare an immutable payload and a calculation trace, then request human approval.
Atomic enqueue reserves the stable serial/TaxID, records the approval digest and
writes an outbox item **before any network transmission**. A durable worker, not an
LLM polling loop, sends and reconciles. An HTTP acknowledgment is not final fiscal
acceptance, buyer approval or input-tax credit confirmation.

See `contracts/domain-contracts.md` for state transitions and timeout recovery.
Exactly-once local enqueue is achievable through uniqueness/transactions; do not
claim exactly-once delivery over the remote network. A response lost after send
produces UNKNOWN_OUTCOME and inquiry/manual reconciliation, not a fresh invoice.

Cryptography uses vetted libraries behind `Signer` and `SubmissionTransport`.
The prior epoch, Verhoeff preprocessing, algorithm suite, endpoint and sandbox URL
are **unverified protocol inputs**, not canonical implementation constants. Obtain
an official document/version/hash and independent golden vectors for canonical
serialization, TaxID, signing, encryption, authentication and inquiry. Implement
only that verified profile. A simulator using the same algorithm is not independent
proof of conformance. No guessed government endpoint is shipped enabled.

Default environment is `offline`; production requires explicit operator config,
validated key identity, the selected protocol version and per-payload approval.
Remote errors are namespaced (`authority_code`) separately from local error codes.
Timeouts/retry intervals are configurable budgets, not a universal MCP 60-second
assumption. Inquiry results retain raw status plus adapter version and observation
time, and may not regress a terminal state without a documented correction event.

## 9. Regulatory data, sales caps and tax advisory

`policies/readiness.json` and `policies/regulatory-rules.json` are the starting
registry. **No Iranian fiscal rule is activated by this architecture review.**
They record what must be verified rather than laundering the previous report's
FACT labels into executable rules. Do not copy rates, deadlines, thresholds,
exemptions, crime classifications or tax-ID constants from chat answers.

A rule needs an ID, semantic version, effective interval, jurisdiction, taxpayer
scope, source URL/document/hash/paragraph, supersession link, review identity/time,
rounding/applicability policy and tests. Snapshot rule IDs/versions on decisions.
Overlaps, gaps, expired reviews or absent evidence result in UNKNOWN/REVIEW_REQUIRED.
An update creates a new immutable version; it cannot rewrite old decisions.

Article 6 has three distinct quantities: an externally observed authority balance,
a clearly labelled local estimate, and local reservations for pending invoices.
Record source, as-of time and freshness. Never count a payment/guarantee as confirmed
capacity solely because a user entered it. Reserve/release once per invoice in a
transaction. Account for external submissions and stale observations. Local cap
policy can require human review, but it must not declare a legal prohibition or
create an automatic corrective invoice from an unverified estimate.

Tax shields track candidate, evidence_complete, reviewed, claimed, accepted,
utilized, expired and clawback_review separately. The system must not automatically
book financing expense from a shareholder deposit, treat R&D spending as approved
credit, treat all bank credits as revenue, declare tax crime, or prohibit asset
revaluation by asset category. These are evidence-dependent matters. Maintain
credit balances and prevent double utilization; distinguish deductions, credits,
zero rate and exemptions.

Inflation outputs are labelled managerial scenarios. They do not change statutory
costs or create tax deductions. Inputs include observed replacement cost, cash
collection timing, tax assumptions and dividends. The prior 100-unit example yields
86.6667 replacement units after the assumed dividend, a 13.3333% quantity decline,
not 20%. Without that dividend its stated cash exceeds replacement cost. The cash
shortfall, holding component and actual tax effect must be reported separately.

## 10. MCP, parsing and deployment

Use the official Python MCP SDK's FastMCP interface initially. Do not mix it with
an independently versioned package also named FastMCP. F0 pins and tests the SDK
and protocol/client matrix. stdio is the first transport; logs go to stderr.
Streamable HTTP is the remote design; legacy HTTP+SSE is compatibility-only and
must not be the default for a new service. Pin a supported protocol revision;
transport behavior differs across revisions.

Results use typed `structuredContent` plus a bounded human-readable text summary
where the selected SDK/protocol supports it. A domain failure is also an MCP tool
error, not text saying ERROR inside an otherwise successful result. Return
schema_version, request_id, status, typed data, warnings and evidence references.
Paginate reports. SUCCESS never means authority acceptance unless the explicitly
named authority state says so.

Parse text-bearing PDFs first; use optional vision only with explicit data-sharing
policy and limits. Uploads are untrusted data, not instructions. Require size/page/
CPU/memory limits, path isolation, content sniffing, malware policy and field-level
provenance. Never fetch arbitrary document URLs or execute embedded content.
Uncertain extracted money, identity, dates or currency remain unresolved drafts;
confidence scores cannot substitute for human approval.

CI has no production keys or fiscal network side effects. Build reproducibly from
a dependency lock, verify packaged resources, test fresh migrations and restores,
run as non-root with bounded resources and persistent volumes. Use a secure SQLite
backup mechanism; copying only a live database file in WAL mode is not a backup
strategy. Rust, distributed workers and multi-tenant SaaS remain future adapters,
not prerequisites for the first working accounting slice.

## 11. Acceptance and change control

`IMPLEMENTATION_PLAN.md` defines required acceptance evidence per phase.
`review/ARCHITECTURE_REVIEW.md` traces the original defects to replacements. The
foundation SQL tests are executable examples, not an application test suite.
Do not declare implementation complete because a design file or simulator passes.

All future changes state the parent commit, architecture version, changed rule or
contract, migration impact and acceptance-test evidence. Changes affecting approved
payloads, fiscal protocol, tax applicability or financial rounding require explicit
review and invalidate affected approvals. The release manifest, not README badges,
controls which tools can be enabled.
