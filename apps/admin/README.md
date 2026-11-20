# HesabYar Admin — Persian accounting dashboard

Next.js 16 App Router + React 19 + TypeScript (strict) + Tailwind CSS v4,
RTL-first Persian UI. Spec: `~/workspace/user/files/design-eng_0_zaup.md`
§5 (frontend/design system), §19 rule 1 (no direct frontend-to-Tryton),
§4 (credentials server-side only).

## Architecture in one paragraph

The browser never talks to FastAPI, Tryton or PostgreSQL, and never holds
credentials. The operator pastes an API key (`hya_live_...`) once on
`/login`; the BFF validates it against FastAPI and keeps it in a
server-side session (opaque id in an httpOnly cookie). All data calls go
through BFF routes (`/api/bff/...`) that attach the key server-side.
Feature pages use the typed client in `lib/api-client.ts` (all 29
Phase 1+2 endpoints) and import UI only from the facade
(`@/components/ui`).

## Run it

```bash
# 1. Backend (from the repo root): FastAPI on :8001, Tryton on :8000.
#    See docs/dev-setup.md. PostgreSQL 16 + trytond must be up first.
export PATH="$HOME/.local/bin:$PATH"
export HESABYAR_DATABASE_URL="postgresql+psycopg://hesabyar:hesabyar_pass@127.0.0.1:5432/hesabyar_test"
uv run uvicorn hesabyar.interfaces.rest.app:app --port 8001

# 2. Admin app (this directory).
cd apps/admin
cp .env.local.example .env.local   # then fill in the dev key, see below
pnpm install
pnpm dev                           # http://127.0.0.1:3000
```

## Dev API key (local dev only, never committed)

Mint an operator key with the Phase 1 e2e pattern (idempotent
provisioning; each run issues a fresh key, printed once):

```bash
# From the repo root, with HESABYAR_DATABASE_URL set (see above).
uv run python apps/admin/scripts/mint-dev-key.py
# -> save the printed secret in apps/admin/.env.local:
#    HESABYAR_DEV_API_KEY=<redacted>
```

The script provisions a dev tenant (`hesabyar-dev-admin`), client and
engine mapping for the "HesabYar Adapter Test" company, and refuses to
run against a non-dev database. After a VM replacement wipes PostgreSQL,
restore the DB (`hesabyar-reference/restore-dev-env.sh`) and re-run the
script — the old key is gone with the old DB.

> **A full `pytest` run wipes the dev rows.** Some test modules truncate
> the shared `hesabyar_test` database, so after `pytest` the minted key
> stops authenticating — re-run the script and update `.env.local`.
> Conversely, a minted dev key's engine-connection row
> (`tryton` / `hesabyar_tryton_test`) collides with the
> `test_api_key_auth.py` fixtures, which insert the same pair: the first
> full `pytest` run after minting fails those 3 tests with an
> IntegrityError, and the second run is green (the tables were truncated
> in between). This is a known shared-DB ordering quirk, not a code
> regression — see the Phase 2 notes in `progress.txt`.

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `HESABYAR_API_BASE_URL` | `http://127.0.0.1:8001` | FastAPI origin (server-side only) |
| `HESABYAR_DEV_API_KEY` | — | Dev operator key for the Playwright login test (`.env.local` only) |

## Commands

| Command | What it does |
| --- | --- |
| `pnpm dev` | Dev server on :3000 (Turbopack) |
| `pnpm build` | Production build (typecheck included) |
| `pnpm lint` | ESLint |
| `pnpm typecheck` | `tsc --noEmit` |
| `pnpm test` | Unit + component tests (vitest, 49 tests) |
| `pnpm test:e2e` | Browser tests (Playwright, needs backend + dev key) |

## Layout

```text
app/
  (auth)/login/          login page (operator API key)
  (dashboard)/           auth-gated shell (layout redirects to /login);
                         layout.tsx renders DashboardShell (sidebar +
                         top bar + company context) — keep the
                         `await connection()` + session check at its top
  (dashboard)/page.tsx   dashboard home (US-302): §11.3 widgets from
                         live API data only, honest coming-soon tiles
  (dashboard)/accounting/
    page.tsx             section landing -> redirects to accounts
    accounts/            chart of accounts (US-302): tree, search,
                         type filter, detail panel
    fiscal-years/        fiscal years + periods + setup-status (US-302),
                         one page with three tabs; journals live at
                         /accounting/journals (US-303)
  (dashboard)/invoices/  (US-304)   payments/ (US-304)
  (dashboard)/banking/statements/ (US-304)   reports/ (US-305)
  api/auth/              login / logout / session routes
  api/bff/[...path]/     catch-all BFF proxy to FastAPI
components/
  ui/                    THE FACADE (spec §5.3) — feature pages import
                         ONLY from here; see THIRD-PARTY-NOTICES.md
  layout/                dashboard shell: navigation (13 §18 modules),
                         sidebar, topbar, breadcrumbs, company-selector,
                         dashboard-shell, logout-button
features/
  dashboard/             company-context (active company for every
                         company-scoped call), dashboard-home,
                         dashboard-data (pure aggregations), widgets
  chart-of-accounts/     accounts-page, fiscal-view, account-tree
                         (tree/filter/sort), account-types (Persian
                         labels for Tryton account types)
lib/
  api-client.ts          typed client over the BFF (29 endpoints)
  auth.ts                server-only session store (in-memory Map on
                         globalThis; swap for Redis/DB for multi-instance)
  backend.ts             server-only FastAPI fetch helper
  format-money.ts        integer IRR; Toman display-only + labelled
  format-date.ts         Jalali display of ISO dates/timestamps
  jalali.ts              deterministic Jalali<->Gregorian (VibeFarsi +
                         strict ISO helpers)
  permissions.ts         spec §14.2 scope catalog + hasScope helpers
styles/
  tokens.css             SINGLE theme-token layer (VibeFarsi paper,
                         light default; `.dark` opt-in)
  globals.css            Tailwind import + base styles (logical props only)
scripts/mint-dev-key.py  dev operator key (see above)
e2e/                     Playwright specs (real backend)
```

## Rules for feature stories (US-302+)

- Import UI only from `@/components/ui` — never from copied registry
  files directly. One implementation per primitive per page.
- Money: integer IRR in state/payloads; `formatMoney` / `MoneyDisplay`
  for display (Toman labelled). Persian digits for display only;
  normalize to ASCII for validation/storage.
- Dates: Gregorian ISO (`YYYY-MM-DD`) in state/payloads; Jalali for
  entry/display via `JalaliDatePicker` / `formatIsoDate`. Never infer a
  calendar from an ambiguous string.
- Sorting/filtering/copy/exports use normalized values, never localized
  display strings. LTR-isolate codes, IBANs, card numbers, ids
  (`.ltr-isolate`).
- All user-facing text is Persian; all numbers/dates in the UI are
  Persian/Jalali. Light theme is the default.
- Mutations send an `Idempotency-Key` (the client generates one per
  call). Surface API errors as Persian messages from the stable
  `ApiError.code`, never raw JSON. 401 → back to `/login`.
- No fake data: figures without an API source are honest coming-soon
  states (see the US-302+ plan notes).
- Next 16 notes: `cacheComponents` is on (scaffold default), so the
  dashboard group opts out of prerendering with
  `export const instant = false` in its layout AND `await connection()`
  before the session check — `instant = false` alone does not stop the
  prerender, and without `connection()` production serves a 200 error
  page on `/` instead of redirecting to `/login` (the session check
  uses `Date.now()`). `dynamic = "force-dynamic"` is not allowed under
  `cacheComponents`. Route-handler `params` and `cookies()` are async.

## Environment quirks (this VM)

- pnpm runs as root in a user namespace where `chown(0, 65534)` fails.
  pnpm chowns replaced lockfiles to match the existing file's owner, so
  the lockfiles must be `root:root` (`chown root:root pnpm-lock.yaml
  node_modules/.pnpm/lock.yaml`) or every `pnpm add`/`install` fails
  with `EPERM chown`. Fixed once; the lockfile stays root-owned after
  that. If it ever regresses, re-run that chown.
- Playwright browsers live in `~/.cache/ms-playwright` (survives VM
  replacement); system libraries from `playwright install-deps` do not
  — re-run it after a replacement if Chromium fails to launch.
- `next/font/google` fetches Vazirmatn at build time; the build needs
  network access to Google Fonts.
