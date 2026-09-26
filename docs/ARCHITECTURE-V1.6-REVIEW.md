# Architecture v1.6.0 Review Record

**Architecture:** HYA-A1-FINAL  
**Reviewed version:** 1.6.0-ARCH  
**Date:** 2026-09-26  
**State:** REVIEWED — awaiting CI / final merge decision  
**Frozen implementation baseline:** F0 `76c26f2f188310bcc178d9e955e4faf0706fcbef`

## Review objective

Ensure the architecture can grow into a practical, complete accounting operations agent without requiring a redesign of the Ledger/Foundation, while explicitly avoiding premature infrastructure and speculative domain implementation.

## Review 1 — completeness and contradiction audit

Verified that the normative architecture now reserves explicit boundaries for:

- Ledger;
- Treasury, bank, cash, payment channels and cheques;
- AR/AP and open-item settlement;
- Inventory and reconciliation;
- Fixed Assets and depreciation;
- Cost Accounting;
- Budgeting/Forecasting;
- Payroll/Labor/Social Insurance/Salary Tax;
- Versioned legal/accounting rules;
- Electronic Commercial Books;
- Invoice Domain;
- Moadian;
- Document Intake;
- Accounting Integration Hub;
- Data import/export profiles;
- API/File/Desktop Computer Use transports;
- shared Reconciliation;
- MCP/harness interoperability;
- optional Decision Intelligence.

The duplicated legacy roadmap was removed. Section 28 is the only authoritative roadmap.

Previous v1.4 security/quality requirements remain explicit, including:

- `uv sync --frozen`;
- single Alembic head;
- tenant identity from trusted context;
- cross-tenant DB attack tests;
- optimistic concurrency;
- idempotency;
- transactional outbox;
- host/origin controls;
- endpoint allowlisting;
- PII/secrets controls;
- malicious document tests;
- no production network in CI;
- MCP SDK v2 / stdio / Streamable HTTP;
- Moadian profile verification.

## Review 2 — legal-change resilience and deterministic truth

Removed timeless Payroll execution constants from architecture.

Payroll/social-insurance/salary-tax calculations now resolve:

```text
period
 -> VERIFIED source version
 -> effective legal rule version
 -> effective component classification
 -> effective parameters
 -> deterministic calculation
 -> source-backed trace
```

Rates, ceilings, brackets, deadlines and component tax/insurance treatment are not architecture constants.

Payroll account posting uses semantic account roles mapped to each tenant's COA instead of hard-coded account numbers/rates.

A future legal change should normally require a new source/rule/parameter version + tests, not a rewrite of the stable Payroll engine.

## Review 3 — over-engineering and implementation practicality

Explicitly rejected premature:

- microservices;
- Kafka;
- Redis;
- independent event brokers;
- generic workflow engines;
- vector databases merely for architecture symmetry;
- speculative future-domain tables/classes;
- duplicated business engines inside Skills or harness plugins.

Reserved domains are implemented only when their stage begins and only from concrete user workflows.

Integration transport is capability-driven:

```text
API/Web Service
File Import/Export
Desktop/Computer Use
```

Computer Use is first-class where necessary, but mutations require observe/plan/preview/approval-policy/execute/verify/audit.

Payroll approval is tenant/risk-policy driven instead of forcing two artificial roles on every small business.

Costing does not force BOM/WIP/manufacturing complexity unless a real tenant workflow needs it.

DataExchange supports extensible profiles rather than implementing every file format upfront.

DecisionProvider/Jev-like systems are optional judgment helpers only; they cannot become accounting/legal/authorization truth.

## Canonical implementation roadmap

- F0 — Foundation [FROZEN]
- F1 — Accounting Ledger
- F2 — Versioned Legal & Accounting Rule Registry
- F3 — Treasury, Banking, Cheques & AR/AP
- F4 — Payroll, Labor, Social Insurance & Salary Tax
- F5 — Inventory & Reconciliation
- F6 — Fixed Assets, Costing & Budgeting
- F7 — Integration Hub, Data Exchange & Desktop Bridge
- F8 — Electronic Commercial Books
- F9 — Invoice Domain
- F10 — Moadian Verification & Simulator
- F11 — MCP / Agent Interface
- F12 — Production Government/External Compliance Adapters
- F13 — Advisory, Automation & Decision Intelligence

## Current implementation boundary

No F1+ production-domain implementation is authorized by this review.

The architecture additions reserve boundaries and contracts only.

The existing F0 baseline remains immutable except for separately justified bug/security corrections.

## Final freeze conditions

Before marking v1.6.0-ARCH as frozen:

1. GitHub Actions for the final architecture head must be green.
2. Payroll legal-source/parameter validators must remain green.
3. No unintended `src/` implementation diff may exist.
4. Architecture/ADR links must resolve.
5. No second conflicting implementation roadmap may reappear.
6. Final architecture head SHA must be pinned in the handoff.

