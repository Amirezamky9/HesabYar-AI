# HesabYar-AI — Final Implementation Architecture

**Architecture ID:** HYA-A1-FINAL
**Version:** 1.6.0-ARCH
**Date:** 2026-09-26
**State:** `ARCH_UPDATE`
**Pinned F0 Baseline:** `76c26f2f188310bcc178d9e955e4faf0706fcbef` (tag `v0.1.0-f0-frozen`, merge commit `935a6a5f3f0b630576c8054c0efb30c9a1f8e20a`)
**Scope:** Normative implementation architecture for a complete accounting operations agent: accounting ledger; treasury, banks and cheques; receivables/payables; inventory and reconciliation; fixed assets and depreciation; cost accounting; budgeting/forecasting; versioned accounting, tax, labor, insurance and payroll rules; payroll; accounting-software integration (API, file exchange, and governed Desktop/Computer Use); electronic commercial books; invoice/Moadian; document intake; import/export profiles; MCP/harness interoperability; and bounded advisory/decision intelligence.

> This is the **single normative architecture document for implementation**.
>
> The coding agent MUST implement from this file. It MUST NOT infer implementation rules from `docs/research_tax_inflation.md`, the old architecture text, README prose, examples, or external blog posts.
>
> If any other repository file conflicts with this document, **this document wins**. A future architecture change is valid only when both:
> 1. this file is version-bumped and updated; and
> 2. a new ADR records the reason (see ADR-0001 for versioned policy, ADR-0002 for payroll/labor/insurance boundary, ADR-0003 for accounting integration hub, ADR-0004 for the complete accounting-agent operating model).
>
> No ADR, research note, commit message, README section, or code comment may silently override this file.

---
# 0. Executive decision

HesabYar-AI will be implemented as a **modular monolith with explicit domain boundaries and ports/adapters**.

The system has nine different kinds of truth and they MUST stay separate:

1. **Accounting invariants** — deterministic and transactional.
2. **Accounting standards** — versioned professional rules with source provenance.
3. **Tax/legal policy** — effective-dated, source-backed, changeable rules.
4. **Payroll / labor / social security** — effective-dated statutory parameters, multi-axis assessability, balanced sub-ledger posting.
5. **External accounting integration** — canonical model abstraction, connector ports, governed dry-run/preview mutations.
6. **External government protocol behavior** — versioned integration contracts that are untrusted until verified against an official technical specification.
7. **Regulatory filing/export obligations** — versioned schemas plus compliance-calendar events; never inferred from ledger structure alone.
8. **Operational sub-ledger truth** — treasury, receivables/payables, inventory, fixed assets, costing and budgets own their operational state but affect accounting only through explicit Ledger posting contracts.
9. **Advisory/decision intelligence** — AI/Jev-like judgments may rank, classify or suggest; they never replace deterministic accounting, verified legal rules, authorization or approval.

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

- a production-grade double-entry ledger;
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

## 4.4 Core libraries

- Pydantic v2 for boundary schemas
- `decimal.Decimal` for rates/quantities/FX calculations
- Python `int` for IRR money in domain objects
- `httpx` for allowed external HTTP adapters
- `cryptography` only where required by a verified Moadian protocol profile

No pandas/numpy dependency is required for the core ledger.

---

# 5. Physical module layout and dependency rules

```text
src/hesabyar/
├── __init__.py
├── domain/
│   ├── __init__.py
│   ├── common/
│   │   ├── money.py
│   │   ├── dates.py
│   │   ├── ids.py
│   │   ├── result.py
│   │   └── context.py
│   ├── ledger/
│   │   ├── accounts.py
│   │   ├── vouchers.py
│   │   ├── periods.py
│   │   ├── trial_balance.py
│   │   └── rules.py
│   ├── standards/
│   │   ├── registry.py
│   │   ├── dsl.py
│   │   ├── model.py
│   │   └── validator.py
│   ├── tax_policy/
│   │   ├── registry.py
│   │   ├── engine.py
│   │   ├── decisions.py
│   │   └── rules/
│   │       ├── vat.py
│   │       ├── article6.py
│   │       └── corporate.py
│   ├── payroll/
│   │   ├── __init__.py
│   │   ├── models.py               # Employee, Contract, Period, Attendance, Leave
│   │   ├── components.py           # ComponentMatrix, 11 axes, 8 rule references
│   │   ├── assessments.py          # InsuranceAssessment, SalaryTaxAssessment
│   │   ├── posting.py              # PayrollPosting, VoucherDraft balanced contract
│   │   └── state_machine.py        # PayrollRun lifecycle, dual approval
│   ├── integration_hub/
│   │   ├── __init__.py
│   │   ├── models.py               # ExternalSystemConnection, Mapping, Checkpoint, Conflict
│   │   ├── canonical.py            # CanonicalAccount, CanonicalTafsili, CanonicalVoucher
│   │   ├── ports.py                # AccountingConnector abstract port interface
│   │   ├── governance.py           # ExternalMutationApproval, dry-run & preview contracts
│   │   └── reconciliation.py       # ReconciliationResult and discrepancy detection
│   ├── commercial_books/
│   │   ├── journal.py
│   │   ├── general_ledger.py
│   │   ├── export_profile.py
│   │   └── compliance_calendar.py
│   └── invoice/
│       ├── model.py
│       ├── line_items.py
│       ├── tax_id.py
│       └── validation.py
├── application/
│   ├── ledger/
│   ├── standards/
│   ├── tax_policy/
│   ├── payroll/
│   ├── integration_hub/
│   ├── commercial_books/
│   ├── invoice/
│   ├── moadian/
│   ├── intake/
│   └── audit/
├── infrastructure/
│   ├── persistence/
│   │   ├── models.py
│   │   ├── session.py
│   │   ├── alembic/
│   │   └── repositories/
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── base.py                 # Common integration connector harness
│   │   ├── sepidar/                # Sepidar adapter; transport chosen from verified capabilities
│   │   ├── holoo/                  # Holoo adapter; API/file/Desktop transport as verified
│   │   ├── parmis/                 # Parmis adapter
│   │   └── generic_csv/            # Generic CSV / Excel import-export adapter
│   ├── moadian/
│   │   ├── client.py
│   │   ├── security.py
│   │   ├── simulator.py
│   │   └── profiles/
│   ├── intake/
│   └── security/
└── interfaces/
    └── mcp/
        ├── server.py
        ├── auth.py
        ├── error_handlers.py
        └── tools/
            ├── ledger_tools.py
            ├── standards_tools.py
            ├── tax_tools.py
            ├── payroll_tools.py
            ├── integration_tools.py
            ├── books_tools.py
            └── invoice_tools.py

skills/
├── iran-payroll-insurance/
│   ├── SKILL.md
│   ├── references/
│   │   ├── annual-1404.yaml
│   │   ├── annual-1405.yaml
│   │   ├── source-status.md
│   │   ├── payroll-components.yaml
│   │   ├── labor-law-index.md
│   │   ├── social-security-index.md
│   │   └── salary-tax-index.md
│   └── scripts/
│       ├── validate_sources.py
│       ├── validate_parameters.py
│       ├── diff_annual_rules.py
│       └── check_source_freshness.py
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
- domain importing HTTP clients;
- domain importing OCR/LLM SDKs;
- domain reading environment variables;
- repositories calling MCP;
- an MCP tool directly calling SQLAlchemy models;
- tax-policy code posting ledger entries;
- document parser directly posting ledger entries.

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

# 8. Ledger bounded context

The ledger is deterministic and transactionally authoritative.

## 8.1 Required entities

- Tenant
- FiscalYear
- PostingPeriod
- Account
- Tafsili
- AccountTafsiliLink
- Voucher
- VoucherLine
- VoucherApproval
- NumberSequence

## 8.2 Account hierarchy

Support group / kol / moein and floating tafsili association.

The old schema documented “matrix tafsili” but did not create the required link table. R0 MUST include a real many-to-many `account_tafsili_links` table with validity dates/status if needed.

A tafsili may be linked to multiple eligible moein accounts.

## 8.3 Voucher states

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

PostgreSQL must enforce:

- foreign keys;
- tenant-aware unique constraints;
- unique `(tenant_id, fiscal_year_id, voucher_number)`;
- line one-sided CHECK constraints;
- no update/delete of posted vouchers/lines through normal application DB role;
- transaction-time validation that a voucher is balanced before POSTED state is committed.

Use a DB constraint trigger or equivalent transaction-safe enforcement for the cross-row balance invariant.

## 8.5 Sequence allocation

Never use `MAX(voucher_number)+1`.

Use a sequence/allocation row keyed by tenant + fiscal year and lock it transactionally (`SELECT ... FOR UPDATE` or an equivalent safe allocator).

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

# 11. Payroll, labor, social insurance, and salary tax bounded context

The Payroll context (`src/hesabyar/domain/payroll/`) governs employment records, timesheet and attendance tracking, leave management, multi-axis compensation assessment, Social Security insurance contributions, salary tax withholding, and balanced sub-ledger posting.

## 11.1 Ubiquitous language & 14 domain entities

1. `Employee`: Tenant-isolated natural person in employment relationship. Tracks National ID (`national_id`), employee number (`employee_no`), full name, father's name, birth certificate number, insurance number (`insurance_no`), hire date, IBAN, and employment status (`ACTIVE`, `TERMINATED`, `SUSPENDED`, `ON_LEAVE`).
2. `EmploymentContract`: Formal labor agreement. Tracks `contract_no`, `employee_id`, contract type, start/end dates, wage terms, fixed allowances, agreed working schedule, probation terms where applicable, and lifecycle status. Compliance with statutory hours, wage floors, probation limits, and other changing legal conditions is resolved from the applicable VERIFIED rule version rather than hard-coded into the entity.
3. `PayrollPeriod`: Monthly fiscal cycle. Tracks `fiscal_year` (Solar Hijri), `month_number` (1 to 12), start date, end date, calendar days (31 for months 1-6, 30 for months 7-11, 29/30 for month 12), standard monthly working hours, and lifecycle status (`OPEN`, `PROCESSING`, `CLOSED`, `ARCHIVED`).
4. `Attendance` & `Timesheet`: Monthly operational attendance. Tracks `employee_id`, `period_id`, worked days, standard hours worked, overtime hours, night work hours, shift work category, Friday work hours, mission days, excused absences, unexcused absences, and verification status (`DRAFT`, `VERIFIED`, `LOCKED`).
5. `Leave`: Statutory leave events under Labor Law Arts. 64-74. Tracks `employee_id`, leave type (`ENTITLED_ANNUAL`, `SICK`, `MARRIAGE_EMERGENCY`, `BEREAVEMENT_EMERGENCY`, `MATERNITY`, `UNPAID`), start date, end date, total days, approval state (`REQUESTED`, `APPROVED`, `REJECTED`, `CANCELLED`), and SSO Medical Commission certification flag for sick leave.
6. `PayrollComponent`: Canonical earning, deduction, or employer contribution element. Enforces 11 independent boolean classification axes, 8 effective-dated statutory rule references, and annual decree notes.
7. `PayrollRun`: Operational sub-ledger batch calculation. Tracks `run_id`, `tenant_id`, `period_id`, run type (`REGULAR_MONTHLY`, `MID_MONTH_ADVANCE`, `SEVERANCE_SETTLEMENT`, `EIDI_BONUS`, `ADJUSTMENT`), calculation timestamp, aggregated gross pay, assessable insurance, employee insurance deduction, employer insurance expense, unemployment insurance expense, taxable gross pay, tax withheld, and net payable. Lifecycle states: `DRAFT` -> `CALCULATED` -> `UNDER_REVIEW` -> `APPROVED` -> `POSTED` -> `RECONCILED` / `VOIDED`.
8. `PayrollLine`: Line-item employee receipt. Binds an employee in a payroll run to a specific component, recording calculated gross amount, assessable insurance amount, taxable portion, employee deduction, employer overhead cost, and calculation formula trace.
9. `PayrollApproval`: Governed authorization ticket. Required approver count/scopes are determined by tenant/risk policy. High-impact or regulated workflows may require two distinct actors; small-business deployments are not forced to create artificial roles when one authorized human approval is sufficient under configured policy.
10. `PayrollPosting`: Immutable double-entry bridge to the Ledger context. Generates a balanced `VoucherDraft` with debit to wage expenses, debit to employer insurance expenses, credit to payable to employees, credit to payable to Social Security Organization (Tamin), and credit to salary tax withholding payable.
11. `InsuranceAssessment`: Social-insurance audit breakdown for an employee. Records assessable base, contribution components, ceilings/floors, exemptions and the exact legal-rule/source versions used. Rates and ceilings are effective-dated parameters, never entity constants.
12. `SalaryTaxAssessment`: Salary-tax audit breakdown for an employee. Records taxable base, deductions/exemptions, bracket/rate calculations and the exact legal-rule/source versions used. Deductions, exemptions, bracket counts and rates are not timeless domain constants.
13. `PayrollSubmission`: Regulatory export package. Tracks versioned export-profile ID, generation timestamp, artifact hashes, lifecycle status and official tracking/receipt metadata when applicable. Concrete DBF/XML/CSV layouts live in verified DataExchange/Regulatory profiles rather than the Payroll domain model.
14. `PayrollComplianceEvent`: Compliance-calendar obligation. Tracks filing/remittance event type, applicable period, due date and source-rule version. Deadlines are resolved from verified effective-dated sources and are not hard-coded into the entity.

## 11.2 Multi-axis payroll component matrix schema

The system strictly rejects the assumption that `is_insurance_assessable == is_taxable`.
Every component evaluates 11 orthogonal classification axes and 8 effective-dated legal rule references:

```yaml
component_code: "HOUSING_ALLOWANCE"
name_fa: "حق مسکن"
classification: "EARNING"
classification_rules:
  wage_base_rule_ref: "RULESET-RESOLVED-BY-PERIOD"
  insurance_assessability_rule_ref: "RULESET-RESOLVED-BY-PERIOD"
  taxability_rule_ref: "RULESET-RESOLVED-BY-PERIOD"
  overtime_base_rule_ref: "RULESET-RESOLVED-BY-PERIOD"
  severance_base_rule_ref: "RULESET-RESOLVED-BY-PERIOD"
  eidi_base_rule_ref: "RULESET-RESOLVED-BY-PERIOD"
  leave_settlement_rule_ref: "RULESET-RESOLVED-BY-PERIOD"
  employer_cost_rule_ref: "RULESET-RESOLVED-BY-PERIOD"
```

## 11.3 Legal-family resolution rules

Payroll must represent at least three independently versioned legal families:

1. **Labor/employment rules** — wage floors, working time, overtime/night/shift/holiday treatment, leave, severance, annual bonus and related employment conditions.
2. **Social-insurance rules** — assessable/non-assessable components, employee/employer contribution components, ceilings/floors, filing obligations and contribution-specific exceptions.
3. **Salary-tax rules** — taxable/non-taxable components, deductions/exemptions, annual budget thresholds/brackets, withholding and filing obligations.

The architecture intentionally does **not** freeze percentages, ceilings, bracket counts, deadlines, welfare-benefit treatment, or annual amounts here.

For every payroll calculation:

```text
payroll_period
 -> applicable legal-source versions
 -> applicable component-classification versions
 -> effective-dated parameters
 -> deterministic calculation
 -> calculation trace with source IDs
```

A legal change normally creates a new source/rule/parameter version and golden tests. It does not require rewriting stable Payroll entities.

If an applicable mandatory rule is missing, stale, disputed or unverified, finalization fails closed with an explicit reviewable error.

## 11.4 Lifecycle state machines & dual-control governance

```text
  [DRAFT] ──(calculate)──> [CALCULATED] ──(submit)──> [UNDER_REVIEW]
                                                               │
                                                       (dual approval)
                                                               │
                                                               v
  [VOIDED] <──(cancel)── [POSTED] <──(post to ledger)── [APPROVED]
                               │
                       (reconciliation)
                               │
                               v
                         [RECONCILED]
```

- A `PayrollRun` cannot transition to `APPROVED` until the configured tenant/risk approval policy is satisfied.
- Policies may require one authorized human or multiple distinct approvers depending on risk/regulatory need.
- The model/agent cannot approve its own PayrollRun.
- Only `APPROVED` runs can emit a `PayrollPosting` command to the Ledger.

## 11.5 Balanced journal voucher posting contract (`PayrollPosting`)

An approved PayrollRun emits a **balanced VoucherDraft** using a tenant-configured Payroll Account Mapping.

The domain uses semantic account roles, not hard-coded chart codes:

```text
GROSS_WAGE_EXPENSE
EMPLOYER_CONTRIBUTION_EXPENSE
NET_PAYABLE_TO_EMPLOYEES
SOCIAL_INSURANCE_PAYABLE
SALARY_TAX_PAYABLE
OTHER_PAYROLL_PAYABLE_OR_RECEIVABLE
```

Calculated amounts come from the effective Payroll rule set. The tenant maps semantic roles to its own COA/Tafsili structure.

Non-negotiable invariants:

1. total debit equals total credit exactly to the Rial;
2. no Payroll code writes directly to Ledger tables;
3. missing account mapping blocks posting and returns a reviewable error;
4. posting preserves PayrollRun ID, rule/source versions and calculation trace;
5. posted payroll corrections use Ledger reversal/replacement semantics, not in-place mutation.


---
# 12. Accounting integration hub bounded context

The Accounting Integration Hub (`src/hesabyar/domain/integration_hub/` and `src/hesabyar/infrastructure/integrations/`) governs interoperability between HesabYar-AI and external Iranian desktop and cloud accounting systems (e.g., Sepidar System, Holoo, Parmis, Hamkaran System, Rayvarz, generic CSV/Excel).

## 12.1 Canonical architecture & anti-corruption layer

```text
  ┌────────────────────────────────────────────────────────┐
  │              HesabYar-AI Core Domains                  │
  │     (Ledger, Payroll, Invoices, Commercial Books)       │
  └───────────────────────────▲────────────────────────────┘
                              │
                 Canonical Accounting Model DTOs
            (CanonicalAccount, CanonicalVoucher, etc.)
                              │
  ┌───────────────────────────▼────────────────────────────┐
  │             AccountingConnector Port                   │
  │    (connect, disconnect, fetch_delta, push_batch, ...) │
  └───────────────────────────▲────────────────────────────┘
                              │
  ┌───────────────────────────┴────────────────────────────┐
  │     Vendor Integration Adapters (Infrastructure)       │
  │  ┌──────────────┐ ┌──────────────┐ ┌────────────────┐  │
  │  │   Sepidar    │ │    Holoo     │ │  Parmis / CSV  │  │
  │  │ capabilities │ │ capabilities │ │ capabilities   │  │
  │  └──────────────┘ └──────────────┘ └────────────────┘  │
  └────────────────────────────────────────────────────────┘
```

The domain and application layers remain 100% vendor-agnostic. All vendor-specific protocol/file/UI translations reside in `src/hesabyar/infrastructure/integrations/<vendor>/`. Direct vendor-database access is permitted only when the vendor exposes/supports that mechanism and the capability profile is explicitly verified; otherwise use supported API, file exchange, or Desktop Bridge.

## 12.2 Core domain models

1. `ExternalSystemConnection`: Tenant-scoped connection credentials, encrypted secret references, endpoint URL, driver type, timeout parameters, and health status.
2. `ConnectorManifest`: Vendor metadata, supported protocol versions, supported sync modes, and credential schema definitions.
3. `ConnectorCapability`: Fine-grained capability negotiation flags (`READ_COA`, `READ_TAFSILI`, `READ_VOUCHERS`, `WRITE_VOUCHERS`, `READ_INVOICES`, `WRITE_INVOICES`, `SUPPORTS_WEBHOOKS`, `SUPPORTS_DELTA_SYNC`, `SUPPORTS_DRY_RUN`).
4. `ExternalObjectMapping`: Bi-directional identity mapping binding canonical entity IDs to external system IDs (with tenant isolation, entity type, content hash, and timestamp).
5. `SyncCheckpoint`: High-water mark state tracker storing sync cursors, transaction log sequence numbers, timestamps, and processed record counts.
6. `SyncConflict`: Immutable record of detected data conflicts between local and external systems, documenting local state, remote state, conflict nature, and resolution audit trail.
7. `ExternalMutationApproval`: Governed approval ticket containing payload preview, validation status, requesting actor, approving actor, expiration, and execution results.
8. `ReconciliationResult`: Financial discrepancy audit report comparing local vs. external balances, line item counts, and account totals.

## 12.3 Connector operation modes

Connections operate under four mutually exclusive operational modes per entity type:
- `READ_ONLY`: Connects solely for telemetry, health checking, or read verification; zero state is imported into HesabYar or written externally.
- `IMPORT`: One-way data ingestion from the external system into HesabYar (external system is authoritative source).
- `EXPORT`: One-way data publishing from HesabYar to the external system (HesabYar is authoritative source, requiring `ExternalMutationApproval`).
- `BIDIRECTIONAL`: Two-way synchronization with strict conflict resolution and echo-suppression.

## 12.4 The 18 architectural concerns (ADR-0003)

1. **Canonical vs. Vendor Model Abstraction:** Strict Anti-Corruption Layer (ACL) shielding core ledger invariants from third-party proprietary schemas.
2. **Connector Port Contracts (`AccountingConnector`):** Abstract lifecycle interfaces defining `connect()`, `disconnect()`, `healthcheck()`, `fetch_delta()`, `push_batch()`, and `preview_mutations()`.
3. **Vendor Isolation:** Vendor-specific code lives in `src/hesabyar/infrastructure/integrations/<vendor>/` with zero leakage into domain models.
4. **Capability Negotiation:** Dynamic runtime discovery via `ConnectorCapability` flags before invoking vendor features.
5. **Connector Manifest & Metadata:** Versioned definitions declaring supported vendor versions, schema capabilities, and credential requirements.
6. **Connection Management & Secret Isolation:** Tenant-scoped credentials stored securely with encrypted connection strings; secrets never exposed in logs or MCP tool arguments.
7. **Operational Modes Enforcement:** Strict runtime guards enforcing `READ_ONLY`, `IMPORT`, `EXPORT`, or `BIDIRECTIONAL` boundaries.
8. **Configurable Source of Truth (SoT) per Tenant:** Explicit per-entity configuration (e.g. COA = External, Vouchers = HesabYar, Customers = External) preventing dual-master split-brain.
9. **Identity Mapping & FK Abstraction:** Bi-directional persistent translation via `ExternalObjectMapping` maintaining canonical UUID to vendor primary key bindings.
10. **Bi-temporal & Delta Sync Tracking:** Cursor-based high-water mark tracking via `SyncCheckpoint` ensuring only incremental changes are processed.
11. **Conflict Detection & Resolution Policies:** Deterministic conflict handling (`LOCAL_WINS`, `REMOTE_WINS`, `MANUAL_REVIEW`, `MERGE_IMMUTABLE`).
12. **Strict Read/Write Governance & Mutation Approvals:** Dry-run/preview is required for governed external mutations. Explicit human approval is mandatory for high-impact writes; only explicitly configured reversible low-risk actions may use `AUTO_LOW_RISK`. No connector bypasses domain approval policy.
13. **Idempotency & Replay Resistance:** Deterministic mutation tokens and hash-based deduplication preventing double-posting of journal vouchers or invoices.
14. **Bidirectional Sync Loop Prevention:** Change-set origin tracking and cryptographic payload fingerprinting to eliminate infinite echo loops.
15. **Schema & Semantic Translation:** Canonical normalization for currencies (IRR / Toman), calendar dates (Jalali / Gregorian), and floating Tafsili dimensions.
16. **Transactional Outbox & Resilient Execution:** Reuse the existing PostgreSQL transactional outbox/idempotency infrastructure for outbound work, retryable failures and terminal/dead states. Do not introduce a separate message broker merely for connector retries.
17. **Financial Reconciliation & Parity Auditing:** Continuous automated `ReconciliationResult` auditing trial balances, debit/credit parity, and line-item integrity across systems.
18. **Observability:** Reuse the platform's structured logs/metrics/audit facilities to track sync throughput, latency, conflict rate and connector health. No specific monitoring product is required by this architecture.

---

# 12A. Complete accounting operations scope (reserved, incremental)

This section reserves the minimum seams required for HesabYar to become a practical **Accounting Operations Agent** without forcing premature implementation.

These are modules/bounded contexts inside the existing modular monolith. They are **not microservices**, and their detailed database schemas MUST NOT be created until their implementation stage begins.

## 12A.1 Product role

HesabYar may act as an accounting assistant and, where explicitly authorized, an operational accounting agent.

It may:

- read accounting and operational data;
- detect missing, duplicate, stale or conflicting records;
- reconcile bank, cheque, inventory, invoice and external-accounting records;
- prepare deterministic calculations and draft postings;
- generate and validate import/export artifacts;
- operate supported accounting software through governed connectors;
- surface deadlines, review queues and anomalies;
- provide advisory analysis.

It does not become the source of legal truth, financial arithmetic truth, tenant identity, authorization, or approval merely because an AI model requested an action.

## 12A.2 Progressive tenant onboarding

Initial setup MUST be progressive. Do not require a small business to configure modules it does not use.

Minimum onboarding:

- company/tenant profile;
- fiscal-year start/end and locale/date presentation;
- actor roles and approval policy;
- accounting system of record: HesabYar or an external package;
- currency is canonical IRR; optional Toman presentation is display-only.

Capability-specific onboarding is requested only when enabled:

- accounting-software connection and COA/Tafsili mapping;
- bank accounts, cash boxes, POS terminals and payment gateways;
- cheque workflows;
- customer/vendor/open-item settings;
- warehouses, items and stock sources;
- fixed assets;
- costing dimensions if needed;
- budgets/scenarios if needed;
- employees, contracts and payroll;
- tax/compliance identifiers and profiles;
- import/export profiles;
- allowed automation level per capability.

Secrets/passwords/tokens are provided to a secret provider or local trusted bridge. They are never stored in prompts, legal Skill files, ordinary business tables, or logs.

## 12A.3 Treasury, banking and cheques

Reserve a **Treasury & Settlement** domain.

Minimum responsibilities:

- bank and cash-account references;
- POS/payment-gateway references;
- receipts and payments;
- settlement allocation;
- cheque register;
- incoming/outgoing cheque lifecycle;
- cheque due dates and status;
- bank and cheque reconciliation;
- due/overdue operational queues.

Cheque handling must support answering:

- is this cheque already registered locally?
- is it registered in the configured external accounting system?
- is it a probable duplicate?
- do cheque identifier, amount, due date, bank/account and counterparty agree?
- has its bank outcome changed while accounting status is stale?
- is a follow-up posting or settlement action required?

Matching may use deterministic identifiers first and optional DecisionProvider assistance for ambiguous candidates. Final mutation follows the configured approval policy.

Do not invent a cheque-specific database schema until Treasury implementation starts.

## 12A.4 Accounts receivable and accounts payable

Reserve an **AR/AP & Open Items** domain for:

- customer/vendor balances;
- invoice/open-item settlement;
- receipts/payments allocation;
- advances and unapplied amounts;
- ageing buckets;
- overdue queues;
- reconciliation against Ledger and external accounting systems.

AR/AP is operational detail over Ledger-backed accounting truth. It MUST NOT become a second general ledger.

## 12A.5 Inventory and inventory reconciliation

Reserve an **Inventory** domain for:

- warehouses;
- items/SKUs and units of measure;
- stock movements;
- counted/physical stock;
- transfers;
- adjustments;
- quantity reconciliation;
- inventory-source mappings.

Typical reconciliation:

```text
physical/count source
        ↕
external accounting inventory
        ↕
website / commerce inventory
        ↓
exact match | conflict | missing | duplicate | review
```

A discrepancy report may propose a correction. It never silently creates an inventory adjustment or accounting posting.

Inventory valuation methods and manufacturing detail are deferred until the Cost Accounting stage and tenant requirements justify them.

## 12A.6 Fixed assets and depreciation

Reserve a **Fixed Assets** domain for:

- asset register;
- acquisition;
- asset tag / location / custodian;
- depreciation policy/version;
- useful life and residual value where applicable;
- transfer;
- impairment/revaluation where supported by verified accounting policy;
- disposal/sale;
- balanced posting drafts.

Accounting depreciation and tax depreciation may differ. Their rule sources and effective dates MUST remain independent.

Do not hard-code annual tax depreciation rates in domain code.

## 12A.7 Cost accounting

Reserve a **Cost Accounting** domain.

Minimum future capabilities:

- cost centers;
- direct material;
- direct labor;
- overhead allocation;
- product/service/project cost objects;
- deterministic cost calculation;
- cost-to-ledger posting drafts;
- variance reporting.

Manufacturing routing, BOM, WIP and advanced production costing are **optional extensions**, not R0 requirements. Add them only when a concrete tenant workflow requires them.

## 12A.8 Budgeting and forecasting

Reserve a **Budget & Planning** domain for:

- versioned budgets;
- revenue/expense budgets;
- cash budgets;
- cost-center budgets;
- scenarios;
- actual-vs-budget;
- forecasts.

Budget and forecast values are planning truth, not posted accounting truth.

Forecast/AI output MUST NOT modify Ledger, payroll, tax filings, inventory or external accounting systems.

## 12A.9 Shared reconciliation capability

Reconciliation is a shared application capability, not a second database of financial truth.

Canonical outcomes:

```text
EXACT_MATCH
SUGGESTED_MATCH
CONFLICT
MISSING_LOCAL
MISSING_REMOTE
DUPLICATE
REVIEW_REQUIRED
```

Domain-specific matchers may exist for:

- bank transactions;
- cheques;
- AR/AP settlements;
- inventory;
- invoices/payments;
- external accounting systems.

Every suggested match stores evidence/provenance. A reconciliation result never silently rewrites a POSTED or externally authoritative record.

## 12A.10 Import and export profiles

Data exchange is a first-class capability.

Support versioned profiles behind one canonical DataExchange port.

Potential formats include:

- XLSX/XLS;
- CSV/TSV;
- JSON;
- XML;
- PDF reports;
- DBF where legally/vendor required;
- OFX, MT940 or CAMT when a verified banking profile is available;
- vendor-specific import/export formats.

This is an extensible profile mechanism, **not** a requirement to implement every format in the first release.

Each profile declares at minimum:

```text
profile_id
direction = IMPORT | EXPORT
entity_types
format
schema_version
encoding/locale
vendor_or_authority
effective_from/effective_to where applicable
verification_status where legally/vendor sensitive
validation_contract
```

Exports intended for re-import into another system MUST be deterministic and validation-tested against that profile.

Human-readable Excel/PDF reporting is separate from canonical machine contracts.

## 12A.11 API, file exchange and Desktop/Computer Use

Accounting integration has three first-class transport families:

```text
API / Web Service
File Import / Export
Local Desktop / Computer Use
```

Transport selection is capability-driven. Prefer the most deterministic supported mechanism for the requested action, but Desktop/Computer Use is a supported transport when an accounting package lacks an adequate API or supported file workflow.

Desktop/Computer Use runs through a tenant-scoped **Desktop Bridge** and MUST follow:

```text
OBSERVE
 -> PLAN
 -> PREVIEW
 -> APPROVAL when required
 -> EXECUTE
 -> VERIFY RESULT
 -> AUDIT
```

Rules:

- no blind write sequence without post-action verification;
- ambiguous UI state fails closed;
- vendor UI workflows/selectors are versioned capability profiles;
- a vendor UI change may disable that capability without breaking core domain code;
- credentials remain local/secret-provider scoped;
- screenshots/logs minimize or redact sensitive data where practical;
- external mutation idempotency/duplicate defenses still apply;
- Computer Use never bypasses Ledger, Payroll, Treasury or tax approval policy.

## 12A.12 Harness and agent portability

HesabYar exposes one canonical MCP surface.

Business logic MUST NOT be duplicated for Hermes, Claude Code, Codex, Gemini, Cursor or another harness.

Harness-specific plugins/configuration are thin adapters containing only:

- MCP connection/config metadata;
- optional installation/bootstrap instructions;
- UI or harness-specific convenience prompts;
- no accounting calculation engine;
- no legal-rule copy;
- no duplicate authorization logic.

If a client cannot consume the supported MCP transport, an optional REST/CLI/SDK adapter may be added later behind the same application ports.

## 12A.13 Optional decision intelligence

Define an optional `DecisionProvider` port for Jev-like models or general LLMs.

Good uses:

- probable duplicate classification;
- bank/payment/invoice candidate matching;
- document type classification;
- anomaly triage;
- workflow/Skill routing;
- deciding whether an ambiguous case should be escalated.

Forbidden uses:

- deciding whether debit equals credit;
- statutory payroll/tax/insurance arithmetic;
- replacing a VERIFIED legal rule;
- tenant identity;
- authorization;
- approving irreversible mutations.

Decision records include:

```text
provider
model/version
use_case
input_hash
output
score/probability where available
calibration_profile
timestamp
```

Thresholds are calibrated per workflow from representative data. There is no universal confidence threshold.

## 12A.14 Automation levels

Per tenant and capability, support policy levels:

```text
READ_ONLY
SUGGEST
DRAFT
EXECUTE_WITH_APPROVAL
AUTO_LOW_RISK
```

`AUTO_LOW_RISK` is allowed only for explicitly configured, reversible and low-impact workflows.

The following remain approval/policy gated:

- posting accounting vouchers;
- payroll finalization;
- tax/government submissions;
- destructive inventory adjustments;
- cheque/payment state mutations that affect accounting;
- external-system writes classified as high impact.

## 12A.15 Over-engineering guard

The presence of a domain in this architecture does not authorize speculative implementation.

For every future domain:

1. reserve ownership and integration seams here;
2. implement only when its roadmap stage begins;
3. start from concrete user workflows and the smallest useful model;
4. add tables/classes only when required by those workflows;
5. reuse PostgreSQL, application command contracts, audit, idempotency and outbox infrastructure;
6. do not introduce Kafka, Redis, a workflow engine, vector database, microservices, or a second accounting engine without a demonstrated requirement and a new ADR.


# 13. Electronic commercial books compliance

Electronic commercial books are a separate regulatory-output boundary over the authoritative ledger.

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
- ledger revision/hash;
- export profile version;
- source version;
- generated timestamp.

The export boundary MUST NOT mutate ledger postings.

If the required active schema/profile for a filing period is missing or unverified, return `BOOKS_PROFILE_UNVERIFIED`.

## 11.3 1405 current requirement

Current 1405 notices demonstrate that electronic commercial-books upload is operational and deadline-driven. Therefore this is an R0 compliance requirement, not a future roadmap placeholder.

The project MUST implement the boundary and calendar before claiming 1405 filing readiness.

---

# 14. Invoice domain and Moadian protocol profiles

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

# 15. External submission workflow

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

# 16. Document intake

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

# 17. MCP/API boundary

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

# 18. Application command contract

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

# 19. Security and secrets

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

# 20. Audit architecture

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

# 21. Database model — minimum required tables

The old nine-table SQLite DDL is superseded.

Minimum PostgreSQL tables:

```text
tenants
actors

fiscal_years
posting_periods
accounts
tafsilis
account_tafsili_links
number_sequences
vouchers
voucher_lines
voucher_approvals

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

- `employees`: employee master record scoped to tenant (`tenant_id`, `employee_no`, `national_id`, `status`).
- `employment_contracts`: labor contracts (`tenant_id`, `employee_id`, `contract_no`, `daily_base_wage`, `status`).
- `payroll_periods`: monthly fiscal cycles (`tenant_id`, `fiscal_year`, `month_number`, `status`).
- `attendances`: timesheet entries (`tenant_id`, `period_id`, `employee_id`, `worked_days`, `overtime_hours`, `status`).
- `leaves`: leave requests and approvals (`tenant_id`, `employee_id`, `leave_type`, `start_date`, `end_date`, `status`).
- `payroll_runs`: sub-ledger payroll calculations (`tenant_id`, `period_id`, `run_type`, `total_net_payable`, `state`).
- `payroll_lines`: line item receipts (`tenant_id`, `run_id`, `employee_id`, `component_code`, `gross_amount`, `taxable_amount`).
- `payroll_postings`: balanced journal voucher emission records (`tenant_id`, `run_id`, `voucher_draft_id`, `posted_at`).
- `payroll_approvals`: dual-control sign-off tickets (`tenant_id`, `run_id`, `approval_step`, `approving_actor_id`).
- `external_system_connections`: tenant integration connection profiles (`tenant_id`, `vendor`, `connection_mode`, `is_active`).
- `connector_manifests`: vendor capability definitions (`vendor`, `protocol_version`, `supported_modes`).
- `external_object_mappings`: cross-system ID translations (`tenant_id`, `connection_id`, `entity_type`, `canonical_id`, `external_id`).
- `sync_checkpoints`: delta sync cursors (`tenant_id`, `connection_id`, `entity_type`, `high_water_mark`, `updated_at`).
- `sync_conflicts`: sync conflict records (`tenant_id`, `connection_id`, `conflict_type`, `local_state`, `remote_state`, `status`).
- `external_mutation_approvals`: write governance approval tickets (`tenant_id`, `connection_id`, `preview_hash`, `approved_by`).
- `reconciliation_results`: cross-system parity reports (`tenant_id`, `connection_id`, `discrepancy_count`, `status`).

## 21.1 Important uniqueness

At minimum:

```text
accounts: UNIQUE(tenant_id, code)
tafsilis: UNIQUE(tenant_id, code)
vouchers: UNIQUE(tenant_id, fiscal_year_id, voucher_number)
voucher_lines: UNIQUE(voucher_id, line_number)
invoice_drafts: UNIQUE(tenant_id, internal_id, version)
idempotency_keys: UNIQUE(tenant_id, command_name, request_id)
integration_outbox: UNIQUE(tenant_id, idempotency_key)
```

Any external tax identifier uniqueness is determined by the verified active protocol profile and enforced as appropriate.

- `employees (tenant_id, national_id)`
- `employees (tenant_id, employee_no)`
- `employment_contracts (tenant_id, contract_no)`
- `payroll_periods (tenant_id, fiscal_year, month_number)`
- `attendances (tenant_id, period_id, employee_id)`
- `payroll_runs (tenant_id, period_id, run_type)`
- `payroll_lines (tenant_id, run_id, employee_id, component_code)`
- `payroll_postings (tenant_id, run_id)`
- `payroll_approvals (tenant_id, run_id, approval_step, approving_actor_id)`
- `external_system_connections (tenant_id, vendor)`
- `external_object_mappings (tenant_id, connection_id, entity_type, canonical_id)`
- `external_object_mappings (tenant_id, connection_id, entity_type, external_id)`
- `sync_checkpoints (tenant_id, connection_id, entity_type)`

## 21.2 Foreign-key tenant safety

Repository methods always scope by tenant.

Where practical, use composite foreign keys/constraints so a child cannot reference a parent from another tenant.

Tests MUST attempt cross-tenant ID substitution and prove failure.

---

# 22. Error taxonomy

Use stable application error codes.

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


## 22.6 Payroll & Labor
- `PAYROLL_RULE_UNVERIFIED`
- `UNBALANCED_PAYROLL_POSTING`
- `DUAL_APPROVAL_REQUIRED`
- `PAYROLL_PERIOD_CLOSED`
- `EMPLOYEE_NOT_FOUND`
- `INVALID_LEAVE_DATES`
- `TIMESHEET_NOT_VERIFIED`
- `STATUTORY_MINIMUM_VIOLATION`
- `SSO_CEILING_CALCULATION_ERROR`

## 22.7 Accounting Integration Hub
- `CONNECTOR_NOT_FOUND`
- `CAPABILITY_NOT_SUPPORTED`
- `EXTERNAL_CONNECTION_FAILED`
- `UNAUTHORIZED_EXTERNAL_MUTATION`
- `SYNC_CONFLICT_DETECTED`
- `RECONCILIATION_IMBALANCE`
- `ECHO_LOOP_DETECTED`
- `IDEMPOTENT_MUTATION_REPLAYED`
- `SOURCE_OF_TRUTH_VIOLATION`
# 23. Moadian simulator contract

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

# 24. Deployment architecture

One codebase, two production processes:

```text
+-----------------------+
| hesabyar-mcp          |
| stdio or HTTP server  |
+-----------+-----------+
            |
            v
       PostgreSQL
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
```

This is still a modular monolith, not microservices.

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

# 25. Observability

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

# 26. CI quality gates

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

# 27. Migration from the current repository

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

---

# 28. Canonical implementation sequence and stop gates

This is the **only authoritative roadmap**. Older stage numbering is superseded by this section.

Stages are gated. A later stage may be researched in advance, but implementation MUST NOT silently cross the current stop gate.

### Stage F0 — Foundation [FROZEN]

Pinned baseline:

`76c26f2f188310bcc178d9e955e4faf0706fcbef`

Deliverables already accepted:

- project/package baseline;
- PostgreSQL/Alembic foundation;
- tenant/actor isolation;
- MoneyIRR / Decimal policy;
- fail-closed safe standards DSL;
- CI quality gates.

### Stage F1 — Accounting Ledger

Deliver the smallest complete deterministic ledger:

- COA and Tafsili links;
- fiscal years/posting periods;
- voucher draft/review/approve/post/reverse;
- immutable POSTED state;
- numbering;
- trial balance;
- audit/idempotency/concurrency.

**STOP GATE F1:** DB and property invariants prove balanced posting, tenant isolation, immutability and closed-period protection.

### Stage F2 — Versioned Legal & Accounting Rule Registry

Deliver:

- immutable source snapshots;
- standard revisions;
- legal rule versions;
- effective-date resolution;
- typed rule evaluation;
- historical golden tests.

**STOP GATE F2:** no executable policy result without an applicable VERIFIED source.

### Stage F3 — Treasury, Banking, Cheques & AR/AP

Deliver concrete operational workflows for:

- bank/cash/payment-channel references;
- receipts/payments;
- incoming/outgoing cheques;
- cheque registration/duplicate/status checks;
- open items and settlement allocation;
- bank/cheque reconciliation;
- ageing/due queues;
- balanced posting drafts to Ledger.

**STOP GATE F3:** no silent posting or settlement mutation; reconciliation and cheque lifecycle tests are green.

### Stage F4 — Payroll, Labor, Social Insurance & Salary Tax

Deliver:

- employee/contracts;
- periods/attendance/leave;
- effective-dated component classification;
- payroll calculations;
- insurance/tax assessments;
- approvals;
- PayrollPosting drafts;
- required regulatory export profiles.

**STOP GATE F4:** verified applicable rule sources, zero float, historical golden cases, balanced posting drafts and approval controls all green.

### Stage F5 — Inventory & Reconciliation

Deliver only the inventory workflows required for general businesses:

- warehouse/item references;
- stock movements;
- counts;
- transfers/adjustments;
- inventory reconciliation with external/site sources;
- posting drafts where required.

Do not add manufacturing/WIP complexity yet.

**STOP GATE F5:** quantity invariants, tenant isolation, reconciliation evidence and governed adjustment tests green.

### Stage F6 — Fixed Assets, Costing & Budgeting

Implement these as separate modules but in one gated stage to avoid infrastructure sprawl.

Fixed Assets:
- asset register and deterministic depreciation;
- transfer/disposal;
- accounting posting drafts.

Costing:
- cost centers and practical direct-material/direct-labor/overhead calculations;
- only add BOM/WIP/manufacturing extensions when demanded by concrete workflows.

Budgeting:
- budgets/scenarios;
- actual-vs-budget;
- forecasts as non-posting planning outputs.

**STOP GATE F6:** deterministic arithmetic and posting boundaries proven; forecasts cannot mutate accounting truth.

### Stage F7 — Integration Hub, Data Exchange & Desktop Bridge

Deliver:

- canonical connector contracts;
- Source-of-Truth configuration;
- external-object mapping/checkpoints/conflicts;
- API/Web Service transport;
- versioned file import/export profiles;
- governed Desktop/Computer Use bridge;
- dry-run/preview/approval/verify/audit;
- initial vendor adapters only where a verified capability is available.

Candidate vendors include Sepidar, Holoo and Parmis; do not fabricate capabilities that have not been verified.

**STOP GATE F7:** connector contract tests, duplicate/idempotency tests, UI/file/API failure-mode tests, and approval controls green.

### Stage F8 — Electronic Commercial Books

Deliver versioned compliance profiles, posted-ledger cutoff exports, manifests/hashes and compliance calendar.

**STOP GATE F8:** no readiness claim without a VERIFIED applicable profile/source.

### Stage F9 — Invoice Domain

Deliver business invoice drafts/versioning independent of government protocol payloads.

**STOP GATE F9:** schema/version/approval invalidation tests green; no production gateway dependency.

### Stage F10 — Moadian Verification & Simulator

Pin official technical sources, build active protocol profiles, fixtures, simulator, key-provider port, outbox and reconciliation.

**STOP GATE F10:** production gateway remains disabled until verified source/profile contract tests pass.

### Stage F11 — MCP / Agent Interface

Deliver the canonical harness-independent MCP surface:

- stdio;
- Streamable HTTP;
- auth/scopes;
- read/draft/review tools across implemented domains;
- no tenant/actor identity from model arguments;
- no model self-approval.

Thin harness adapters/plugins may be provided for compatible clients without duplicating domain logic.

**STOP GATE F11:** remote security, permission and cross-harness contract tests green.

### Stage F12 — Production Government/External Compliance Adapters

Activate only verified production adapters such as Moadian and other regulatory gateways.

**STOP GATE F12:** owner-controlled enablement, verified protocol/source, secret isolation, reconciliation and operational runbook.

### Stage F13 — Advisory, Automation & Decision Intelligence

Add:

- analytics;
- cash-flow and working-capital views;
- budget/forecast assistance;
- anomaly and reconciliation triage;
- optional DecisionProvider (including Jev-like providers);
- low-risk automation where explicitly enabled.

Advisory/decision models never become accounting or legal truth.

**STOP GATE F13:** calibrated use-case tests, auditability and mutation-policy tests green.


# 30. Coding-agent prohibitions

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
24. create speculative tables/classes for reserved future domains before their implementation stage;
25. duplicate accounting/legal/business logic inside harness plugins or Skills;
26. let Computer Use bypass preview/approval/verification/audit requirements;
27. treat an AI/Jev score as accounting, legal, authorization, or approval truth;
28. hard-code vendor UI/API behavior without a versioned verified capability profile;
29. silently auto-correct inventory, cheque, bank or external-system discrepancies.

---

# 31. Definition of Done for any critical feature

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

# 32. Source verification notes from the final review

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

