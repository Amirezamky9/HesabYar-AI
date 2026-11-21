# Release documentation (US-705)

How HesabYar versions, documents, checks and ships a release —
and how a release moves from development to staging to
production. Spec §17 phase 7; phased design §1.3 phase 7.

The upgrade PROCEDURE (the steps, the rollback) lives in
`docs/runbook-upgrade-rollback.md`. This document is the release
itself: what a version is, what the checklist requires, and what
each environment must prove before the next one trusts it.

## 1. Versioning

HesabYar versions with **Semantic Versioning** (`MAJOR.MINOR.PATCH`,
<https://semver.org/>):

| Bump | When |
|---|---|
| MAJOR | A breaking change: a removed/renamed REST endpoint, a removed scope, a migration that cannot downgrade, a Tryton module set change that drops a module |
| MINOR | A backwards-compatible feature: a new endpoint, a new module, a new migration (with a working downgrade) |
| PATCH | A backwards-compatible fix: a bug fix, a docs-only change bundled with a fix, a dependency patch |

The version lives in TWO places, bumped together in the release
commit:

- `pyproject.toml` (`version = "0.1.0"`)
- `src/hesabyar/__init__.py` (`__version__ = "0.1.0"` — the
  version the API reports: `GET /api/v1/health` and the OpenAPI
  document carry it)

No automated check keeps the two in sync today — the release
checklist (§3) is the check. (A sync check is a future story;
until then, a mismatch is caught by the checklist, not by CI.)

Releases are tagged in git: `vX.Y.Z` on the merge commit that
carries the version bump. The tag is what staging and production
check out — never a branch head, never `main` by date.

**Current state:** the application is at `0.1.0` (pre-1.0 — the
API is not yet stability-promised). The `1.0.0` release is the
first production release: it ships when Phase 7's exit criteria
are met (spec §17: a fresh store install, upgrade, backup and
restore are repeatable and tested).

## 2. Changelog

`CHANGELOG.md` (repo root) follows [Keep a Changelog](https://keepachangelog.com/fa/1.1.0/)
(Persian, like the rest of the project's user-facing text).
Two histories live in it — do not confuse them:

- **`[1.0.0] - 1403/07/01` and below** are the LEGACY standards
  catalog (35 Iranian accounting standards, validators,
  templates) — the repo's earlier life as a standards skill
  project. That history is closed; do not add application
  entries under it.
- **Application releases** go under `## [Unreleased]` (work in
  progress) and then a versioned section per release
  (`## [0.2.0] - 1405/09/01`), with the usual Keep-a-Changelog
  groups (added / changed / fixed / removed). The `[Unreleased]`
  section is where every merged story lands its entry — the
  release checklist (§3) moves it into the versioned section.

Each release's changelog entry MUST name, when they apply:

- the Alembic head after the release (e.g. `0042`) and whether
  any migration in the release is destructive or irreversible
  (see the upgrade runbook §4.2 — the operator reads this
  before deciding a rollback is possible);
- the Tryton module set change, if any (added/updated modules
  and their versions);
- any manual step the upgrade requires (a config rename, a
  one-off data fix, a new required `HESABYAR_*` setting);
- any credential rotation the release forces (e.g. a pepper
  change invalidates all API keys — threat model §6.2 A7).

## 3. Release checklist

Every box is checked before the tag is cut. A release that skips
a box is not a release — it is a draft with a version number.

- [ ] **Version bumped** in `pyproject.toml` AND
      `src/hesabyar/__init__.py` (§1), same value in both.
- [ ] **Changelog** moved from `[Unreleased]` into the versioned
      section (§2), with the migration head + destructiveness
      note, the module-set note, manual steps, and forced
      rotations.
- [ ] **CI green on the release commit** — all five jobs:
      `python-gates` (ruff + pyright), `python-tests`,
      `python-tests-tryton` (live Tryton), `frontend` (tsc +
      eslint + vitest + build), `playwright` (the full E2E
      suite). No job skipped, no known-failing test.
- [ ] **Single Alembic head.** `alembic heads` prints exactly
      one revision, and it is the head the changelog names. (The
      drill's `hesabyar.migrations` check and the migration
      tests assert this in CI; the checklist is the human
      confirmation.)
- [ ] **Migration downgrades reviewed.** Every new migration's
      `downgrade()` was read: destructive ones (drop table /
      column) are named in the changelog, and a migration with
      no working downgrade is a MAJOR bump (§1) with the
      backup-restore-only rollback stated.
- [ ] **Docs updated.** The affected `docs/*.md` files describe
      the new behaviour; a new endpoint is in `docs/rest-api.md`;
      a new setting is in its doc's configuration table; a new
      alert rule is in `docs/monitoring.md` with its runbook
      section (the runbook-ref test,
      `tests/unit/test_runbook_refs_us705.py`, fails CI when a
      ref points at a missing section).
- [ ] **Staging rehearsal done** (§4): the upgrade runbook ran
      end-to-end on staging, timed, with the smoke checks green.
- [ ] **Rollback reviewed.** The rollback path for THIS release
      was read against the upgrade runbook §4 — which layers can
      roll back, which need a backup restore — and the
      decision-maker for the production window is named.
- [ ] **Tag cut** (`vX.Y.Z`) on the release commit; the tag is
      what staging and production check out.

## 4. Environment promotion (dev → staging → production)

| Environment | What it is | What runs there |
|---|---|---|
| **dev** | A developer's checkout (or CI) | The full test suite; simulators + sandbox Fintech; the local-directory backup backend (dev/CI only — never a production destination) |
| **staging** | A full installation mirroring production: both databases, trytond with the full module set, worker, MCP, admin | The upgrade rehearsal (§3), the smoke suite (§4.1), the restore drill against real backups |
| **production** | The live store installation | Real data, real money, real backups (S3 destination decided — `docs/backup-restore.md` §2) |

Promotion gates:

- **dev → staging:** CI green (§3) + the tag cut. Staging
  checks out the tag and runs the upgrade runbook (§3 step 0 is
  the rehearsal — staging IS the rehearsal).
- **staging → production:** the staging rehearsal green (smoke
  suite §4.1 + the restore drill passing on staging's own
  backups) + the release checklist complete + the maintenance
  window announced. Production checks out the same tag — never
  a newer commit, even a "trivial" one. A fix found on staging
  means a new patch release (new tag, new checklist), not a
  quiet extra commit.

Environment differences that are KNOWN and accepted (not drift
to fix silently):

- **Staging uses sandbox Fintech + the simulator.** No live
  Fintech token exists yet (the ۵ت remainder is deferred);
  production Fintech go-live needs the Iran-hosting decision
  (US-706) and a legal-entity contract. Staging never carries
  production Fintech credentials.
- **Staging uses sandbox gateways.** Production gateway
  credentials live only in production's vault.
- **Moadian is test-profile + simulator everywhere** until
  production Moadian connects (threat model §6.2 A13). No
  production Moadian submission happens from any environment
  today.
- **Staging data is seeded test data**, not a production copy.
  Never restore a production backup into staging — it carries
  real customer data and real credentials.

### 4.1 The smoke suite

The same checks after every promotion step (and after every
upgrade — the upgrade runbook §3 step 9 runs this list):

1. `GET /api/v1/readiness` → `ready`.
2. `GET /api/v1/installation` → Alembic revision == the release
   head; Tryton release + module versions == the release's set.
3. `GET /health` on the MCP server → `ok`.
4. One report (trial balance, current fiscal year) renders.
5. The inbox drains (the worker's `events.sweep` runs every
   minute; queue depth in `/monitoring` falls).
6. One MCP read call succeeds through an authorized client.
7. The admin panel loads; the dashboard renders.
8. `POST /api/v1/monitoring/evaluate` — no new critical alert.
9. The review queue shows no new failure items from the window.
10. A backup runs and the drill passes (staging: on demand;
    production: the next scheduled run is watched, not assumed).

## 5. Rollback of a release

A released version that must come back follows the upgrade
runbook §4–§5 (per-layer rollback + decision criteria). The
release checklist's "rollback reviewed" box (§3) exists so the
rollback path is known BEFORE production, not discovered during
an outage. A rollback does not un-tag the release: the tag stays
(as history), the changelog gains a note ("rolled back on <date>
because <reason>"), and the fix ships as a new patch release.

## 6. What is NOT decided yet (honest limits)

- **No production server exists.** US-706 owns the deployment
  (Iran hosting / egress, reverse proxy, TLS, process
  supervision). Until it lands, "production" in this document is
  a defined environment with no machine — the promotion gates
  are written for the day it exists.
- **No S3 backup destination is decided** (`docs/backup-restore.md`
  §2 — UNVERIFIED until the deployment decision). Production
  backups cannot be configured until it is; staging uses the
  local-directory backend, which is explicitly NOT a production
  destination.
- **No release has ever shipped.** This document is the
  procedure for the first one; the first release's retrospective
  (what the checklist missed, what the rehearsal did not catch)
  updates this document. The checklist is a living list, not a
  monument.
