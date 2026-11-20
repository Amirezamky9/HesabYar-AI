# trytond-hesabyar-setup

A small versioned Tryton module (GPL-3.0+, like all Tryton modules) that
makes chart installation and update callable over the Tryton REST API,
and extends the Universal Chart of Accounts with the Iranian accounts
HesabYar needs.

## Why this module exists

Tryton's Create Chart / Update Chart wizards have no REST surface
(`docs/setup-wizard.md`, spike US-203), so the HesabYar setup wizard
cannot install a chart through the REST API alone. This module adds two
`__rpc__` methods to `company.company` that run the wizards' transitions
directly — calling Tryton's own template-expansion code, not copying
it — so the HesabYar adapter can drive them with ordinary REST actions.
Each call is one engine transaction (a failure leaves no partial chart)
and runs with access checks on, unlike wizard execution.

## RPC methods

### `install_chart(template_id, account_receivable_code='1.2.1', account_payable_code='2.1.1')`

Installs a chart from a root template. Mirrors
`account.create_chart.transition_create_account` +
`transition_create_properties`:

- refuses a company that already has accounts with a plain `UserError`
  (no warning-dismissal dance over REST);
- validates the template is a root template (`parent is None`);
- runs the seven template-expansion steps (types, accounts, taxes, tax
  codes, tax code lines, `update_account2`, tax rules + rule lines);
- validates the receivable / payable defaults against the wizard's
  properties domain (open, right type flag, party required, same
  company) and writes them to `account.configuration` in the same call.

The defaults are given as account **codes**, resolved against the
freshly installed chart. The spike sketch (`docs/setup-wizard.md` §2)
passed account ids, but ids are unknowable before the install — codes
are stable, listable from the template beforehand, and what the
dashboard wizard shows the user. Pass `null` for either code to leave
that default unset.

Returns `{"company", "company_name", "template", "accounts",
"account_types", "default_account_receivable",
"default_account_payable"}`.

```
POST /rest/model/company.company/<id>/install_chart
{"template_id": 1, "account_receivable_code": "1.2.1", "account_payable_code": "2.1.1"}
```

### `update_chart()`

Updates the company's chart from its template, mirroring
`account.update_chart.transition_update`: existing accounts / types /
taxes are updated from their templates and missing ones — added to the
template since the install — are created. This is how an existing
company picks up the HesabYar template accounts below after this module
is activated.

Returns `{"company", "company_name", "template", "accounts_before",
"accounts_after", "accounts_added"}`.

```
POST /rest/model/company.company/<id>/update_chart
{}
```

### `process_rest` on `account.payment` (`payment.py`)

Exposes the payment forward step (submitted/approved → processing)
as a REST action. The engine's `process` method is wizard-only in
stock Tryton, and the `proceed` button is NOT the forward step (it
is only visible for succeeded/failed manual payments —
re-processing). `process_rest` is a thin wrapper delegating to the
engine's `process` with access checks disabled for the call --
exactly the scope the wizard's `execute` RPC disables them for. A
plain `__rpc__` registration of `process` does not work because the
REST dispatcher never consults the RPC `check_access` flag. See
`docs/tryton-spikes-502.md` §1 for the full reasoning.

```
POST /rest/model/account.payment/<id>/process_rest
{}
```

### `assign_and_do_rest` on `stock.move` (`move.py`)

Assigns then completes draft stock moves (draft → assigned → done)
as a REST action. A standalone move out of a storage location cannot
be completed through stock Tryton over REST: the `do` button is
readonly on draft moves whenever `assignation_required` is true
(every move out of a storage/view location), and `assign` is neither
a button nor in `__rpc__`. The wrapper calls the engine's own
`assign` + `do` in one transaction, with access checks disabled for
the call — `do`'s body reads `res.user` internally, which the
group-less REST service account cannot read with checks on. Only
draft moves are touched, so a repeated call is a no-op for completed
moves. Used by HesabYar's packaging consumption moves (US-515).

```
POST /rest/model/stock.move/<id>/assign_and_do_rest
{}
```

## Chart extension (`account_chart_en.xml`)

Ten template accounts added to the Universal Chart of Accounts
(English tree). This follows the Tryton-native per-language chart
pattern (see `account_asset/account_chart_en.xml`): the `account`
module ships one chart tree per language (ids suffixed `_en`, `_de`,
…), and a chart extension ships one file per language, attaching to
that language's tree. HesabYar ships only the English file — the
Tryton server runs with language `en`, so the English tree is the one
installed. Names are English, consistent with the rest of the chart;
the Persian labels live in the HesabYar dashboard, which maps account
codes to Persian names in its own UI.

| Code | English | Persian |
| --- | --- | --- |
| 1.2.4 | Notes Receivable Held | اسناد دریافتنی نزد صندوق |
| 1.2.5 | Notes Receivable in Clearing | اسناد دریافتنی در جریان وصول |
| 2.1.5 | Notes Payable | اسناد پرداختنی |
| 7.0.0 | Memorandum Accounts (group) | حساب‌های انتظامی |
| 7.1.0 | Collateral Cheques (group) | اسناد تضمینی |
| 7.1.1 | Collateral Cheques Received | اسناد تضمینی دریافتی |
| 7.1.2 | Collateral Cheques Held by Others | اسناد تضمینی نزد دیگران |
| 7.2.0 | Contra Memorandum Accounts (group) | طرف حساب‌های انتظامی |
| 7.2.1 | Contra - Collateral Cheques Received | طرف حساب اسناد تضمینی دریافتی |
| 7.2.2 | Contra - Collateral Cheques Held by Others | طرف حساب اسناد تضمینی نزد دیگران |

- 1.2.4 / 1.2.5 / 2.1.5 mirror the existing 1.2.1 / 2.1.1 leaves:
  `party_required` + `reconcile`.
- The 7.x groups carry **no type**: Tryton refuses move lines on
  typeless accounts (`account.move.Line.check_account`), so a group
  without a type is unpostable by construction — exactly how the
  universal chart's own groups work. Only the leaves carry the
  off-balance type, and they are `party_required` so cheque journals
  post per party.
- The 7.2.x contra accounts mirror 7.1.x one-for-one: every
  memorandum debit has its contra credit, so the memorandum section
  always nets to zero (Iranian انتظامی bookkeeping).

## Install + activate

```bash
# Build the wheel (offline; needs setuptools + wheel in the build env)
python3 setup.py bdist_wheel
# Install into the Tryton venv
uv pip install --python <tryton-env>/bin/python dist/trytond_hesabyar_setup-8.2.0-py3-none-any.whl
# Activate in the database (with the other US-502 modules)
trytond-admin -c <conf> -d <db> -u hesabyar_setup --activate-dependencies
```

See `scripts/activate-tryton-modules.sh` in the HesabYar repo for the
full module list and the repeatable activation procedure.
