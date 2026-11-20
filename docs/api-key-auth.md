# API-key authentication (US-103)

Scoped API-key auth for the HesabYar REST API (design-eng spec §7.5,
§14.1/§14.2). This document is the operator and endpoint-author reference;
the implementation lives in `src/hesabyar/infrastructure/auth/` and the
FastAPI dependencies in `src/hesabyar/interfaces/rest/dependencies.py`.

## The key

- Format: `hya_live_<43 url-safe base64 chars>` (52 characters). The
  random part is 32 bytes from `secrets` — 256 bits of entropy.
- The secret is shown **exactly once**, at issuance. Only two values are
  stored (`api_keys` table, US-102 schema): the visible 16-character
  `key_prefix` (the lookup key) and `key_hash`, a versioned
  `v1:<hex>` HMAC-SHA-256 of the secret keyed with a server-side pepper.
- Comparison is constant-time (`hmac.compare_digest`).
- Keys never appear in logs, URLs, error responses or frontend bundles.
  Server-side logs record the key prefix and a failure reason only
  (`malformed` / `unknown` / `revoked` / `expired` /
  `client_inactive`).

## Configuration

| Setting | Env var | Default |
| --- | --- | --- |
| API-key pepper | `HESABYAR_AUTH__API_KEY_PEPPER` | `hesabyar-dev-pepper-do-not-use-in-production` (dev only) |

Production **refuses to start** with the dev pepper
(`AppSettings` validation). Rotating the pepper invalidates every stored
key hash, so treat it as a long-lived secret: generate one random value
per installation and keep it in the secret manager.

## Sending a key

```http
GET /api/v1/companies HTTP/1.1
Authorization: Bearer hya_live_…
```

The spec does not mandate a header; `Authorization: Bearer` was chosen
over a custom `X-API-Key` header because the Tryton REST API already uses
Bearer, so integrations carry one auth convention, and because FastAPI's
`HTTPBearer` documents it in the generated OpenAPI security scheme.

## Errors

| Situation | Status | Code | Detail |
| --- | --- | --- | --- |
| Missing, malformed, unknown, revoked or expired key, or inactive client | 401 | `UNAUTHORIZED` | `A valid API key is required.` (identical for every failure mode) |
| Authenticated key lacks a required scope | 403 | `FORBIDDEN` | Names the missing scope(s) |
| Key restricted to a different company | 403 | `FORBIDDEN` | `The API key is restricted to a different company.` |
| Unknown company (or a company of another tenant) | 404 | `COMPANY_NOT_FOUND` | — |

All errors use the typed problem-details envelope (§9.1).

## Scopes

Granular capabilities from spec §14.2 — `company:read`, `account:read`,
`ledger:read`, `ledger:draft:create`, `ledger:post`, `invoice:read`,
`reports:read`, `standards:validate`, `integrations:manage`,
`api_keys:manage`, `users:manage`, `settings:manage`, `mcp:connect`, …
(full list: `KNOWN_SCOPES` in `infrastructure/auth/scopes.py`). There is
no wildcard and no `admin` scope: an operation requires exactly the
scopes it lists. Unknown scopes are rejected at issuance, not silently
ignored.

## Company restriction

A key may be restricted to one engine company (`api_keys.engine_company_id`,
an FK to the HesabYar-owned `engine_companies` row — resolve the Tryton
company through that row, never by a cross-database FK). An unrestricted
key may access any company of its tenant; a restricted key only its own.
Company resolution is tenant-scoped: a company belonging to another
tenant looks exactly like a company that does not exist (404).

## Issuing, rotating and revoking keys

There is no HTTP key-management endpoint yet (a later story adds admin
endpoints); keys are issued through `ApiKeyService`, e.g. from a
management console:

```python
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import get_settings

service = ApiKeyService(ApiKeyCrypto(get_settings().auth.api_key_pepper))
issued = service.issue_key(
    session,
    client=client,                 # an ApiClientModel row
    name="hayat shop key",
    scopes=["company:read", "account:read"],
    installation_id="00000000-0000-0000-0000-000000000001",
    engine_company_id=None,        # or an engine_companies.id to restrict
    expires_at=None,               # or an aware datetime
)
print(issued.secret)  # shown exactly once — hand it to the owner, then drop it
session.commit()
```

- **Rotate:** `service.rotate_key(session, key=key)` revokes the old key
  (reason `rotated`) and issues a replacement with identical scopes,
  installation, company restriction and expiry. Rotation never extends a
  key's lifetime.
- **Revoke:** `service.revoke_key(session, key=key, reason="compromised")`
  takes effect immediately; revoked keys are rejected on the next request.
- **Last used:** every successful authentication records
  `api_keys.last_used_at` (committed by the REST dependency).

The service flushes but never commits — the caller owns the transaction.

## Endpoint authors

```python
from fastapi import Depends
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_authenticated_client,
    require_company_access,
    require_scopes,
)

# Scope enforcement only:
@router.get("/companies")
def list_companies(
    client: AuthenticatedClient = Depends(require_scopes("company:read")),
): ...

# Scope + company restriction (route must have a {company_id} path param):
@router.get("/companies/{company_id}")
def get_company(
    company_id: str,
    client: AuthenticatedClient = Depends(require_company_access),
): ...

# Authenticated identity without a scope requirement:
@router.get("/me")
def me(client: AuthenticatedClient = Depends(get_authenticated_client)): ...
```

`require_company_access` resolves `{company_id}` to an `engine_companies`
row within the client's tenant (matching either the HesabYar row id or the
engine's external company id) and enforces the key's restriction.

Admin endpoints use `require_admin`, which is `require_scopes("settings:manage")`
(the US-101 placeholder admin token is gone — US-103 replaced it).

Tests override `get_authenticated_client` with a canned
`AuthenticatedClient` via `app.dependency_overrides`; tests that exercise
auth itself use real keys against the test database
(`tests/interfaces/test_rest_auth.py`).

## Out of scope (noted for later stories)

- `allowed_ips` and `rate_limit_per_minute` columns exist on `api_keys`
  (US-102 schema) but are not enforced yet — IP allowlisting and rate
  limiting are separate stories.
- No HTTP endpoints for key management yet; issuance/rotation/revocation
  go through `ApiKeyService` directly.
- The per-request session dependency builds a short-lived engine per
  request (matching US-101's health probes); a shared engine/session
  factory lands with the connection-pool story.
