# Cheque decisions — §5 §9.1 open decisions with build defaults (US-530)

Source: `hesabyar-reference/phased-design-checklist-2026-11-17.md` §5
§9.1 (15 open decisions) + §9.2 (UNVERIFIED items). This document
records every decision with the default the build uses and what stays
gated. **Nothing accountant-gated is designed here by guess** — the
gated flows are built as transition + review-queue filing + gate, and
their journal design waits for the accountant.

Per-installation settings live in the cheque settings panel (US-535):
clearing window, leaf-arrival deadline, auto-post policy, bounce
destination. Env names (names only, never values):
`CHEQUE_CLEARING_WINDOW_DAYS`, `CHEQUE_SITE_EXPIRY_DAYS`.

## #1 — When is the first journal issued?

**Build default: at the `registered → held` transition.** A site
registration is a claim, not a delivery; a leaf unregistered in
Sayyad has no commercial validity (art. 21 repeated + note 1;
ruling 870). The intake record (`registered`) has **no journal**;
the first journal (سند ۱) is issued only when the physical leaf has
arrived and Sayyad registration is confirmed. **If the user later
wants an earlier journal, the whole never-arrived exception design
changes** — this is the decision that blocks the entire ۵ث core, and
it is settled on the research proposal unless the user reopens it.

## #2 — Bounce destination account

**Build default: `حساب‌های دریافتنی` (1.2.1).** The alternative
destination — `چک‌های برگشتی / مطالبات غیرجاری` — exists in the
chart as **1.2.9 Bounced Cheques** (added by US-530) and is chosen
by a **per-installation setting**. The bounce journal (سند ۴) is
posted in the **current** period, never by reopening a closed one.
Handling a bounced cheque (destination choice + reason/certificate +
dunning start) is an accountant / follow-up-owner action.

## #3 — Journal for a bounced endorsed cheque — ACCOUNTANT-GATED

**No guessed journal design.** A bounced cheque that was endorsed
away is no longer our asset; its bounce means we owe the next
beneficiary — a separate flow with a reverse-of-endorsement journal.
**US-532 (built, 2026-11-19):** the transition (`endorsed_bounce`,
endorsed → bounced, NO journal) + evidence recording (reason +
date + bank certificate) + CRITICAL review-queue filing are built
and verified live; the journal design itself is STILL gated —
the accountant designs it per case. Blocks only the
bounced-endorsed-cheque flow.

## #4 — Collateral call-in (`called_in`) — ACCOUNTANT-GATED

**No guessed journal design; accountant approval per case (§5 §4).**
The proposed design (two simultaneous journals in one transaction:
سند ۹الف memorandum reversal + سند ۹ب commercial recognition, then
the §2.1 cycle) is a *proposal* that must be confirmed with the
accountant before implementation.
**US-532 (built, 2026-11-19):** the transition + per-case approval
gate + review filing are built and verified live — the engine
applies the §2.4 proposal as specified (۹الف + ۹ب in one
transaction, purpose flips to `payment`). Honest note: applying
the proposal is NOT the same as the accountant confirming it —
the review filing carries that confirmation as an open item, and
the design stays a proposal until the accountant signs it off.
The same gate applies to the unexpected call-in of an *issued*
collateral cheque (counterparty took it to the bank without
notice): **US-532 builds** the immediate CRITICAL alert +
statement-withdrawal matching (by leaf number) + memorandum
reversal + review filing — and the commercial journal is
deliberately NOT posted and NOT designed here; it remains
accountant-gated by design. Blocks only the `called_in`
transition.

## #5 — Re-presentment of a bounced cheque — DISABLED

**`bounced → in_clearing` is DISABLED in the build** (UNVERIFIED —
open question for the accountant/bank, §9.2 #10). Only
`bounced → returned` + a replacement payment (cash / new cheque) is
supported. Blocks only the re-presentment transition.

## #6 — Clearing window + leaf-arrival deadline

**Both configurable per installation; leaf-arrival deadline default
is 7 days** (`CHEQUE_SITE_EXPIRY_DAYS`, proposal from the design;
Procrastinate reminders on day 3 and day N−1; a record that expires
never revives — a late leaf is a new registration with the same
`sayad_id`). **The clearing window has no hard-coded product-wide
default**: the standard clearing duration is UNVERIFIED (§9.2 #6)
and must not be hard-coded. The per-installation setting
(`CHEQUE_CLEARING_WINDOW_DAYS`) is a go-live checklist item (§5
§1.6); US-534 defines the behaviour while it is unset. Blocks only
the clearing-window default value — the build ships with the setting.

## #7 — Auto-post policy for cheque journals

**Build default: draft.** Every auto-created cheque journal stays a
draft until a human confirms it (the golden rule, ADR 0003 decision
1). Automatic definitive posting is supported **only with explicit,
reversible opt-in per installation** (user answer 2026-11-18: both
modes required; draft is the default). Every auto-created journal
passes the three-layer policy gate (event type + amount cap +
period lock). Blocks automatic posting of cheque journals.

## #8 — One cheque, one order?

**Build default: one cheque links to one order / invoice.** A cheque
split across two orders goes to the unified review queue (proposal
from part-2-4 §7.4 — needs confirmation). `applied_amount_irr`
records the amount allocated when a cheque amount differs from the
linked document's amount. Blocks only cheque-splitting across orders.

## #9 — Fintech contract: which scopes + tariff per call

**Open — blocks the Fintech contract (C2, legal tier).** The 18
Sayyad scopes are named in §5 §1.3, but their exact input/output
fields (§9.2 #1), the `ac-sayad-*` auth model (#2) and the tariff
per call (#3) are UNVERIFIED and must be read from the Fintech
service-detail pages / console before any contract or
implementation. Until then: simulator + manual fallback (US-534),
manual `sayad_status`, structured inquiry receipts. No production
Fintech call without credentials.

**US-534 verification note:** the simulator wires all 18 scope
names (10 base + 8 `sms-*`) and the live client raises
`SayyadLiveNotReadyError` — no field was guessed. Details:
`docs/sayyad-reconciliation.md`.

## #10 — Ehrazino for natural persons

**Open — blocks the API colour path in the natural tier** (§9.2
#9: whether Ehrazino opens a business account for natural persons).
Until verified: the natural tier uses manual inquiry + structured
`cheque_inquiry_receipts` (SMS / CBI website / bank app), and colour
is issuer history only — never a statement about a specific leaf.
Blocks only the Ehrazino API path.

**US-534 verification note (2026-11-19):** verified from
Ehrazino's public API pages — cheque-color: input `nationalCode`
(10 digits natural / 11 digits legal), output colour name +
numeric code 1–5, **1,700 Toman = 17,000 Rial/call**; sayad-check:
input `sayadId` (16 digits), outputs `branch_code`, `check_type`,
`expiration_date`, `iban`, `issue_date`, `name`,
`returned_cheques`, `sayad_id`, `serial_no`, `series_no`,
**6,900 Toman = 69,000 Rial/call**, no colour. Still UNVERIFIED:
endpoint URL path + auth header (docs page could not be opened)
and the natural-person business account (§9.2 #9). The live
client raises `SayyadLiveNotReadyError` until those land.
Details: `docs/sayyad-reconciliation.md`.

## #11 — What is "عدم وصول" (user step 6)? — DEFERRED-GATED

**Build default: interpretation (a)** — a cheque deposited to the
bank that neither cleared nor produced a non-payment certificate
(= a *stale in-clearing* cheque, §5 §2.1; no separate state, it
stays in `in_clearing` and is flagged in the aging report,
report 4). Interpretations (b) (a past-due cheque never deposited)
and (c) (any other meaning) would need a separate terminal state;
the question stays open for the user/accountant. Blocks only a
separate "عدم وصول" state, if one is ever needed.

## #12 — Endorsement to a non-supplier ("other person") — ACCOUNTANT-GATED

**No guessed journal design.** The debit account for an endorsement
to a person with no matching payable is an open accountant question.
**US-532 (built, 2026-11-19):** the transition + Sayyad transfer
registration + review-queue filing are built and verified; the
journal is STILL deferred (no journal guessed) and
`endorsee_type=other` is shown separately in report 5. The
endorsement to a *supplier* (سند ۵: حساب‌های پرداختنی / اسناد
دریافتنی نزد صندوق) is designed and is not gated — with the
US-532 correction that سند ۵ debits Trade Payables **2.1.1**,
not Notes Payable 2.1.5 (verified live on lane 1). Blocks only
the endorsement-to-other journal.

## #13 — Late fee on a bounced installment cheque — DEFERRED-GATED

**Build default: the `pending_clearance` interval is NOT overdue,
and no late fee accrues on it, unless the accountant says
otherwise.** The fee's start date — the installment's original due
date or the bounce date — needs accountant confirmation, as does
whether the interval counts as overdue. US-531 implements the
link + leg-status hooks; the two-way test with a real
`installment_leg` lands in ۵ج. Blocks only the bounced-installment
/ dunning interaction.

## #14 — Default storage location + custody-move control

**Build default: `vault` (خزانه) at the `held` transition**,
configurable per installation (`cashbox` is the other option the
design proposes). **Custody-move control: single-operator entry**
— one treasurer/accountant records the move with actor + time in
`account.check.custody_log`; no second approval (four-eyes) is
required by default. Four-eyes approval is a per-installation
option, not the default. A custody move is a custody event, never a
journal. Blocks only the default value and the move control (both
shippable with these defaults).

## #15 — One cheque covering several installments? — DEFERRED-GATED

**Build default: one cheque covers one installment leg**
(`installment_leg` + `applied_amount_irr` for a partial
allocation). Multi-leg allocation (one cheque for a leg + part of
another) is deferred to ۵ج — it is the same family as #8 and needs
confirmation of how the amount is allocated per leg. Blocks only
multi-installment allocation of a single cheque.

## Blocking summary (§9.1 خط مسدودی)

- #1 blocks the whole ۵ث core — **settled on the research proposal
  above unless the user reopens it**.
- #2 and #7 block the bounce flow and automatic posting —
  **settled with the defaults above**.
- #3, #4, #5, #12 block only their own flows — **accountant-gated,
  no guessed design**.
- #6 and #14 block only default values — **settled with the
  defaults above** (clearing window: configurable, no hard-coded
  standard).
- #8, #11, #13, #15 block only their own flows — **deferred-gated
  with the defaults above**.
- #9 and #10 block the Fintech contract and the natural-tier API
  colour path — **open, simulator + manual fallback until verified**.
