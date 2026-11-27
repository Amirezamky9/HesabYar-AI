# Setup wizard — research spike (US-203)

**Question:** can company creation + chart installation for the dashboard
setup wizard (design-eng spec §17, Phase 2) be driven through the Tryton
REST API, or does it need a small versioned custom Tryton module?

**Verdict: NO-GO for pure REST chart installation; GO for a small versioned
custom Tryton module** exposing one `__rpc__` method, called through the
existing REST action mechanism. Everything else the wizard needs — company
creation, the access grant, fiscal year + periods — already works over
REST today, verified live below. The JSON-RPC wizard path also works
end-to-end (proven live), but is NOT recommended as the product path: it
breaks the REST-only architecture decision, needs password-based session
auth, and runs with access checks disabled.

Researched 2026-11-17 against Tryton 8.2.3 (trytond 8.2.3,
trytond-account 8.2.2), live server on port 8000, installed source in
`~/workspace/projects/hesabyar-reference/tryton-env/`, and the official
8.2 docs. Nothing in this document is unverified: every claim cites a
probe result (§3), a source file, or a docs page.

## 1. The setup flow, step by step

| Step | Mechanism | Verdict |
| --- | --- | --- |
| 1. Create currency (if missing) | REST `POST currency.currency` | ✅ works (probe E0a, via JSON-RPC; same model CRUD as REST) |
| 2. Create party + company | REST `POST party.party`, `POST company.company` | ✅ works (probes B1–B4) |
| 3. Grant the company to the API user | REST `PUT res.user` (`companies` add command) | ✅ works (probe B5) — and **required** (§4.1) |
| 4. Ensure `company_filter='all'` on the API user | REST `PUT res.user` | ✅ works (probes F0–F1) — and **required on fresh DBs** (§4.2) |
| 5. Install the chart from a template | **custom module `__rpc__` method via REST action** | ✅ recommended (§2); pure REST impossible (§3, probe C) |
| 6. Create move sequence + fiscal year | REST `POST ir.sequence.strict`, `POST account.fiscalyear` | ✅ works (probes D0–D2) |
| 7. Create monthly periods | REST action `account.fiscalyear.create_period` | ✅ works (probes D3–D4) |
| 8. Verify | `GET /api/v1/accounting/setup-status` (US-203) | ✅ exists |

So the wizard is one custom-module method away from being fully
REST-driven. The rest is ordinary model CRUD plus one already-exposed
RPC action.

## 2. Recommended approach: a small versioned custom Tryton module

**Module:** a pip-installable package (e.g. `trytond-hesabyar-setup`,
GPL-3.0+ like all Tryton modules) declaring the `trytond.modules`
entry-point group — Tryton discovers modules from `trytond/modules/`
subdirectories *and* from that entry-point group
(`trytond/modules/__init__.py`, lines 412–413). Installed into the
Tryton venv, pinned like the other `trytond-*` packages, activated with
`trytond-admin -u hesabyar_setup --activate-dependencies`.

**What it contains:** one `__rpc__` method, added to `company.company`
via the standard extension pattern (`__setup__` +
`cls.__rpc__.update({...})`, the same pattern `account.move` uses for
`post` and `account.fiscalyear` uses for `create_period`):

```python
# Sketch — the implementation story writes the real module.
class Company(metaclass=PoolMeta):
    __name__ = 'company.company'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__rpc__.update({
            'install_chart': RPC(readonly=False, instantiate=0),
        })

    def install_chart(self, template_id, receivable_id=None, payable_id=None):
        # 1. Refuse when the company already has a chart (the wizard
        #    raises ChartWarning; the module raises a plain UserError so
        #    REST returns a clean 400).
        # 2. Validate the template is a root template (parent is None).
        # 3. Run the exact code of CreateChart.transition_create_account
        #    (types, accounts, taxes, tax codes, tax code lines,
        #    update_account2, tax rules, tax rule lines) for this
        #    company + template — call the template methods directly,
        #    do not copy them.
        # 4. Validate receivable/payable against the wizard's properties
        #    domain (closed != True, type receivable/payable,
        #    party_required, same company) and write them to
        #    account.configuration (transition_create_properties).
        # Returns the installed chart summary (account count, template
        # name) for the wizard to display.
```

**How HesabYar calls it:** `POST
/rest/model/company.company/<id>/install_chart` with a JSON body — the
same REST action mechanism the adapter already uses for `Move.post`
(docs/adapter.md), so no new transport, no new auth, no new error
taxonomy. One REST call is one engine transaction, so the chart install
is atomic: a failure leaves no partial chart behind.

**Why this shape, and not the alternatives (§6):**

- It reuses Tryton's own template-expansion code (the wizard's
  transitions call `AccountTemplate.create_account`,
  `TaxTemplate.create_tax`, …). No logic is duplicated in HesabYar, so
  a Tryton upgrade that changes template handling is picked up by
  upgrading the pinned module, not by rewriting client code.
- It runs under the API user's normal access checks
  (`check_access=True`, the `__rpc__` default) — unlike the wizard
  path, which skips them (§4.3). The grant step (§4.1) must therefore
  run first; the wizard flow already does it via REST.
- It is small and versioned: one method, one module, pinned in
  `tryton-requirements.txt` next to the other `trytond-*` packages.
  The module is a deployment artifact of the Tryton side, not of the
  HesabYar repo — HesabYar's own code stays MIT; only this module is
  GPL-3.0+ (as all Tryton modules are).

**What this spike did NOT verify:** the module itself does not exist
yet — writing, packaging and activating it is the implementation story.
The mechanism it relies on (REST action → `__rpc__` method) is already
proven in production by `Move.post` (adapter + contract tests), and
module discovery via entry points was verified in the installed source
only, not by installing a test module.

> **Update (US-502, 2026-11-18): the module now exists** —
> `tryton-modules/hesabyar_setup/` in this repo
> (`trytond-hesabyar-setup` 8.2.0, GPL-3.0+), with both `install_chart`
> and `update_chart` (§4.5) plus the Iranian chart extension
> (notes receivable/payable + cheque memorandum accounts). See its
> README and `docs/tryton-modules.md`. Step 1 of §7 below is done;
> steps 2–6 remain for later stories.

## 3. Evidence — what was tried, what the server returned

### 3.1 REST has no wizard surface (probes C1–C2)

The installed `trytond/protocols/rest.py` (8.2.3) defines exactly seven
routes: search, get, create, update, delete, **action**, report. The
word "wizard" does not appear in the file. The action route only calls
methods listed in the model's `__rpc__` (`rpc = Model.__rpc__.get(action)`;
unknown action → HTTP 403).

Live, against the test DB:

- `POST /rest/model/company.company/<id>/create_chart` →
  **403** (`TrytonAdapterError: ... action company.company.create_chart:
  access forbidden.`). There is no such RPC method.
- `GET /rest/model/account.create_chart` (search) → engine error
  (`'account.create_chart'` — wizards are not models, `pool.get`
  cannot resolve one).

A grep of the installed `account` module confirms no chart-installing
RPC method exists anywhere: the module exposes only
`FiscalYear.create_period` (`fiscalyear.py`) and `Move.post`
(`move.py`); the `company` module exposes no `__rpc__` at all.

### 3.2 Company creation works over REST (probes B1–B5)

Live, against the test DB (probe company deleted afterwards; §3.5):

- `POST party.party {"name": "HesabYar Spike Probe"}` → party id 9.
- `POST company.company {"party": 9, "currency": <IRR id>}` → company
  id 6; readable back (`rec_name` matches); chart search returns `[]`.
- `PUT res.user/<admin> {"companies": [["add", [6]]]}` → grant applied
  (verified by re-reading the user).

`company.company` needs only `party` (required) and `currency`
(required) — `company.py` lines 50–65.

### 3.3 Fiscal year + periods work over REST (probes D0–D4)

Live, against the probe company (deleted afterwards):

- `ir.model.data` search for `(module='account',
  fs_id='sequence_type_account_move')` → `db_id` 2. The move-sequence
  type is resolvable over REST; no console needed.
- `POST ir.sequence.strict` (incremental, company-scoped) → created.
- `POST account.fiscalyear` (name, dates, company, move_sequence) →
  created. `move_sequence` is required (`fiscalyear.py` line 52).
- `POST /rest/model/account.fiscalyear/<id>/create_period` (the
  `__rpc__` action) → **12 open monthly standard periods** for 2027,
  first `2027-01` (`2027-01-01`–`2027-01-31`), last `2027-12`.

One failure on the way, worth recording: the sequence create initially
failed with `You are not allowed to create records of "Sequence Strict"
because they fail on at least one of these rules: User in companies
Within groups` — because the probe company had not been granted to the
admin yet. After the grant (probe B5) the identical create succeeded.
See §4.1.

### 3.4 The JSON-RPC wizard path works end-to-end (probes E, F)

To test the alternative honestly, the full `account.create_chart`
wizard was driven over JSON-RPC — the same protocol Sao uses
(`sao/src/wizard.js`: `wizard.<name>.create` / `.execute` / `.delete`).
Because chart installation is destructive, this ran in a **scratch
database** (`hesabyar_spike_test`, created for the spike, activated with
`account` + dependencies, dropped afterwards) — the shared test DB was
never touched.

Sequence and results (session auth: `common.db.login` as admin,
`Session` header):

1. `model.currency.currency.create` (IRR), `model.party.party.create`,
   `model.company.company.create` → company id 1.
2. `model.res.user.write` — grant company 1 to admin.
3. `model.account.account.template.search_read` (parent is None) →
   one root template: `Universal Chart of Accounts` (id 1).
4. `wizard.account.create_chart.create` → session 1, start `start`,
   end `end`.
5. `execute(session, {}, 'start')` → start view, buttons `end` /
   `account`.
6. `execute(session, {'account': {'company': 1, 'account_template': 1}},
   'account')` → account view.
7. `execute(session, {}, 'create_account')` → properties view.
   Verified via psql: **135 accounts, 46 account types** created.
8. `execute(session, {'properties': {'company': 1,
   'account_receivable': 39, 'account_payable': 67}}, 'properties')`
   → properties view.
9. `execute(session, {}, 'create_properties')` → `{}` (end state).
   Verified: `account.configuration` defaults set
   (`default_account_receivable` 39 = `1.2.1`, `default_account_payable`
   67 = `2.1.1 Trade Payables`).
10. `wizard.account.create_chart.delete` → session cleaned up.

So the JSON-RPC wizard path is **technically viable**. It is still not
recommended (§6) — see the reasons there. Two pitfalls found while
running it, relevant to any future wizard driver:

- View `defaults` contain `company.` (with a trailing dot) as a nested
  `rec_name` dict. Passing defaults back verbatim fails with
  `'account.create_chart.properties' object has no attribute
  'company.'` — only real field names may be sent.
- The properties domain requires `party_required` accounts. The
  universal chart has 3 party-required receivable and 3 party-required
  payable accounts, so the wizard cannot pre-fill either (it only
  pre-fills when exactly one exists) — the dashboard wizard must let
  the user choose, defaulting sensibly (e.g. `1.2.1` / `2.1.1`).

### 3.5 Cleanup verified

- REST probes: periods → fiscal year → sequence → company → party all
  deleted via REST; final search for the probe party returned `[]`;
  psql confirms 0 probe parties / companies in `hesabyar_tryton_test`.
- JSON-RPC probes: scratch DB dropped (`DROP DATABASE
  hesabyar_spike_test` after terminating trytond's pooled connections
  to it); `\l` shows only `hesabyar_test` and `hesabyar_tryton_test`.

Probe scripts (ephemeral, in `/tmp` — results are recorded here, not in
the scripts): `/tmp/spike_probe_rest.py`, `/tmp/spike_probe_wizard.py`,
`/tmp/spike_probe_wizard2.py`, `/tmp/spike_console_check.py`.

## 4. Findings that shape the wizard design

### 4.1 A new company is unusable until granted to the API user

Every company-scoped model carries the global `ir.rule`
`[('company', 'in', Eval('companies', []))]`, where `companies` comes
from `res.user.get_companies()` — the user's **granted** companies
(`modules/company/res.py`, lines 154–174), even with
`company_filter='all'`. A freshly created company is in nobody's grant
list, so creating a sequence / fiscal year / accounts for it fails the
rule (quoted in §3.3), and searches silently return nothing. The wizard
must grant the company to the service user (REST `PUT res.user`,
`companies` add command — probe B5) immediately after creating it and
before any company-scoped step. The dev provisioning script already
does this (`grant_admin_access` in `setup_adapter_test_company.py`).

### 4.2 A fresh database's API user sees nothing by default

`res.user.company_filter` defaults to `'one'` on a fresh database
(verified: scratch DB admin had `'one'`, main dev DB admin has `'all'`
— set by the restore script). With `'one'` and no current company set,
`get_companies()` returns `()` and **all** company-scoped data is
invisible, even to granted companies: the probe's verification search
returned 0 accounts for a chart that psql proved existed (135 rows).
Setting `company_filter='all'` via JSON-RPC immediately made the chart
visible (probe F2). The wizard's deployment checklist must set
`company_filter='all'` on the service user (or grant + set a current
company); the setup-status endpoint will otherwise report a ready
company as empty.

### 4.3 Wizard execution skips access checks

`Wizard.__rpc__['execute']` is declared with `check_access=False`
(`trytond/wizard/wizard.py`), and `RPC.convert` only sets
`_check_access` when `check_access` is true (`trytond/rpc.py`). So
everything a wizard transition does — including creating 135 accounts
for an arbitrary company id — runs with rule checks disabled. That is
why the probe's chart install succeeded even while the same user's
searches were rule-blocked (§4.2). It is also a reason to prefer the
custom module: its `__rpc__` method runs with checks **on**, so a bug
or a wrong company id fails loudly instead of writing unchecked.

### 4.4 The wizard's duplicate-chart guard

`transition_create_account` refuses a company that already has accounts
unless the `duplicated_chart.<company>` user warning was dismissed
(`ChartWarning`). The custom module should mirror the refusal with a
plain error (no warning-dismissal dance over REST), and the dashboard
wizard should check `setup-status` first so it never offers to install
a chart twice.

### 4.5 Chart updates go through the same door

The `account.update_chart` wizard (same constraints as Create Chart —
wizard, no REST surface) updates a company's structure from template
changes. If the product later needs "update chart", the custom module
gains a second method; no new mechanism is needed.

## 5. Which template is installed? (the `chart_template` field)

The setup-status endpoint reports `chart_template: null` today. That
is a port limitation, not an engine limitation — the engine **does**
record template provenance, and a future adapter story can expose it:

- Every wizard-created account records its template:
  `account.account.template` is a real field, set by
  `AccountTemplate._get_account_value` (`modules/account/account.py`,
  line 701–702). Verified live: **135/135** accounts of the Adapter
  Test company have `template` set (probe A1).
- The chart root (the one account with no parent) carries the root
  template: id 1, `Universal Chart of Accounts`, readable over REST
  with `parent: None` (probes A2–A3).
- Root templates are listable over REST
  (`account.account.template`, `parent is None` — probe A4); the
  current install has exactly one.

Derivation for the future story: read the chart root's `template` over
REST, read that template's `name` (walk `parent` up if the root
account's template ever isn't the root template), and return the name.
No HesabYar-side recording of the template choice is needed — but the
wizard should still record which template it *asked* for, so a
mismatch between "asked" and "installed" is detectable.

## 6. Rejected alternatives

**Pure REST chart installation.** Impossible: no wizard endpoint exists
in REST (§3.1), and no `__rpc__` method installs a chart. Not a matter
of finding the right call — the surface does not exist.

**Reimplementing the wizard client-side over REST.** The transition
does seven template-expansion steps (types, accounts, taxes, tax codes,
tax code lines, `update_account2`, tax rules + rule lines) with
cross-referencing id maps, inside one transaction. Replicating that in
HesabYar would mean ~200 REST calls, no atomicity (a failure mid-way
leaves a partial chart), and a fork of Tryton's template logic that
breaks on every Tryton upgrade. Rejected on correctness and maintenance
grounds; the custom module reuses Tryton's code instead.

**JSON-RPC wizard as the product path.** Works end-to-end (§3.4), but:
(a) it breaks the REST-only architecture decision (docs/adapter.md —
the adapter uses REST only; JSON-RPC is used solely for the read-only
server-version call); (b) it needs password-based session auth for a
service account instead of the REST application key — a second
credential to manage and rotate; (c) wizard execution is multi-step and
stateful (session ids, view round-trips), so a failure mid-wizard
leaves server-side session state to clean up; (d) it runs with access
checks disabled (§4.3). Kept as the documented fallback if the custom
module ever cannot be deployed.

**trytond-console provisioning.** The current dev seeding method
(`setup_adapter_test_company.py`) runs the wizard code path from the
server shell. It is operator-only, not API-driven, and cannot serve a
dashboard wizard. It stays as the dev-seeding tool.

## 7. Concrete next steps (for the implementation story)

1. Create the `trytond-hesabyar-setup` module (repo + packaging +
   `trytond.modules` entry point), with the `install_chart` method
   sketched in §2. Include: duplicate-chart refusal, root-template
   validation, properties-domain validation, a returned summary.
2. Pin it in `tryton-requirements.txt`; extend `restore-dev-env.sh`
   and `docs/dev-setup.md` to install + activate it; add the
   `company_filter='all'` requirement for the service user to the
   deployment checklist (§4.2).
3. Extend the port + adapter with `install_chart` (REST action call),
   following the `post_journal` precedent (command object, stable
   errors, contract tests against live Tryton).
4. Build the dashboard wizard flow on top (§1 table), each step a
   HesabYar endpoint; verify the full flow against a scratch database
   first, as this spike did.
5. Later adapter story: expose the installed chart template
   (`chart_template` in setup-status) via the root account's
   `template` field (§5).
6. Deferred, not dropped: `account.update_chart` support (§4.5).

> **US-706 update:** step 4's "verify the full flow against a
> scratch database" now exists as the CI install scenario
> (`tests/contract/test_fresh_install_us706.py`): it provisions a
> fresh company (steps 1–7 of §1) over REST, then runs the first
> order → invoice → payment → settlement and asserts the trial
> balance balances. The dashboard wizard UI itself (the "build the
> wizard flow" half of step 4) is still not built — the dashboard
> shows the installed state read-only (setup-status tab), and
> provisioning remains an operator procedure
> (`docs/deployment-iran.md` §6).

## 8. Sources

- Official docs: REST client guide
  (`https://docs.tryton.org/8.2/client-rest/index.html` — search, create,
  update, delete, **action**, report; no wizard operations);
  Accounting setup
  (`https://docs.tryton.org/latest/modules-account/setup.html` — Create
  Chart wizard creates the accounts structure from a template; fiscal
  year + Create Periods wizard); Templates
  (`https://docs.tryton.org/7.0/modules-account/design/template.html` —
  Create Chart / Update Chart wizards).
- Installed source (trytond 8.2.3 / trytond-account 8.2.2):
  `trytond/protocols/rest.py` (7 routes, action requires `__rpc__`);
  `trytond/modules/account/account.py` (`CreateChart`, lines
  3126–3300; `_get_account_value` sets `template`, lines 678–702);
  `trytond/modules/account/fiscalyear.py` (`create_period` RPC,
  `move_sequence` required); `trytond/modules/company/company.py`
  (company fields); `trytond/modules/company/res.py`
  (`get_companies`, `company_filter`); `trytond/modules/company/ir.py`
  (rule context); `trytond/wizard/wizard.py` (`create` / `execute` /
  `delete` RPC, `check_access=False` on execute);
  `trytond/protocols/dispatcher.py` + `trytond/rpc.py` (JSON-RPC
  dispatch, `_check_access`); `trytond/ir/rule.py` (rule domain cache);
  `trytond/modules/__init__.py` (module discovery incl. entry points);
  `sao/src/wizard.js` (Sao's wizard protocol — the reference client).
- Live probes: §3 (REST probes A–D against `hesabyar_tryton_test`;
  JSON-RPC probes E–F against scratch DB `hesabyar_spike_test`,
  dropped after use).
