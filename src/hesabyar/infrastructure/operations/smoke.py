"""Post-deploy smoke suite (US-706, Phase 7).

Runs the deployment checklist's smoke checks against a deployed
HesabYar instance — health, readiness, login (API-key auth), one
report, one MCP call — plus an OPTIONAL webhook check that is off
by default. See ``docs/deployment-iran.md`` §12 and ``scripts/smoke.py``.

**Read-only by default.** Every check except the webhook check is a
GET or a read-only MCP tool call: nothing is written, nothing is
posted, no production data is touched. The webhook check is the one
exception and it is opt-in only: it posts ONE event to a
dedicated smoke-test connector/store (never a real store's
connector), then re-posts it to prove idempotent re-delivery. The
event lands in the smoke tenant's review queue — the operator
clears it afterwards (or leaves it as a visible smoke artifact).
There is no destructive step in this suite, by default or by flag.

**No secrets in output.** The report never contains the API key or
the webhook secret — neither is echoed into any check detail, the
rendered text, or the JSON dict. Tests grep the report for both.

The MCP check uses the real MCP SDK client (``streamable_http_client``
+ ``ClientSession``, the same pattern ``tests/mcp/test_mcp_http.py``
proves) against the deployed MCP origin. For unit tests the call is
injectable (``mcp_call``), because there is no deployed server in CI.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

import httpx

SmokeStatus = Literal["pass", "fail", "skip"]

#: The read-only MCP tool the smoke suite calls (scope ``reports:read``).
MCP_SMOKE_TOOL = "get_trial_balance"

#: Default per-request timeout (the dashboard batch took ~105s against
#: remote Supabase once — smoke checks stay generous, never tight).
DEFAULT_TIMEOUT_SECONDS = 60.0

#: The webhook check's event type: a payment capture is the smallest
#: event that exercises ingest + HMAC + idempotency end to end.
WEBHOOK_EVENT_TYPE = "payment.captured"


@dataclass(frozen=True)
class SmokeCheck:
    """One check's outcome. ``detail`` never contains a secret."""

    name: str
    status: SmokeStatus
    detail: str


@dataclass(frozen=True)
class SmokeReport:
    """The whole suite's outcome, in check order."""

    checks: tuple[SmokeCheck, ...] = ()

    @property
    def ok(self) -> bool:
        """True when no check failed (skips do not fail the suite)."""
        return all(check.status != "fail" for check in self.checks)

    @property
    def failed(self) -> tuple[SmokeCheck, ...]:
        """The failed checks, in order."""
        return tuple(check for check in self.checks if check.status == "fail")

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable report (no secrets — see the module docstring)."""
        return {
            "ok": self.ok,
            "checks": [
                {"name": check.name, "status": check.status, "detail": check.detail}
                for check in self.checks
            ],
        }

    def render_text(self) -> str:
        """One line per check, plus a summary line."""
        lines = []
        for check in self.checks:
            mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[check.status]
            lines.append(f"{mark} {check.name}: {check.detail}")
        lines.append("smoke: OK" if self.ok else "smoke: FAILED")
        return "\n".join(lines)


@dataclass(frozen=True)
class WebhookCheckConfig:
    """What the OPTIONAL webhook check needs.

    ``connector`` / ``store_id`` name a DEDICATED smoke-test connector
    and store — never a real store's connector. The secret is the
    smoke store's HMAC webhook secret (from the environment, never a
    CLI argument — it would show in ``ps``).
    """

    connector: str
    store_id: str
    secret: str


@dataclass(frozen=True)
class SmokeConfig:
    """Everything the suite needs to run against one deployment."""

    api_base_url: str
    mcp_url: str
    api_key: str
    company: str | None = None
    date_from: date = field(default_factory=lambda: date(date.today().year, 1, 1))
    date_to: date = field(default_factory=date.today)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    webhook: WebhookCheckConfig | None = None

    def api_url(self, path: str) -> str:
        """Join ``path`` onto the API base URL (one slash, always)."""
        return f"{self.api_base_url.rstrip('/')}{path}"


@dataclass(frozen=True)
class McpCallResult:
    """Outcome of the smoke MCP tool call."""

    ok: bool
    balanced: bool | None
    detail: str


def _error_detail(prefix: str, exc: Exception) -> str:
    """One-line failure detail for an exception (no secrets — the
    exception text comes from httpx/the SDK, never from our config)."""
    text = str(exc) or exc.__class__.__name__
    return f"{prefix}: {text[:200]}"


def _check_health(client: httpx.Client, config: SmokeConfig) -> SmokeCheck:
    """Liveness: ``GET /api/v1/health`` answers 200 + ``status: ok``."""
    try:
        response = client.get(config.api_url("/api/v1/health"))
    except httpx.HTTPError as exc:
        return SmokeCheck("health", "fail", _error_detail("unreachable", exc))
    if response.status_code != 200:
        return SmokeCheck(
            "health", "fail", f"HTTP {response.status_code} (expected 200)"
        )
    try:
        body = response.json()
    except ValueError:
        return SmokeCheck("health", "fail", "response is not JSON")
    if body.get("status") != "ok":
        return SmokeCheck("health", "fail", f"unexpected status: {body.get('status')!r}")
    return SmokeCheck(
        "health", "pass", f"ok (service {body.get('service', '?')}, env {body.get('environment', '?')})"
    )


def _check_readiness(client: httpx.Client, config: SmokeConfig) -> SmokeCheck:
    """Readiness: ``GET /api/v1/readiness`` answers 200 + ``ready``.

    A 503 means the API is up but a dependency (HesabYar DB or Tryton)
    is not — the check names the failing component when the body says
    so, so the operator knows where to look.
    """
    try:
        response = client.get(config.api_url("/api/v1/readiness"))
    except httpx.HTTPError as exc:
        return SmokeCheck("readiness", "fail", _error_detail("unreachable", exc))
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or body.get("status") != "ready":
        components = body.get("components") or []
        failed = [
            str(item.get("name", "?"))
            for item in components
            if isinstance(item, dict) and item.get("status") not in ("ok", "ready")
        ]
        detail = f"HTTP {response.status_code}"
        if failed:
            detail += f" — failing components: {', '.join(failed)}"
        elif body.get("status"):
            detail += f" — status {body.get('status')!r}"
        return SmokeCheck("readiness", "fail", detail)
    return SmokeCheck("readiness", "pass", "ready (database + Tryton reachable)")


def _check_auth(
    client: httpx.Client, config: SmokeConfig
) -> tuple[SmokeCheck, str | None]:
    """Login: the operator API key authenticates a scoped read.

    ``GET /api/v1/companies`` (scope ``company:read``) proves the key
    works and the tenant mapping resolves. It also resolves the smoke
    company: the configured ``--company`` when given, else the first
    company the key can see (the report + MCP checks read that one).
    Returns the check plus the resolved company id (None on failure).
    """
    try:
        response = client.get(config.api_url("/api/v1/companies"))
    except httpx.HTTPError as exc:
        return SmokeCheck("login", "fail", _error_detail("unreachable", exc)), None
    if response.status_code == 401:
        return SmokeCheck("login", "fail", "HTTP 401 — the API key is invalid, revoked or expired"), None
    if response.status_code == 403:
        return SmokeCheck("login", "fail", "HTTP 403 — the API key lacks scope company:read"), None
    if response.status_code != 200:
        return SmokeCheck("login", "fail", f"HTTP {response.status_code} (expected 200)"), None
    try:
        body = response.json()
    except ValueError:
        return SmokeCheck("login", "fail", "response is not JSON"), None
    items = body.get("items") or []
    if config.company is not None:
        known = {str(item.get("id")) for item in items if isinstance(item, dict)}
        known |= {str(item.get("engine_id")) for item in items if isinstance(item, dict)}
        if config.company not in known:
            return (
                SmokeCheck(
                    "login",
                    "fail",
                    f"company {config.company!r} is not visible to this key "
                    f"({len(items)} compan{'y' if len(items) == 1 else 'ies'} listed)",
                ),
                None,
            )
        return (
            SmokeCheck("login", "pass", f"key ok; company {config.company!r} visible"),
            config.company,
        )
    if not items:
        return SmokeCheck("login", "fail", "key ok but the tenant has no companies"), None
    first = items[0]
    company_id = str(first.get("id"))
    name = first.get("name", "?")
    return (
        SmokeCheck("login", "pass", f"key ok; using first company {name!r} ({len(items)} listed)"),
        company_id,
    )


def _check_report(
    client: httpx.Client, config: SmokeConfig, company: str | None
) -> SmokeCheck:
    """One report: the trial balance for the smoke company + period.

    Read-only (scope ``reports:read``). An unbalanced trial balance
    over posted moves fails the smoke — posted moves are balanced by
    the engine, so an unbalanced read means the deployment or the
    ledger has a real problem, not a smoke problem.
    """
    if company is None:
        return SmokeCheck("report", "skip", "no company resolved (login check failed)")
    try:
        response = client.get(
            config.api_url("/api/v1/reports/trial-balance"),
            params={
                "company": company,
                "date_from": config.date_from.isoformat(),
                "date_to": config.date_to.isoformat(),
            },
        )
    except httpx.HTTPError as exc:
        return SmokeCheck("report", "fail", _error_detail("unreachable", exc))
    if response.status_code == 403:
        return SmokeCheck("report", "fail", "HTTP 403 — the API key lacks scope reports:read")
    if response.status_code != 200:
        return SmokeCheck("report", "fail", f"HTTP {response.status_code} (expected 200)")
    try:
        body = response.json()
    except ValueError:
        return SmokeCheck("report", "fail", "response is not JSON")
    if not body.get("balanced", False):
        return SmokeCheck(
            "report",
            "fail",
            f"trial balance does NOT balance: debit {body.get('total_debit')} != "
            f"credit {body.get('total_credit')} (IRR)",
        )
    return SmokeCheck(
        "report",
        "pass",
        f"trial balance balanced: {body.get('move_count', 0)} moves / "
        f"{body.get('line_count', 0)} lines, total {body.get('total_debit', 0)} IRR",
    )


def _webhook_envelope(config: WebhookCheckConfig, event_id: str) -> dict[str, Any]:
    """The smoke webhook's envelope: a minimal ``payment.captured``.

    Amounts are in Toman (the store prices in Toman; the connector
    boundary converts ×10) — but no accounting document is ever
    created: the smoke connector has no mappings, so the pipeline
    routes the event to the review queue. The envelope has the same
    shape as the contract tests' ``raw_envelope`` (all required §10.2
    fields), so ingest accepts it.
    """
    return {
        "schema_version": 1,
        "source_system": config.connector,
        "source_store_id": config.store_id,
        "source_event_id": event_id,
        "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "currency": "IRR",
        "external_entity_type": "payment",
        "external_entity_id": f"smoke-{event_id}",
        "correlation_id": f"smoke-{event_id}",
        "payload": {
            "payment_channel": "smoke",
            "gateway_payment_id": f"smoke-{event_id}",
            "gross_amount": 1000,
            "fee_amount": 0,
            "net_amount": 1000,
        },
    }


def _sign_webhook(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex signature — the same computation the receiver
    verifies (``tests/interfaces/test_rest_store_webhooks.py``)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _check_webhook(
    client: httpx.Client, config: SmokeConfig
) -> SmokeCheck:
    """OPTIONAL webhook check (skipped unless configured).

    Posts ONE signed event to the dedicated smoke connector, then
    re-posts the identical body: the first post must be 201
    ``created`` with the signature verified, the re-post 200
    ``duplicate`` — ingest + HMAC + idempotency, end to end. The
    event lands in the smoke tenant's review queue (no mappings for
    the smoke connector); the operator clears it afterwards.
    """
    webhook = config.webhook
    if webhook is None:
        return SmokeCheck(
            "webhook", "skip", "optional check not requested (pass --webhook to run it)"
        )
    event_id = f"smoke-{uuid.uuid4().hex[:12]}"
    body = json.dumps(_webhook_envelope(webhook, event_id)).encode()
    headers = {
        "Content-Type": "application/json",
        "X-HesabYar-Event-Type": WEBHOOK_EVENT_TYPE,
        "X-HesabYar-Signature": _sign_webhook(webhook.secret, body),
    }
    url = config.api_url(f"/api/v1/integrations/{webhook.connector}/events")
    try:
        first = client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        return SmokeCheck("webhook", "fail", _error_detail("unreachable", exc))
    if first.status_code == 401:
        return SmokeCheck(
            "webhook", "fail", "HTTP 401 — signature rejected (wrong smoke secret?)"
        )
    if first.status_code == 403:
        return SmokeCheck(
            "webhook", "fail", "HTTP 403 — the API key lacks scope integrations:manage"
        )
    if first.status_code != 201:
        return SmokeCheck("webhook", "fail", f"first post: HTTP {first.status_code} (expected 201)")
    try:
        first_body = first.json()
    except ValueError:
        return SmokeCheck("webhook", "fail", "first post: response is not JSON")
    if first_body.get("status") != "created":
        return SmokeCheck(
            "webhook", "fail", f"first post: status {first_body.get('status')!r} (expected 'created')"
        )
    if not first_body.get("signature_verified", False):
        return SmokeCheck("webhook", "fail", "first post: signature was NOT verified")
    try:
        second = client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        return SmokeCheck("webhook", "fail", _error_detail("re-post unreachable", exc))
    if second.status_code != 200:
        return SmokeCheck("webhook", "fail", f"re-post: HTTP {second.status_code} (expected 200)")
    try:
        second_body = second.json()
    except ValueError:
        return SmokeCheck("webhook", "fail", "re-post: response is not JSON")
    if second_body.get("status") != "duplicate":
        return SmokeCheck(
            "webhook",
            "fail",
            f"re-post: status {second_body.get('status')!r} (expected 'duplicate' — idempotency broken)",
        )
    return SmokeCheck(
        "webhook",
        "pass",
        "created + verified, re-post duplicate (event in the smoke tenant's review queue — clear it)",
    )


async def _run_mcp_call(config: SmokeConfig, company: str) -> McpCallResult:
    """One read-only MCP tool call against the deployed MCP origin.

    The SDK client negotiates the protocol (max 2025-11-25 — gotcha:
    2026-07-28 is unreachable via initialize) and the session
    carries the API key as a Bearer token. ``get_trial_balance``
    needs ``mcp:connect`` at the transport plus ``reports:read`` for
    the tool; the key's company restriction (if any) must cover the
    smoke company.
    """
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import TextContent

    # The SDK speaks httpx2 (its own runtime dependency — the same
    # client the MCP HTTP tests pass in), and its streamable client
    # takes auth headers only via the passed-in client, so the smoke
    # check builds one with the Bearer token (exactly the pattern
    # tests/mcp/test_mcp_http.py uses).
    headers = {"Authorization": f"Bearer {config.api_key}"}
    timeout = httpx2.Timeout(config.timeout_seconds)
    async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client:
        async with streamable_http_client(
            config.mcp_url, http_client=http_client
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    MCP_SMOKE_TOOL,
                    {
                        "company": company,
                        "date_from": config.date_from.isoformat(),
                        "date_to": config.date_to.isoformat(),
                    },
                )
    if result.is_error:
        return McpCallResult(False, None, "tool call returned an error result")
    texts = [
        block.text for block in result.content if isinstance(block, TextContent)
    ]
    if not texts:
        return McpCallResult(False, None, "tool call returned no text content")
    try:
        payload = json.loads(texts[0])
    except ValueError:
        return McpCallResult(False, None, "tool result is not JSON")
    balanced = payload.get("balanced")
    if balanced is None:
        return McpCallResult(False, None, "tool result has no 'balanced' field")
    if not balanced:
        return McpCallResult(False, False, "trial balance does NOT balance (via MCP)")
    return McpCallResult(
        True,
        True,
        f"trial balance balanced via MCP ({payload.get('move_count', 0)} moves)",
    )


def _default_mcp_call(config: SmokeConfig, company: str) -> McpCallResult:
    """Run the MCP check synchronously (``asyncio.run`` + wait_for,
    the same shape as the MCP HTTP tests). Any transport/protocol
    failure is a failed check, never an exception out of the suite."""
    try:
        return asyncio.run(
            asyncio.wait_for(_run_mcp_call(config, company), timeout=config.timeout_seconds)
        )
    except Exception as exc:  # noqa: BLE001 — the suite reports, never raises
        return McpCallResult(False, None, _error_detail("MCP call failed", exc))


def _check_mcp(
    config: SmokeConfig,
    company: str | None,
    mcp_call: Callable[[SmokeConfig, str], McpCallResult],
) -> SmokeCheck:
    """One MCP call: ``get_trial_balance`` over Streamable HTTP."""
    if company is None:
        return SmokeCheck("mcp", "skip", "no company resolved (login check failed)")
    result = mcp_call(config, company)
    if not result.ok:
        return SmokeCheck("mcp", "fail", result.detail)
    return SmokeCheck("mcp", "pass", result.detail)


def run_smoke_suite(
    config: SmokeConfig,
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
    mcp_call: Callable[[SmokeConfig, str], McpCallResult] | None = None,
) -> SmokeReport:
    """Run every check in order and return the report.

    ``client_factory`` builds the REST client (default: a real
    ``httpx.Client`` with the API key as a Bearer token); tests
    inject a ``MockTransport`` client. ``mcp_call`` runs the MCP
    check (default: the real SDK client); tests inject a fake.
    """
    if client_factory is None:

        def _default_client() -> httpx.Client:
            return httpx.Client(
                headers={"Authorization": f"Bearer {config.api_key}"},
                timeout=config.timeout_seconds,
            )

        client_factory = _default_client
    if mcp_call is None:
        mcp_call = _default_mcp_call
    checks: list[SmokeCheck] = []
    with client_factory() as client:
        checks.append(_check_health(client, config))
        checks.append(_check_readiness(client, config))
        auth_check, company = _check_auth(client, config)
        checks.append(auth_check)
        checks.append(_check_report(client, config, company))
        checks.append(_check_webhook(client, config))
    checks.append(_check_mcp(config, company, mcp_call))
    return SmokeReport(checks=tuple(checks))
