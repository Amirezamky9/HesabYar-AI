# ADR-0003: Store connectivity, banks, gateways and cheques — dual-entry architecture

- **Status:** Accepted — 2026-11-17 (user approved execution on the phased design built from this ADR: `hesabyar-reference/phased-design-checklist-2026-11-17.md`). Round-2 findings incorporated 2026-11-17 (see below).
- **Date:** 2026-11-17
- **Research:** `hesabyar-reference/research/store-connectivity-banking-2026-11-17.md` (round 1) and `hesabyar-reference/research/store-connectivity-banking-round2-2026-11-17.md` (round 2, 337 lines + 3,106 lines of parts; 116 ⚠️ UNVERIFIED references across the part files — re-check before any contract or architecture commitment)

## Context

On 2026-11-17 the user corrected the architecture: store data (orders and
invoices auto-created on the store website) must flow into HesabYar
**automatically via endpoints, alongside manual entry** — manual entry alone is
not viable. Direct store-database access must not be forbidden.

The user then answered the research open questions (~22:11 local):

1. The Hayat store is **custom-coded** (not WooCommerce/Sazito). The connector
   must be generic and code-compatible, usable on any platform for any
   audience (future-proofing, not a one-store hack).
2. **Bank Melli is a must**; other banks are good to have.
3. Gateways: **ZarinPal + Melli (Sadad)**.
4. **Cheques exist** but are handled manually today.
5. The production server **will move to Iran later** (no IP problem).
6. **Legal entities** for companies, **natural persons** for simple sites —
   the product must serve both tiers.

Research findings that bear on this decision:

- The spec's SQL ban (§19 rule 2) covers **Tryton's database only**. Nothing
  in the spec forbids reading the store's database; the earlier "forbidden"
  framing was overstated.
- **No open-source Iranian cheque module exists anywhere**, and Tryton core
  has no cheque module. Cheques must be built.
- **No unified national bank API** exists in Iran; the Central Bank has no
  comprehensive open-banking framework. Fintech is the only verified
  aggregator with public docs + OAuth2 + statements (5 banks: Ayandeh,
  Keshavarzi, Parsian, Khavarmianeh, Saderat — legal entities only). Bank
  Melli has **no public statement API** (Sadad is payment-only).
- **No payment gateway has a settlement API** — settlement data comes only
  from portal Excel exports / email.
- Tryton 8.2 already has most installment tooling (multi-step payment terms,
  `account_payment_clearing`, `account_dunning` + `account_dunning_fee`).
- Nearly all Iranian banking services require an **Iranian IP**.

Spec gaps this ADR closes: §10 assumes a store adapter but names no platform;
"cheque" appears **zero** times in the spec; Toman→Rial conversion is
unaddressed; settlement has no defined source; no bank-integration layer
exists.

## Decision

1. **Dual-entry principle (§10 / Phase 5).** Manual entry and automatic store
   connectivity are both first-class. Every auto-created record must be
   traceable to its source event. Draft is the default; auto-post is an
   explicit, reversible opt-in per installation — no store event silently
   creates a posted journal.
2. **Generic store-connector contract (platform-agnostic).** HesabYar exposes
   a canonical events inbound API (orders, invoices, refunds, payments) plus
   a webhook receiver. Any custom-coded store integrates by calling it; the
   Hayat store is the first integration via this generic contract, not via a
   platform adapter. WooCommerce / Sazito adapters become optional later
   additions, not the foundation. Install Tryton 8.2 sale modules (`sale`,
   `sale_shipment_cost`, `sale_payment`, `sale_discount`, `sale_amendment`,
   `sale_invoice_grouping`) — do not rewrite them.
3. **Direct store-DB read is explicitly allowed as read-only
   reconciliation/backfill only**: read-only database user, zero writes, no
   PII dumps, documented versioned queries. The primary path is always
   webhook + API polling. This clarifies — not contradicts — §19 rule 2.
4. **Toman→Rial conversion (×10) at the connector boundary**, explicit and
   tested, with a per-store "store prices are in Toman" setting.
5. **Bank layer — Melli-first, two tiers.**
   - Manual Excel/CSV statement import is the **first** bank capability
     (no public Melli statement API exists; per-bank column mapping, Jalali
     dates, Persian digits, deduplication).
   - Fintech as the aggregator for its 5 covered banks, for **legal
     entities** only. Bankimaye Mellat / Cobalt are contract-based
     alternatives for later.
   - **Natural persons** use the manual-import path plus no-contract
     inquiry services (e.g. Ehrazino for Shaba).
   - **Deployment requirement:** production must be hosted in Iran (or have
     Iranian egress) — banking APIs require an Iranian IP.
6. **Gateways — ZarinPal + Sadad (Melli).** Settlement reporting is built on
   **file import + manual entry + bank reconciliation**, never on an assumed
   live API (none exists). In BNPL the receivable is from the **gateway**,
   not the customer; gateway fees + VAT on fees are recorded separately.
7. **Cheques — build `account_check_ir`.** A custom versioned Tryton module
   (cheque state + accounting entries atomically in the ledger) with HesabYar
   API/UI through the existing port — the same verdict as the Phase 2
   setup-wizard spike. Phase 1: lifecycle + automatic journal entries +
   due/bounced reports, **manual entry first**. The model carries
   `purpose: payment | collateral` from day one: collateral cheques are
   **off-balance-sheet** (memorandum accounts) and never enter payables or
   cash forecasts. Sayyad fields are manual until an API is verified
   (Fintech holds the full Sayyad set for legal entities).
8. **Installments — reuse Tryton, build the schedule.** Reuse multi-step
   payment terms, clearing accounts and dunning; HesabYar builds the
   per-invoice installment schedule plus gradual recognition of unrealised
   finance income (installment method).
9. **Workflows.** Phase 5 inbox/outbox worker with a three-layer policy gate
   (event type + amount cap + period lock), manual DLQ replay from the UI,
   decision audit log (policy/mapping version per decision), and settlement
   tie-out (settlement total = bank deposit) before auto-post. Scheduler:
   **Procrastinate** (MIT, Postgres-native, no Redis). Jalali-aware
   scheduling (`next_run` computed in-app — no Jalali cron exists).
   User-defined rules v1 after Phase 5: `when <event> + <conditions> →
   do <action>` with a closed, safe action set; **no arbitrary code
   execution** (do not copy Odoo's Execute Python Code action).
10. **Credentials.** Production secrets in the Vault; per-installation
    credentials encrypted in the database, never in a shared `.env`; OAuth
    tokens encrypted in the database (they have a lifecycle), never in
    `.env`. **Never ask for or store a customer's internet-banking
    password** — the correct model is OAuth/consent, not password sharing.

## Round 2 updates (2026-11-17)

Round-2 research (6 researchers) confirmed, corrected and extended the above.
These decisions are part of this ADR:

1. **Settlement is per-gateway/contract configurable, never hard-coded to one
   model.** A versioned `gateway_contract` carries the settlement model
   (`tranche_legs`), fee %, payment window and auto-post policy; a
   `settlement_tranche` row exists per tranche of every payment (state
   machine expected → due → confirmed → received → settled, plus
   partial/overdue/exception/cancelled/written-off). **Fee % is entered
   manually by the user per channel/contract — never hard-coded**
   (user requirement 2026-11-17): rates change over time (up or down, e.g.
   year to year), so a rate change creates a NEW contract version with an
   effective date — past settlements keep the old rate, new ones use the
   new rate, and history is never rewritten. Default BNPL model: 4
   monthly tranches; DigiPay / SalamPay / ZarinPal / Sadad follow their own
   contracts (lump-sum or tranches). Re-verification 2026-11-17: SalamPay
   has TWO models — standard 10-business-day lump-sum at 5% fee, and a
   seller credit-line model settling monthly at installment due dates with
   ZERO fee (which model applies is per-contract; the user's memory matches
   the credit-line model). Snapp Pay's proportional per-tranche fee
   deduction is confirmed (official 4-tranche example). Snapp Pay fee
   ranges move over time (9–18% in Oct 2026 vs 10–15% in Nov 2026) —
   another reason fees are never hard-coded. The "24-hour" settlement
   figure belongs to BehPardakht Mellat, NOT Sadad. The sale month is the anchor; the last
   tranche is the remainder so tranches sum exactly to net (rounding rule).
   Fee + VAT-on-fee are recorded per tranche. Merchant settlement ≠ customer
   installments: the gateway bears customer-default risk.
2. **Ilmaz is REMOVED from payment channels.** "Ilmaz" is Emalls
   (emalls.ir), a price-comparison search engine since 1391; it moves no
   money and has no payment API. "Ilmaz Pay" does not exist. No Party,
   journal or settlement import for it. Open question: if "Ilmaz" is
   genuinely active in Hayat as a payment method, which legal entity /
   contract is it?
3. **New API paths (corrections to round 1):** Torob Pay HAS an OAuth REST
   payment API (Verify/Refund/Inquiry/Callback — verified via the MIT
   `payvand` client + official WooCommerce plugin; no public docs, no
   settlement API); ZarinPal HAS a GraphQL transaction API (T+1 settlement;
   a settlement query is still ⚠️ UNVERIFIED). Ehrazino's Sayyad inquiry API
   (cheque colour 1,700 Toman/call, no contract) is the low-friction
   inquiry path for natural persons.
4. **Browser automation (RPA) is explicitly NOT a product path** — proposed
   new §19 rule: no automated login to bank/gateway portals; only official
   APIs or manual file import. (ZarinPal ToS §3-4 and DigiPay ban bots /
   intermediary software; computer-crimes law art. 1: 91 days–1 year,
   art. 11 for banking systems: 3–10 years; OTP/captcha block unattended
   automation. Screen-scraping is never built.)
5. **No API ≠ no automation.** The automation design that solves ~80% of
   manual tracking without entering any portal: store-side capture (the
   store already receives the payment callback) + expected settlement
   schedule (tranches derived from the payment event) + automatic matching
   against bank statements. Snapp Pay monthly settlement Excel may also be
   parsed from email (IMAP).
6. **Fintech inquiry results are ephemeral:** store only verdict/code/time,
   never raw PII content (contractual Fintech term).
7. **Cheque intake from site registration creates NO journal entry.**
   `cheque.received.registered` creates an intake record (`site_registered`)
   with a 23-field payload (14 mandatory + 9 optional, counted from
   part-2-4 §3.1); the first journal entry is issued only when the
   physical leaf arrives and Sayyad registration is confirmed (an
   unregistered leaf has no commercial validity; ruling 870: a claim to
   compel registration is inadmissible). Three-layer reconciliation: Sayyad
   for legal entities (all 18 Fintech scopes verified), Sayyad for natural
   persons (Ehrazino API + structured SMS-inquiry receipts), and clearing
   matched against bank statements (exact amount + leaf number, 4
   confidence levels, never guess on ambiguous).
8. **Unified review queue + event contracts.** One dashboard + one unified
   review queue for all 9 exception types (uniform structure, ageing/SLA
   alerts, manual DLQ replay, mapping suggestions). New event names beyond
   the 9 in spec §10.2 require ADR approval. The store-side contract: 8
   outbound webhooks + read-only polling API + read-only DB read.
   HesabYar never duplicates: payment pages, gateway panels, marketplace
   panels, a second ledger, validation, or direct Sayyad connection.
9. **New modules:** packaging/shipping costs (consumables in Tryton `stock`,
   reorder points in `stock_supply`, `sale_shipment_cost`,
   per-order cost via `analytic_sale`, days-of-cover + price calculator)
   and cash-box reconciliation (Iranian 3-step method: supervised count,
   signed minutes as supporting document, shortage/surplus journal;
   Tryton `account_statement` covers daily cash books).
10. **Digikala marketplace connector** (seller API, `finance` scope — the
    only documented settlement API found; docs login-gated, contact
    Marketplace-API@digikala.com), separate from gateways.
11. **Deployment requirement (confirmed):** production hosted in Iran or
    with Iranian egress (banking APIs require an Iranian IP).
12. **Payment channels are a configurable registry, not a hard-coded list**
    (user requirement 2026-11-17). HesabYar serves ALL stores, not just
    Hayat — nothing may be hard-limited to Hayat's integrations. Each
    installation has a payment-channel registry: built-in channels
    (ZarinPal, Sadad, Snapp Pay, DigiPay, Torob Pay, SalamPay, …) that can
    be enabled/disabled/selected, plus the ability to define a CUSTOM
    channel manually (name, settlement model, fee %, payment window) for
    anything not built in — e.g. a store-specific or regional payment
    method. Ilmaz stays out of the built-in list (it is not a payment
    channel), but a store that genuinely uses something called "Ilmaz" can
    add it as a custom channel with its own contract terms. The settlement
    engine, webhook routing and import mapping all work off the registry,
    never off a hard-coded channel list.

## Consequences

- **Phase 5 scope changes:** the generic connector API comes first;
  platform adapters are optional follow-ups. The Hayat integration is a
  generic-contract integration.
- **New Tryton modules to install:** the `sale` family, plus
  `account_payment_clearing`, `account_dunning` + `account_dunning_fee`.
  New custom modules: `account_check_ir` and the Iranian-chart installer
  (already approved in Phase 2).
- **New HesabYar entities:** cheque registry, installment schedules, gateway
  settlement imports, statement import mappings, bank-connection registry
  (per-company encrypted credentials), workflow rules, scheduler jobs.
- **Proposed new §19 rules:** no writes to the store database; no silent
  assumptions about gateway settlement; never store a customer's
  internet-banking password.
- **Open items (verify before building):** ZarinPal settlement export format;
  Fintech transaction/balance scope names (read from service detail pages);
  Torob Pay / Basalam Pay only on demand.

## Amendment 2026-11-18 — SMS notification providers + automatic posting modes

(User answers to the two open questions from the requirements-batch-1
integration (`hesabyar-reference/user-requirements-2026-11-18.md`, reviewed
with Superpowers; log at
`hesabyar-reference/requirements-batch1-review-log-2026-11-18.md`).
Recorded here as a dated amendment, not a rewrite.)

1. **SMS notifications are multi-provider and pluggable.** Outbound SMS
   (installment/cheque due reminders and other notifications) goes through
   the notification-provider registry (`notification_provider`, F3), with
   one interface every provider must satisfy. First built-in providers:
   Kavenegar + sms.ir (user answer 2026-11-18: "کاوه نگار ، sms.ir و چنتا
   گزینه دیگه هر کدوم باید سازگار باشه"); additional providers can be added
   per installation. No single provider is hard-coded. Per-provider variable
   facts (price per SMS, rate limits, service vs dedicated line,
   contract/eNamad requirements) are ENTERED MANUALLY by the user per
   provider and NEVER hard-coded (user decision 2026-11-18 — same principle
   as gateway fees, which also vary over time); the registry must provide
   editable fields for them. What still needs verification per provider is
   the API integration shape (endpoints, auth method, message format,
   delivery-status callbacks).
2. **Automatic journal posting supports BOTH modes (confirmation, no
   change).** The user confirmed (2026-11-18) that both automatic draft
   creation and automatic definitive posting are required. This matches
   decision 1 as written (draft by default, auto-post explicit opt-in), so
   no decision change is needed; the phased design records both modes with
   draft as the default.
