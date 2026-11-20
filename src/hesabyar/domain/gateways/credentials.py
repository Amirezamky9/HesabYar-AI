"""Gateway credential payloads (US-524).

A channel's gateway credentials live ENCRYPTED in the channel
registry (``payment_channels.credentials_ciphertext``, Fernet via
the credential vault — the same F3 store as bank/store secrets).
The decrypted payload is one JSON object:

- secrets: the per-gateway fields below (never logged, never
  returned by any API);
- optional non-secret overrides: ``base_url`` (SnappPay/TorobPay/
  DigiPay/ZarinPal), ``requires_settle`` (TorobPay),
  ``paths`` (TorobPay per-endpoint overrides), ``scope``
  (SnappPay/TorobPay OAuth scope), ``graphql_url`` /
  ``rest_base_url`` (ZarinPal).

Required secrets per gateway:

- zarinpal: ``merchant_id`` (REST verify/inquiry/reverse);
  ``access_token`` + ``terminal_id`` additionally for the GraphQL
  list/refund calls.
- snapppay / torobpay / digipay: ``client_id``,
  ``client_secret``, ``username``, ``password`` (OAuth).
- sadad / salampay / custom: no credentials — manual channels.

A note on the password rule: ADR 0003 decision 10 forbids storing
INTERNET-BANKING passwords (bank connections). Gateway OAuth
passwords are server-to-server API credentials issued by the
gateway for its own API — a different thing, stored encrypted like
any API secret. The bank-password rejection in the integrations
service deliberately does NOT apply to gateway credentials.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from hesabyar.domain.gateways.errors import GatewayNotConfiguredError

__all__ = [
    "REQUIRED_CREDENTIAL_FIELDS",
    "parse_gateway_credentials",
    "require_credential_fields",
]

#: Required secret fields per gateway (manual channels need none).
REQUIRED_CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "zarinpal": ("merchant_id",),
    "snapppay": ("client_id", "client_secret", "username", "password"),
    "torobpay": ("client_id", "client_secret", "username", "password"),
    "digipay": ("client_id", "client_secret", "username", "password"),
    "sadad": (),
    "salampay": (),
}


def parse_gateway_credentials(plaintext: str, *, channel_key: str) -> dict[str, object]:
    """Parse a decrypted credential payload into a plain dict.

    The payload must be a JSON object; anything else fails closed
    (a corrupted payload must never be treated as \"no
    credentials\"). Field NAMES travel in errors, never values.
    """
    try:
        parsed = json.loads(plaintext)
    except (ValueError, TypeError) as exc:
        raise GatewayNotConfiguredError(
            f"Channel {channel_key!r} has a stored credential payload that is not valid JSON; rotate the credentials.",
            details={"channel_key": channel_key},
        ) from exc
    if not isinstance(parsed, dict):
        raise GatewayNotConfiguredError(
            f"Channel {channel_key!r} has a stored credential payload that "
            "is not a JSON object; rotate the credentials.",
            details={"channel_key": channel_key},
        )
    return dict(parsed)


def require_credential_fields(
    credentials: Mapping[str, object],
    fields: tuple[str, ...],
    *,
    channel_key: str,
) -> None:
    """Require non-empty string values for ``fields`` (names only in errors)."""
    missing = [
        name for name in fields if not isinstance(credentials.get(name), str) or not str(credentials[name]).strip()
    ]
    if missing:
        raise GatewayNotConfiguredError(
            f"Channel {channel_key!r} is missing credential field(s): {', '.join(missing)}.",
            details={"channel_key": channel_key, "missing_fields": missing},
        )
