# ADR-0004: Complete Accounting Agent Operating Model Without Premature Complexity

- **Status:** Accepted
- **Date:** 2026-09-25
- **Architecture version:** 1.6.0-ARCH

## Context

HesabYar-AI is intended to become a practical Iranian accounting operations agent, not merely a ledger library or chat interface. The product must eventually cover the work normally split across an in-house accountant, accounting assistant, payroll operator, treasury clerk, inventory accountant, tax/compliance operator, and finance analyst.

The architecture already reserves deterministic Ledger, versioned legal/standards policy, Payroll, Moadian, MCP, document intake, and an Accounting Integration Hub. Product review identified additional accounting responsibilities that must be reserved now so later stages do not require redesigning the Ledger, tenant model, audit model, or integration contracts:

- treasury, cash, banks, payment gateways, and cheques;
- accounts receivable/payable and settlement tracking;
- inventory reconciliation and stock movements;
- fixed assets and depreciation;
- cost accounting;
- budgeting/forecasting;
- machine-readable and human-readable import/export;
- desktop/computer-use integration for legacy accounting software;
- optional AI/Jev-like decision providers for ambiguous low-risk judgments.

The risk in adding these requirements is over-engineering. The decision therefore reserves clear domain seams and minimum contracts now, while deferring detailed schemas, algorithms, and infrastructure until each implementation stage begins.

## Decision

### 1. Product role

HesabYar is an **Accounting Operations Agent**.

It may:
- read and reconcile accounting/operational data;
- detect missing, duplicated, stale, or conflicting records;
- prepare deterministic calculations and draft accounting actions;
- produce import/export artifacts;
- operate supported external systems through governed connectors;
- surface deadlines, exceptions, and review queues;
- provide advisory analysis.

It does not become the source of legal truth, arithmetic truth, identity, authorization, or approval merely because an AI model requested an action.

### 2. Progressive onboarding

Do not require the user to configure the entire enterprise on first run.

Minimum onboarding:
- tenant/company profile;
- fiscal year/date settings;
- currency/presentation preference;
- actor/approval roles;
- whether HesabYar or an external accounting package is the system of record.

Additional setup is activated only when the user enables the related capability:
- accounting software connection and COA mapping;
- bank accounts, cash boxes, POS/payment gateways;
- cheque handling;
- warehouses/inventory sources;
- payroll/employees;
- tax/compliance identifiers;
- import/export profiles;
- automation permissions.

Credentials are stored through a secret provider, not in prompts, business tables, or Skill files.

### 3. Reserved domain seams

Keep the modular monolith. These are modules/bounded contexts, **not microservices**.

#### Treasury & Settlement
Owns:
- bank/cash/payment-channel references;
- receipts/payments;
- cheque register and lifecycle;
- settlement allocation;
- bank/cheque reconciliation;
- due-date queues.

Cheques must support answering:
- has this cheque already been registered locally or in the configured external accounting system?
- is it duplicated?
- do amount, due date, bank/account, counterparty, and identifier agree?
- has the bank outcome changed while accounting status is stale?

AI may propose a match. Deterministic identifiers, amounts, dates, source records, and approval rules determine final state.

#### Accounts Receivable / Payable
Owns open-item settlement, customer/vendor balances, ageing and allocation.
It posts through Ledger contracts and does not maintain an independent accounting truth.

#### Inventory & Costing
Inventory owns warehouses, items, stock movements, counted stock and quantity reconciliation.
Costing consumes verified inventory/labor/overhead inputs and produces deterministic cost results/posting drafts.
Do not require manufacturing/WIP complexity for tenants that do not use it.

#### Fixed Assets
Owns asset register, acquisition, location/custodian, depreciation policy/version, transfer, impairment/disposal events and posting drafts.
Tax depreciation and accounting depreciation may use different versioned rule sources; do not collapse them into one timeless rate.

#### Budgeting & Planning
Owns versioned budgets, scenarios and plan-vs-actual reporting.
Budget/forecast data is planning truth, not posted Ledger truth.
Forecasts never mutate accounting records.

### 4. Reconciliation is a shared application capability, not a second ledger

Reconciliation compares records between authoritative sources and produces:
- exact match;
- suggested match;
- conflict;
- missing local;
- missing remote;
- duplicate;
- requires review.

Domain-specific matchers may exist for bank transactions, cheques, inventory, invoices and external accounting systems.
A reconciliation result never silently rewrites posted accounting truth.

### 5. Data exchange profiles are first-class

Support versioned import/export profiles behind a canonical data-exchange port.

Target formats may include:
- XLSX/XLS;
- CSV/TSV;
- JSON;
- XML;
- PDF reports;
- DBF where legally/vendor required;
- bank formats such as OFX/MT940/CAMT when a verified profile exists;
- vendor-specific import/export formats.

This is an extensible profile mechanism, not a requirement to implement every format in the first release.

Every profile declares:
- direction;
- entity types;
- schema/version;
- encoding/locale;
- validation rules;
- source/vendor;
- verification status where legally/vendor sensitive.

### 6. API, file exchange and Computer Use are peer integration transports

Do not treat desktop automation as an architectural hack. Some accounting products expose strong APIs, some expose reliable import/export files, and some require a local desktop workflow.

A connector manifest declares supported transports:
- API/Web Service;
- File Import/Export;
- Local Desktop/Computer Use.

Transport selection is capability-driven. Prefer the most deterministic supported mechanism for the requested action, but Desktop/Computer Use is a supported first-class transport when the vendor workflow requires it.

Computer Use mutation contract:

```text
OBSERVE
 -> PLAN
 -> PREVIEW
 -> APPROVAL (when write/high-impact)
 -> EXECUTE
 -> VERIFY RESULT
 -> AUDIT
```

Requirements:
- run through a tenant-scoped local Desktop Bridge;
- secrets stay in local/secret-provider context;
- screenshots/logs must redact sensitive values where practical;
- no blind click/type sequence without post-action verification;
- vendor UI selectors/workflows are versioned capability profiles;
- ambiguous UI state fails closed;
- a UI change may disable that capability without breaking Ledger/domain code.

### 7. Harness independence through MCP

HesabYar exposes one canonical MCP surface.

Business logic must not be duplicated for Hermes, Claude Code, Codex, Gemini, Cursor, or other harnesses.
Harness-specific packages/configuration are thin integration metadata only.

For clients that cannot consume the supported MCP transport, optional REST/CLI/SDK adapters may be added later without changing domain logic.

### 8. Optional Decision Intelligence provider

Ambiguous low-risk judgments may use a `DecisionProvider` port implemented by Jev-like models or general LLMs.

Examples:
- likely duplicate;
- likely bank-payment/invoice match;
- document classification;
- skill/workflow routing;
- anomaly triage;
- whether a reconciliation case should be escalated.

It must never determine:
- debit/credit equality;
- statutory tax/insurance arithmetic;
- legal applicability when a verified deterministic rule exists;
- tenant identity;
- authorization;
- approval of irreversible actions.

Decision outputs store provider/model/version, inputs hash, score/probability where available, and calibration/use-case identifier.
Thresholds are calibrated per workflow from real test data; do not use one global confidence threshold.

### 9. Human control and automation levels

Each tenant/capability may select an automation level:

- `READ_ONLY`
- `SUGGEST`
- `DRAFT`
- `EXECUTE_WITH_APPROVAL`
- `AUTO_LOW_RISK` only for explicitly allowed reversible workflows

High-impact accounting postings, payroll finalization, legal submissions, destructive inventory adjustments, and external financial mutations require explicit policy/approval even when Computer Use or an API can technically execute them.

### 10. Over-engineering guard

Do not create every reserved table/class now.

For a future domain:
1. reserve its ownership and integration contract in architecture;
2. implement it only when its stage starts;
3. start with the smallest model that satisfies concrete workflows;
4. add infrastructure only after a demonstrated requirement;
5. keep one PostgreSQL database and one modular-monolith codebase unless a later ADR proves a split is necessary.

No Kafka, Redis, event bus, workflow engine, vector database, separate microservice, or vendor-specific duplicate domain model is implied by this ADR.

## Consequences

### Positive
- complete product scope is visible before F1 implementation;
- Ledger and integration contracts should not require redesign when treasury, inventory, assets or budgeting arrive;
- legacy desktop accounting products are supportable without contaminating domain logic;
- import/export becomes a governed core capability;
- AI/Jev can reduce manual triage without becoming financial/legal truth;
- modules can be implemented incrementally.

### Trade-offs
- more reserved domain names exist before implementation;
- connector capability matrices require maintenance;
- Computer Use profiles need regression verification when vendor UIs change;
- legal/vendor format profiles add provenance work.

These trade-offs are accepted because detailed implementation remains deferred until the relevant stage.
