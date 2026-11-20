"""SMS provider credential payloads (US-544).

A provider's credentials live ENCRYPTED in the provider registry
(``sms_providers.credentials_ciphertext``, Fernet via the
credential vault — the same F3 store as bank/store/gateway
secrets). The decrypted payload is one JSON object:

- secrets: the per-provider fields below (never logged, never
  returned by any API);
- optional non-secret overrides: ``base_url`` (Kavenegar /
  sms.ir — for a staging endpoint or a test double).

Required secrets per provider kind:

- kavenegar: ``api_key`` (the panel API key — it travels in the
  request URL path, ``/v1/{api_key}/sms/send.json``, so it must
  never appear in a log line);
- sms_ir: ``api_key`` (sent as the ``x-api-key`` header);
- simulator / custom: no credentials — the simulator needs none,
  and a custom provider has no client yet.

SMS API keys are server-to-server API credentials issued by the
provider for its own API — the same category as gateway OAuth
credentials, stored encrypted like any API secret. (ADR 0003
decision 10 forbids storing INTERNET-BANKING passwords; it does
not apply here.)
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from hesabyar.domain.sms.errors import SmsProviderNotConfiguredError

__all__ = [
    "REQUIRED_CREDENTIAL_FIELDS",
    "parse_sms_credentials",
    "require_credential_fields",
]

#: Required secret fields per provider kind (custom providers have
#: no client, so no credential shape is defined for them).
REQUIRED_CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "kavenegar": ("api_key",),
    "sms_ir": ("api_key",),
    "simulator": (),
    "custom": (),
}


def parse_sms_credentials(plaintext: str, *, provider_key: str) -> dict[str, object]:
    """Parse a decrypted credential payload into a plain dict.

    The payload must be a JSON object; anything else fails closed
    (a corrupted payload must never be treated as "no
    credentials"). Field NAMES travel in errors, never values.
    """
    try:
        parsed = json.loads(plaintext)
    except (ValueError, TypeError) as exc:
        raise SmsProviderNotConfiguredError(
            provider_key,
            reason="the stored credential payload is not valid JSON; rotate the credentials.",
        ) from exc
    if not isinstance(parsed, dict):
        raise SmsProviderNotConfiguredError(
            provider_key,
            reason="the stored credential payload is not a JSON object; rotate the credentials.",
        )
    return dict(parsed)


def require_credential_fields(
    payload: Mapping[str, object], *, provider_kind: str, provider_key: str
) -> dict[str, str]:
    """Require the kind's secret fields (non-empty strings).

    Returns the secret fields as a plain ``str`` mapping (plus any
    non-secret overrides the payload carries, e.g. ``base_url``).
    A missing or empty required field fails closed — a live call
    is never attempted with half a credential.
    """
    required = REQUIRED_CREDENTIAL_FIELDS.get(provider_kind, ())
    missing = [
        name
        for name in required
        if not isinstance(payload.get(name), str) or not str(payload.get(name)).strip()
    ]
    if missing:
        raise SmsProviderNotConfiguredError(
            provider_key,
            reason="missing required credential field(s): " + ", ".join(sorted(missing)) + ".",
        )
    return {name: str(value) for name, value in payload.items() if isinstance(value, str)}
