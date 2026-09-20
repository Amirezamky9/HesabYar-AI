---
name: hesabyar-iran-accounting
description: >
  Use for Iranian accounting and HesabYar-AI work: accounting-standard lookup,
  financial-statement validation, period-aware applicability, Iranian tax-policy
  analysis, Moadian/electronic-invoice work, and electronic commercial-books
  compliance. Always resolve the requested accounting/tax period first, use the
  applicable verified source/revision, and fail closed when the current source
  is missing or unverified. For 1405 periods, enforce the current-source gates
  documented in docs/CURRENT_RULES_1405.md and docs/ARCHITECTURE.md.
---

# HesabYar — Iranian Accounting & Compliance

Treat this file as the Skill control plane, not as the legal/standard knowledge dump.

## Required workflow

1. Determine the relevant **financial period / performance year / filing year / as-of date**.
2. Read `metadata.json` and select only the standard revision applicable to that period.
3. For 1405/current compliance work, read `docs/CURRENT_RULES_1405.md`.
4. For implementation behavior, read `docs/ARCHITECTURE.md`; it is the normative architecture.
5. Load only the relevant `standards/<slug>/SKILL.md` and source text.
6. For tax/Moadian/electronic-books questions, require a source-backed effective-dated rule/profile.
7. If a required current source is blocked, missing, disputed, or unverified, return that status instead of substituting an older rule.

## Current applicability gates

- **Standard 43 — Revenue from Contracts with Customers:** applicable to periods beginning 1404/01/01 and later; it replaces Standards 3, 9 and 29 for those periods.
- **Standard 44 — Leases:** applicable to periods beginning 1405/01/01 and later; it replaces Standard 21 for those periods.
- **Standard 15 — Investments:** the repository's bundled source is legacy. For periods beginning 1405/01/01 and later, do **not** use it as current authority until the official revised-1404 source is imported and verified.
- **Standard 35 — Income Taxes:** use for income-tax/deferred-tax accounting; do not route deferred-tax questions to Standard 22.
- Annual tax rates, exemptions, Article 100 thresholds, Article 6 parameters and deadlines are **not timeless constants**.

## Validation rules

- Never use LLM arithmetic as a financial control.
- Never treat `NOT_APPLICABLE` as `PASS`.
- Mandatory-rule evaluation failure is `ERROR`, not `PASS`.
- Never execute arbitrary Python/YAML expressions from accounting rules.
- Return the source/revision/version used for every standards/tax compliance conclusion.

## Trust boundaries

- OCR/document extraction creates candidates, never posted accounting truth.
- Research notes and examples are explanatory only.
- A current web result does not become executable law until the project source registry marks its snapshot `VERIFIED`.
- Moadian TaxID/crypto/schema/endpoints must come from an active verified protocol profile.
- Electronic commercial-book exports use versioned export profiles and compliance-calendar deadlines.

## Repository navigation

- `docs/ARCHITECTURE.md` — implementation authority.
- `docs/CURRENT_RULES_1405.md` — current-year evidence/status overlay.
- `metadata.json` — period-aware standards catalog.
- `standards/` — per-standard summaries and bundled source texts.
- `validators/` — legacy validation rules; safe typed DSL migration is required by architecture.
- `mappings/` — informational crosswalks; never use IFRS equivalence as proof of identical Iranian requirements.

## Output discipline

For any current-period accounting/tax answer include, where applicable:

- applicable period;
- standard/rule revision;
- verification status;
- source/provenance;
- missing evidence or blocked source;
- whether professional/manual review is required.
