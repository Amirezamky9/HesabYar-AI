---
name: iran-payroll-insurance
description: >
  Use for Iranian payroll, labor law, social security, and salary tax compliance:
  employment contracts, attendance and timesheet rules, leave calculations (Labor Law Arts. 64-74),
  multi-axis component matrix (earnings, deductions, employer contributions), Social Security
  insurance assessments, progressive salary tax withholding (Direct Tax Law Arts. 84-86),
  Tamin DBF diskettes, and tax portal export. Always resolve the calculation period and
  effective-dated legal parameters first. Fail closed if required statutory parameters are
  missing or unverified. Never assume insurance assessability equals taxability.
---

# Iran Payroll, Labor & Insurance Compliance

This skill serves as the **operational and compliance control plane** for Iranian payroll, labor law, social security insurance, and salary tax withholding within HesabYar-AI.

> **Crucial System Boundary:**  
> This skill provides reference indices, statutory rules, parameter schemas, and validation tooling.  
> It does **NOT** perform financial arithmetic or database mutations. All domain calculations are executed by deterministic Python engines in `src/hesabyar/domain/payroll/`.

---

## 1. Trigger Conditions & Routing Rules

Invoke this skill when:
- Designing, reviewing, or validating employment contracts, probationary periods, or termination terms under the Iranian Labor Law.
- Configuring or checking the 11-axis **Payroll Component Matrix** (ماتریس عوامل حقوق و دستمزد).
- Auditing Social Security assessments (مشمول بیمه تامین اجتماعی: سهم بیمه‌شده، سهم کارفرما، بیمه بیکاری).
- Calculating or reviewing progressive salary tax withholding under Articles 84, 85, and 86 of the Direct Tax Law (مالیات حقوق).
- Preparing or validating regulatory submission artifacts:
  - Tamin monthly diskettes (`DSKWOR00.DBF` and `DSKKAR00.DBF`).
  - INTA monthly salary tax portal export lists.
- Auditing annual statutory parameters (minimum daily wage, housing allowance, grocery coupon, tax brackets) for Solar years (1404, 1405, etc.).
- Verifying legal source provenance and gazette decree status.

---

## 2. Core Invariants & Architectural Directives

When interacting with payroll domain logic, agents MUST enforce these non-negotiable rules:

### 1. The Independence Ban
**Never infer or assume `is_insurance_assessable == is_taxable`.**  
In Iranian labor and tax jurisprudence, insurance assessability and taxability diverge significantly:
- Grocery coupon (بن خواربار) and Housing allowance (حق مسکن) are assessable for Social Security, but exempt from salary tax under General Assembly of the Administrative Court verdicts (Ruling No. 1957 and Circular 200/1401/10).
- Child allowance (حق اولاد / کمک عائله‌مندی) is exempt from Social Security contributions (Social Security Law Art. 86 as amended by Family Support Laws) but is taxable for salary income tax.
- Every earning component MUST be evaluated against its independent flags and statutory rule references in the component matrix.

### 2. Zero Hardcoding Rule
- No monetary rial amounts or tax bracket thresholds may be hard-coded in code, prompts, or schemas.
- All figures must be loaded as effective-dated parameters keyed by the period's Gregorian/Jalali dates from `annual-*.yaml`.

### 3. Decoupled Ledger Posting
- Payroll is an operational sub-ledger. It **never** writes directly to Ledger database tables.
- Approved payroll runs emit a balanced double-entry `PayrollPosting` voucher draft to the Ledger context via port interfaces.

### 4. Fail-Closed on Unverified Rules
- If a statutory parameter (e.g., annual housing allowance decree) is unverified or marked `PENDING_UPDATE`, calculations must fail closed with `PAYROLL_RULE_UNVERIFIED`.
- Never substitute an informal blog post or unverified news report for an official Cabinet decree.

### 5. Dual-Control Authorization
- A `PayrollRun` cannot transition to `APPROVED` or be posted to the Ledger without two distinct authorized actor approvals (`PayrollApproval`).

---

## 3. Reference Pointers & Progressive Disclosure

Consult these dedicated reference files on demand:

| Topic | Reference Path | Purpose |
|---|---|---|
| **Labor Law** | `references/labor-law-index.md` | Articles 34–47 (wages), 51–63 (hours, overtime 140%, shifts, night 135%), 64–74 (leave, accrual, carryover), 21–33 (severance), Eidi law. |
| **Social Security** | `references/social-security-index.md` | Articles 28, 36, 39, 7× ceiling, assessable wage floor, DBF diskette specifications (`DSKWOR00`, `DSKKAR00`). |
| **Salary Tax** | `references/salary-tax-index.md` | Articles 82–86, 2/7th health insurance deduction, Administrative Court welfare exemption rulings, progressive brackets. |
| **Component Matrix** | `references/payroll-components.yaml` | Machine-readable 11-axis classification for 17 standard Iranian payroll items. |
| **Annual Parameters** | `references/annual-1405.yaml` | Parameterized schema for Solar year 1405 statutory rates and thresholds. |
| **Source Status** | `references/source-status.md` | Audit registry of official decrees, gazette numbers, verification statuses, and cryptographic hashes. |

---

## 4. Verification & Diagnostic Tooling

The skill provides standalone CLI utilities in `scripts/`:

```bash
# Validate cryptographic integrity of reference source snapshots
uv run python skills/iran-payroll-insurance/scripts/validate_sources.py

# Validate schema and constraints of annual parameter files
uv run python skills/iran-payroll-insurance/scripts/validate_parameters.py --all

# Inspect statutory parameter deltas between consecutive solar years
uv run python skills/iran-payroll-insurance/scripts/diff_annual_rules.py --from-year 1404 --to-year 1405

# Audit freshness of legal sources and warn on pending decrees
uv run python skills/iran-payroll-insurance/scripts/check_source_freshness.py --as-of-date 2026-09-20
```

*Note: These scripts are strictly for validation and integrity checking. They do not execute business calculations.*
