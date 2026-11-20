# HesabYar-AI — Final Implementation Architecture

**Architecture ID:** HYA-A1-FINAL  
**Version:** 1.5.0  
**Date:** 2026-11-16  
**State:** `FROZEN_IMPLEMENTATION`  
**Supersedes:** 1.4.0-FROZEN (2026-09-20), per [ADR 0002](adr/0002-tryton-as-accounting-engine.md)  
**Scope:** R0/R1 implementation foundation for the Tryton-owned accounting ledger (accessed through an engine-neutral port), standards validation, versioned Iranian tax policy, electronic commercial books, electronic invoice/Moadian integration, document intake, advisory analytics, and MCP interface.

> This is the **single normative architecture document for implementation**.
>
> The coding agent MUST implement from this file. It MUST NOT infer implementation rules from `docs/research_tax_inflation.md`, the old architecture text, README prose, examples, or external blog posts.
>
> If any other repository file conflicts with this document, **this document wins**. A future architecture change is valid only when both:
> 1. this file is version-bumped and updated; and
> 2. a new ADR records the reason.
>
> No ADR, research note, commit message, README section, or code comment may silently override this file.

---

# Version history

| Version | Date | Change |
|---|---|---|
| 1.4.0-FROZEN | 2026-09-20 | Initial frozen implementation architecture (HYA-A1-FINAL). R0 goal included a production-grade native HesabYar double-entry ledger with HesabYar-owned voucher/journal tables (§3.1, §8, §19) and a native-ledger Stage F1 (§26). |
| 1.5.0 | 2026-11-16 | Supersedes 1.4.0 per [ADR 0002](adr/0002-tryton-as-accounting-engine.md): Tryton is the default accounting engine and the only active general-ledger authority in the default deployment. HesabYar exposes an engine-neutral `AccountingEnginePort` with a Tryton adapter, creates no duplicate journal-entry tables, and never posts the same financial event into both engines. The 1.4.0 native-ledger R0 goal, ledger bounded context, HesabYar-owned ledger tables, and Stage F1 are superseded as noted in each section; the rest of 1.4.0 stands unchanged. |

The 1.4.0 text is preserved in git history (commit `e0972c0`,
`docs/ARCHITECTURE.md` as committed before this update). No 1.4.0 requirement
was silently deleted: every superseded section below carries an explicit
supersession note pointing at ADR 0002.

---

# 0. Executive decision

HesabYar-AI will be implemented as a **modular monolith with explicit domain boundaries and ports/adapters**.

Tryton is the accounting system of record: it owns accounting transactions and their lifecycle. HesabYar is the Iranian accounting, tax, standards, integration, and AI intelligence layer on top of the engine; it does not maintain a second general ledger (ADR 0002).

The system has four different kinds of truth and they MUST stay separate:

1. **Accounting invariants** — deterministic and transactional.
2. **Accounting standards** — versioned professional rules with source provenance.
3. **Tax/legal policy** — effective-dated, source-backed, changeable rules.
4. **External government protocol behavior** — versioned integration contracts that are untrusted until verified against an official technical specification.
5. **Regulatory filing/export obligations** — versioned schemas plus compliance-calendar events; never inferred from ledger structure alone.

The LLM/MCP layer is an orchestration interface. It is **never** the source of financial truth, legal truth, authorization, tenant identity, or arithmetic.

The system is designed to fail closed on financial, legal, security, and transmission uncertainty.

---

# 1. Authority hierarchy and source policy

## 1.1 Repository authority order

For implementation decisions, use this order:

1. `docs/ARCHITECTURE.md` — this file.
2. Accepted tests that implement this file.
3. Current source code that passes those tests.
4. Versioned source snapshots registered by the legal/standards/protocol registries.
5. README and operational documentation.
6. Research notes and examples.

Items 5–6 are explanatory only. They are not normative.

## 1.2 External source hierarchy

For legal/tax/accounting/protocol facts:

1. Official statute, regulation, circular, technical specification, or Accounting Standards Organization/Audit Organization publication.
2. Official archive/copy with identifiable document number/version/date.
3. Trusted secondary copy used only to locate an official source.
4. Commentary/blog/social media — discovery evidence only; never executable truth.

A source is not `VERIFIED` merely because multiple websites repeat it.

## 1.3 Required source record

Every legal, standards, and Moadian protocol source used by production logic MUST have:

```text
source_id
domain = tax | accounting_standard | moadian_protocol
authority
document_number_or_version
title
published_at
effective_from
effective_to
retrieved_at
canonical_url
snapshot_uri
sha256
verification_status = DRAFT | VERIFIED | RETIRED | DISPUTED
verified_by
verified_at
notes
```

Production logic may only use source versions with `verification_status=VERIFIED`.

Live URLs are not reproducible evidence. A hash-addressed snapshot is required.

---

# 2. Mandatory corrections to v1.1 assumptions

The coding agent MUST NOT implement the following old statements as executable facts.

| Old assumption | Final rule |
|---|---|
| Tax-loss carryforward = “Article 140” | Do not encode this label. Use a versioned tax policy based on the verified legal source applicable to `as_of_date`; current research points to Article 148(12), but the active source record controls. |
| Deferred tax under Iranian Standard 22 | Wrong. Income taxes/deferred tax belong to Standard 35. Standard 22 is interim financial reporting. |
| Article 274 contains eight offences | Do not hard-code a count. Current reviewed text contains seven items; production uses a verified source version. |
| Bank inflows below a numeric threshold are safe from tax review | Prohibited assumption. Thresholds are risk signals, not a safe harbour or proof of taxability/non-taxability. |
| 100 inflows + 35 million toman automatically makes income taxable | Prohibited. It may only be a versioned risk signal if the relevant rule is verified for the requested date. |
| Article 138 bis = principal × assumed 23% automatic tax deduction | Prohibited. Eligibility, participatory contract, actual/allowable return, retention period, payment, and evidence are distinct predicates. |
| “Phantom profit / working-capital meltdown” is a posting rule | Prohibited. It is advisory scenario analysis only. |
| VAT rate is forever 10% | Prohibited. VAT is an effective-dated tax parameter. |
| Article 6 cash prepayment always gives a fixed 10× cap increase | Prohibited as a timeless rule. It must be computed from the active verified legal rule and VAT treatment. |
| Article 6 over-cap invoice must always be rejected and auto-corrected | Prohibited. The engine may advise or block according to a verified active policy, but it never auto-creates a corrective invoice from research prose. |
| Moadian TaxID/JWS/JWE/endpoints from v1.1 are already verified | Prohibited. They are integration hypotheses until pinned to an official protocol profile and contract-tested. |
| Seven invoice patterns are timeless enums | Prohibited. Invoice type/pattern/schema belong to a versioned Moadian protocol profile. |
| SHA-256 audit hashes alone provide legal non-repudiation | Prohibited claim. They provide tamper evidence only unless a separate trust/signature model establishes non-repudiation. |
| “35 standards present” means full standards compliance coverage | Prohibited claim. Corpus availability and executable validation coverage are separate metrics. |
| Bundled Standard 15 is current for 1405 | Prohibited. The bundled source is legacy; current-period use is blocked until the official revised-1404 source is imported and verified. |
| Standard 21 remains the current lease standard in 1405 | Prohibited. Standard 44 applies to periods beginning 1405/01/01 and replaces Standard 21 for those periods. |
| Paper purchase invoices remain automatically VAT-credit-bearing after 1404/10/01 | Prohibited. VAT-credit evidence must follow the active effective-dated electronic-invoice rule and buyer-workspace status. |
| An internal double-entry ledger alone satisfies 1405 electronic commercial-book obligations | Prohibited. Electronic-book filing/export is a separate versioned compliance boundary. |

---

# 3. R0 goals and explicit non-goals

## 3.1 R0 goals

R0 MUST provide:

- a Tryton-owned double-entry ledger, reached only through the engine-neutral `AccountingEnginePort` and its Tryton adapter (ADR 0002); HesabYar provides engine-neutral accounting contracts, deterministic pre-posting checks, workflow orchestration, and audit correlation — not a second ledger;
- multi-tenant isolation;
- fiscal years and posting periods;
- chart of accounts and floating tafsili dimensions;
- draft/review/post/reversal workflow;
- trial balance and core financial-statement validation;
- versioned accounting-standard metadata and provenance;
- versioned Iranian tax-policy registry;
- versioned compliance calendar;
- electronic commercial-books export/compliance boundary;
- invoice drafting and schema validation;
- a local Moadian simulator driven only by verified fixtures;
- an external submission architecture using transactional outbox + reconciliation;
- local MCP over stdio;
- remote MCP over Streamable HTTP;
- authentication/authorization for every remote request;
- complete audit events for mutations;
- explicit human approval before production tax submission;
- deterministic money/date arithmetic;
- tests for all critical invariants.

## 3.2 R0 non-goals

R0 MUST NOT:

- implement microservices;
- maintain a second HesabYar general ledger alongside Tryton in the default deployment;
- post the same financial event into both engines as two authoritative books;
- write directly to Tryton's database (all engine access goes through the `AccountingEnginePort` adapter);
- auto-file or auto-submit tax invoices without human approval;
- make criminal/legal conclusions about a user or transaction;
- auto-classify all bank inflows as revenue;
- implement OCR output as trusted ledger data;
- implement unverified Moadian protocol details;
- provide a generic Python-expression rule engine;
- use LLM arithmetic as a financial control;
- use legacy HTTP+SSE as a new remote MCP transport;
- add a Rust port;
- implement every Moadian invoice pattern before verified schemas exist;
- claim complete tax/legal compliance solely from passing software checks.

Rust, advanced multi-agent workflows, and automated tax planning are post-R1 concerns.

---

# 4. Fixed technology baseline

The coding agent MUST use this baseline unless this file is changed.

## 4.1 Language and packaging

- Python: **3.12**
- Packaging/environment: **uv**
- Project metadata: `pyproject.toml`
- Exact dependency resolution: committed `uv.lock`
- Source layout: `src/hesabyar/`
- Type checking: `pyright`
- Lint/format: `ruff`
- Tests: `pytest` + `hypothesis`

No production dependency may remain unpinned in the lockfile.

## 4.2 MCP

Use the **official MCP Python SDK v2** and its `MCPServer` API.

Protocol target: **MCP 2026-07-28**, while allowing the official SDK's normal compatibility negotiation for older clients.

Transports:

- local: `stdio`
- remote: **Streamable HTTP**
- legacy HTTP+SSE: **not implemented in R0**

The old v1.1 `--transport sse` design is superseded.

## 4.3 Persistence

Canonical R0 persistence:

- **PostgreSQL 16+**
- SQLAlchemy 2.x
- Alembic migrations
- psycopg 3
- PostgreSQL integration tests in CI

SQLite from the old architecture is **not the production datastore** and its old DDL MUST NOT be copied. A future local SQLite adapter requires a separate ADR after R0.

Each installation has two separate logical databases (design-eng spec §7.1): a **Tryton database**, owned and migrated by Tryton and its installed modules, and a **HesabYar database**, owned and migrated by HesabYar/Alembic. They may run on the same PostgreSQL server for a small installation, but must use separate database names, roles, passwords, and backup/restore procedures. HesabYar tables may store Tryton record identifiers as external references, but MUST NOT create SQL foreign keys into Tryton's database. Cross-database relationships are enforced through adapter contracts and reconciliation.

## 4.4 Core libraries

- Pydantic v2 for boundary schemas
- `decimal.Decimal` for rates/quantities/FX calculations
- Python `int` for IRR money in domain objects
- `httpx` for allowed external HTTP adapters
- `cryptography` only where required by a verified Moadian protocol profile

No pandas/numpy dependency is required for the core accounting layer.

---

# 5. Physical module layout and dependency rules

Required target layout:

```text
src/hesabyar/
  domain/
    common/
      money.py
      dates.py
      ids.py
      errors.py
      result.py
    accounting/
      refs.py
      value_objects.py
      commands.py
      queries.py
      ports.py
      policies.py
    standards/
      models.py
      rules.py
      ports.py
    tax_policy/
      models.py
      decisions.py
      handlers.py
      ports.py
    electronic_books/
      models.py
      export_profile.py
      ports.py
    invoices/
      entities.py
      state_machine.py
      ports.py
    documents/
      models.py
      ports.py

  application/
    commands/
    queries/
    services/
    authorization.py
    transaction.py

  infrastructure/
    persistence/
      postgres/
        models.py
        repositories.py
        uow.py
    accounting/
      tryton/
        client.py
        adapter.py
        mapping.py
        errors.py
        contracts.py
    standards/
      registry.py
    tax_policy/
      source_registry.py
      rule_repository.py
    electronic_books/
      profile_registry.py
      exporter.py
      calendar.py
    moadian/
      protocol_profiles.py
      simulator.py
      gateway.py
      key_provider.py
    documents/
      parsers.py
      storage.py
    auth/
      claims.py

  interfaces/
    mcp/
      server.py
      tools_read.py
      tools_write.py
      tools_tax.py
    cli/
      main.py

  worker/
    outbox_worker.py
    reconciliation_worker.py

tests/
  unit/
  property/
  integration/
  contract/
  e2e/

alembic/
docs/
```

## 5.1 Dependency direction

Allowed:

```text
interfaces -> application -> domain
worker     -> application -> domain
infrastructure -> domain ports
application -> domain ports
```

Forbidden:

- domain importing MCP;
- domain importing SQLAlchemy;
- domain importing Tryton clients;
- domain importing HTTP clients;
- domain importing OCR/LLM SDKs;
- domain reading environment variables;
- repositories calling MCP;
- an MCP tool directly calling SQLAlchemy models;
- tax-policy code posting ledger entries;
- document parser directly posting ledger entries;
- any code writing directly to Tryton's database or bypassing the `AccountingEnginePort` to reach engine data.

All infrastructure is reached through ports.

---

# 6. Core value objects and arithmetic

## 6.1 Money

`MoneyIRR`:

- underlying value: Python `int`;
- unit: IRR only;
- no implicit toman conversion;
- no float constructor;
- explicit add/subtract/compare;
- multiplication by rates goes through Decimal and an explicit rounding policy.

Database money columns: `BIGINT`.

User-facing toman conversion belongs to presentation only.

## 6.2 Decimal quantities and rates

Use `Decimal` for:

- quantity;
- VAT rate;
- discounts expressed as rate;
- FX rate;
- percentages;
- tax rates.

Database type: `NUMERIC` with an explicitly chosen precision/scale.

Binary float is forbidden in financial domain models and database writes.

## 6.3 Rounding

Every policy calculation MUST name a rounding rule.

No implicit `round()` in legal/financial computations.

A policy result includes:

```text
rounding_mode
rounding_scale
rounding_source_rule_version
```

## 6.4 Dates

Persist:

- event timestamps: `TIMESTAMPTZ`, UTC;
- legal/accounting dates: PostgreSQL `DATE` as canonical Gregorian date;
- Jalali representation: computed/presented or stored only as additional display/source data when legally necessary.

Never use a Jalali text field as the only canonical date.

---

# 7. Tenant and actor model

## 7.1 Tenant isolation

Every business object belongs to a tenant.

`tenant_id` MUST exist on:

- fiscal years;
- posting periods;
- accounts;
- tafsili entities;
- account-tafsili links;
- vouchers and lines;
- approvals;
- invoice drafts/lines;
- submission/outbox records;
- documents/extractions;
- policy decisions;
- audit events.

Global legal/source registries may have nullable tenant scope only when the record is truly shared.

Ledger objects in the list above (fiscal years, posting periods, accounts, tafsili entities, account-tafsili links, vouchers and lines) are Tryton-owned records, scoped by Tryton company; HesabYar maps each tenant to its engine company through the `engine_companies` table (see §8, §19) and never duplicates these records as HesabYar-owned tables in the default deployment (ADR 0002).

## 7.2 Tenant identity is not a tool argument

For remote MCP, `tenant_id` and `actor_id` are derived from validated authentication claims.

The model/user MUST NOT be able to switch tenant by passing `tenant_id` to a tool.

For stdio, tenant/actor are loaded from a trusted local profile/configuration, not LLM text.

## 7.3 Roles/scopes

Minimum scopes:

```text
hesabyar.read
hesabyar.ledger.draft
hesabyar.ledger.post
hesabyar.tax.draft
hesabyar.tax.submit
hesabyar.admin
```

The application layer checks authorization. UI/MCP wrappers are not the security boundary.

---

# 8. Accounting bounded context (Tryton-owned ledger)

> **Supersedes 1.4.0 §8.** In 1.4.0 this section specified a native HesabYar ledger with HesabYar-owned voucher/journal tables. Per ADR 0002 (2026-11-16), the ledger is owned by Tryton; HesabYar's accounting context is engine-neutral contracts, commands, queries, and value objects plus the `AccountingEnginePort`. The posting invariants in §8.3 are unchanged in substance: HesabYar validates every draft against them before delegating to the engine, and the engine enforces them transactionally.

The ledger is deterministic and transactionally authoritative. Tryton is its system of record; HesabYar is never a second book.

## 8.1 Engine-neutral references (no HesabYar-owned ledger tables)

HesabYar does NOT own ledger tables in the default deployment. It works with engine-neutral references and views returned by the `AccountingEnginePort`:

- `CompanyRef` — the Tryton company holding the tenant's books;
- `AccountView` — a chart-of-accounts entry (group / kol / moein, tafsili links);
- `JournalDraftRef` — a draft journal created through the port;
- `PostedJournalRef` — a posted journal, immutable in the engine;
- `AccountingReportQuery` / `AccountingReportResult` — trial balance and statements read through the port.

Fiscal years, posting periods, accounts, tafsili links, vouchers, approvals, and number sequences live in the Tryton database. HesabYar stores only external references to them (see §19 engine-mapping tables).

## 8.2 Account hierarchy

The chart of accounts is Tryton-owned. HesabYar provides Iranian chart templates, mapping, and validation. The engine chart MUST support group / kol / moein and floating tafsili association.

R0 MUST ensure the engine provides a real many-to-many account–tafsili association (in Tryton, via its account model or a HesabYar Tryton module); HesabYar MUST NOT recreate it as a HesabYar-owned table in the default deployment.

A tafsili may be linked to multiple eligible moein accounts.

## 8.3 Voucher states

The voucher/journal lifecycle below is the contract HesabYar enforces on drafts and the engine enforces on posting. `POSTED` is authoritative only in the engine; HesabYar never holds a competing posted state.

```text
DRAFT -> REVIEW_REQUIRED -> APPROVED -> POSTED
  |           |               |
  +-> VOID    +-> REJECTED    +-> approval expires if draft hash changes

POSTED -> REVERSED
```

Rules:

- only DRAFT may be edited;
- any material edit invalidates prior approval;
- POSTED is immutable;
- correction = reversal voucher + replacement voucher;
- deletion of POSTED records is forbidden;
- a reversal references the original voucher;
- a voucher has at least two lines;
- every line is one-sided: debit XOR credit;
- all amounts are non-negative;
- posted sum(debit) == sum(credit);
- total must be > 0;
- every account is active and belongs to the same tenant;
- required tafsili must be supplied and linked to the account;
- posting date must be inside an open period.

## 8.4 Database enforcement

Do not rely only on Pydantic.

Transactional enforcement of the ledger invariants lives in the Tryton database, not in HesabYar tables: foreign keys, company-aware uniqueness, one-sided line checks, immutability of posted moves, and transaction-time balance validation are the engine's responsibility, verified through the adapter contract tests (§24, Gate 3). HesabYar MUST NOT duplicate these tables or constraints in its own database to re-enforce them. HesabYar's own database enforces only its own records (idempotency keys, audit events, engine-mapping references) and validates every draft against §8.3 before submission.

## 8.5 Sequence allocation

Voucher/journal numbering is issued by the engine. HesabYar MUST NOT allocate competing voucher numbers with `MAX(voucher_number)+1` or its own sequence table. HesabYar correlates its drafts to engine-issued numbers through the `external_id_map` (see §19) and its own idempotency records (§16).

---

# 9. Accounting standards bounded context

The existing standards corpus is a knowledge asset, not proof of current compliance.

## 9.1 Standard version registry

Each standard/revision MUST carry:

```text
standard_number
title
revision_id
published_at
effective_from
effective_to
source_id
status
supersedes_revision_id
```

Example: Standard 22 and Standard 35 must never be inferred only from file names; their registered revision controls applicability.

Standard 44/leases is effective-dated and must be treated by revision, not as a timeless fact.

### 9.1.1 Current 1405 source gates

The implementation MUST enforce these repository gates before standards validation:

- Standard 43: applicable for periods beginning 1404/01/01 and later; supersedes Standards 3, 9 and 29 for those periods.
- Standard 44: applicable for periods beginning 1405/01/01 and later; supersedes Standard 21 for those periods.
- Standard 15: bundled repository source is legacy. For periods beginning 1405/01/01 and later, return `STANDARD_SOURCE_BLOCKED` until the official revised-1404 text is imported, snapshotted, hashed and VERIFIED.
- Standard 35 handles income-tax/deferred-tax accounting; Standard 22 is interim reporting.

These gates are also reflected in `metadata.json` and `docs/CURRENT_RULES_1405.md`.

## 9.2 Validator replacement

The existing validator has two prohibited behaviors:

1. Python `eval()` on rule expressions.
2. returning PASS when a formula cannot be evaluated.

Both MUST be removed during Phase 0.

## 9.3 Rule DSL

Do not store arbitrary executable expressions.

Use a closed, typed rule schema. Example:

```yaml
id: BS001
type: equation
left:
  field: total_assets
right:
  add:
    - field: total_liabilities
    - field: total_equity
tolerance:
  absolute_irr: 0
severity: error
standard_refs:
  - standard: 1
    revision: "..."
    paragraph: "..."
```

Allowed operations are explicitly implemented by code:

- field;
- constant;
- add;
- subtract;
- multiply;
- divide with zero guard;
- sum_items;
- compare;
- all/any;
- conditional applicability.

No `eval`, `exec`, Jinja expression execution, or dynamic Python import.

## 9.4 Validation result states

A rule result is one of:

- `PASS`
- `FAIL`
- `NOT_APPLICABLE`
- `ERROR`

`NOT_APPLICABLE` is not counted as pass.

For a mandatory rule, `ERROR` makes the overall report invalid.

An unknown field or unevaluable formula must never become PASS.

## 9.5 Coverage

Report separately:

- standards available in corpus;
- standards with machine-readable rules;
- rules executed;
- rules passed;
- rules failed;
- rules not applicable;
- rule errors.

Do not advertise “35 standards validated” unless executable coverage actually supports that statement.

---

# 10. Tax/legal policy bounded context

Tax policy is versioned decision support, not hard-coded application logic.

## 10.1 Required public interface

```python
evaluate(
    policy_id: PolicyId,
    facts: PolicyFacts,
    as_of_date: date,
) -> PolicyDecision
```

`as_of_date` is required.

## 10.2 PolicyDecision

Must contain:

```text
decision_id
policy_id
as_of_date
outcome = ALLOW | BLOCK | ELIGIBLE | INELIGIBLE | NEEDS_EVIDENCE | RULE_UNVERIFIED | REVIEW_REQUIRED
calculation
assumptions
missing_evidence
warnings
source_versions
logic_version
input_hash
created_at
requires_professional_review
```

## 10.3 Rule storage vs logic code

Legal text/parameters are data; executable logic is typed code.

Do not store Python expressions in the database.

Each policy handler declares the legal rule versions it supports.

A legal change that changes logic requires:

1. new source snapshot;
2. new legal rule version;
3. updated typed handler or parameters;
4. golden tests;
5. reviewer activation.

## 10.4 High-risk policy outputs

The following outputs MUST be advisory/reviewable and never direct legal conclusions:

- bank-transaction classification;
- suspected tax evasion;
- Article 274 risk;
- tax-shield eligibility when evidence is incomplete;
- Article 100 eligibility;
- Article 6 cap when authoritative current data is missing.

The application says “potential compliance risk” or “needs evidence”, not “crime committed” or “tax definitely owed” unless a deterministic statutory calculation is fully supported by verified facts.

## 10.5 Article 6 design

Article 6 is a policy module, not a hard-coded formula in invoice code.

Inputs include, as applicable:

- period;
- taxpayer status;
- verified prior-period values;
- active legal parameters;
- payments/guarantees/purchases only when verified by allowed evidence;
- current remote/cached cap snapshot and its timestamp.

Output states:

- `ALLOW`
- `BLOCK`
- `UNKNOWN_STALE_DATA`
- `NEEDS_EVIDENCE`
- `REVIEW_REQUIRED`

If cap data is stale or unknown, production submission fails closed according to the active policy.

The engine MUST NOT auto-create corrective invoices after a cap change.

## 10.6 Current 1405 policy families

R0/R1 policy registry MUST be able to represent, without hard-coding:

- annual VAT parameters and exemptions/special rates;
- Article 84 and Article 101 annual thresholds;
- Article 100 performance-year vs filing-year thresholds/deadlines;
- Article 6 sales-cap rules and evidence freshness;
- VAT input-credit eligibility tied to electronic-invoice/workspace evidence for applicable periods;
- tax-loss carryforward under the verified applicable Direct Tax Law source;
- electronic commercial-books filing deadlines;
- the 1404 anti-speculation/capital-gains law as a **TRANSITIONAL** policy family.

The anti-speculation/capital-gains family MUST remain inactive until the applicable operational/effective conditions, executive rules and system-readiness requirements are verified. Enactment alone is not sufficient to activate calculations.

---

# 11. Electronic commercial books compliance

Electronic commercial books are a separate regulatory-output boundary over the authoritative (Tryton-owned) ledger.

## 11.1 Required model

Each export profile contains:

```text
profile_id
authority
source_id
schema_version
effective_from
effective_to
status = DRAFT | VERIFIED | ACTIVE | RETIRED
field_mapping_version
validation_version
```

Each compliance-calendar event contains:

```text
event_id
obligation_type
affected_period
taxpayer_population
deadline
source_id
status
supersedes_event_id
```

Deadlines are data. Do not hard-code calendar dates in application conditionals.

## 11.2 Export contract

An electronic-books export is immutable and reproducible from:

- tenant;
- fiscal period/cutoff;
- engine ledger revision/hash (obtained through the adapter);
- export profile version;
- source version;
- generated timestamp.

The export boundary MUST NOT mutate ledger postings.

If the required active schema/profile for a filing period is missing or unverified, return `BOOKS_PROFILE_UNVERIFIED`.

## 11.3 1405 current requirement

Current 1405 notices demonstrate that electronic commercial-books upload is operational and deadline-driven. Therefore this is an R0 compliance requirement, not a future roadmap placeholder.

The project MUST implement the boundary and calendar before claiming 1405 filing readiness.

---

# 12. Invoice domain and Moadian protocol profiles

## 11.1 Separate business invoice from protocol payload

The canonical business object is `InvoiceDraft`.

The external wire payload is generated by a `MoadianProtocolProfile`.

Do not make the business domain mirror one specific government JSON version.

## 11.2 Protocol profile registry

Each Moadian profile includes:

```text
profile_id
official_document_version
source_id
effective_from
effective_to
schema_version
status = DRAFT | VERIFIED | ACTIVE | RETIRED
endpoint_profile
crypto_profile
tax_id_profile
invoice_schema_refs
error_catalog_version
activated_by
activated_at
```

Production gateway accepts only `ACTIVE` profiles.

## 11.3 Current discovery note

Publicly discoverable material in September 2026 points to an electronic-invoice instruction identified as **version 7.9, Tir 1405**, with changed fields/rules including “invoice sending rule”, “note 1”, “note 2”, and revisions around reference/corrective/return invoices.

This is a **discovery signal**, not sufficient production authority by itself.

Before Moadian production implementation, obtain the official technical document, store its snapshot/hash, mark it VERIFIED, and write contract fixtures from that exact version.

The coding agent MUST NOT implement v1.1 V6/V7 assumptions merely because they are already described in the repository.

## 11.4 Invoice schema

Do not represent invoice patterns as a timeless Python enum `1..7`.

Store/validate:

```text
protocol_profile_id
invoice_type_code
pattern_code
lifecycle_code
schema_payload
```

Typed convenience models may exist for common patterns, but the registry is authoritative.

Unsupported profile/type/pattern combination => `PROTOCOL_SCHEMA_UNSUPPORTED` before transmission.

## 11.5 TaxID and cryptography

`TaxId` is an opaque domain value.

Generation/checksum format is implemented inside the active protocol adapter only after official verification.

The architecture does **not** prescribe Verhoeff, epoch base, memory-ID layout, JWS algorithm, JWE algorithm, or endpoint path until the active profile specifies them.

Do not copy v1.1 crypto prose into code without an official profile fixture.

---

# 13. External submission workflow

External tax submission is an irreversible side effect and MUST use a transactional outbox.

## 12.1 Invoice state machine

```text
DRAFT
  -> VALIDATION_FAILED
  -> VALIDATED
  -> APPROVAL_REQUIRED
  -> APPROVED
  -> QUEUED
  -> SUBMITTING
  -> SUBMITTED
  -> ACCEPTED
  -> REJECTED
  -> PENDING_RECONCILIATION

APPROVED -> DRAFT only by creating a new draft version; approval is invalidated.
```

No arbitrary state assignment is allowed.

## 12.2 Human approval

R0 production submission requires a human/operator approval record for the **exact canonical payload hash**.

The model-facing MCP server does **not** expose a tool that can grant its own tax-submission approval.

Approval is created through a trusted operator CLI/admin surface authenticated with `hesabyar.tax.submit` or a stronger approval role.

If draft content changes after approval, approval is invalid.

## 12.3 Transactional outbox

`submit_approved_invoice` does not perform the remote POST inline.

It:

1. validates tenant/actor/scope;
2. verifies active protocol profile;
3. verifies exact approved draft hash;
4. writes an outbox message in the same DB transaction;
5. returns `QUEUED` + `submission_job_id`.

A worker reads the outbox and performs transmission.

This prevents the “remote succeeded but local transaction crashed” ambiguity from being handled by blind resubmission.

## 12.4 Delivery semantics

Do not claim exactly-once network delivery.

Design for:

- at-least-once worker execution;
- business-level idempotency;
- deterministic payload hash;
- stable submission identity;
- reconciliation before retry after ambiguous remote outcomes.

An ambiguous timeout after sending becomes `PENDING_RECONCILIATION`, not immediate blind retry.

## 12.5 Reconciliation

The worker records immutable status events.

Reconciliation compares local state with the remote status API according to the active protocol profile.

Manual intervention is available for unresolved ambiguity.

---

# 14. Document intake

Documents are untrusted input.

## 13.1 Pipeline

```text
RAW_FILE
  -> STORED
  -> EXTRACTED_CANDIDATE
  -> FIELD_VALIDATED
  -> REVIEW_REQUIRED
  -> APPROVED_DRAFT
  -> optional ledger/invoice application command
```

## 13.2 Required provenance

Store:

- original file SHA-256;
- MIME type detected from content, not filename only;
- size;
- parser name/version;
- extraction timestamp;
- field-level source/provenance;
- extraction confidence;
- reviewer/approval;
- resulting draft IDs.

OCR/parser output is never automatically a posted voucher.

## 13.3 Prompt-injection boundary

Text extracted from invoices/PDFs is data.

It MUST NOT be concatenated into system/developer instructions or treated as agent instructions.

Parser text cannot request tool execution, change tenant, change policy, or authorize a mutation.

## 13.4 File security

Enforce:

- size limits;
- MIME allowlist;
- decompression/page limits;
- parser timeouts;
- isolated temporary storage;
- no shell execution from document content;
- no arbitrary URL fetch initiated by embedded document links.

---

# 15. MCP/API boundary

## 14.1 Transport

Local:

```text
stdio
```

Remote:

```text
Streamable HTTP /mcp
TLS at reverse proxy
MCP protocol target 2026-07-28
```

Do not build new R0 functionality on legacy SSE.

## 14.2 Authentication

Every remote MCP request is authenticated.

Preferred architecture:

- external OIDC/OAuth authorization server or trusted gateway;
- Bearer access token validation;
- issuer/audience/expiry/scope validation;
- tenant and actor derived from claims;
- no self-issued ad-hoc auth protocol.

Follow the current MCP authorization specification supported by the official SDK.

## 14.3 Host and origin security

For remote Streamable HTTP:

- explicit host allowlist;
- origin validation where applicable;
- DNS-rebinding protections;
- request body limits;
- rate limiting;
- reverse-proxy TLS;
- no binding to public `0.0.0.0` without auth and configured allowed hosts.

## 14.4 Tool classes

### Read-only tools

- `system_capabilities`
- `trial_balance`
- `validate_financial_statements`
- `evaluate_tax_policy`
- `validate_invoice_draft`
- `get_submission_status`
- `get_audit_event`

### Local mutation tools

- `create_voucher_draft`
- `update_voucher_draft`
- `request_voucher_review`
- `post_approved_voucher`
- `create_invoice_draft`
- `update_invoice_draft`
- `request_invoice_review`

### External side-effect tool

- `submit_approved_invoice`

There is no model-facing `approve_invoice_submission` tool in R0.

## 14.5 Tool arguments

Never accept from model input:

- tenant identity;
- actor identity;
- actor role;
- arbitrary database path;
- private-key path;
- arbitrary Moadian endpoint URL;
- arbitrary executable policy code.

These come from trusted configuration/context.

## 14.6 Response envelope

Canonical structured response:

```json
{
  "status": "SUCCESS|WARNING|ERROR|PENDING",
  "data": {},
  "messages": [],
  "provenance": [],
  "request_id": "uuid",
  "error": {
    "code": "optional",
    "retryable": false,
    "details": {}
  }
}
```

Rendered Markdown is presentation output and must not be the canonical machine contract.

---

# 16. Application command contract

Every mutating application command carries:

```text
request_id
actor_context (trusted)
payload
expected_version (where optimistic concurrency applies)
```

The service computes canonical `payload_hash`.

## 15.1 Idempotency

For every mutation, store a unique idempotency record keyed by:

```text
tenant_id + command_name + request_id
```

Reusing the same request ID with the same payload returns the original result.

Reusing it with a different payload => `IDEMPOTENCY_CONFLICT`.

## 15.2 Optimistic concurrency

Mutable drafts have `version`.

Update commands require `expected_version`.

Mismatch => `CONCURRENCY_CONFLICT`.

Posted/accounting immutable records do not use update-in-place semantics.

---

# 17. Security and secrets

## 16.1 Private keys

Private keys/tokens MUST NOT be stored in business tables, audit events, logs, exceptions, or MCP responses.

Use a `KeyProvider`/signing port.

The Moadian adapter asks the signer to sign; domain/application code does not manipulate raw private-key bytes.

## 16.2 Endpoint allowlist

Production external network adapters may connect only to configured allowlisted hosts.

A tool call cannot override the endpoint.

## 16.3 PII

National IDs, economic codes, bank identifiers, addresses and phone numbers are sensitive business/identity data.

Rules:

- store only when required;
- scope by tenant;
- mask in logs;
- return only fields required for the operation;
- do not place full PII in exception messages;
- production backups must be encrypted by the deployment platform.

## 16.4 Default-deny feature flags

Defaults:

```text
HESABYAR_PROD_MOADIAN_ENABLED=false
HESABYAR_AUTO_POST_ENABLED=false
HESABYAR_ALLOW_UNVERIFIED_RULES=false
HESABYAR_LEGACY_SSE_ENABLED=false
```

Missing configuration means disabled, never enabled.

R0 does not implement auto-post even if the environment variable exists; the flag is reserved.

---

# 18. Audit architecture

## 17.1 Audit events

For every mutation record:

- event_id;
- tenant_id;
- actor_id;
- actor_type;
- command;
- object_type/id;
- request_id;
- input hash;
- before hash where relevant;
- after hash where relevant;
- active source/rule/protocol versions;
- timestamp;
- outcome;
- previous audit hash;
- event hash.

Use canonical serialization for hash calculation.

## 17.2 Tamper evidence

Audit records are append-only for the application DB role.

A hash chain detects modification/reordering under normal controls.

This is **tamper-evident audit logging**, not a claim of legal non-repudiation.

A later external signed checkpoint/WORM store may strengthen this model.

## 17.3 Remote payload retention

Do not dump secrets or unnecessary PII into `response_raw_json` as v1.1 proposed.

Store:

- normalized status/error fields;
- response hash;
- encrypted raw response blob only if operational/legal need justifies it;
- retention policy.

---

# 19. Database model — minimum required tables

The old nine-table SQLite DDL is superseded.

Minimum PostgreSQL tables:

```text
tenants
actors

engine_connections
engine_companies
external_id_map

standard_sources
standard_revisions
validation_rules
validation_rule_versions

legal_sources
legal_rules
legal_rule_versions
policy_decisions

electronic_book_profiles
compliance_calendar_events
electronic_book_exports

moadian_protocol_profiles
invoice_drafts
invoice_lines
invoice_approvals
integration_outbox
moadian_transmissions
moadian_status_events

documents
document_extractions

idempotency_keys
audit_events
```

> **Supersedes 1.4.0 §19 in part.** The 1.4.0 list included `fiscal_years`, `posting_periods`, `accounts`, `tafsilis`, `account_tafsili_links`, `number_sequences`, `vouchers`, `voucher_lines`, and `voucher_approvals` as HesabYar-owned tables. Per ADR 0002 those records live in the Tryton database; HesabYar holds only engine-mapping references to them (`engine_connections`, `engine_companies`, `external_id_map`). A native-ledger adapter would reintroduce HesabYar-owned journal tables only in a mutually exclusive standalone mode, under a separate ADR.

## 18.1 Important uniqueness

At minimum:

```text
engine_companies: UNIQUE(tenant_id, engine_company_ref)
external_id_map: UNIQUE(tenant_id, entity_type, engine_record_ref)
invoice_drafts: UNIQUE(tenant_id, internal_id, version)
idempotency_keys: UNIQUE(tenant_id, command_name, request_id)
integration_outbox: UNIQUE(tenant_id, idempotency_key)
```

Uniqueness of ledger records (accounts, vouchers, voucher lines) is enforced by the Tryton engine in its own database, not by HesabYar (ADR 0002).

Any external tax identifier uniqueness is determined by the verified active protocol profile and enforced as appropriate.

## 18.2 Foreign-key tenant safety

Repository methods always scope by tenant.

Where practical, use composite foreign keys/constraints so a child cannot reference a parent from another tenant.

Tests MUST attempt cross-tenant ID substitution and prove failure.

---

# 20. Error taxonomy

Use stable application error codes.

Ledger codes below are raised by HesabYar's pre-posting validation or mapped from engine errors by the `TrytonAccountingAdapter`; the engine remains the authority for the underlying state (ADR 0002).

## 19.1 Ledger

- `LEDGER_UNBALANCED`
- `LEDGER_PERIOD_CLOSED`
- `LEDGER_ACCOUNT_NOT_FOUND`
- `LEDGER_TAFSILI_REQUIRED`
- `LEDGER_TAFSILI_NOT_ALLOWED`
- `LEDGER_POSTED_IMMUTABLE`
- `LEDGER_APPROVAL_REQUIRED`

## 19.2 Validation

- `RULE_EVALUATION_ERROR`
- `RULE_SOURCE_UNVERIFIED`
- `RULE_INPUT_MISSING`
- `STANDARD_REVISION_UNKNOWN`
- `STANDARD_SOURCE_BLOCKED`

## 19.3 Tax policy

- `POLICY_RULE_UNVERIFIED`
- `POLICY_NEEDS_EVIDENCE`
- `POLICY_DATE_NOT_COVERED`
- `POLICY_REVIEW_REQUIRED`

## 20.4 Electronic books

- `BOOKS_PROFILE_UNVERIFIED`
- `BOOKS_PERIOD_NOT_COVERED`
- `BOOKS_EXPORT_VALIDATION_FAILED`
- `COMPLIANCE_DEADLINE_UNKNOWN`

## 20.5 Moadian

- `PROTOCOL_PROFILE_NOT_ACTIVE`
- `PROTOCOL_SCHEMA_UNSUPPORTED`
- `INVOICE_VALIDATION_FAILED`
- `INVOICE_APPROVAL_REQUIRED`
- `INVOICE_APPROVAL_STALE`
- `SUBMISSION_DISABLED`
- `SUBMISSION_AMBIGUOUS`
- `PENDING_RECONCILIATION`
- `REMOTE_REJECTED`

Do not invent government “official error codes” in the simulator. If a fixture contains an official code, store the source profile that proves it.

---

# 21. Moadian simulator contract

The simulator is not a fantasy replica.

It may implement only behavior supported by verified protocol fixtures.

For unsupported behavior it returns:

`SIMULATOR_BEHAVIOR_NOT_IMPLEMENTED`

—not a fabricated tax-authority response.

Simulator goals:

- deterministic request validation;
- protocol schema checks;
- known-answer crypto tests if the official protocol requires crypto;
- state-machine behavior;
- acceptance/rejection fixtures;
- idempotency/reconciliation scenarios.

CI never calls production tax endpoints.

---

# 22. Deployment architecture

One codebase, three production processes:

```text
+-----------------------+
| hesabyar-mcp          |
| stdio or HTTP server  |
+-----------+-----------+
            |
            v
       PostgreSQL
      (HesabYar DB)
            ^
            |
+-----------+-----------+
| hesabyar-worker       |
| outbox + reconcile    |
+-----------+-----------+
            |
            v
   Moadian gateway
   (only if enabled)

hesabyar-mcp / hesabyar-worker
            |
            |  AccountingEnginePort
            |  (TrytonAccountingAdapter)
            v
+-----------------------+
| tryton-server         |
+-----------+-----------+
            |
            v
       PostgreSQL
      (Tryton DB)
```

This is still a modular monolith, not microservices. The Tryton server runs in its own runtime; do not force it to share HesabYar's Python environment (design-eng spec §6.1). HesabYar reaches Tryton only through the adapter; it never writes to the Tryton database directly (ADR 0002).

## 21.1 Environments

### dev
- local PostgreSQL;
- simulator;
- external submission disabled.

### test/CI
- ephemeral PostgreSQL;
- simulator/fixtures;
- network blocked except dependency setup as needed;
- secrets are fake test material.

### staging
- production-like auth/DB;
- production Moadian disabled until a verified test environment/profile exists;
- manual protocol verification allowed.

### production
Requires all:

- database migrations current;
- active verified protocol profile;
- external endpoint allowlist;
- valid secret/key provider;
- remote auth configured;
- TLS;
- backups;
- `HESABYAR_PROD_MOADIAN_ENABLED=true`;
- explicit human invoice approval.

---

# 23. Observability

Structured logs contain:

- timestamp;
- level;
- request_id;
- tenant pseudonymous identifier;
- actor id where appropriate;
- command/tool;
- duration;
- outcome/error code.

Never log:

- private keys;
- access tokens;
- full national IDs by default;
- full invoice payloads by default.

Metrics:

- command latency/error rate;
- ledger posting failures;
- outbox queue depth/age;
- reconciliation age;
- remote submission outcomes;
- rule-evaluation errors.

Tracing may propagate standard trace context, but observability must not become a source of sensitive-data leakage.

---

# 24. CI quality gates

A PR cannot merge if any required gate fails.

## Gate 0 — repository hygiene

- `uv sync --frozen`
- no secrets detected;
- migrations have a single head;
- architecture-linked checks pass.

## Gate 1 — lint/type

- ruff
- pyright

No new untyped critical-domain code.

## Gate 2 — unit

- domain value objects;
- policy handlers;
- state machines;
- rule DSL.

## Gate 3 — property/invariant

Ledger invariants below are verified through the `AccountingEnginePort` contract tests (test double in unit/property suites; real Tryton instance in integration). Transactional enforcement of these invariants is the engine's responsibility; HesabYar validates drafts before submission and verifies postings after (ADR 0002).

Hypothesis tests at minimum:

- posted vouchers always balance;
- reversal nets original;
- posted records cannot be mutated;
- closed periods cannot post;
- request idempotency;
- tenant isolation;
- Decimal/money calculations never use float paths.

## Gate 4 — PostgreSQL integration

- migrations up/down on disposable DB;
- constraints/triggers;
- concurrent sequence allocation;
- outbox atomicity;
- cross-tenant FK attacks.

## Gate 5 — standards

- no `eval`/`exec` rule evaluation;
- unknown mandatory input => ERROR/FAIL;
- N/A is not PASS;
- source version returned.

## Gate 6 — policy

Golden tests by `as_of_date`.

Every golden case identifies its source version.

## Gate 7 — Moadian contract

- verified fixture tests;
- schema/profile tests;
- ambiguous timeout/reconciliation tests;
- no production network.

## Gate 8 — security

- auth required on remote endpoint;
- scope enforcement;
- host/origin configuration;
- secret redaction;
- PII log masking;
- document parser malicious-input tests.

## Gate 9 — E2E

Required happy path:

```text
document
 -> extraction candidate
 -> reviewed voucher draft
 -> approved & posted voucher
 -> invoice draft
 -> policy/schema validation
 -> human approval
 -> outbox
 -> simulator submission
 -> reconciliation
```

---

# 25. Migration from the current repository

The coding agent MUST preserve the useful standards corpus while replacing the prototype runtime architecture.

## Phase 0A — package foundation

1. Add `pyproject.toml`, `uv.lock`, `src/hesabyar/`.
2. Keep existing `standards/`, `core/*.md`, mappings and examples as source material.
3. Mark old `scripts/validator.py` as legacy until replaced.
4. Do not delete historical standards source files.

## Phase 0B — validator safety

1. Create the typed rule DSL.
2. Port existing rules.
3. Remove `eval()`.
4. Change “cannot evaluate => pass” to `ERROR`.
5. Add standard/source revision metadata.
6. Keep legacy tests but add regression tests proving old unsafe behavior is gone.

## Phase 0C — database

1. Add PostgreSQL models.
2. Add Alembic initial migration.
3. Do not translate old SQLite DDL line-for-line.
4. Add tenant scope and constraints before business features.

## Phase 0D — Tryton engine decision (1.5.0)

1. Accept ADR 0002 (this update).
2. Refactor the ledger package into engine-neutral accounting contracts, commands, queries, and value objects (`domain/accounting/`).
3. Put Tryton-specific data mapping under `infrastructure/accounting/tryton/`.
4. Do not create `vouchers`, `voucher_lines`, `accounts`, or other journal-entry tables in HesabYar Alembic migrations in the default deployment.

---

# 26. Implementation sequence and stop gates

The coding agent works in this exact order.

## Stage F0 — Foundation

Deliver:

- package layout;
- locked dependencies;
- config model;
- PostgreSQL/Alembic;
- tenant/actor context;
- common value objects;
- error/result types;
- CI updates;
- safe standards validator skeleton.

**STOP GATE F0:** no ledger feature work until migrations, tenant isolation, MoneyIRR/Decimal policy, fail-closed rule engine, and CI are green.

## Stage F1 — Accounting engine integration

> **Supersedes 1.4.0 Stage F1 — Ledger** per ADR 0002 (2026-11-16).

Deliver:

- `AccountingEnginePort` and `TrytonAccountingAdapter`;
- engine-neutral accounting commands/queries (draft, validate, post, reverse);
- chart-of-accounts mapping and Iranian chart templates;
- tafsili association mapping;
- fiscal year/period reads through the engine;
- trial balance and core financial-statement validation reading engine reports;
- audit events correlated to engine postings.

**STOP GATE F1:** adapter contract tests green; no HesabYar-owned journal tables in the default deployment; no financial event posted into both engines. A posted record must be immutable in the engine, and any HesabYar mutation attempt against a posted journal must fail.

## Stage F2 — Versioned standards/tax sources

Deliver:

- source snapshot registry;
- standard revision registry;
- legal rule/version registry;
- typed tax policy framework;
- historical `as_of_date` golden tests.

**STOP GATE F2:** no production policy result can run without a verified applicable source.

## Stage F3 — Electronic commercial books

Deliver:

- compliance-calendar registry;
- versioned electronic-books export profiles;
- deterministic export from posted ledger cutoff;
- immutable export artifact hash/manifest;
- export validation and current-period source provenance.

**STOP GATE F3:** no claim of 1405 electronic-books readiness unless an applicable VERIFIED export profile and deadline source are active.

## Stage F4 — Invoice domain

Deliver:

- business invoice drafts;
- versioning;
- review hash/approval model;
- protocol-profile registry;
- schema validation without network.

**STOP GATE F4:** no production gateway code until profile activation rules and approval invalidation tests are green.

## Stage F5 — Moadian verification + simulator

Deliver:

- pin official technical specification;
- create verified protocol profile;
- contract fixtures;
- simulator;
- key-provider port;
- gateway interface;
- outbox/reconciliation.

**STOP GATE F5:** production gateway remains disabled until official protocol source is VERIFIED and contract tests pass.

## Stage F6 — MCP

Deliver:

- official MCP Python SDK v2;
- stdio;
- Streamable HTTP;
- auth/scopes;
- read tools;
- local write tools;
- `submit_approved_invoice`.

**STOP GATE F6:** remote security tests green; no legacy SSE.

## Stage F7 — Production Moadian adapter

Deliver only if F5/F6 complete:

- verified crypto/tax ID behavior;
- allowlisted endpoint adapter;
- remote response normalization;
- reconciliation;
- manual operational runbook.

**STOP GATE F7:** owner-controlled production enablement only.

## Stage F8 — Advisory analytics

After deterministic foundations:

- inflation scenarios;
- working-capital analysis;
- legal tax-shield opportunity analysis;
- bank-flow evidence classification.

No advisory feature may write ledger or submit invoices.

---

# 27. Coding-agent prohibitions

The coding agent MUST NOT:

1. reinterpret or redesign the architecture without an ADR request;
2. treat research prose as executable law;
3. hard-code a current tax rate/threshold without a rule version;
4. use `eval`, `exec`, dynamic imports, or arbitrary expression evaluation for financial rules;
5. use float for money/rates/quantities in financial computation;
6. use `MAX(id)+1` or `MAX(serial)+1` for numbering;
7. edit posted vouchers;
8. auto-approve its own production tax submission;
9. call production Moadian in tests;
10. fabricate official Moadian error codes;
11. implement an unverified TaxID/crypto algorithm from old docs;
12. expose an unauthenticated remote MCP endpoint;
13. accept tenant/role/private-key/endpoint from model tool arguments;
14. log secrets or full PII;
15. silently skip a mandatory validation rule;
16. return PASS for an evaluation error;
17. auto-create corrective tax invoices;
18. classify a person as committing tax fraud/crime;
19. claim a tax outcome is final when evidence or rule version is missing;
20. introduce microservices, Kafka, Redis, Celery, or another infrastructure component without a demonstrated R0 requirement and architecture update;
21. implement Rust in R0;
22. copy the v1.1 SQLite DDL as production schema;
23. add placeholder “official” protocol logic just to make tests green;
24. create duplicate journal-entry tables in the default deployment (ADR 0002);
25. post the same financial event into both the Tryton engine and any other ledger (ADR 0002);
26. write directly to Tryton's database or bypass the `AccountingEnginePort` (ADR 0002).

---

# 28. Definition of Done for any critical feature

A critical feature is complete only if:

- domain behavior is implemented;
- application authorization is enforced;
- tenant isolation is enforced;
- deterministic calculation uses correct numeric types;
- DB constraints/migrations exist;
- audit event exists for mutation;
- idempotency is tested;
- failure mode is explicit;
- source/protocol version is returned when applicable;
- unit/property/integration tests exist;
- no sensitive data leaks to logs/errors;
- docs/tool schema match actual behavior.

“Code compiles” is not Definition of Done.

---

# 29. Source verification notes from the final review

These notes explain architecture choices. They are not substitutes for source snapshots in production.

## 28.1 MCP

As of 2026-09-20:

- MCP specification `2026-07-28` is current.
- It introduced a stateless protocol core and authorization hardening.
- legacy HTTP+SSE is deprecated.
- official SDK guidance recommends Streamable HTTP for remote deployments and stdio for local child-process integrations.
- the current official Python SDK line is v2 and uses `MCPServer`.

Therefore R0 uses official MCP Python SDK v2 + stdio/Streamable HTTP.

## 28.2 Moadian

Public search surfaced a Tir 1405 electronic-invoice instruction described as version 7.9 and containing changes beyond older repository assumptions.

Because the official authority copy was not pinned inside this repository during architecture review, the profile remains unverified until implementation Stage F4 obtains the official document and snapshot.

## 29.3 Accounting standards

The repository correctly identifies Standard 35 as Income Taxes, while Standard 22 is Interim Financial Reporting.

Final 1405 audit additionally found:
- Standard 43 is effective for periods beginning 1404/01/01 and later and replaces 3/9/29.
- Standard 44 is effective for periods beginning 1405/01/01 and later and replaces Standard 21.
- the bundled Standard 15 source is legacy and must be blocked for 1405+ until the official revised-1404 source is imported and verified.

Accounting standards are revision/effective-date sensitive. The corpus must therefore be revisioned, not treated as a timeless set of 35 files.

## 29.4 Tax law

Current review confirms why tax logic must be effective-dated:

- Article 274 text is version-sensitive and should not be encoded from an old “eight-item” research list.
- Article 138 bis eligibility contains conditions beyond a simple assumed-rate multiplication.
- loss carryforward must be tied to the verified applicable legal basis rather than the old “Article 140” label.
- Article 6 rules and annual thresholds are changeable and must be source-versioned.
- from 1404/10/01, current reviewed guidance ties buyer VAT input-credit evidence to electronic invoices registered in the buyer workspace and buyer confirmation for the applicable population/period.
- 1405 electronic commercial-books obligations require a dedicated versioned export/compliance boundary and compliance calendar.
- the 1404 anti-speculation/capital-gains legislation adds a new transitional policy family whose activation depends on verified rollout/effective conditions.

---

# 30. Final architecture freeze

Implementation status:

```text
ARCHITECTURE_ID = HYA-A1-FINAL
ARCHITECTURE_VERSION = 1.5.0
ARCHITECTURE_STATE = FROZEN_IMPLEMENTATION
SUPERSEDES = 1.4.0-FROZEN (2026-09-20) per ADR 0002 (2026-11-16)
IMPLEMENTATION_AUTHORIZED = YES
PRODUCTION_MOADIAN_AUTHORIZED = NO
```

Coding is authorized for **Stage F0 Foundation**.

Production Moadian transmission remains blocked until Stage F5 verification and Stage F7 owner-controlled enablement.

**There are no implicit implementation decisions outside this file. When something is not specified here, choose the simplest design that preserves these invariants; do not invent legal/protocol behavior.**
