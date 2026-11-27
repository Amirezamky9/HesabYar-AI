"""Unit tests for the US-706 post-deploy smoke suite.

Every check runs against an ``httpx.MockTransport`` — no server, no
database — and the MCP check runs against an injected fake (the real
SDK client needs a deployed MCP origin, which CI does not have; the
SDK call itself is the pattern ``tests/mcp/test_mcp_http.py`` proves).

The suite's two safety properties are asserted here, not just its
happy path:

* read-only by default — the only POST the suite ever makes is the
  opt-in webhook check's, and it is skipped unless configured;
* no secrets in output — the rendered text and the JSON dict are
  grepped for the API key and the webhook secret on every run shape.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from hesabyar.infrastructure.operations.smoke import (
    McpCallResult,
    SmokeCheck,
    SmokeConfig,
    SmokeStatus,
    WebhookCheckConfig,
    run_smoke_suite,
)

API_KEY = "hya_live_smoke-test-key-000000000000000000000000000000"
WEBHOOK_SECRET = "smoke-webhook-secret-0000000000000000"
API_URL = "https://hesabyar.example.com"
MCP_URL = "https://mcp.example.com/mcp"
COMPANY_ID = "company-row-1"


def _config(**overrides: object) -> SmokeConfig:
    values: dict[str, object] = {
        "api_base_url": API_URL,
        "mcp_url": MCP_URL,
        "api_key": API_KEY,
        "date_from": date(2026, 1, 1),
        "date_to": date(2026, 11, 21),
    }
    values.update(overrides)
    return SmokeConfig(**values)  # type: ignore[arg-type]


def _mcp_ok(config: SmokeConfig, company: str) -> McpCallResult:
    assert company == COMPANY_ID
    return McpCallResult(True, True, "trial balance balanced via MCP (6 moves)")


def _mcp_error(config: SmokeConfig, company: str) -> McpCallResult:
    return McpCallResult(False, None, "tool call returned an error result")


def _handler(
    requests: list[httpx.Request],
    *,
    health_status: int = 200,
    health_body: dict | None = None,
    readiness_status: int = 200,
    readiness_body: dict | None = None,
    companies_status: int = 200,
    companies: list[dict] | None = None,
    report_status: int = 200,
    report_body: dict | None = None,
    webhook_posts: list[int] | None = None,
):
    """A MockTransport handler serving the happy-path API by default."""
    posts_seen: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v1/health":
            return httpx.Response(
                health_status,
                json=health_body
                or {"status": "ok", "service": "hesabyar", "environment": "production"},
            )
        if path == "/api/v1/readiness":
            return httpx.Response(
                readiness_status,
                json=readiness_body or {"status": "ready", "components": []},
            )
        if path == "/api/v1/companies":
            items = (
                companies
                if companies is not None
                else [
                    {
                        "id": COMPANY_ID,
                        "engine_id": 1,
                        "name": "Smoke Store",
                        "currency_code": "IRR",
                    }
                ]
            )
            return httpx.Response(
                companies_status, json={"items": items, "next_cursor": None}
            )
        if path == "/api/v1/reports/trial-balance":
            return httpx.Response(
                report_status,
                json=report_body
                or {
                    "balanced": True,
                    "total_debit": 1000,
                    "total_credit": 1000,
                    "move_count": 6,
                    "line_count": 12,
                },
            )
        if path.startswith("/api/v1/integrations/") and path.endswith("/events"):
            posts_seen.append(1)
            statuses = webhook_posts or [201, 200]
            status = statuses[len(posts_seen) - 1] if len(posts_seen) <= len(statuses) else 200
            created = status == 201
            return httpx.Response(
                status,
                json={
                    "status": "created" if created else "duplicate",
                    "inbox_event_id": "evt-1",
                    "inbox_status": "received",
                    "signature_verified": True,
                    "signature_note": "HMAC-SHA256 signature verified.",
                    "derived_inbox_event_ids": [],
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    return handle


def _run(config: SmokeConfig, handler, *, mcp_call=_mcp_ok):
    transport = httpx.MockTransport(handler)
    return run_smoke_suite(
        config,
        client_factory=lambda: httpx.Client(
            transport=transport,
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=30.0,
        ),
        mcp_call=mcp_call,
    )


def _by_name(report) -> dict[str, SmokeCheck]:
    return {check.name: check for check in report.checks}


def _assert_no_secrets(report) -> None:
    rendered = report.render_text()
    dumped = json.dumps(report.to_dict(), ensure_ascii=False)
    for secret in (API_KEY, WEBHOOK_SECRET):
        assert secret not in rendered, "secret leaked into the text report"
        assert secret not in dumped, "secret leaked into the JSON report"


# ---------------------------------------------------------------------------
# Happy path + shape
# ---------------------------------------------------------------------------


def test_all_checks_pass_and_webhook_skipped_by_default() -> None:
    requests: list[httpx.Request] = []
    report = _run(_config(), _handler(requests))
    assert report.ok
    assert [check.name for check in report.checks] == [
        "health",
        "readiness",
        "login",
        "report",
        "webhook",
        "mcp",
    ]
    checks = _by_name(report)
    assert checks["health"].status == "pass"
    assert checks["readiness"].status == "pass"
    assert checks["login"].status == "pass"
    assert checks["report"].status == "pass"
    assert checks["webhook"].status == "skip"
    assert checks["mcp"].status == "pass"
    # Read-only by default: no POST anywhere.
    assert all(request.method == "GET" for request in requests)
    _assert_no_secrets(report)


def test_explicit_company_is_used_and_verified_visible() -> None:
    requests: list[httpx.Request] = []
    report = _run(_config(company=COMPANY_ID), _handler(requests))
    assert report.ok
    report_request = next(
        r for r in requests if r.url.path == "/api/v1/reports/trial-balance"
    )
    assert report_request.url.params["company"] == COMPANY_ID


def test_report_json_shape() -> None:
    requests: list[httpx.Request] = []
    report = _run(_config(), _handler(requests))
    data = report.to_dict()
    assert data["ok"] is True
    assert len(data["checks"]) == 6
    assert data["checks"][0]["name"] == "health"
    assert data["checks"][0]["status"] == "pass"
    assert "smoke: OK" in report.render_text()


def test_api_url_joins_one_slash() -> None:
    assert _config(api_base_url="https://x.example.com/").api_url("/api/v1/health") == (
        "https://x.example.com/api/v1/health"
    )
    assert _config(api_base_url="https://x.example.com").api_url("/api/v1/health") == (
        "https://x.example.com/api/v1/health"
    )


# ---------------------------------------------------------------------------
# Health + readiness failures
# ---------------------------------------------------------------------------


def test_health_unreachable_fails() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    report = _run(_config(), handle)
    assert not report.ok
    assert _by_name(report)["health"].status == "fail"
    _assert_no_secrets(report)


def test_health_non_200_fails() -> None:
    requests: list[httpx.Request] = []
    report = _run(_config(), _handler(requests, health_status=503))
    assert _by_name(report)["health"].status == "fail"
    assert "503" in _by_name(report)["health"].detail


def test_health_wrong_status_fails() -> None:
    requests: list[httpx.Request] = []
    report = _run(
        _config(), _handler(requests, health_body={"status": "degraded"})
    )
    assert _by_name(report)["health"].status == "fail"


def test_readiness_503_names_the_failing_component() -> None:
    requests: list[httpx.Request] = []
    report = _run(
        _config(),
        _handler(
            requests,
            readiness_status=503,
            readiness_body={
                "status": "not_ready",
                "components": [
                    {"name": "database", "status": "ok"},
                    {"name": "tryton", "status": "error"},
                ],
            },
        ),
    )
    check = _by_name(report)["readiness"]
    assert check.status == "fail"
    assert "tryton" in check.detail
    assert not report.ok
    _assert_no_secrets(report)


# ---------------------------------------------------------------------------
# Login (auth) failures + company resolution
# ---------------------------------------------------------------------------


def test_login_401_means_bad_key() -> None:
    requests: list[httpx.Request] = []
    report = _run(_config(), _handler(requests, companies_status=401))
    check = _by_name(report)["login"]
    assert check.status == "fail"
    assert "401" in check.detail
    # Without a company, the report + MCP checks skip (they cannot run).
    assert _by_name(report)["report"].status == "skip"
    assert _by_name(report)["mcp"].status == "skip"
    assert not report.ok
    _assert_no_secrets(report)


def test_login_403_means_missing_scope() -> None:
    requests: list[httpx.Request] = []
    report = _run(_config(), _handler(requests, companies_status=403))
    assert "company:read" in _by_name(report)["login"].detail


def test_explicit_company_not_visible_fails_login() -> None:
    requests: list[httpx.Request] = []
    report = _run(_config(company="no-such-company"), _handler(requests))
    assert _by_name(report)["login"].status == "fail"
    assert _by_name(report)["report"].status == "skip"
    assert _by_name(report)["mcp"].status == "skip"


def test_no_companies_fails_login() -> None:
    requests: list[httpx.Request] = []
    report = _run(_config(), _handler(requests, companies=[]))
    assert _by_name(report)["login"].status == "fail"


# ---------------------------------------------------------------------------
# Report failures
# ---------------------------------------------------------------------------


def test_unbalanced_trial_balance_fails_report() -> None:
    requests: list[httpx.Request] = []
    report = _run(
        _config(),
        _handler(
            requests,
            report_body={
                "balanced": False,
                "total_debit": 1000,
                "total_credit": 999,
                "move_count": 6,
                "line_count": 12,
            },
        ),
    )
    check = _by_name(report)["report"]
    assert check.status == "fail"
    assert "does NOT balance" in check.detail
    assert not report.ok
    _assert_no_secrets(report)


def test_report_403_means_missing_scope() -> None:
    requests: list[httpx.Request] = []
    report = _run(_config(), _handler(requests, report_status=403))
    assert "reports:read" in _by_name(report)["report"].detail


# ---------------------------------------------------------------------------
# Webhook check (opt-in only)
# ---------------------------------------------------------------------------


def _webhook_config() -> SmokeConfig:
    return _config(
        webhook=WebhookCheckConfig(
            connector="smoke-test", store_id="smoke-1", secret=WEBHOOK_SECRET
        )
    )


def test_webhook_check_posts_created_then_duplicate() -> None:
    requests: list[httpx.Request] = []
    report = _run(_webhook_config(), _handler(requests))
    assert report.ok
    assert _by_name(report)["webhook"].status == "pass"
    posts = [r for r in requests if r.method == "POST"]
    assert len(posts) == 2
    assert posts[0].url.path == "/api/v1/integrations/smoke-test/events"
    # The same body twice (idempotent re-delivery), signed + typed.
    assert posts[0].content == posts[1].content
    assert posts[0].headers["X-HesabYar-Event-Type"] == "payment.captured"
    assert posts[0].headers["X-HesabYar-Signature"]
    envelope = json.loads(posts[0].content)
    assert envelope["source_system"] == "smoke-test"
    assert envelope["source_store_id"] == "smoke-1"
    _assert_no_secrets(report)


def test_webhook_401_means_wrong_secret() -> None:
    requests: list[httpx.Request] = []
    report = _run(_webhook_config(), _handler(requests, webhook_posts=[401]))
    check = _by_name(report)["webhook"]
    assert check.status == "fail"
    assert "401" in check.detail
    assert not report.ok
    _assert_no_secrets(report)


def test_webhook_repost_not_duplicate_fails() -> None:
    requests: list[httpx.Request] = []
    posts_seen: list[int] = []
    fallback = _handler(requests)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            posts_seen.append(1)
            # First post creates; the re-post ALSO claims "created"
            # (idempotency broken) instead of "duplicate".
            created = len(posts_seen) == 1
            return httpx.Response(
                201 if created else 200,
                json={
                    "status": "created",
                    "inbox_event_id": "evt-1",
                    "inbox_status": "received",
                    "signature_verified": True,
                    "signature_note": "verified",
                    "derived_inbox_event_ids": [],
                },
            )
        return fallback(request)

    report = _run(_webhook_config(), handle)
    check = _by_name(report)["webhook"]
    assert check.status == "fail"
    assert "duplicate" in check.detail


# ---------------------------------------------------------------------------
# MCP check
# ---------------------------------------------------------------------------


def test_mcp_error_fails() -> None:
    requests: list[httpx.Request] = []
    report = _run(_config(), _handler(requests), mcp_call=_mcp_error)
    assert _by_name(report)["mcp"].status == "fail"
    assert not report.ok
    _assert_no_secrets(report)


def test_mcp_call_receives_the_resolved_company() -> None:
    seen: list[str] = []

    def capture(config: SmokeConfig, company: str) -> McpCallResult:
        seen.append(company)
        return McpCallResult(True, True, "ok")

    requests: list[httpx.Request] = []
    report = _run(_config(), _handler(requests), mcp_call=capture)
    assert seen == [COMPANY_ID]
    assert _by_name(report)["mcp"].status == "pass"


@pytest.mark.parametrize("status", ["pass", "skip"])
def test_skips_do_not_fail_the_suite(status: SmokeStatus) -> None:
    # A suite of only passes + skips is OK; only a fail fails it.
    from hesabyar.infrastructure.operations.smoke import SmokeReport

    report = SmokeReport(
        checks=(
            SmokeCheck("a", "pass", "ok"),
            SmokeCheck("b", status, "not run"),
        )
    )
    assert report.ok
    assert report.failed == ()
