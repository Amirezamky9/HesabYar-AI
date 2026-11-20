# Cheques (US-531 + US-533) — build summary

Received commercial cheque lifecycle + atomic journals + manual
entry (Phase 5-4 ۵ث, story US-531) and site intake
(`cheque.received.registered` / `cheque.received.cancelled`,
US-533). The Tryton module `account_check_ir` is US-530's
deliverable (built in parallel); this story is the HesabYar layer
against the contracts fixed in `prd.json` US-530/US-531,
documented in `docs/cheque-engine-contract.md`.

## What was built

- **Domain** (`src/hesabyar/domain/cheques/`): `Cheque` /
  `ChequeSettings` / `CustodyLogEntry` / `InquiryReceipt` models;
  `state_machine.py` — the §2.1 transitions, the §2.4 journal
  designs (سند ۱–۶ as account roles), pre-acceptance verdicts,
  installment-leg hooks; `validation.py` — the 24-field payload
  (fail-closed, all field errors at once, Toman→Rial ×10 at the
  boundary); `ports.py` — the repository port +
  `registration_to_cheque`.
- **Application** (`src/hesabyar/application/commands/cheques.py`,
  `src/hesabyar/application/queries/cheques.py`): the command
  service (register / confirm-receipt / deposit / clear / bounce /
  endorse / return / expire / reject-mismatch / receipts / custody
  / daily job) and the query service (get / list / settings).
- **Engine seam** (`src/hesabyar/infrastructure/accounting/tryton/`):
  `CreateEngineCheque` / `TransitionEngineCheque` commands,
  `ChequeEngineView`, adapter methods (`create_engine_cheque`,
  `get_engine_cheque`, `list_engine_cheques`,
  `transition_engine_cheque`) — one `__rpc__` action call per
  transition, engine-first, read-back after. 403/404 → "module not
  installed".
- **Repository** (`src/hesabyar/infrastructure/persistence/postgres/cheques_repository.py`
  + migration `0023`): tables `cheques`, `cheque_custody_log`,
  `cheque_inquiry_receipts`, `cheque_settings`; partial unique
  index `uq_cheques_sayad_id_live` (live states only — expired /
  rejected leaves free their `sayad_id`).
- **REST** (`/api/v1/cheques`, 11 routes): create, list, settings,
  detail, confirm-receipt, deposit, clear, bounce, endorse,
  return, reject-mismatch, receipts. Scopes `cheques:read`,
  `cheques:write`, `cheques:transition` (catalog now 43).
- **Worker** (`src/hesabyar/infrastructure/events/cheque_queue.py`,
  `scripts/run_event_worker.py`): daily `cheque.daily` job (06:30)
  — day-3 + day-(N-1) never-arrived reminders, expiry at the
  deadline; idempotent; a failed expiry is retried next run.
- **Engine side** (`tryton-modules/account_check_ir/`, US-531,
  after the rebase onto merged US-530): `Check.transition_rest`
  — one `__rpc__` method, one Tryton transaction per transition
  (state + سند ۱–۶ journal; `destination_account` field added for
  `clear`; `move.py` extends `account.move._get_origin` with
  `account.check` so journals link back; moves post to the MISC
  journal, dated today in the open period; the move description
  carries the سند number). The adapter resolves the issuer party
  at intake (find-or-create `party.party` by national id →
  `party.code`); a journal transition without a party fails
  closed. Installed on lane 2 (`trytond-admin -u
  account_check_ir` + trytond restart).

## The atomicity rule (golden rule)

Every transition is engine-FIRST, then mirrored into the
HesabYar registry. Engine failure leaves the registry untouched;
a registry-write failure after engine success files a
`cheque_exception` review item and raises
`ChequeRegistryDivergenceError` (never silent). Registration is
the exception: intake is a claim, so the registry row is created
first (state `registered`, NO journal), then the engine link; a
row with `engine_id=None` is retried by re-registering the same
`sayad_id` instead of duplicating.

## Policy gate (F3/F4)

Every journal-creating transition passes the three-layer gate
(event type + amount cap + period lock) through
`PolicyGateService`; the verdict travels to the engine as
`auto_post`. Draft is the default — a journal auto-posts only
with an explicit opt-in. Internal gate event-type keys:
`cheque.transition.held` / `.in_clearing` / `.cleared` /
`.bounced` / `.endorsed` / `.returned` (NOT canonical events;
the only canonical cheque event is
`cheque.received.registered`, ADR 0004/US-530).

## Settings defaults (§9.1 build defaults)

Leaf-arrival deadline 7 days; clearing window has NO hard-coded
default (§9.1 #6 / docs/cheque-decisions.md — the standard clearing
duration is UNVERIFIED, so the setting stays unset until the
installation sets it; US-534 defines behaviour while it is unset);
bounce destination `receivables`; red/brown policy
`manager_approval`; default storage `vault`. `auto_post` is NOT
in cheque settings — it lives in installation settings +
per-event policy rows (default draft).

## Decisions worth remembering

- Re-presentment is DISABLED: `bounced → in_clearing` raises
  (§9.1 #5).
- Pre-acceptance before `held`: the four Sayyad items must match
  (mismatch → never confirm, use `reject_mismatch`); Sayyad
  acceptance required; colour 1–5 (1=white … 5=red, count-based);
  colour ≥4 or in-transit commitments above threshold → the
  installation policy (reject or manager approval); white colour
  always carries the "رنگ سفید تضمین نیست" warning.
- Endorsement to a non-supplier (§9.1 #12): state moves + Sayyad
  transfer registered, NO journal guessed — `defer_journal=True`,
  `journal_pending_review=True`, review item filed.
- Bounce: requires reason + Jalali bounce date + bank
  non-payment certificate reference; reversal in the CURRENT
  period; dunning notification filed as a `cheque_exception`
  review item (subtype `cheque_bounced`).
- Custody moves (cashbox↔vault) are custody events, NEVER
  journals; only for `held` cheques. Custody log is append-only.
- Inquiry receipts store verdict/code/time/reference only, never
  raw PII (§3.5). SMS cost 3,500 Rial. Missing receipt at
  confirm-receipt → `receipt_missing_warning=True` (transition
  proceeds with warning, per US-534's mandatory-receipt rule).
- `kind=issued` / `purpose=collateral` are carried on the model
  from day one but their transitions are refused here (US-532
  owns them); collateral registration (intake, no journal) is
  allowed.
- Colour mapping 1=white…5=red is a judgment call — US-534 to
  confirm against the CBI source.

## US-533 — site intake (rebased onto the US-534 merge)

- **Wiring**: the two canonical names are ingestible now —
  `CanonicalEventType` members, `cheque.registered` /
  `cheque.cancelled` store webhook topics (nine topics), the
  poll/sync mapping, and the pipeline branch
  (`EventPipelineService._run_cheque_intake` → `SiteChequeIntakeHandler`
  → `ChequeCommandService.register_site` /
  `cancel_site_registration`). No policy gate on intake (no
  journal); the gate is evaluated at `registered → held`.
- **Convergence**: exactly one HesabYar cheque per site
  registration — the partial unique index on `sayad_id` now also
  excludes `voided` rows; `same_cheque_claim` compares the
  business-identifying subset (optional fields only when both
  sides carry a value). Identical re-registration returns the
  existing record; a contradicting payload files
  `duplicate_sayad_id` (CRITICAL) and raises
  `ChequeDuplicateError` — the handler translates it into a
  processed outcome, so the event is never retried.
- **Payee check**: the mapped company's national id is its Tryton
  party's `code` (`CompanyView.national_id`, via
  `list_companies`); unknown company / no national id / mismatch
  all fail closed to the review queue (`MappingUnresolvedError`).
- **Three-way matrix** (`domain/cheques/mismatch.py`): site vs
  leaf vs Sayyad on amount/date; an unknown reading is not a
  mismatch; any disagreement files `three_way_mismatch`
  (CRITICAL) and rejects the intake — never confirm on mismatch.
  `verify_leaf_against_sources` runs the matrix; the caller then
  confirms receipt. Review subtypes:
  `three_way_mismatch`, `duplicate_sayad_id`,
  `cancellation_after_held`, `stale_in_clearing`,
  `never_arrived_cheque`, `ambiguous_matching`.
- **Cancellation**: before `held` → `registered → voided` (the
  engine's `void_intake` transition, no journal; the sayad_id is
  free for a new registration). After `held` →
  `cancellation_after_held` review (manual return, never
  automatic void). Already-terminal replays as a no-op (via the
  terminal-inclusive `find_by_sayad_id` lookup); unknown
  sayad_id fails closed.
- **Scan**: `scan_url` is downloaded best-effort into the local
  filestore (`<dir>/cheques/<sayad_id>-<hash>.<ext>`,
  `HESABYAR_FILESTORE__DIRECTORY`, default `filestore/`) as
  `scan_filestore_ref`; the site's own `scan_ref` is untouched.
  The Sayyad leaf QR is site-side capture — the 16 digits arrive
  as `sayad_id` and are validated fail-closed.
- **Intake queue**: `GET /cheques/intake` (cheques:read) lists
  `registered` cheques oldest first with `age_days`;
  `ChequeResponse` also carries `source_event_id` and
  `scan_filestore_ref`. Day-3 / day-(N-1) reminders and expiry
  already ran in the daily Procrastinate job (US-531); US-533
  adds the `never_arrived_cheque` filing on expiry and the
  `stale_in_clearing` filing (once per cheque, past due date +
  clearing window; never auto-bounce). When no clearing window
  is configured the staleness cannot be judged and those cheques
  are skipped — US-534 did not change that default.
- **Migration 0024** (`0024_us533_cheque_intake.py`) adds
  `source_event_id` / `scan_filestore_ref` /
  `stale_in_clearing_filed_at` and widens the live-sayad index
  to exclude `voided`. NOTE: US-534 claimed revision `0024` on
  `ralph/phase-5-4` first — this file collides with
  `0024_us534_cheque_reconciliation.py` (same revision id); the
  coordinator re-chains it to `0025` at merge, so `alembic`
  commands fail on this branch until then (see Deferred).

## Deferred

- **Transition methods in the module** (`transition_rest` + a
  `destination_account` field): the US-530 scaffold has NO
  transition methods — its docstring says "US-531 / US-532 add
  one atomic transaction per transition". They land AFTER the
  rebase onto merged US-530 (the scaffold is not in this branch
  yet — it exists only as the US-530 worker's uncommitted work).
  The contract is fully specified in
  `docs/cheque-engine-contract.md` (one `__rpc__` method, returns
  `{"id", "state", "move_id"}`); the live transition tests in
  `tests/contract/test_cheques_us531.py` skip until the action is
  exposed (403 probe). Intake via direct REST `create` IS verified
  live on lane 2.
- **Party resolution** (issuer → `party.party`): all cheque
  accounts are `party_required`, so no journal can post without
  a party. The adapter sends `party` only when the command
  carries one; the find-or-create (natural key: national id →
  `party.code`, since no `party.identifier.type` model is
  installed) lands with the transition methods.
- **Bank resolution** (`issuer_bank` "056 سامان" → `bank`
  record): no engine field takes the code+name string, and no
  `bank` records exist in the test DBs. Leave unset in US-531.
- **Two-way test with a real `installment_leg`** (۵ج): the link +
  hooks are implemented (`leg_status_effect`: clear → leg `paid`,
  bounce → leg `unpaid`, persisted as `installment_leg_status`);
  the test against a real leg LANDED in US-542
  (`tests/contract/test_installments_us542.py`) — the leg side
  (pending_clearance, the collection hook) is in
  `docs/installments.md`.
- **Rebase onto merged US-530**: align the adapter to the real
  module (done for intake — direct `create`, real field set),
  add `transition_rest` to the module, install on lane 2,
  re-run the targeted + live contract tests on that tree, then
  claim the story.

## US-532 — Issued cheques + collateral (off-balance-sheet) + endorsement + custody

Issued lifecycle (سند ۱۰–۱۳), collateral memorandum (سند ۷/۸), call-in
(سند ۹الف + ۹ب), unexpected call-in, endorsed bounce, checkbook page
(report 8), segregation (report 5/6). Phase 5-4 ۵ث, story US-532.

- **Domain**: `ChequeState` += `ISSUED` / `VOIDED` / `CALLED_IN`;
  `state_machine.py` — the issued + collateral transitions,
  `assert_transition_scope(kind, purpose, state, transition)` (the
  mirror of the engine's `_check_scope`), `JOURNAL_DESIGNS` now
  10 (سند ۱–۶ + ۱۰ + ۱۳ + ۹ب + ۷-معکوس), `COLLATERAL_JOURNAL_DESIGNS`
  (سند ۷/۸ + issued mirrors, memorandum roles only),
  `ISSUED_JOURNAL_DESIGNS` (سند ۱۱/۱۲); `checkbook.py` — the
  Fintech chequebook-inquiry SIMULATOR (never invents Sayyad data).
- **Application**: command service gains `issue_cheque` / `void_issued`
  / `call_in` / `unexpected_call_in` / `record_endorsed_bounce`;
  `clear` / `bounce` / `return` branch by kind (issued bounce needs
  reason + date only, no certificate, no dunning; issued-collateral
  return goes back to the vault); `move_custody` allows `called_in`;
  the daily job skips issued cheques in `registered`. Query service
  gains the segregation seams: list filters (kind / purpose /
  endorsee_type / storage_location), `list_commercial_cheques`
  (`purpose=payment` only), `collateral_summary` (report 6, with
  the `secured_obligation` dimension), `endorsement_summary`
  (report 5, supplier vs other), `cashbox_vault_leaves` (§6),
  `get_checkbooks` / `get_checkbook_status` (report 8).
- **Engine seam**: `AccountingEnginePort` += `list_engine_checkbooks`
  / `get_engine_checkbook`; the adapter sends `checkbook` +
  `secured_obligation` at create, resolves the BENEFICIARY party
  for issued cheques, passes `accountant_approval` / `void_reason`,
  and merges the `move_ids` map (call-in's two moves).
- **Repository** (migration `0024` — **COLLIDES with US-534's
  `0024`; the coordinator re-chains this story's migration to
  `0025` at merge**): `secured_obligation`,
  `checkbook_engine_id`, `call_in_approved_by/at`, `void_reason`,
  `unexpected_call_in`, `call_in_statement_reference` + the
  segregation indexes.
- **REST** (`/api/v1/cheques`, +8 routes): `issue`, `void`,
  `call-in`, `unexpected-call-in`, `endorsed-bounce`,
  `checkbooks`, `checkbooks/{id}`, `collateral-summary`,
  `endorsement-summary`.
- **Engine side** (`tryton-modules/account_check_ir/`):
  `transition_rest` gains `issue` / `void` / `call_in` /
  `unexpected_call_in` / `endorsed_bounce`; `_check_scope`
  enforces the issued/collateral scope; **سند ۵/۱۰ correction:
  Trade Payables is 2.1.1, Notes Payable is 2.1.5** (US-531 had
  debited 2.1.5 for سند ۵ — verified live on lane 1, the contract
  tests assert the move-line codes). Installed on lane 1
  (`trytond-admin -u account_check_ir` + trytond restart).
- **Gated, honestly**: the call-in design is the §2.4 proposal
  applied per case under accountant approval — it still awaits
  the accountant's confirmation (open review item). The
  unexpected-call-in commercial journal is NEVER guessed (alert +
  matching + filing only). The endorsed-bounce journal is NEVER
  guessed (transition + evidence + CRITICAL filing only).
