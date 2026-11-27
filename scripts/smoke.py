"""Post-deploy smoke suite CLI (US-706).

Runs the deployment checklist's smoke checks against a deployed
HesabYar instance: health, readiness, login (API-key auth), one
report, one MCP call — plus an OPTIONAL webhook check that is off
by default. See docs/deployment-iran.md §12 for the checklist and
the scopes the smoke key needs.

Read-only by default: nothing is written, nothing is posted, no
production data is touched. The webhook check (--webhook) posts
ONE event to a DEDICATED smoke-test connector/store — never a real
store's connector — and the event lands in the smoke tenant's
review queue for the operator to clear.

Secrets come from the ENVIRONMENT, never from CLI arguments (an
argument shows in ``ps``) and never from the output: the report
never prints the API key or the webhook secret.

Usage (from the repo root)::

    export HESABYAR_SMOKE_API_KEY='hya_live_...'
    uv run python scripts/smoke.py --api-url https://hesabyar.example.com \\
        --mcp-url https://mcp.example.com/mcp
    uv run python scripts/smoke.py --api-url https://hesabyar.example.com \\
        --mcp-url https://mcp.example.com/mcp --company 1 --json
    # Optional webhook check (smoke connector + store only):
    export HESABYAR_SMOKE__WEBHOOK_SECRET='...'
    uv run python scripts/smoke.py --api-url https://hesabyar.example.com \\
        --mcp-url https://mcp.example.com/mcp \\
        --webhook --webhook-connector smoke-test --webhook-store-id smoke-1

Exit codes: 0 all checks passed (skips do not fail), 1 a check
failed, 2 usage error (missing key, bad dates, webhook flags
without --webhook).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from hesabyar.infrastructure.operations.smoke import (
    DEFAULT_TIMEOUT_SECONDS,
    SmokeConfig,
    WebhookCheckConfig,
    run_smoke_suite,
)

API_KEY_ENV = "HESABYAR_SMOKE_API_KEY"
WEBHOOK_SECRET_ENV = "HESABYAR_SMOKE__WEBHOOK_SECRET"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"not a YYYY-MM-DD date: {value!r}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Post-deploy smoke suite (US-706): health, readiness, "
        "login, one report, one MCP call. Read-only by default."
    )
    parser.add_argument(
        "--api-url",
        required=True,
        help="HesabYar API base URL, e.g. https://hesabyar.example.com",
    )
    parser.add_argument(
        "--mcp-url",
        required=True,
        help="MCP Streamable HTTP URL, e.g. https://mcp.example.com/mcp "
        "(the MCP server runs on its own origin — there is no safe default)",
    )
    parser.add_argument(
        "--api-key-env",
        default=API_KEY_ENV,
        help=f"Environment variable holding the smoke API key (default: {API_KEY_ENV})",
    )
    parser.add_argument(
        "--company",
        default=None,
        help="Smoke company id (HesabYar row id or engine id); default: "
        "the first company the key can see",
    )
    parser.add_argument(
        "--date-from",
        type=_parse_date,
        default=date(date.today().year, 1, 1),
        help="Trial-balance period start, YYYY-MM-DD (default: 1 January this year)",
    )
    parser.add_argument(
        "--date-to",
        type=_parse_date,
        default=date.today(),
        help="Trial-balance period end, YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--webhook",
        action="store_true",
        help="Run the OPTIONAL webhook check (posts one event to the "
        "dedicated smoke connector/store only — never a real store)",
    )
    parser.add_argument(
        "--webhook-connector",
        default=None,
        help="Smoke connector name (required with --webhook)",
    )
    parser.add_argument(
        "--webhook-store-id",
        default=None,
        help="Smoke store id (required with --webhook)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the report as JSON instead of text",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        print(
            f"No API key: set {args.api_key_env} in the environment "
            "(never pass a key on the command line).",
            file=sys.stderr,
        )
        return 2
    if args.date_to < args.date_from:
        print("date-to must be on or after date-from.", file=sys.stderr)
        return 2
    webhook = None
    if args.webhook:
        if not args.webhook_connector or not args.webhook_store_id:
            print(
                "--webhook needs --webhook-connector and --webhook-store-id.",
                file=sys.stderr,
            )
            return 2
        secret = os.environ.get(WEBHOOK_SECRET_ENV)
        if not secret:
            print(
                f"--webhook needs the smoke webhook secret in {WEBHOOK_SECRET_ENV}.",
                file=sys.stderr,
            )
            return 2
        webhook = WebhookCheckConfig(
            connector=args.webhook_connector,
            store_id=args.webhook_store_id,
            secret=secret,
        )
    elif args.webhook_connector or args.webhook_store_id:
        print(
            "--webhook-connector/--webhook-store-id need --webhook.",
            file=sys.stderr,
        )
        return 2
    config = SmokeConfig(
        api_base_url=args.api_url,
        mcp_url=args.mcp_url,
        api_key=api_key,
        company=args.company,
        date_from=args.date_from,
        date_to=args.date_to,
        timeout_seconds=args.timeout,
        webhook=webhook,
    )
    report = run_smoke_suite(config)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(report.render_text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
