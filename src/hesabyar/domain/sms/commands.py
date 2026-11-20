"""Write commands for the SMS provider registry (US-544).

The repository port takes these command objects (the same pattern
as ``hesabyar.domain.integrations.commands`` for payment channels):
the service validates the payload, encrypts any credentials
through the vault, and hands the repository a command that already
carries ciphertext — the repository never sees a plaintext secret.

``UpdateSmsProvider`` fields are all optional (PATCH semantics):
``None`` means "leave unchanged" — except ``line_number``,
``price_per_sms_toman``, ``rate_limit_per_minute``, ``line_type``
and ``notes``, where an explicit null clears the field. The
service distinguishes "absent" from "null" before building the
command (see ``SmsRegistryService.update_provider``), so the
command uses a ``_UNSET`` sentinel for the clearable fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from hesabyar.domain.sms.models import SmsProviderStatus

#: Sentinel for "this PATCH field was not supplied" (vs an explicit null).
UNSET: Any = object()


@dataclass(frozen=True)
class CreateSmsProvider:
    """Create one provider row (born disabled unless the caller says otherwise)."""

    installation_id: str
    provider_key: str
    display_name_fa: str
    provider_kind: str
    is_builtin: bool = False
    enabled: bool = False
    is_default: bool = False
    status: SmsProviderStatus = SmsProviderStatus.SETUP
    line_number: str | None = None
    price_per_sms_toman: Decimal | None = None
    rate_limit_per_minute: int | None = None
    line_type: str | None = None
    contract_required: bool = False
    enmad_required: bool = False
    notes: str | None = None


@dataclass(frozen=True)
class UpdateSmsProvider:
    """Patch one provider row (only the supplied fields change)."""

    provider_id: str
    installation_id: str
    display_name_fa: str | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    status: SmsProviderStatus | None = None
    line_number: Any = UNSET
    price_per_sms_toman: Any = UNSET
    rate_limit_per_minute: Any = UNSET
    line_type: Any = UNSET
    contract_required: bool | None = None
    enmad_required: bool | None = None
    notes: Any = UNSET


@dataclass(frozen=True)
class RotateSmsProviderCredentials:
    """Replace one provider's stored credential payload (already encrypted)."""

    provider_id: str
    installation_id: str
    credentials_ciphertext: str
    credentials_key_id: str
    #: Fields carried for the audit trail only — never the values.
    credential_field_names: tuple[str, ...] = field(default_factory=tuple)
