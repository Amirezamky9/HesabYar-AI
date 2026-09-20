# HesabYar-AI — Implementation Foundation v1.2

**Status:** NORMATIVE / IMPLEMENTATION GATE  
**Date:** 2026-09-20  
**Supersedes:** conflicting implementation guidance in `docs/ARCHITECTURE.md` v1.1.0-R0 and `docs/research_tax_inflation.md`.

## 1. Why this baseline exists

The repository began as a comprehensive Iranian accounting-standards skill and the v1.1 architecture expanded it into a transactional accounting/tax MCP system. The expansion is directionally sound, but the current architecture mixes:

- stable accounting rules,
- time-sensitive tax/legal rules,
- assumptions about external government APIs,
- advisory analytics,
- and irreversible write operations.

Those concerns MUST NOT be compiled into one undifferentiated deterministic engine.

This document is the implementation contract for coding. If another repository document conflicts with this file, **this file wins until a later ADR explicitly replaces it**.

## 2. Non-negotiable corrections before implementation

### 2.1 Legal knowledge is versioned data, not hard-coded truth

Do not hard-code tax rates, thresholds, deadlines, legal interpretations, invoice templates, banking thresholds, or Article 6 behavior into domain code.

Every legal rule must carry:

```text
rule_id
jurisdiction
authority
source_uri
source_title
published_at
effective_from
effective_to (nullable)
retrieved_at
version
status = verified | disputed | provisional | expired
confidence
supersedes (nullable)
```

A calculation that depends on a legal rule MUST return the rule/version used.

### 2.2 Correct known research errors

The following v1.1 statements are prohibited as implementation facts:

1. **Tax-loss carryforward must not be implemented as “Article 140”.** The engine must model it as a configurable legal rule and use the currently verified legal basis (including Article 148(12) where applicable).
2. **Iranian Accounting Standard 35 is income taxes. Standard 22 is interim financial reporting.** Deferred-tax logic belongs under Standard 35.
3. **Article 274 must not be encoded as an “eight-item” enum.** Store verified statutory offences as versioned legal data.
4. **The 5-billion-toman banking figure must not become a safe-harbour rule.** Bank inflows are evidence requiring classification; an amount threshold alone cannot classify a receipt as taxable income.
5. **The 100-inflows / 35-million-toman account heuristic must not block transactions or assert tax liability.** At most it may be a versioned compliance signal after verification for the relevant period.
6. **Article 138 bis must not be modeled as automatic principal × assumed 23% deduction.** Eligibility, participatory-contract structure, actual/allowable return, retention period, and documentary evidence are separate predicates.
7. **“Phantom profit / working-capital meltdown” is advisory analysis, not an accounting posting rule.** It may produce scenarios and warnings only.
8. **No current VAT rate or annual threshold may be embedded as a timeless default.**

### 2.3 External Moadian protocol is unverified until contract-tested

The exact TaxID composition, checksum, cryptographic envelope, endpoint URLs, invoice pattern schemas, error codes, and sandbox availability in v1.1 are **assumptions until validated against current official technical specifications and captured as fixtures**.

Therefore:

- production transmission is disabled by default;
- `NativeMoadianClient` cannot be considered complete from architecture prose;
- no production endpoint may be called in unit/CI tests;
- protocol fixtures must be traceable to an official specification version;
- a simulator may reproduce only verified behavior and must never label invented error codes as official.

## 3. Target architecture

Use a **modular monolith with ports/adapters** for R0. Do not introduce microservices.

```text
LLM / MCP client
      |
      v
+---------------------------+
| MCP Application Boundary  |
| auth / tenant / approval  |
+-------------+-------------+
              |
      command/query DTOs
              |
+-------------v-------------+
| Application Services      |
| use-cases + transactions  |
+---+-----------+-----------+
    |           | 
    v           v
Accounting   Compliance/Tax
Domain       Policy Engine
    |           |
    +-----+-----+
          |
       Ports
  +-------+--------+----------------+
  |                |                |
LedgerRepo   LegalRuleRepo    MoadianGateway
  |                |                |
SQLite       Versioned DB      Simulator first
Postgres     + source refs      Production opt-in

Separate read-only services:
- Standards knowledge/retrieval
- Document parsing/OCR
- Scenario analytics
```

### Dependency rule

Domain modules MUST NOT import FastMCP, HTTP clients, SQLite, OCR, or LLM SDKs.

MCP tools call application services. Application services depend on protocols/ports. Adapters implement those ports.

## 4. Bounded contexts

### A. Accounting Ledger — deterministic, transactional

Owns:
- chart of accounts,
- floating tafsili dimensions,
- vouchers and voucher lines,
- posting periods,
- trial balance,
- immutable posted entries,
- reversals/adjustments.

Hard invariants:
- IRR stored as integer;
- debit == credit per voucher;
- posted vouchers are immutable;
- correction is reversal + replacement, never destructive edit;
- DB constraints enforce invariants in addition to Python validation;
- voucher numbering is unique per tenant/fiscal year;
- posting to closed periods is rejected.

### B. Financial Reporting & Standards — deterministic + versioned knowledge

The existing 35-standard corpus remains a separate bounded context.

The current YAML validator can be reused only after two changes:
- **fail closed:** an unevaluable mandatory formula is an error, never a pass;
- every rule carries provenance/version metadata.

The current behavior in `scripts/validator.py` that returns a passing result when a formula cannot be evaluated MUST be removed before relying on the validator for compliance.

### C. Tax Policy — versioned decision support

Owns:
- VAT rules,
- Article 6 policies,
- tax-loss rules,
- exemptions/rate-zero rules,
- R&D credit eligibility,
- Article 138 bis eligibility,
- annual Article 100 thresholds.

It does **not** own accounting postings.

Required API:

```python
evaluate(policy_id, facts, as_of_date) -> PolicyDecision
```

`PolicyDecision` includes:
- outcome,
- calculation,
- assumptions,
- missing_evidence,
- legal_rule_versions,
- warnings,
- requires_professional_review.

No decision may silently substitute a current rule for the requested historical date.

### D. Moadian Integration — external side effect

State machine:

```text
DRAFT -> VALIDATED -> APPROVED -> QUEUED -> SUBMITTED
                                  |          |
                                  v          v
                                FAILED    ACCEPTED/REJECTED
```

Rules:
- `validate` never transmits;
- `approve` records actor/time/hash;
- `submit` requires an approval for the exact payload hash;
- retries reuse an idempotency key and may not create a new economic invoice accidentally;
- remote response and local state are reconciled asynchronously;
- transmission log is append-only.

### E. Document Intake — untrusted input

PDF/image/OCR output is never ledger truth.

Pipeline:

```text
raw document -> extracted candidate -> validation -> human/agent review -> approved draft -> posting
```

Persist:
- source file hash,
- parser name/version,
- extraction confidence,
- field-level provenance,
- reviewer/approval.

### F. Advisory Analytics — non-posting

Inflation scenarios, tax-shield suggestions, anomaly/risk indicators and cash-flow simulations live here. Outputs are recommendations/scenarios and cannot mutate ledger or submit tax documents.

## 5. Multi-tenant and security model

R0 MUST be tenant-aware even if the first deployment has one company.

Every business table includes `tenant_id`. Sensitive resources are scoped by tenant.

Roles:
- `viewer`: read/report
- `accountant`: create drafts and vouchers
- `reviewer`: approve postings
- `tax_operator`: approve tax payloads
- `admin`: configuration/key rotation

Production Moadian submission requires an explicit privileged action. An LLM may prepare and validate a payload but MUST NOT gain implicit authority to transmit it.

Secrets:
- never store private keys or tokens in SQLite business tables;
- use environment/secret-store references;
- logs must redact tokens, keys, national identifiers where unnecessary;
- key access is isolated behind a `KeyProvider` port.

Remote MCP transport must have authentication, authorization, TLS, request limits, and audit logging. Do not expose unauthenticated SSE/HTTP on a public interface.

## 6. Persistence model

Use migrations from day one. Recommended implementation: SQLAlchemy 2 + Alembic; SQLite for local/single-user, PostgreSQL adapter later.

Minimum tables:

```text
tenants
users / actors
fiscal_years
account_groups
accounts
tafsilis
account_tafsili_links
vouchers
voucher_lines
posting_periods

legal_sources
legal_rules
legal_rule_versions
policy_decisions

invoice_drafts
invoice_lines
invoice_approvals
moadian_transmissions
moadian_status_events

documents
document_extractions
audit_events
idempotency_keys
```

Do not store mutable “current status history” only in one column. Keep append-only status/audit events and derive current state.

### Concurrency

SQLite WAL does not remove write races. Serial/tax identifiers and voucher numbers must be allocated transactionally with unique constraints and retry-on-conflict. Never implement `MAX(serial)+1` without a protected allocator.

## 7. Money, dates and calculations

- IRR: signed 64-bit integer or arbitrary-precision integer at domain boundary.
- Never use binary `float` for money, VAT, FX or quantity × price calculations.
- Use `Decimal` for rates, quantities and FX; rounding mode must be explicit per rule.
- Store canonical Gregorian UTC timestamps for events.
- Store fiscal/local dates separately where legally required.
- Jalali conversion is presentation/domain-date logic, not the sole persisted timestamp.

The current v1.1 Pydantic examples using `float` for quantity, VAT rate and foreign-currency amounts are not acceptable for production financial calculations.

## 8. API/MCP contract

Separate **queries**, **draft commands**, and **irreversible commands**.

Examples:

Read-only:
- `trial_balance`
- `audit_financial_statements`
- `evaluate_tax_policy`
- `validate_moadian_invoice`
- `get_moadian_status`

Draft/write-local:
- `create_voucher_draft`
- `post_voucher`
- `create_invoice_draft`

External side effect:
- `approve_invoice_submission`
- `submit_approved_invoice`

Every response uses a typed envelope:

```json
{
  "status": "success|warning|error|pending",
  "data": {},
  "messages": [],
  "provenance": [],
  "request_id": "...",
  "error": null
}
```

Do not make rendered Markdown part of the canonical data contract. Rendering belongs at the presentation boundary.

## 9. Idempotency and audit

All mutating commands require:
- `request_id`,
- actor,
- tenant,
- payload hash.

External submissions additionally require an idempotency key.

Audit events record:
- who,
- what command,
- object id,
- before/after hashes where relevant,
- policy/rule versions,
- timestamp,
- correlation/request id.

Audit data must not claim cryptographic non-repudiation merely because SHA-256 hashes are stored. Strong non-repudiation requires an appropriate trust/signature model.

## 10. Error policy: fail closed

For financial/tax-critical operations:
- missing required evidence => `NEEDS_EVIDENCE`, not guessed;
- unknown legal version => `RULE_UNVERIFIED`;
- formula cannot be evaluated => validation failure;
- external response ambiguous => `PENDING_RECONCILIATION`;
- unsupported invoice template => reject before submission.

LLM-generated values never fill required legal/financial fields silently.

## 11. Testing gates

### Gate A — existing baseline
Keep existing validator tests passing while refactoring.

### Gate B — accounting invariants
Property tests for:
- every posted voucher balances,
- no mutation of posted voucher,
- reversal nets original,
- closed period rejects posting,
- tenant isolation.

### Gate C — policy engine
Golden tests by `as_of_date`. Each legal-rule fixture includes source/version.

### Gate D — Moadian
- official protocol fixtures where legally/publicly available;
- crypto known-answer tests;
- idempotent retry tests;
- state-machine tests;
- simulator contract tests;
- production adapter disabled in CI.

### Gate E — security
- authorization tests for every mutating tool;
- secret redaction tests;
- tenant escape tests;
- malicious document/parser input tests.

### Gate F — end-to-end
```text
document -> draft -> review -> balanced posting -> invoice draft
-> policy validation -> approval -> simulator submission -> reconciliation
```

No production submission is part of automated tests.

## 12. Implementation sequence

### Phase 0 — foundation
1. Create `pyproject.toml` and package `src/hesabyar/`.
2. Add SQLAlchemy/Alembic migrations.
3. Move/re-wrap existing validator behind a `StandardsValidator` port.
4. Change validator unknown/unevaluable rules to fail closed.
5. Introduce typed money/date/value objects.

### Phase 1 — ledger
Implement tenant, fiscal year, COA, tafsili, vouchers, posting, reversal, trial balance and invariant tests.

### Phase 2 — legal-rule registry
Implement source/rule/version schema and policy evaluation with historical `as_of_date`.

### Phase 3 — invoice domain
Implement draft schemas and state machine **without network transmission**.

### Phase 4 — Moadian simulator + verified protocol
Implement only fields/crypto verified against a pinned official spec. Capture fixtures and provenance.

### Phase 5 — MCP
Expose read-only tools first, then local writes, then separately gated external submission.

### Phase 6 — advisory modules
Add inflation/tax-shield scenarios only after accounting and policy foundations are stable.

## 13. Definition of “ready to code”

Coding may start when these conditions are accepted:

- this document is the normative baseline;
- unverified tax research is not treated as executable truth;
- Moadian production protocol is behind a verification gate;
- the ledger is implemented before advisory automation;
- money uses integer/Decimal, never float;
- migrations, tenant scope, authorization, idempotency and audit are foundation concerns;
- external tax submission requires explicit approval;
- validator fails closed.

**Architecture state after this review: `READY_FOR_FOUNDATION_IMPLEMENTATION`.**
