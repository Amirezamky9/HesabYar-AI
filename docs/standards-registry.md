# Standards Registry (US-401)

The Iranian accounting standards registry and validation service
(design-eng spec §7.4, §12.1; ARCHITECTURE.md §9).

## What lives where

| Piece | Location |
|---|---|
| Tables | Alembic `0005`: `standard_sources`, `standard_revisions`, `standard_rules`, `validation_runs`, `validation_results` |
| ORM models | `src/hesabyar/infrastructure/persistence/postgres/models.py` |
| Corpus loader + DSL port + seed | `src/hesabyar/infrastructure/persistence/postgres/seed_standards.py` |
| Registry port implementation | `src/hesabyar/infrastructure/persistence/postgres/standards_repository.py` (`PostgresStandardsRegistry`) |
| Closed DSL engine (reused, not reimplemented) | `src/hesabyar/domain/standards/` (`dsl.py`, `models.py`, `ports.py` — incl. `StandardsRegistryPort`) |
| Query service | `src/hesabyar/application/queries/standards.py` |
| Validation service | `src/hesabyar/application/commands/standards.py` |
| REST | `src/hesabyar/interfaces/rest/routers/standards.py` (+ `schemas/standards.py`) |

## Seeding

```bash
# Against the configured database (HESABYAR_DATABASE_URL):
python -m hesabyar.infrastructure.persistence.postgres.seed_standards
```

The seed is idempotent (upserts by `standard_code`,
`(source_id, revision_id)`, `(revision_id, rule_id)`). It loads:

- **35 standards** from `metadata.json` — one source + one revision
  each. `sha256` pins the exact bytes of
  `standards/<slug>/source/standard.md`; `snapshot_uri` records the
  repo-relative path; `source_url` is the per-standard PDF URL.
- **36 rules** from `validators/rules.yaml` (all under Standard 1 —
  the file declares `standard: 1`), ported to the closed DSL:
  - equation rules → `(expr - expected) <= tol and (expr - expected) >= -tol`,
    with the tolerance rendered as an exact integer fraction (`1/100`)
    because the DSL forbids binary-float literals;
  - threshold rules (`min`/`max`) → `expr >= min` / `expr <= max`;
  - the CF005 disclosure requirement has no formula and is seeded with
    `is_executable = false`.
  - the four `materiality_thresholds` entries are ratios, not pass/fail
    rules, and are NOT seeded.
- `ruleset_version` is the source file's version (`1.0.0-legacy`) and is
  recorded on every rule and every run.

## Verification statuses

Labels are `docs/CURRENT_RULES_1405.md` §1, plus `UNVERIFIED`
(HesabYar extension: bundled text present, official source snapshot
not yet pinned/verified — the honest default).

| Standards | Status | Effective |
|---|---|---|
| 43 (Revenue) | `VERIFIED_TEXT` | from 1404/01/01 (2025-03-21); supersedes 3, 9, 29 |
| 44 (Leases) | `VERIFIED_TEXT` | from 1405/01/01 (2026-03-21); supersedes 21 |
| 15 (Investments) | `BLOCKED_SOURCE_GAP` | bundled revision effective 1380/01/01–1404/12/29 |
| other 32 | `UNVERIFIED` | — |

### Standard 15 gate

The bundled Standard 15 text is the pre-1404 revision. The
validation service refuses any run that requests Standard 15 when the
period is unknown (fail closed) or begins on/after 1405/01/01
(2026-03-21) — typed error `STANDARD_SOURCE_BLOCKED` (HTTP 422).
Historical periods (beginning on/before 1404/12/29) may still use the
bundled revision. The gate is generic: any standard whose latest
revision is `BLOCKED_SOURCE_GAP` blocks the same way.

## Coverage (spec §12.1)

Coverage is reported separately — a corpus is not compliance coverage:

- `corpus_standards` — standards in the registry (35);
- `standards_with_rules` — standards with machine-readable rules (1);
- `rules_implemented` — rules seeded (36);
- `rules_verified` — rules whose revision is `VERIFIED_TEXT` (0 —
  all 36 rules belong to Standard 1, whose bundled revision is
  `UNVERIFIED`);
- `rules_executable` — rules the DSL engine can evaluate (35);
- `rules_executed` — rules a run actually evaluated (per run).

A run also lists `not_executed` rules (currently CF005) with reasons,
so the gap between implemented and executed is always visible.

## Validation runs

Outcomes are exactly `pass` / `fail` / `not_applicable` / `error`.
Missing data or an unevaluable rule yields `error`, never `pass`
(fail-closed, enforced by the DSL). A rule whose revision is not
applicable to the run's period (effective dates) yields
`not_applicable`.

### Data shape

Financial data is a document of per-statement namespaces
(`balance_sheet`, `income_statement`, `comprehensive_income`,
`equity_changes`, `cash_flow`) plus optional top-level fields. Each
rule evaluates against its own category's statement slice merged over
the document (the slice wins for flat field names); cross-check rules
evaluate against the whole document via dotted paths
(`income_statement.net_profit`). This mirrors the legacy validator's
per-category contexts and is the only reading under which the ported
rules are mutually consistent (e.g. `owners_of_parent` means one thing
in the income statement and another in comprehensive income).

JSON numbers are normalized at the service boundary: integral floats
become ints, other floats become exact Decimals via `str()` — the DSL
itself still rejects raw floats. The run stores a SHA-256 of the
canonical normalized input, so it is reproducible from its record.

## API

Scopes: `standards:read`, `standards:validate` (already in the §14.2
catalog — no new scopes were needed).

- `GET /api/v1/standards` — filters `verification_status`, `phase`,
  `has_rules`; sort `standard_code` (numeric) / `title`; cursor pagination.
- `GET /api/v1/standards/{standard_code}` — revisions (full §12.1
  provenance) + rules.
- `POST /api/v1/validation-runs` — creates a run (201); unknown
  standard → 404 `STANDARD_NOT_FOUND`; blocked standard → 422
  `STANDARD_SOURCE_BLOCKED`; bad period range → 422 `VALIDATION_ERROR`.
- `GET /api/v1/validation-runs/{run_id}` — the run with its results;
  another tenant's run answers 404.

Runs are tenant-scoped; `company_id` on a run is an optional plain
reference (row id or engine id), not validated against the engine —
US-403 ties runs to live trial-balance data.
