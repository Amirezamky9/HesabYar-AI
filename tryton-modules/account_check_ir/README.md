# account_check_ir — HesabYar Iranian cheque module (Tryton 8.2, GPL-3.0)

Custom versioned Tryton module for the Iranian cheque registry
(HesabYar Phase 5-4, ۵ث; phased design §5; ADR 0003 decision 7,
ADR 0004). No open-source Iranian cheque module exists and Tryton core
has no cheque module, so this one is built — modelled only on the
*pattern* of `tryton-ar/account_check_ar` (two models + one journal
per state transition), never on its code.

## Models

- `account.checkbook` — a chequebook: bank, bank account, first/last
  leaf numbers, computed used / voided / remaining leaves.
- `account.check` — one model for received and issued cheques
  (`kind`), commercial or collateral (`purpose`, mandatory from day
  one). Sayyad id is a 16-digit identifier, unique among all live
  states; `sayad_status` (unregistered / registered / confirmed /
  transferred) is tracked separately from the accounting `state`.
  Amounts are whole Rial (`digits=(16, 0)`); dates are stored as
  Gregorian `Date` in Tryton and shown as Jalali in the HesabYar UI.
- `account.check.endorsement` — one row per endorsement in a cheque's
  chain (endorser, date, Sayyad transfer receipt, reason).
- `account.check.custody_log` — append-only, read-only log of every
  physical leaf move (origin, destination, time, actor, reason). A
  custody move is a custody event, never a journal.

## Rules enforced here (scaffold, US-530)

- `sayad_id` must be exactly 16 digits; `amount` must be > 0;
  `due_date >= issue_date`; an issued leaf number must fall inside
  its checkbook's first/last range.
- `sayad_id` uniqueness is a partial exclusion constraint: it applies
  to every state *except* `expired_unreceived` and
  `rejected_mismatch`, so a leaf that never arrived (or was rejected
  on mismatch) can be re-registered with the same id — the old record
  never revives.
- The custody log refuses `write` and `delete`.

## What is deliberately NOT here yet

- Lifecycle transitions and their journals (سند ۱–۱۳) land in US-531
  / US-532 — one atomic Tryton transaction per transition, state +
  journal together.
- The Sayyad field shapes for the Fintech scopes are UNVERIFIED
  (§5 §9.2 #1–3); no Fintech field is guessed. US-534 builds the
  simulator + manual fallback.
- `installment_leg` is a plain HesabYar reference (Char) until Phase
  ۵ج builds the real installment-leg model; `secured_obligation`
  is likewise a reference (installment plan / contract / invoice)
  until a real obligation model exists.
- The cheque's scan/photo is the `attachment` link to one
  `ir.attachment` record (the primary leaf scan); further files use
  Tryton's standard attachment list on the record.

## Chart accounts

The cheque accounts live in the Iranian chart extension in
`hesabyar_setup` (`account_chart_en.xml`): 1.2.4 Notes Receivable
Held (اسناد دریافتنی نزد صندوق), 1.2.5 Notes Receivable in Clearing
(اسناد دریافتنی در جریان وصول), 2.1.5 Notes Payable (اسناد
پرداختنی), 1.2.9 Bounced Cheques (چک‌های برگشتی / مطالبات
غیرجاری), 7.1.1 / 7.1.2 collateral memorandum accounts + 7.2.1 /
7.2.2 contra accounts (اسناد تضمینی انتظامی دریافتی / نزد دیگران +
طرف حساب‌های انتظامی). The dependent Tryton modules
(`account_statement`, `account_payment_clearing`, `account_dunning`
+ `account_dunning_fee`) are installed, not rewritten.

## Install

Build the wheel (`python3 setup.py bdist_wheel`), install it into
the Tryton venv, then `trytond-admin -c <conf> -d <db> -u
account_check_ir --activate-dependencies` (a single small module:
direct activation against Supabase is acceptable, per the US-516
precedent in `docs/tryton-modules.md`). Existing companies pick up
the new chart template account with `update_chart`.
