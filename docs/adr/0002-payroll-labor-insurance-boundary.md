# ADR-0002: Dedicated Bounded Context for Payroll, Labor, Social Insurance, and Salary Tax

- **Status:** Accepted
- **Date:** 2026-09-20

## Context

Labor law, social security, and salary tax rules in Iran present significant domain complexity and continuous statutory evolution:
1. **Annual Volatility:** Key parameters—including minimum daily wages, housing allowances (حق مسکن), grocery coupons / food allowances (بن خواربار), child allowances (حق اولاد), marriage allowances (حق تاهل), and seniority bases (پایه سنوات)—are revised annually by decrees of the Supreme Labor Council (شورای عالی کار) and the Council of Ministers (هیئت وزیران).
2. **Tax Brackets & Exemptions:** Article 84 and Article 85 of the Direct Tax Law (قانون مالیات‌های مستقیم) define progressive salary tax rates and annual exemption ceilings governed by annual State Budget Laws (قانون بودجه کل کشور).
3. **Complex Assessability Divergence:** In Iranian practice, earnings subject to Social Security insurance contributions (مشمول بیمه تامین اجتماعی) do not match earnings subject to salary tax (مشمول مالیات حقوق). For example, housing allowance and food vouchers have distinct exemption and assessability treatments across Social Security regulations and General Assembly of the Administrative Court (دیوان عدالت اداری) rulings. Conflating insurance assessability with taxability introduces severe compliance defects.
4. **Integration with the General Ledger:** Payroll is an operational sub-ledger. In traditional ERPs, payroll engines often mutate general ledger tables directly, bypassing accounting controls, period checks, and voucher authorization workflows.
5. **AI Agent Context Overhead:** Exposing an LLM or agent to monolithic tax and labor statutes wastes prompt tokens and risks hallucinations regarding changing annual thresholds.

## Decisions

### 1. Dedicated Bounded Context (`src/hesabyar/domain/payroll/`)
Isolate Payroll, Labor, Social Insurance, and Salary Tax into a dedicated bounded context within `src/hesabyar/domain/payroll/`. The domain encapsulates employee master records, employment contracts, timesheet attendance data, leave calculations, payroll run orchestration, social security assessment, salary tax withholding assessment, and official regulatory submission formats (Tamin diskette and Tax portal data).

### 2. Strict Architectural Separation: Ledger ↕ Payroll ↕ Legal Rule Registry
Enforce one-way, decoupled integration between boundaries:
- **Payroll does NOT mutate Ledger tables directly:** The Payroll context never executes direct SQL inserts or updates against `vouchers`, `voucher_lines`, or `accounts`.
- **Posting via Voucher Contract (`PayrollPosting`):** When a `PayrollRun` reaches the `APPROVED` state, a dedicated domain adapter constructs a balanced double-entry draft voucher (`VoucherDraft`) mapped to the Chart of Accounts (COA) and floating Tafsili dimensions. The Ledger context processes this voucher through its standard invariant verification, authorization, and posting pipeline.
- **Rules provided by Registry:** All labor thresholds, insurance contribution rates, and tax brackets are supplied as external immutable parameters from the Versioned Legal Rule Registry (`src/hesabyar/domain/tax_policy/` and `src/hesabyar/domain/payroll/`).

### 3. Zero Hardcoding of Annual Rial Amounts and Statutory Thresholds
Hardcoding annual monetary amounts (such as 1404 or 1405 minimum wage numbers, housing allowance rials, or tax brackets) in Python code, database schemas, or validation rules is strictly prohibited:
- All statutory figures must be loaded as effective-dated legal parameters keyed by `as_of_date` or period boundaries.
- Formulas consume parameter tokens rather than literal numeric constants.
- If a required parameter is missing or unverified for a given calculation period, the calculation engine fails closed with `PAYROLL_RULE_UNVERIFIED`.

### 4. Multi-Axis Payroll Component Matrix
Every earning and deduction item is governed by a multi-axis classification matrix. The system explicitly rejects any architectural assumption that `is_insurance_assessable == is_taxable`.
The matrix evaluates each component across independent orthogonal axes:
- `is_wage_base` (مزد شغل / پایه)
- `is_insurance_assessable` (مشمول بیمه سهم کارگر و کارفرما)
- `is_taxable` (مشمول مالیات بر درآمد حقوق)
- `is_overtime_base` (مبنای اضافه کاری)
- `is_night_base` (مبنای شب کاری)
- `is_shift_base` (مبنای نوبت کاری)
- `is_friday_base` (مبنای جمعه کاری)
- `is_severance_base` (مبنای محاسبه حق سنوات و مزایای پایان کار)
- `is_eidi_base` (مبنای محاسبه پاداش و عیدی سالانه)
- `is_leave_settlement_base` (مبنای بازخرید ایام مرخصی استفاده نشده)
- `is_employer_cost` (هزینه‌های تبعی و بالاسری کارفرما)

### 5. Versioned Legal Source Governance with Strict Verification Statuses
All labor law articles, Supreme Labor Council circulars, social security regulations, and budget laws must be registered with provenance metadata:
- Permitted verification statuses: `VERIFIED`, `CORROBORATED`, `DISCOVERY_ONLY`, `DISPUTED`, `RETIRED`, `PENDING_UPDATE`.
- Strict fail-closed policy: Production payroll calculations may only evaluate source versions marked `VERIFIED`.

### 6. Progressive Skill Loading Architecture for AI Agents
To prevent context saturation and maintain deterministic boundaries:
- Knowledge of labor law, social security circulars, and salary tax rules is packaged into modular, on-demand skills (e.g., `skills/iran-payroll-insurance/`).
- Agents load high-level indices and schema contracts on demand, avoiding monolithic context injections.
- Skill files provide reference indices, YAML parameter definitions, and CLI validation scripts. Skill scripts must NEVER contain business calculation engines; calculation logic resides exclusively in the domain layer.

### 7. Updated Implementation Roadmap (Stages F1 through F9)
The sequence of implementation is updated to establish the Payroll bounded context at Stage F3, immediately following the core Ledger (F1) and Legal Rule Registry (F2):
- **Stage F0:** Foundation (Frozen at commit `76c26f2f188310bcc178d9e955e4faf0706fcbef`)
- **Stage F1:** Accounting Ledger (Double-entry, COA, Tafsili, Vouchers, Trial Balance)
- **Stage F2:** Versioned Legal & Accounting Rule Registry (Sources, Revisions, DSL)
- **Stage F3:** Payroll, Labor, Social Insurance & Salary Tax Context
- **Stage F4:** Electronic Commercial Books (Export profiles, compliance calendar)
- **Stage F5:** Invoice Domain (Drafts, protocol schemas, validations)
- **Stage F6:** Moadian Protocol Verification & Simulator
- **Stage F7:** MCP & Agent Interface (Official MCP SDK v2, Streamable HTTP)
- **Stage F8:** Production Moadian Gateway Adapter
- **Stage F9:** Advisory, Analytics & AI Workflows

## Consequences

### Positive
- **Domain Decoupling:** Core accounting ledger invariants remain pure and completely unpolluted by labor laws, attendance rules, or employee contract nuances.
- **Historical Reproducibility:** Because annual parameters, tax brackets, and component matrices are versioned with effective dates, historical payroll runs can be re-executed or audited with 100% bit-for-bit fidelity.
- **Auditability and Dual Control:** Separation of draft calculations from ledger posting guarantees that payroll runs must undergo explicit review and dual approval before affecting the general ledger.
- **Regulatory Agility:** New annual wage decrees or Supreme Court verdicts (e.g., ruling on tax-exempt allowances) can be adopted by adding versioned configuration records without modifying core calculation code.
- **Agent Efficiency:** AI agents consume compact, structured skill references rather than attempting to memorize massive legal compendia.

### Negative / Trade-offs
- **Additional Metadata & Schema Overhead:** Requires maintaining explicit component matrices, parameter schemas, and source snapshots.
- **Two-Step Posting Coordination:** Creating a payroll run does not instantaneously update account balances; it requires an explicit posting command that crosses the context boundary via a draft journal voucher.
- **Strict Data Dependencies:** An unverified or missing legal parameter halts payroll finalization, requiring administrative resolution before monthly payroll can be closed.
