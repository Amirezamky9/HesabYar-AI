# Tax-Policy Registry (US-402)

The effective-dated, versioned tax-policy registry (design-eng spec
§7.4, §12.2; `docs/CURRENT_RULES_1405.md` §3, §12).

## What lives where

| Piece | Location |
|---|---|
| Tables | Alembic `0006`: `tax_sources`, `tax_policy_versions`, `tax_decisions` |
| ORM models | `src/hesabyar/infrastructure/persistence/postgres/models.py` |
| Seed (1405 candidates) | `src/hesabyar/infrastructure/persistence/postgres/seed_tax_policy.py` |
| Domain models | `src/hesabyar/domain/tax_policy/models.py` |
| Repository port | `src/hesabyar/domain/tax_policy/ports.py` (`TaxPolicyRepositoryPort`) |
| SQLAlchemy repository | `src/hesabyar/infrastructure/persistence/postgres/tax_policy_repository.py` |
| Query service | `src/hesabyar/application/queries/tax_policy.py` (port-injected, spec §6.3) |
| Decision service | `src/hesabyar/application/commands/tax_policy.py` (port-injected, spec §6.3) |
| REST | `src/hesabyar/interfaces/rest/routers/tax_policy.py` (+ `schemas/tax_policy.py`) |

## The rule: no timeless constants

An annual VAT rate, exemption or threshold is never a hard-coded
constant in code. Every figure lives on a `tax_policy_versions` row
with:

- `parameter_value` (exact `Numeric`, never a binary float) +
  `value_unit` (`percent` — the figure as written, 10 means 10% — or
  `irr`);
- `rule_year`, `performance_year` and `filing_year` as **separate**
  persisted fields — a 1405 rule may govern performance year 1404 and
  be filed in 1405, so no value is ever labelled merely "1405";
- `effective_from` / `effective_to` (Gregorian, inclusive both ends);
- `verification_status` (the `CURRENT_RULES_1405.md` §1 labels, plus
  `UNVERIFIED`, shared with the standards registry);
- full provenance: primary source FK, `source_authority`,
  `source_url`, `snapshot_uri`, `sha256`, `corroboration_urls`,
  `verified_by` / `verified_at`.

The seed module is the only place the figures appear as data (that is
what the registry *is*); tests assert the same figures as expected
values. A repo-wide grep confirms no other module hard-codes an annual
parameter.

## Seeding

```bash
# Against the configured database (HESABYAR_DATABASE_URL):
python -m hesabyar.infrastructure.persistence.postgres.seed_tax_policy
```

The seed is idempotent (upserts by `source_key` and
`(rule_key, version_id)`). It loads the five 1405 parameter
candidates of `CURRENT_RULES_1405.md` §3 — all
`CORROBORATED_CURRENT`, effective 1405/01/01–1405/12/29
(2026-03-21–2027-03-20):

| Rule | Value | Rule / perf. / filing year |
|---|---|---|
| `vat_general_rate` | 10% | 1405 / 1405 / 1405 |
| `art84_salary_exemption` | 4,800,000,000 IRR | 1405 / 1405 / 1405 |
| `art101_exemption` | 2,800,000,000 IRR | 1405 / 1405 / 1405 |
| `art100_flat_tax_ceiling` | 720,000,000,000 IRR | 1405 / **1404** / 1405 |
| `growth_threshold_art105_131` | 45% | 1405 / **1404** / 1405 |

…and the three Tasnim corroboration sources (`tasnim-budget-1404-11-25`,
`tasnim-art100-implementation-1405-06-12`,
`tasnim-art100-announcement-1405-02-28`). Each source row notes that
it is corroboration, not the official legal text, and carries no
`sha256` until an official snapshot is pinned.

## The production gate

A version that is not `VERIFIED_TEXT` — `CORROBORATED_CURRENT`,
`UNVERIFIED`, `DISCOVERY_ONLY`, `BLOCKED_SOURCE_GAP` or
`TRANSITIONAL` — must never drive production use
(`CURRENT_RULES_1405.md` §12). The decision service enforces this:

- `resolve_decision(usage_context="production")` with a
  non-`VERIFIED_TEXT` version raises `TAX_POLICY_SOURCE_BLOCKED`
  (HTTP 422), naming the missing official source pin, and records
  **no** decision row.
- `usage_context="reference"` (the default) resolves any active
  version, but the decision records the version's verification status,
  so callers can see the parameter is unverified.

Production activation requires the §12 promotion checklist: official
source acquired, immutable snapshot stored, SHA-256 recorded,
effective dates modelled, status set to `VERIFIED`, golden tests
created, reviewer activation. Until then every seeded 1405 parameter
is reference-only.

## Resolution

For a `(rule_key, decision_date)`, the decision service picks the
newest-effective `active` version whose effective range covers the
date (inclusive both ends; `NULL effective_to` is open-ended). A
superseded version is never resolved. A date no version covers —
historical, future, or an unknown rule — raises
`TAX_POLICY_NOT_FOUND` (HTTP 404); there is no silent fallback to
another year's parameter.

Each decision snapshots the resolving version (row id, version id,
parameter value/unit, verification status, source key) into
`tax_decisions`, so it stays auditable even if the version row is
later deleted (`policy_version_id` is `SET NULL`) or superseded.
Decisions are tenant-scoped; `company_id` is an optional plain
reference (row id or engine id), like on validation runs.

## API

Scopes: `tax:read`, `tax:decide` (added to the §14.2 catalog in
`infrastructure/auth/scopes.py`).

- `GET /api/v1/tax-policy/sources` — list sources (sort
  `source_key` / `title`; cursor pagination).
- `GET /api/v1/tax-policy/sources/{source_key}` — one source with its
  versions; unknown key → 404 `TAX_SOURCE_NOT_FOUND`.
- `GET /api/v1/tax-policy/versions` — filters `rule_key`,
  `verification_status`, `effective_on`, `rule_year`,
  `performance_year`, `filing_year`; sort `rule_key` /
  `effective_from`; cursor pagination.
- `GET /api/v1/tax-policy/versions/{version_id}` — one version with
  its source; unknown id → 404 `TAX_POLICY_NOT_FOUND`.
- `POST /api/v1/tax-policy/decisions` — resolve + record a decision
  (201); no covering version → 404 `TAX_POLICY_NOT_FOUND`;
  production + unverified → 422 `TAX_POLICY_SOURCE_BLOCKED`.
- `GET /api/v1/tax-policy/decisions/{decision_id}` — one decision;
  another tenant's decision answers 404 `TAX_DECISION_NOT_FOUND`.

Tax CALCULATION on invoices is out of scope for this story — the
registry + decisions only.
