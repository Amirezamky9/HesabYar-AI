"""Integration registry write use cases (US-503, Phase 5 F3).

Channel registry, versioned gateway contracts, the mapping center,
the policy-gate config + evaluation, bank connections, stores, and
installation settings/holidays.

Every service depends only on domain ports — the
:class:`hesabyar.domain.integrations.ports.IntegrationsRegistryPort`,
the fiscal-period checker and the credential vault — never on a
session, never on infrastructure (spec §6.3). The repository flushes
on save but no layer here commits — the caller (REST route) owns the
transaction, like every other service that writes through a request
session.

Fail-closed rules (spec §10.5, phased design §6.2, ADR 0003):

- A new channel is born disabled (status ``setup``) until the operator
  enables it; enabling without an active contract leaves it in
  ``setup`` — a channel with no contract can never auto-post.
- A rate change creates a NEW contract version; the previous active
  version is superseded (status + ``valid_to`` only) and its figures
  are never rewritten.
- A mapping resolves only when an ACTIVE version exists for the exact
  (kind, channel, source); anything else raises
  :class:`IntegrationMappingNotFoundError` — the pipeline routes the
  event to the review queue, never to a default account.
- A channel cannot be deleted while contracts or mappings reference
  it (history is never orphaned).
- An internet-banking password is never accepted as a bank-connection
  credential (ADR 0003 decision 10); credentials are encrypted via
  the vault before they reach the repository, and plaintext secrets
  never enter a command.
- The policy gate denies when anything is unknown: no policy row, no
  cap, no open period, no contract for the channel, a disabled
  channel, or a failed tie-out.
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from decimal import Decimal

from hesabyar.application.errors import (
    IntegrationBankConnectionConflictError,
    IntegrationBankConnectionNotFoundError,
    IntegrationChannelInUseError,
    IntegrationChannelKeyConflictError,
    IntegrationChannelNotFoundError,
    IntegrationChannelReservedError,
    IntegrationContractInvalidError,
    IntegrationCredentialsForbiddenError,
    IntegrationCredentialVaultUnavailableError,
    IntegrationHolidayNotFoundError,
    IntegrationMappingInvalidError,
    IntegrationMappingNotFoundError,
    IntegrationMappingNotPromotableError,
    IntegrationMappingSuggestionNotFoundError,
    IntegrationMappingSuggestionResolvedError,
    IntegrationSettingsInvalidError,
    IntegrationStoreKeyConflictError,
    IntegrationStoreNotFoundError,
)
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.integrations.commands import (
    AddHoliday,
    CreateBankConnection,
    CreateGatewayContract,
    CreateIntegrationMapping,
    CreateMappingSuggestion,
    CreatePaymentChannel,
    CreateStore,
    RotateBankConnectionCredentials,
    RotateChannelCredentials,
    RotateStoreApiKey,
    RotateStoreWebhookSecret,
    SetStoreDbDsn,
    UpdateBankConnection,
    UpdatePaymentChannel,
    UpdateStore,
    UpsertInstallationSettings,
    UpsertPolicyGateConfig,
)
from hesabyar.domain.integrations.holidays import HolidayCalendar
from hesabyar.domain.integrations.models import (
    MAPPING_AUTO_MIN_CONFIRMATIONS,
    RESERVED_CHANNEL_KEYS,
    AutoPostPolicy,
    BankConnection,
    ChannelStatus,
    ContractStatus,
    GatewayContract,
    Holiday,
    InstallationSettings,
    IntegrationMapping,
    MappingKind,
    MappingStatus,
    MappingSuggestion,
    PaymentChannel,
    PolicyGateConfig,
    Store,
    SuggestionMode,
    SuggestionStatus,
    TrancheLeg,
    validate_tranche_legs,
)
from hesabyar.domain.integrations.policy_gate import (
    GateLayerResult,
    PolicyGateDecision,
    evaluate_policy_gate,
)
from hesabyar.domain.integrations.ports import (
    CredentialVaultPort,
    FiscalPeriodCheckerPort,
    IntegrationsRegistryPort,
)

#: Registry keys (channel, store, bank) are URL-safe slugs.
_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Mapping target kinds (Tryton targets — a closed schema vocabulary).
_TARGET_TYPES = frozenset({"account", "journal", "party", "tax_policy", "dimension", "company"})

#: Default read-only scopes for a new store's API key. The store key
#: can only ever read: every scope must end in ``:read`` (enforced in
#: :class:`StoreService`).
READ_ONLY_STORE_SCOPES: tuple[str, ...] = ("orders:read", "payments:read", "cheques:read")


def _validate_key(kind: str, key: str) -> None:
    """Validate a registry slug, raising a typed error on a bad one."""
    if not _KEY_PATTERN.match(key):
        raise IntegrationMappingInvalidError(
            f"Invalid {kind} key {key!r}: use lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit (max 64 characters).",
            details={"key": key},
        )


def _require_non_empty(field: str, value: str) -> str:
    """Strip + require a non-empty string, raising a typed error otherwise."""
    cleaned = value.strip()
    if not cleaned:
        raise IntegrationMappingInvalidError(
            f"{field} must not be empty.",
            details={"field": field},
        )
    return cleaned


def _encode_gateway_credentials(
    credentials: Mapping[str, object],
    vault: CredentialVaultPort | None,
) -> tuple[str, str]:
    """Validate + encrypt a gateway credential payload.

    Unlike :func:`_encode_credentials` (bank connections), this does
    NOT reject ``password`` fields: gateway OAuth passwords are
    server-to-server API credentials issued by the gateway for its
    own API, not internet-banking passwords — ADR 0003 decision 10
    forbids the latter only. The payload is still encrypted at rest
    (Fernet) and never logged or returned.
    """
    if not credentials:
        raise IntegrationCredentialsForbiddenError(
            "A credential rotation needs a non-empty credential payload.",
            details={},
        )
    if vault is None:
        raise IntegrationCredentialVaultUnavailableError(
            "Cannot store credentials: no credential-encryption key is configured "
            "(HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY).",
        )
    plaintext = json.dumps(credentials, sort_keys=True, ensure_ascii=False)
    return vault.encrypt(plaintext), vault.key_id


class ChannelRegistryService:
    """Payment-channel registry writes over the repository port."""

    def __init__(
        self,
        repository: IntegrationsRegistryPort,
        vault: CredentialVaultPort | None = None,
    ) -> None:
        self._repository = repository
        self._vault = vault

    def create_channel(
        self,
        installation_id: str,
        channel_key: str,
        display_name_fa: str,
        channel_type: str,
        *,
        merchant_ref: str | None = None,
        settlement_iban: str | None = None,
        environment: str = "production",
        notes: str | None = None,
    ) -> PaymentChannel:
        """Create a custom channel (built-ins come from the seed).

        New channels are born disabled with status ``setup`` — the
        operator enables them after attaching a contract.
        """
        _validate_key("channel", channel_key)
        display_name_fa = _require_non_empty("display_name_fa", display_name_fa)
        channel_type = _require_non_empty("channel_type", channel_type)
        if self._repository.get_channel(installation_id, channel_key) is not None:
            raise IntegrationChannelKeyConflictError(
                f"Payment channel {channel_key!r} already exists in this installation.",
                details={"channel_key": channel_key},
            )
        return self._repository.save_channel(
            CreatePaymentChannel(
                installation_id=installation_id,
                channel_key=channel_key,
                display_name_fa=display_name_fa,
                channel_type=channel_type,
                merchant_ref=merchant_ref,
                settlement_iban=settlement_iban,
                environment=environment,
                notes=notes,
            )
        )

    def update_channel(
        self,
        installation_id: str,
        channel_key: str,
        *,
        display_name_fa: str | None = None,
        channel_type: str | None = None,
        enabled: bool | None = None,
        merchant_ref: str | None = None,
        settlement_iban: str | None = None,
        environment: str | None = None,
        notes: str | None = None,
    ) -> PaymentChannel:
        """Update a channel's mutable fields.

        ``enabled`` drives the derived status: disabling sets
        ``disabled``; enabling sets ``active`` when an active contract
        exists, else ``setup`` (a channel without a contract is not
        operable yet).
        """
        channel = self._repository.get_channel(installation_id, channel_key)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment channel {channel_key!r} is not in this installation's registry.",
                details={"channel_key": channel_key},
            )
        if enabled is True and channel_key in RESERVED_CHANNEL_KEYS:
            raise IntegrationChannelReservedError(
                f"Payment channel {channel_key!r} is RESERVED and stays disabled "
                "until its integration is verified (the Torob order API is "
                "UNVERIFIED); it cannot be enabled.",
                details={"channel_key": channel_key},
            )
        status: str | None = None
        if enabled is not None:
            if not enabled:
                status = ChannelStatus.DISABLED.value
            elif self._repository.get_active_contract(channel.id) is not None:
                status = ChannelStatus.ACTIVE.value
            else:
                status = ChannelStatus.SETUP.value
        updated = self._repository.update_channel(
            UpdatePaymentChannel(
                installation_id=installation_id,
                channel_key=channel_key,
                display_name_fa=display_name_fa,
                channel_type=channel_type,
                enabled=enabled,
                status=status,
                merchant_ref=merchant_ref,
                settlement_iban=settlement_iban,
                environment=environment,
                notes=notes,
            )
        )
        assert updated is not None  # the channel existed a moment ago
        return updated

    def rotate_channel_credentials(
        self,
        installation_id: str,
        channel_key: str,
        credentials: Mapping[str, object],
    ) -> PaymentChannel:
        """Replace a channel's stored gateway credentials (encrypted).

        US-524: the payload is the gateway's secrets plus optional
        non-secret overrides (``base_url``, ``requires_settle``, …).
        Gateway OAuth passwords are legitimate API credentials here
        (see :func:`_encode_gateway_credentials`) — the bank-password
        ban does not apply. The response carries only
        ``has_credentials`` — never the payload.
        """
        channel = self._repository.get_channel(installation_id, channel_key)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment channel {channel_key!r} is not in this installation's registry.",
                details={"channel_key": channel_key},
            )
        ciphertext, key_id = _encode_gateway_credentials(credentials, self._vault)
        rotated = self._repository.rotate_channel_credentials(
            RotateChannelCredentials(
                installation_id=installation_id,
                channel_key=channel_key,
                credentials_ciphertext=ciphertext,
                credentials_key_id=key_id,
            )
        )
        assert rotated is not None  # the channel existed a moment ago
        return rotated

    def delete_channel(self, installation_id: str, channel_key: str) -> None:
        """Delete a channel — refused while contracts/mappings reference it."""
        channel = self._repository.get_channel(installation_id, channel_key)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment channel {channel_key!r} is not in this installation's registry.",
                details={"channel_key": channel_key},
            )
        contracts = self._repository.count_contracts(channel.id)
        mappings = self._repository.count_mappings_for_channel(installation_id, channel_key)
        if contracts or mappings:
            raise IntegrationChannelInUseError(
                f"Payment channel {channel_key!r} cannot be deleted: it has "
                f"{contracts} contract version(s) and {mappings} mapping(s). "
                "Disable it instead; history is never deleted.",
                details={
                    "channel_key": channel_key,
                    "contracts": contracts,
                    "mappings": mappings,
                },
            )
        self._repository.delete_channel(installation_id, channel_key)

    def seed_builtin_channels(self, installation_id: str) -> Sequence[PaymentChannel]:
        """Idempotently create the built-in channel rows for an installation.

        The built-in list is seed DATA (see ``seed_payment_channels``)
        — this method only reaches it through the repository port, and
        no behaviour anywhere branches on it.
        """
        return self._repository.seed_builtin_channels(installation_id)


class GatewayContractService:
    """Versioned gateway-contract writes over the repository port."""

    def __init__(self, repository: IntegrationsRegistryPort) -> None:
        self._repository = repository

    def create_version(
        self,
        installation_id: str,
        channel_key: str,
        *,
        valid_from: datetime.date,
        fee_percent: Decimal,
        fee_vat_rate: Decimal | None = None,
        fee_vat_claimable: bool = False,
        fee_timing: str = "proportional",
        fee_accrual_policy: str = "at_sale",
        settlement_model: str = "tranches_4",
        tranche_legs: tuple[TrancheLeg, ...] | list[TrancheLeg] = (),
        payout_window_start_day: int | None = None,
        payout_window_end_day: int | None = None,
        payout_calendar: str = "jalali",
        working_day_adjustment: bool = False,
        day_lag: int | None = None,
        grace_days_after_due: int = 0,
        match_tolerance_irr: int = 0,
        tieout_tolerance_irr: int = 0,
        auto_post_policy: AutoPostPolicy = AutoPostPolicy.DRAFT_ONLY,
        auto_post_max_amount_irr: int | None = None,
        source_note: str | None = None,
    ) -> GatewayContract:
        """Create a NEW contract version for a channel.

        A rate change is always a new version: the previous active
        version is superseded (status + ``valid_to`` only) and its
        figures are never rewritten. ``valid_from`` must move forward
        in time — versions are effective-dated, not backdated.
        """
        channel = self._repository.get_channel(installation_id, channel_key)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment channel {channel_key!r} is not in this installation's registry.",
                details={"channel_key": channel_key},
            )
        legs = tuple(tranche_legs)
        problems = self._validate_contract(
            fee_percent=fee_percent,
            fee_vat_rate=fee_vat_rate,
            tranche_legs=legs,
            payout_window_start_day=payout_window_start_day,
            payout_window_end_day=payout_window_end_day,
            day_lag=day_lag,
            grace_days_after_due=grace_days_after_due,
            match_tolerance_irr=match_tolerance_irr,
            tieout_tolerance_irr=tieout_tolerance_irr,
            auto_post_max_amount_irr=auto_post_max_amount_irr,
        )
        if problems:
            raise IntegrationContractInvalidError(
                f"Contract version is invalid: {'; '.join(problems)}",
                details={"problems": problems, "channel_key": channel_key},
            )
        existing = self._repository.list_contracts(channel.id)
        next_version = max((c.version for c in existing), default=0) + 1
        active = next((c for c in existing if c.status == ContractStatus.ACTIVE), None)
        if active is not None:
            if valid_from <= active.valid_from:
                raise IntegrationContractInvalidError(
                    f"valid_from {valid_from} must be after the active version's "
                    f"valid_from {active.valid_from}: versions move forward in time.",
                    details={
                        "channel_key": channel_key,
                        "valid_from": valid_from.isoformat(),
                        "active_valid_from": active.valid_from.isoformat(),
                    },
                )
            self._repository.supersede_contract(
                active.id, valid_from - datetime.timedelta(days=1)
            )
        return self._repository.save_contract(
            CreateGatewayContract(
                channel_id=channel.id,
                version=next_version,
                valid_from=valid_from,
                fee_percent=fee_percent,
                fee_vat_rate=fee_vat_rate,
                fee_vat_claimable=fee_vat_claimable,
                fee_timing=fee_timing,
                fee_accrual_policy=fee_accrual_policy,
                settlement_model=settlement_model,
                tranche_legs=legs,
                payout_window_start_day=payout_window_start_day,
                payout_window_end_day=payout_window_end_day,
                payout_calendar=payout_calendar,
                working_day_adjustment=working_day_adjustment,
                day_lag=day_lag,
                grace_days_after_due=grace_days_after_due,
                match_tolerance_irr=match_tolerance_irr,
                tieout_tolerance_irr=tieout_tolerance_irr,
                auto_post_policy=auto_post_policy,
                auto_post_max_amount_irr=auto_post_max_amount_irr,
                source_note=source_note,
            )
        )

    @staticmethod
    def _validate_contract(
        *,
        fee_percent: Decimal,
        fee_vat_rate: Decimal | None,
        tranche_legs: tuple[TrancheLeg, ...],
        payout_window_start_day: int | None,
        payout_window_end_day: int | None,
        day_lag: int | None,
        grace_days_after_due: int,
        match_tolerance_irr: int,
        tieout_tolerance_irr: int,
        auto_post_max_amount_irr: int | None,
    ) -> list[str]:
        """Collect every contract defect (empty list = valid)."""
        problems: list[str] = []
        if fee_percent < 0 or fee_percent > 100:
            problems.append(f"fee_percent must be in [0, 100], got {fee_percent}")
        if fee_vat_rate is not None and (fee_vat_rate < 0 or fee_vat_rate > 100):
            problems.append(f"fee_vat_rate must be in [0, 100], got {fee_vat_rate}")
        problems.extend(validate_tranche_legs(tranche_legs))
        for label, day in (
            ("payout_window_start_day", payout_window_start_day),
            ("payout_window_end_day", payout_window_end_day),
        ):
            if day is not None and (day < 1 or day > 31):
                problems.append(f"{label} must be 1..31, got {day}")
        if (
            payout_window_start_day is not None
            and payout_window_end_day is not None
            and payout_window_start_day > payout_window_end_day
        ):
            problems.append("payout_window_start_day must not exceed payout_window_end_day")
        if day_lag is not None and day_lag < 0:
            problems.append(f"day_lag must be >= 0, got {day_lag}")
        if grace_days_after_due < 0:
            problems.append(f"grace_days_after_due must be >= 0, got {grace_days_after_due}")
        if match_tolerance_irr < 0:
            problems.append(f"match_tolerance_irr must be >= 0, got {match_tolerance_irr}")
        if tieout_tolerance_irr < 0:
            problems.append(f"tieout_tolerance_irr must be >= 0, got {tieout_tolerance_irr}")
        if auto_post_max_amount_irr is not None and auto_post_max_amount_irr < 0:
            problems.append(
                f"auto_post_max_amount_irr must be >= 0, got {auto_post_max_amount_irr}"
            )
        return problems


class MappingService:
    """Mapping-center writes: versions, resolution, suggestions (§10.5)."""

    def __init__(self, repository: IntegrationsRegistryPort) -> None:
        self._repository = repository

    def create_mapping(
        self,
        installation_id: str,
        *,
        mapping_kind: MappingKind,
        channel_key: str = "",
        source_key: str,
        target_type: str,
        target_ref: str,
        suggestion_mode: SuggestionMode = SuggestionMode.SUGGEST,
        source: str = "manual",
        confidence: Decimal | None = None,
        notes: str | None = None,
        created_by: str | None = None,
        confirmations: int | None = None,
    ) -> IntegrationMapping:
        """Create a NEW mapping version (supersedes the active one, if any).

        Confirmations (US-552): re-creating the mapping with an
        UNCHANGED target carries the superseded version's count + 1
        (a re-confirmation of the same target); a changed target, or
        a first version, starts at 1. An explicit ``confirmations``
        overrides the derivation — used by promotion, which carries
        the count unchanged (promoting is not a confirmation).
        """
        source_key = _require_non_empty("source_key", source_key)
        target_ref = _require_non_empty("target_ref", target_ref)
        if target_type not in _TARGET_TYPES:
            raise IntegrationMappingInvalidError(
                f"Unknown target_type {target_type!r}. Allowed: {', '.join(sorted(_TARGET_TYPES))}.",
                details={"target_type": target_type},
            )
        if channel_key:
            _validate_key("channel", channel_key)
            if self._repository.get_channel(installation_id, channel_key) is None:
                raise IntegrationChannelNotFoundError(
                    f"Payment channel {channel_key!r} is not in this installation's registry.",
                    details={"channel_key": channel_key},
                )
        if confidence is not None and (confidence < 0 or confidence > 1):
            raise IntegrationMappingInvalidError(
                f"confidence must be in [0, 1], got {confidence}.",
                details={"confidence": str(confidence)},
            )
        active = self._repository.resolve_mapping(
            installation_id, mapping_kind, channel_key, source_key
        )
        siblings = self._repository.list_mappings(
            installation_id,
            mapping_kind=mapping_kind,
            channel_key=channel_key,
            source_key=source_key,
        )
        next_version = max((m.version for m in siblings), default=0) + 1
        if confirmations is None:
            if (
                active is not None
                and active.target_type == target_type
                and active.target_ref == target_ref
            ):
                confirmations = active.confirmations + 1
            else:
                confirmations = 1
        if active is not None:
            self._repository.supersede_mapping(active.id)
        return self._repository.save_mapping(
            CreateIntegrationMapping(
                installation_id=installation_id,
                mapping_kind=mapping_kind,
                channel_key=channel_key,
                source_key=source_key,
                target_type=target_type,
                target_ref=target_ref,
                version=next_version,
                suggestion_mode=suggestion_mode,
                source=source,
                confidence=confidence,
                notes=notes,
                created_by=created_by,
                confirmations=confirmations,
            )
        )

    def promote_mapping(
        self,
        installation_id: str,
        mapping_id: str,
        *,
        promoted_by: str | None = None,
    ) -> IntegrationMapping:
        """Promote an ACTIVE Suggest mapping to Auto (US-552, §7.2).

        The learning loop's last step: a rule the operator confirmed
        ``MAPPING_AUTO_MIN_CONFIRMATIONS`` times may be promoted —
        the promotion creates a NEW version in ``auto`` mode (the
        old Suggest version is superseded, never rewritten) with the
        confirmation count carried unchanged. An already-auto
        mapping, a superseded version, or an under-confirmed rule
        raises :class:`IntegrationMappingNotPromotableError`; an
        unknown id (or another installation's) raises
        :class:`IntegrationMappingNotFoundError`.
        """
        mapping = self._repository.get_mapping(mapping_id)
        if mapping is None or mapping.installation_id != installation_id:
            raise IntegrationMappingNotFoundError(
                f"Mapping {mapping_id!r} is not in this installation's registry.",
                details={"mapping_id": mapping_id},
            )
        if not mapping.auto_eligible:
            if mapping.status != MappingStatus.ACTIVE:
                reason = f"mapping is {mapping.status.value}, not active"
            elif mapping.suggestion_mode != SuggestionMode.SUGGEST:
                reason = f"mapping is already {mapping.suggestion_mode.value}"
            else:
                reason = (
                    f"mapping has {mapping.confirmations} confirmation(s); "
                    f"{MAPPING_AUTO_MIN_CONFIRMATIONS} are required to promote to Auto"
                )
            raise IntegrationMappingNotPromotableError(
                f"Mapping {mapping_id!r} cannot be promoted to Auto: {reason}.",
                details={
                    "mapping_id": mapping_id,
                    "status": mapping.status.value,
                    "suggestion_mode": mapping.suggestion_mode.value,
                    "confirmations": mapping.confirmations,
                    "required_confirmations": MAPPING_AUTO_MIN_CONFIRMATIONS,
                },
            )
        return self.create_mapping(
            installation_id,
            mapping_kind=mapping.mapping_kind,
            channel_key=mapping.channel_key,
            source_key=mapping.source_key,
            target_type=mapping.target_type,
            target_ref=mapping.target_ref,
            suggestion_mode=SuggestionMode.AUTO,
            source=mapping.source,
            confidence=mapping.confidence,
            notes=mapping.notes,
            created_by=promoted_by,
            confirmations=mapping.confirmations,
        )

    def resolve_mapping(
        self,
        installation_id: str,
        mapping_kind: MappingKind,
        channel_key: str,
        source_key: str,
    ) -> IntegrationMapping:
        """Resolve the ACTIVE mapping for a (kind, channel, source).

        Raises :class:`IntegrationMappingNotFoundError` when no active
        mapping exists — the fail-closed signal: the caller routes the
        event to the review queue, never to a default account.
        """
        mapping = self._repository.resolve_mapping(
            installation_id, mapping_kind, channel_key, source_key
        )
        if mapping is None:
            raise IntegrationMappingNotFoundError(
                f"No active {mapping_kind.value} mapping for source {source_key!r} "
                f"(channel {channel_key or 'installation-wide'}).",
                details={
                    "mapping_kind": mapping_kind.value,
                    "channel_key": channel_key,
                    "source_key": source_key,
                },
            )
        return mapping

    def create_suggestion(
        self,
        installation_id: str,
        *,
        mapping_kind: MappingKind,
        channel_key: str = "",
        source_key: str,
        suggested_target_type: str,
        suggested_target_ref: str,
        confidence: Decimal,
        basis: str,
    ) -> MappingSuggestion:
        """Record a proposed mapping (idempotent per pending suggestion)."""
        source_key = _require_non_empty("source_key", source_key)
        suggested_target_ref = _require_non_empty("suggested_target_ref", suggested_target_ref)
        basis = _require_non_empty("basis", basis)
        if suggested_target_type not in _TARGET_TYPES:
            raise IntegrationMappingInvalidError(
                f"Unknown suggested_target_type {suggested_target_type!r}. "
                f"Allowed: {', '.join(sorted(_TARGET_TYPES))}.",
                details={"suggested_target_type": suggested_target_type},
            )
        if confidence < 0 or confidence > 1:
            raise IntegrationMappingInvalidError(
                f"confidence must be in [0, 1], got {confidence}.",
                details={"confidence": str(confidence)},
            )
        if channel_key:
            _validate_key("channel", channel_key)
            if self._repository.get_channel(installation_id, channel_key) is None:
                raise IntegrationChannelNotFoundError(
                    f"Payment channel {channel_key!r} is not in this installation's registry.",
                    details={"channel_key": channel_key},
                )
        existing = self._repository.get_pending_suggestion(
            installation_id, mapping_kind, channel_key, source_key
        )
        if existing is not None:
            return existing
        return self._repository.save_suggestion(
            CreateMappingSuggestion(
                installation_id=installation_id,
                mapping_kind=mapping_kind,
                channel_key=channel_key,
                source_key=source_key,
                suggested_target_type=suggested_target_type,
                suggested_target_ref=suggested_target_ref,
                confidence=confidence,
                basis=basis,
            )
        )

    def accept_suggestion(
        self,
        installation_id: str,
        suggestion_id: str,
        *,
        resolved_by: str | None = None,
    ) -> IntegrationMapping:
        """Accept a suggestion: create the mapping (Suggest mode first).

        The new mapping starts in ``suggest`` mode — the operator
        promotes it to ``auto`` only after trusting it (§1.4).
        """
        suggestion = self._require_pending_suggestion(installation_id, suggestion_id)
        mapping = self.create_mapping(
            installation_id,
            mapping_kind=suggestion.mapping_kind,
            channel_key=suggestion.channel_key,
            source_key=suggestion.source_key,
            target_type=suggestion.suggested_target_type,
            target_ref=suggestion.suggested_target_ref,
            suggestion_mode=SuggestionMode.SUGGEST,
            source="learned",
            confidence=suggestion.confidence,
            created_by=resolved_by,
        )
        self._repository.resolve_suggestion(
            suggestion.id, SuggestionStatus.ACCEPTED, resolved_by
        )
        return mapping

    def reject_suggestion(
        self,
        installation_id: str,
        suggestion_id: str,
        *,
        resolved_by: str | None = None,
    ) -> MappingSuggestion:
        """Reject a suggestion (recorded, so it is not proposed again silently)."""
        suggestion = self._require_pending_suggestion(installation_id, suggestion_id)
        resolved = self._repository.resolve_suggestion(
            suggestion.id, SuggestionStatus.REJECTED, resolved_by
        )
        assert resolved is not None  # the suggestion existed a moment ago
        return resolved

    def _require_pending_suggestion(
        self, installation_id: str, suggestion_id: str
    ) -> MappingSuggestion:
        """Load a suggestion, requiring it to be pending in this installation."""
        suggestion = self._repository.get_suggestion(suggestion_id)
        if suggestion is None or suggestion.installation_id != installation_id:
            raise IntegrationMappingSuggestionNotFoundError(
                f"Mapping suggestion {suggestion_id!r} is not in this installation's registry.",
                details={"suggestion_id": suggestion_id},
            )
        if suggestion.status != SuggestionStatus.PENDING:
            raise IntegrationMappingSuggestionResolvedError(
                f"Mapping suggestion {suggestion_id!r} is already {suggestion.status.value}.",
                details={"suggestion_id": suggestion_id, "status": suggestion.status.value},
            )
        return suggestion


class PolicyGateConfigService:
    """Policy-gate configuration writes over the repository port."""

    def __init__(self, repository: IntegrationsRegistryPort) -> None:
        self._repository = repository

    def upsert_config(
        self,
        installation_id: str,
        event_type: str,
        *,
        allowed: bool,
        max_amount_irr: int | None = None,
        require_tieout: bool = False,
    ) -> PolicyGateConfig:
        """Create or replace one installation's policy row for an event type."""
        event_type = _require_non_empty("event_type", event_type)
        if max_amount_irr is not None and max_amount_irr < 0:
            raise IntegrationMappingInvalidError(
                f"max_amount_irr must be >= 0, got {max_amount_irr}.",
                details={"max_amount_irr": max_amount_irr},
            )
        return self._repository.save_policy_config(
            UpsertPolicyGateConfig(
                installation_id=installation_id,
                event_type=event_type,
                allowed=allowed,
                max_amount_irr=max_amount_irr,
                require_tieout=require_tieout,
            )
        )


class PolicyGateService:
    """Evaluate the three-layer policy gate for one event (§6.2).

    Loads the inputs through the ports (registry policy + contract,
    engine fiscal-period state) and delegates the decision itself to
    the pure domain function — so the gate logic is unit-testable
    without any I/O, and this service is only wiring.
    """

    def __init__(
        self,
        repository: IntegrationsRegistryPort,
        fiscal_checker: FiscalPeriodCheckerPort,
    ) -> None:
        self._repository = repository
        self._fiscal_checker = fiscal_checker

    def evaluate(
        self,
        installation_id: str,
        *,
        event_type: str,
        company: CompanyRef,
        event_date: datetime.date,
        amount_irr: int,
        channel_key: str | None = None,
        tieout_ok: bool | None = None,
    ) -> PolicyGateDecision:
        """Evaluate the gate for one event (fail-closed at every layer).

        ``channel_key`` (when the event carries one) adds two
        fail-closed checks on top of the three layers: the channel must
        exist and be enabled, and it must have an active contract —
        a channel without a contract has no auto-post policy at all.
        """
        settings = self._repository.get_installation_settings(installation_id)
        auto_post_enabled = settings.auto_post_enabled if settings is not None else False
        policy = self._repository.get_policy_config(installation_id, event_type)
        contract: GatewayContract | None = None
        channel_problem: str | None = None
        if channel_key:
            channel = self._repository.get_channel(installation_id, channel_key)
            if channel is None:
                channel_problem = f"Payment channel {channel_key!r} is not in the registry."
            elif not channel.enabled:
                channel_problem = f"Payment channel {channel_key!r} is disabled."
            else:
                contract = self._repository.get_active_contract_for_channel_key(
                    installation_id, channel_key
                )
                if contract is None:
                    channel_problem = (
                        f"Payment channel {channel_key!r} has no active contract."
                    )
        period_open = self._fiscal_checker.is_period_open(company, event_date)
        decision = evaluate_policy_gate(
            event_type=event_type,
            amount_irr=amount_irr,
            policy=policy,
            installation_auto_post_enabled=auto_post_enabled,
            contract=contract,
            period_open=period_open,
            tieout_ok=tieout_ok,
        )
        if channel_problem is None:
            return decision
        layers = decision.layers + (
            GateLayerResult(layer="channel", passed=False, detail=channel_problem),
        )
        return PolicyGateDecision(
            allowed=False,
            action="draft",
            layers=layers,
            policy_config_id=decision.policy_config_id,
            contract_id=decision.contract_id,
            contract_version=decision.contract_version,
            reason=f"{decision.reason} {channel_problem}" if decision.allowed else decision.reason,
            evaluated_at=decision.evaluated_at,
        )


#: Credential field names that are never accepted for a bank
#: connection (ADR 0003 decision 10 / §8.3). Any key containing
#: "password" is rejected — the correct model for bank access is
#: OAuth/consent, never password sharing. The check is a substring
#: match on purpose: ``internet_banking_password``, ``bam_password``,
#: ``online_banking_password`` and plain ``password`` are all the same
#: forbidden thing.
_FORBIDDEN_CREDENTIAL_SUBSTRING = "password"


def _reject_forbidden_credentials(credentials: Mapping[str, object]) -> None:
    """Reject a credential payload carrying an internet-banking password.

    The error names the rejected FIELD, never the value.
    """
    for key in credentials:
        if _FORBIDDEN_CREDENTIAL_SUBSTRING in key.lower():
            raise IntegrationCredentialsForbiddenError(
                f"Credential field {key!r} is forbidden: HesabYar never asks for "
                "or stores an internet-banking password. Bank access uses "
                "OAuth/consent or manual file import — see ADR 0003 decision 10.",
                details={"field": key},
            )


def _encode_credentials(
    credentials: Mapping[str, object] | None,
    vault: CredentialVaultPort | None,
) -> tuple[str | None, str | None]:
    """Validate + encrypt a credential payload (None when no credentials).

    Returns ``(ciphertext, key_id)``. An empty payload means "no
    credentials" (e.g. a manual-import connection).
    """
    if not credentials:
        return None, None
    if vault is None:
        raise IntegrationCredentialVaultUnavailableError(
            "Cannot store credentials: no credential-encryption key is configured "
            "(HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY).",
        )
    _reject_forbidden_credentials(credentials)
    plaintext = json.dumps(credentials, sort_keys=True, ensure_ascii=False)
    return vault.encrypt(plaintext), vault.key_id


class BankConnectionService:
    """Bank-connection registry writes over the repository port."""

    def __init__(
        self,
        repository: IntegrationsRegistryPort,
        vault: CredentialVaultPort | None,
    ) -> None:
        self._repository = repository
        self._vault = vault

    def create_connection(
        self,
        installation_id: str,
        *,
        company_id: str,
        bank_key: str,
        display_name: str,
        connection_kind: str,
        credentials: Mapping[str, object] | None = None,
        notes: str | None = None,
    ) -> BankConnection:
        """Create a bank connection for a company (credentials encrypted)."""
        _validate_key("bank", bank_key)
        display_name = _require_non_empty("display_name", display_name)
        connection_kind = _require_non_empty("connection_kind", connection_kind)
        company_id = _require_non_empty("company_id", company_id)
        for existing in self._repository.list_bank_connections(
            installation_id, company_id=company_id
        ):
            if existing.bank_key == bank_key:
                raise IntegrationBankConnectionConflictError(
                    f"Bank {bank_key!r} is already connected for this company.",
                    details={"bank_key": bank_key, "company_id": company_id},
                )
        ciphertext, key_id = _encode_credentials(credentials, self._vault)
        return self._repository.save_bank_connection(
            CreateBankConnection(
                installation_id=installation_id,
                company_id=company_id,
                bank_key=bank_key,
                display_name=display_name,
                connection_kind=connection_kind,
                credentials_ciphertext=ciphertext,
                credentials_key_id=key_id,
                notes=notes,
            )
        )

    def update_connection(
        self,
        installation_id: str,
        connection_id: str,
        *,
        display_name: str | None = None,
        connection_kind: str | None = None,
        status: str | None = None,
        notes: str | None = None,
    ) -> BankConnection:
        """Update a connection's mutable fields (never its credentials)."""
        updated = self._repository.update_bank_connection(
            UpdateBankConnection(
                installation_id=installation_id,
                connection_id=connection_id,
                display_name=display_name,
                connection_kind=connection_kind,
                status=status,
                notes=notes,
            )
        )
        if updated is None:
            raise IntegrationBankConnectionNotFoundError(
                f"Bank connection {connection_id!r} is not in this installation's registry.",
                details={"connection_id": connection_id},
            )
        return updated

    def rotate_credentials(
        self,
        installation_id: str,
        connection_id: str,
        credentials: Mapping[str, object],
    ) -> BankConnection:
        """Replace a connection's stored credentials (encrypted)."""
        connection = self._repository.get_bank_connection(installation_id, connection_id)
        if connection is None:
            raise IntegrationBankConnectionNotFoundError(
                f"Bank connection {connection_id!r} is not in this installation's registry.",
                details={"connection_id": connection_id},
            )
        ciphertext, key_id = _encode_credentials(credentials, self._vault)
        if ciphertext is None or key_id is None:
            raise IntegrationCredentialsForbiddenError(
                "A credential rotation needs a non-empty credential payload.",
                details={"connection_id": connection_id},
            )
        rotated = self._repository.rotate_bank_connection_credentials(
            RotateBankConnectionCredentials(
                installation_id=installation_id,
                connection_id=connection_id,
                credentials_ciphertext=ciphertext,
                credentials_key_id=key_id,
            )
        )
        assert rotated is not None  # the connection existed a moment ago
        return rotated

    def delete_connection(self, installation_id: str, connection_id: str) -> None:
        """Delete a bank connection (its stored ciphertext goes with it)."""
        if not self._repository.delete_bank_connection(installation_id, connection_id):
            raise IntegrationBankConnectionNotFoundError(
                f"Bank connection {connection_id!r} is not in this installation's registry.",
                details={"connection_id": connection_id},
            )


class StoreCreated:
    """A store plus its freshly generated secrets (shown exactly once).

    The plaintext webhook secret and API key exist only on this object:
    the repository stores the ciphertext, and no later read returns
    the plaintext again. Callers (the REST route) return these in the
    create response and never log them.
    """

    def __init__(self, store: Store, webhook_secret: str, api_key: str) -> None:
        self.store = store
        self.webhook_secret = webhook_secret
        self.api_key = api_key


class StoreService:
    """Store registry writes over the repository port."""

    def __init__(
        self,
        repository: IntegrationsRegistryPort,
        vault: CredentialVaultPort | None,
    ) -> None:
        self._repository = repository
        self._vault = vault

    def _require_vault(self) -> CredentialVaultPort:
        """Return the vault, or fail closed when no key is configured."""
        if self._vault is None:
            raise IntegrationCredentialVaultUnavailableError(
                "Cannot create a store: no credential-encryption key is configured "
                "(HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY).",
            )
        return self._vault

    @staticmethod
    def _validate_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
        """Validate store-key scopes: every scope must be read-only."""
        cleaned = tuple(scope.strip() for scope in scopes if scope.strip())
        for scope in cleaned:
            if not scope.endswith(":read"):
                raise IntegrationMappingInvalidError(
                    f"Store API-key scope {scope!r} is not read-only: "
                    "store keys are scoped read-only, every scope must end in ':read'.",
                    details={"scope": scope},
                )
        return cleaned

    def create_store(
        self,
        installation_id: str,
        *,
        store_key: str,
        display_name: str,
        store_type: str,
        prices_in_toman: bool = True,
        api_key_scopes: Sequence[str] | None = None,
        api_base_url: str | None = None,
        notes: str | None = None,
    ) -> StoreCreated:
        """Create a store, generating its webhook secret + read-only API key.

        Both secrets are generated by HesabYar, stored encrypted, and
        returned in plaintext exactly once (on :class:`StoreCreated`).
        """
        _validate_key("store", store_key)
        display_name = _require_non_empty("display_name", display_name)
        store_type = _require_non_empty("store_type", store_type)
        if self._repository.get_store(installation_id, store_key) is not None:
            raise IntegrationStoreKeyConflictError(
                f"Store {store_key!r} already exists in this installation.",
                details={"store_key": store_key},
            )
        vault = self._require_vault()
        scopes = (
            self._validate_scopes(api_key_scopes)
            if api_key_scopes is not None
            else READ_ONLY_STORE_SCOPES
        )
        webhook_secret = secrets.token_urlsafe(32)
        api_key = f"hya_store_{secrets.token_urlsafe(32)}"
        store = self._repository.save_store(
            CreateStore(
                installation_id=installation_id,
                store_key=store_key,
                display_name=display_name,
                store_type=store_type,
                prices_in_toman=prices_in_toman,
                webhook_secret_ciphertext=vault.encrypt(webhook_secret),
                api_key_ciphertext=vault.encrypt(api_key),
                api_key_scopes=scopes,
                api_base_url=api_base_url,
                notes=notes,
            )
        )
        return StoreCreated(store=store, webhook_secret=webhook_secret, api_key=api_key)

    def update_store(
        self,
        installation_id: str,
        store_key: str,
        *,
        display_name: str | None = None,
        store_type: str | None = None,
        prices_in_toman: bool | None = None,
        api_key_scopes: Sequence[str] | None = None,
        api_base_url: str | None = None,
        status: str | None = None,
        notes: str | None = None,
    ) -> Store:
        """Update a store's mutable fields (never its secrets)."""
        scopes = self._validate_scopes(api_key_scopes) if api_key_scopes is not None else None
        updated = self._repository.update_store(
            UpdateStore(
                installation_id=installation_id,
                store_key=store_key,
                display_name=display_name,
                store_type=store_type,
                prices_in_toman=prices_in_toman,
                api_key_scopes=scopes,
                api_base_url=api_base_url,
                status=status,
                notes=notes,
            )
        )
        if updated is None:
            raise IntegrationStoreNotFoundError(
                f"Store {store_key!r} is not in this installation's registry.",
                details={"store_key": store_key},
            )
        return updated

    def rotate_webhook_secret(self, installation_id: str, store_key: str) -> StoreCreated:
        """Generate a new webhook secret (returned exactly once).

        The returned :class:`StoreCreated` carries the new webhook
        secret in plaintext; its ``api_key`` field is empty — the API
        key did not change.
        """
        store = self._repository.get_store(installation_id, store_key)
        if store is None:
            raise IntegrationStoreNotFoundError(
                f"Store {store_key!r} is not in this installation's registry.",
                details={"store_key": store_key},
            )
        vault = self._require_vault()
        webhook_secret = secrets.token_urlsafe(32)
        rotated = self._repository.rotate_store_webhook_secret(
            RotateStoreWebhookSecret(
                installation_id=installation_id,
                store_key=store_key,
                webhook_secret_ciphertext=vault.encrypt(webhook_secret),
            )
        )
        assert rotated is not None  # the store existed a moment ago
        return StoreCreated(store=rotated, webhook_secret=webhook_secret, api_key="")

    def rotate_api_key(
        self,
        installation_id: str,
        store_key: str,
        *,
        api_key_scopes: Sequence[str] | None = None,
    ) -> StoreCreated:
        """Generate a new read-only API key (returned exactly once).

        The returned :class:`StoreCreated` carries the new API key in
        plaintext; its ``webhook_secret`` field is empty — the webhook
        secret did not change.
        """
        store = self._repository.get_store(installation_id, store_key)
        if store is None:
            raise IntegrationStoreNotFoundError(
                f"Store {store_key!r} is not in this installation's registry.",
                details={"store_key": store_key},
            )
        vault = self._require_vault()
        scopes = (
            self._validate_scopes(api_key_scopes)
            if api_key_scopes is not None
            else store.api_key_scopes
        )
        api_key = f"hya_store_{secrets.token_urlsafe(32)}"
        rotated = self._repository.rotate_store_api_key(
            RotateStoreApiKey(
                installation_id=installation_id,
                store_key=store_key,
                api_key_ciphertext=vault.encrypt(api_key),
                api_key_scopes=scopes,
            )
        )
        assert rotated is not None  # the store existed a moment ago
        return StoreCreated(store=rotated, webhook_secret="", api_key=api_key)

    def delete_store(self, installation_id: str, store_key: str) -> None:
        """Delete a store (its stored secrets go with it)."""
        if not self._repository.delete_store(installation_id, store_key):
            raise IntegrationStoreNotFoundError(
                f"Store {store_key!r} is not in this installation's registry.",
                details={"store_key": store_key},
            )

    def set_store_db_dsn(self, installation_id: str, store_key: str, *, dsn: str) -> Store:
        """Store a store's read-only database DSN (encrypted, never returned).

        The DSN is supplied by the store team: it authenticates as
        their read-only DB user (ADR 0003 decision 3 —
        reconciliation/backfill only, zero writes). Only the
        schemes the read-only reader supports are accepted; the DSN
        itself is never logged or returned — the read model carries
        only ``has_store_db_dsn``.
        """
        store = self._repository.get_store(installation_id, store_key)
        if store is None:
            raise IntegrationStoreNotFoundError(
                f"Store {store_key!r} is not in this installation's registry.",
                details={"store_key": store_key},
            )
        cleaned = dsn.strip()
        scheme = cleaned.split(":", 1)[0].lower() if ":" in cleaned else ""
        if not cleaned or scheme not in {
            "postgresql",
            "postgresql+psycopg",
            "sqlite",
            "sqlite+pysqlite",
        }:
            raise IntegrationSettingsInvalidError(
                "The store database DSN must be a postgresql:// or sqlite:// URL.",
                details={"store_key": store_key},
            )
        vault = self._require_vault()
        updated = self._repository.set_store_db_dsn(
            SetStoreDbDsn(
                installation_id=installation_id,
                store_key=store_key,
                dsn_ciphertext=vault.encrypt(cleaned),
            )
        )
        assert updated is not None  # the store existed a moment ago
        return updated

    def record_successful_poll(self, installation_id: str, store_key: str) -> Store | None:
        """Record a successful reconciliation poll (``last_poll_at`` = now).

        Returns None when the store no longer exists — the poll
        already happened; a missing store must not fail the sync.
        """
        return self._repository.update_store(
            UpdateStore(
                installation_id=installation_id,
                store_key=store_key,
                last_poll_at=datetime.datetime.now(datetime.UTC),
            )
        )


class InstallationSettingsService:
    """Installation settings + holiday-list writes over the repository port."""

    def __init__(self, repository: IntegrationsRegistryPort) -> None:
        self._repository = repository

    def upsert_settings(
        self,
        installation_id: str,
        *,
        weekly_holiday: int = 4,
        matching_window_days: int = 2,
        auto_post_enabled: bool = False,
    ) -> InstallationSettings:
        """Create or replace one installation's settings row."""
        if weekly_holiday < 0 or weekly_holiday > 6:
            raise IntegrationSettingsInvalidError(
                f"weekly_holiday must be a weekday number 0..6 (Monday=0, Friday=4), "
                f"got {weekly_holiday}.",
                details={"weekly_holiday": weekly_holiday},
            )
        if matching_window_days < 0:
            raise IntegrationSettingsInvalidError(
                f"matching_window_days must be >= 0, got {matching_window_days}.",
                details={"matching_window_days": matching_window_days},
            )
        return self._repository.save_installation_settings(
            UpsertInstallationSettings(
                installation_id=installation_id,
                weekly_holiday=weekly_holiday,
                matching_window_days=matching_window_days,
                auto_post_enabled=auto_post_enabled,
            )
        )

    def add_holiday(
        self,
        installation_id: str,
        holiday_date: datetime.date,
        title: str | None = None,
    ) -> Holiday:
        """Add a holiday date (idempotent: re-adding returns the same row)."""
        for existing in self._repository.list_holidays(installation_id):
            if existing.holiday_date == holiday_date:
                return existing
        return self._repository.add_holiday(
            AddHoliday(
                installation_id=installation_id,
                holiday_date=holiday_date,
                title=title,
            )
        )

    def remove_holiday(self, installation_id: str, holiday_date: datetime.date) -> None:
        """Remove a holiday date from the installation's list."""
        if not self._repository.delete_holiday(installation_id, holiday_date):
            raise IntegrationHolidayNotFoundError(
                f"Holiday {holiday_date.isoformat()} is not on this installation's list.",
                details={"holiday_date": holiday_date.isoformat()},
            )

    def get_holiday_calendar(self, installation_id: str) -> HolidayCalendar:
        """Build the installation's holiday calendar from the registry."""
        settings = self._repository.get_installation_settings(installation_id)
        weekly_holiday = settings.weekly_holiday if settings is not None else 4
        holidays = frozenset(
            holiday.holiday_date for holiday in self._repository.list_holidays(installation_id)
        )
        return HolidayCalendar(weekly_holiday=weekly_holiday, holidays=holidays)
