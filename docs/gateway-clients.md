# Gateway payment APIs (US-524, Phase 5-3 Wave B)

One client per gateway, each behind the channel registry, for STATUS
RECONCILIATION only. Settlement itself stays file-based for ALL
gateways — no gateway is assumed to have a settlement API or a server
webhook (verified: none does; see the per-gateway notes below).

Design: phased design §3 §1 (per-gateway checklists) + ADR 0003. The
settlement engine is US-521; settlement accounting is US-522; file
import + three-way matching is US-523; the settlement-center UI is
US-525.

## The rule that shapes everything

A gateway-verified callback creates a `gateway_payment` row (US-521
models), and a BNPL payment creates a receivable from the GATEWAY,
not the customer. The clients never invent a fee % or a settlement
schedule — both come from the channel's ACTIVE contract version via
`SettlementCommandService.record_payment`. The clients never touch
the engine (Tryton): posting the receivable against the gateway party
is US-522's job.

Fail-closed throughout: a disabled channel reconciles nothing; a
channel without stored credentials cannot verify; a gateway answer
that disagrees with the expected amount records nothing; a manual
channel verifies nothing through an API.

## Layout

- `src/hesabyar/domain/gateways/` — ports + models + errors +
  credential parsing. Credential parsing lives in the domain
  because the application layer must not import infrastructure.
- `src/hesabyar/infrastructure/gateways/` — the clients and the
  factory. The factory is the ONLY place that maps a channel key to
  a client implementation.
- `src/hesabyar/application/commands/gateways.py` —
  `GatewayReconciliationService`: verify-and-record, reconcile,
  record-callback, refund.
- `src/hesabyar/interfaces/rest/routers/gateways.py` — the REST API
  (see below).

## Per-gateway API surface

### ZarinPal (`zarinpal`) — REST + GraphQL

Verified from the zarinpal-py-sdk 0.1.1 source (2026-11-19).

- REST base `https://payment.zarinpal.com` (sandbox
  `https://sandbox.zarinpal.com`): `/pg/v4/payment/verify.json`,
  `inquiry.json`, `reverse.json`, `unVerified.json`. Success codes
  100 (verified) / 101 (already verified).
- GraphQL `https://next.zarinpal.com/api/v4/graphql/` (Bearer access
  token): list query `Session(terminal_id, filter, limit, offset)`
  with filters PAID / VERIFIED / TRASH / ACTIVE / REFUNDED; refund
  mutation `AddRefund(session_id, amount, ...)`.
- Settlement: T+1 lump. ⚠️ UNVERIFIED: whether the GraphQL API has
  a settlement query — NOT relied on.
- Verify has no confirmation timestamp in its answer, so
  `gateway_confirmed_at` is the verification time (the list's
  `created_at` refines it when listing).
- Credentials: `merchant_id` (+ `access_token` for GraphQL list /
  refund; `terminal_id` for the list query).

### SnappPay (`snapppay`) — OAuth REST `/api/online`

Verified from the payvand Go client (MIT) + official docs shape.

- Default base `https://api.snapppay.ir` (operator-overridable).
- OAuth `POST /api/online/v1/oauth/token` (form password grant +
  Basic `client_id:client_secret`); envelope
  `{successful, errorData, response}`.
- Follow-ups post `{paymentToken}` to verify / settle / revert /
  cancel; `GET /api/online/payment/v1/status?paymentToken=…`.
- Statuses: SETTLE / CANCEL / VERIFY / PENDING / REVERT.
- Settle-after-verify is MANDATORY (the client does it inside
  `verify_payment`); settlement itself comes from the portal Excel
  + monthly email, never from the API.
- Refund = full revert only; partial amounts are refused rather
  than silently escalated.
- Credentials: `client_id`, `client_secret`, `username`, `password`.

### Torob Pay (`torobpay`) — OAuth REST `/api/online`

⚠️ UNVERIFIED defaults: the shared `/api/online/...` paths and the
default base `https://api.torobpay.com` come from the payvand client
(a secondary source) and have NOT been checked against Torob Pay's
official docs. Everything is operator-overridable via the
credential-payload overrides (`base_url`, `paths`,
`requires_settle`, `scope`).

- Same envelope and follow-ups as SnappPay; settle is
  per-contract (default: NOT required after verify).
- Settlement stays file + bank matching.
- `torobpay` (payment) is never mixed with `torob_marketplace`
  (RESERVED — see below).
- Credentials: `client_id`, `client_secret`, `username`, `password`.

### DigiPay (`digipay`) — UPG

Verified from the phased design + research reports (2,533-line UPG
docs contain zero mentions of a webhook — there is none).

- Base `https://api.mydigipay.com/digipay/api`; OAuth2 password grant.
- `tickets/business?type=11`, `purchases/verify?type={type}`
  (3-minute window, else auto-reverse; idempotent),
  `purchases/deliver?type={type}` (mandatory for CREDIT/BNPL),
  `refunds` (200 = registered only), `refunds/{id}` inquiry
  (0 = Success, 1 = Failed, 2 = Pending).
- Types: WALLET = 11, CREDIT = 5, BNPL = 13, CREDIT-CARD = 24.
- `inquire_status` re-uses the idempotent verify (no separate
  status endpoint exists); a rejection maps to REVERTED.
- `deliver` is exposed on the client but NOT called by the
  reconciliation service — delivery is the store's responsibility.
- ⚠️ UNVERIFIED: the settlement output (what file/report DigiPay
  produces for settlement) — ask DigiPay support. Recorded as an
  open question on the channel's capabilities; never invented.
- Credentials: `client_id`, `client_secret`, `username`, `password`.

### Sadad / Bank Melli (`sadad`) — manual

- No inquiry/refund API (per payvand). Store-side capture + daily
  lump + manual import; the manual client refuses verify / inquiry
  / refund with `GATEWAY_OPERATION_UNSUPPORTED`.
- ⚠️ UNVERIFIED: the Excel output (what the Sadad portal export
  looks like). The "24-hour settlement" figure belongs to
  BehPardakht Mellat, NOT Sadad — do not quote it for Sadad.
- Credentials: none (manual channel).

### SalamPay (`salampay`) — manual

- No API for in-person payments. Store-side capture + manual
  import; the manual client refuses API operations.
- TWO contract models, read from the contract, never assumed:
  standard 10-working-day lump at 5% fee, OR seller credit-line
  monthly at zero fee (both verified — the model is per-contract).
- ⚠️ UNVERIFIED: Mellat-facility settlement timing; whether the
  fee carries VAT.
- Credentials: none (manual channel).

### Custom channels

Any other channel key gets the manual client: no API by design
(phased design §1.7). A store that genuinely uses a channel HesabYar
does not know (e.g. something called "Ilmaz") adds it as a custom
channel with its own contract terms — Ilmaz is NOT a built-in
channel (it is Emalls, a price-comparison search engine; "Ilmaz
Pay" does not exist).

## Registry separation

- `basalam_marketplace` (SalamAPI marketplace channel) is distinct
  from `salampay` (payment channel).
- `torob_marketplace` is RESERVED: seeded DISABLED, and
  `ChannelRegistryService.update_channel` refuses to enable it
  (`INTEGRATION_CHANNEL_RESERVED`, 422) until the Torob ORDER API
  is verified. The factory refuses to build a client for any
  marketplace channel.
- Built-in channels: 8 (was 7 before US-524).

## Credentials (F3 encrypted store)

- Migration 0021 adds `credentials_ciphertext` (Text) +
  `credentials_key_id` (String(64)) to `payment_channels`.
- `PUT /integrations/channels/{channel_key}/credentials`
  (`integrations:manage`) stores the Fernet-encrypted JSON payload;
  the response carries only `has_credentials` — secrets are never
  echoed back.
- The payload holds secrets (per gateway, see above) plus optional
  non-secret overrides (`base_url`, `paths`, `requires_settle`,
  `scope`). Gateway OAuth `password` fields are legitimate API
  credentials here — distinct from the ADR 0003 decision 10 ban on
  INTERNET-BANKING passwords (never stored).
- Required fields are validated at client-build time
  (`GatewayNotConfiguredError` names the missing fields).

## REST API (`/api/v1/gateways`)

Reads need `settlement:read`; mutations need `settlement:manage`.

| Route | Purpose |
| --- | --- |
| `GET /gateways/{channel_key}/capabilities` | What the channel can/cannot do (API vs manual, settlement source, open UNVERIFIED questions). No credentials, no HTTP call. |
| `POST /gateways/{channel_key}/verify-and-record` | Verify with the gateway, record the payment (source `gateway_poll`). 201 created, 200 on idempotent replay. |
| `POST /gateways/{channel_key}/callbacks` | Record a callback the STORE already verified (sources `store_webhook` / `manual_entry` / `manual_import`). 201 / 200 replay. `gateway_poll` is rejected (400 — it goes through verify-and-record). |
| `POST /gateways/{channel_key}/reconcile` | Inquire one payment's status and converge the row: `recorded` / `already_recorded` / `cancelled` / `already_cancelled` / `pending` / `failed` / `unknown`. |
| `POST /gateways/{channel_key}/refunds` | Refund through the gateway, then cancel the row. FULL refunds only — a partial amount is refused (422) because partial-refund tranche semantics do not exist yet (US-522 territory). |

Idempotency: webhook + poll + manual import of the same transaction
converge on ONE `gateway_payment` row (phased design §7.1).

## Error mapping

`GATEWAY_UNREACHABLE` → 502, `GATEWAY_INVALID_RESPONSE` → 502,
`GATEWAY_AUTH_FAILED` / `GATEWAY_REJECTED` / `GATEWAY_NOT_CONFIGURED`
/ `GATEWAY_OPERATION_UNSUPPORTED` / `GATEWAY_VERIFICATION_MISMATCH`
→ 422, `INTEGRATION_CHANNEL_RESERVED` → 422.

## Open questions (UNVERIFIED — do not invent)

1. DigiPay settlement output (what file/report DigiPay produces) —
   ask DigiPay support.
2. Sadad Excel output (what the portal export looks like).
3. ZarinPal settlement query in GraphQL (not relied on).
4. TorobPay base URL + paths (payvand-derived defaults; verify
   against merchant docs).
5. SalamPay Mellat-facility settlement timing; fee VAT.
6. Torob ORDER API (marketplace sync) — gates `torob_marketplace`.

## Testing

- `tests/unit/test_gateway_clients.py` — 48 mocked-HTTP tests
  (success + error + timeout per client).
- `tests/unit/test_gateway_services.py` — 22 service tests
  (fake registry / settlement repo / vault / factory).
- `tests/interfaces/test_rest_gateways.py` — 26 REST tests against
  the real app + DB, with the REAL SnappPay client over
  `httpx.MockTransport`. No live gateway call anywhere.
