"""Integration registries + policy gate domain (US-503, Phase 5 F3).

Payment channels, versioned gateway contracts, the mapping center
(spec §10.5), the three-layer policy gate, the bank-connection and
store registries, and the Iranian holiday calendar — all as
configurable registries, so nothing about a gateway or a rate is ever
hard-coded (ADR 0003 item 12).
"""

from hesabyar.domain.integrations.envelope import CanonicalEventEnvelope
from hesabyar.domain.integrations.holidays import FRIDAY, HolidayCalendar
from hesabyar.domain.integrations.models import (
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

__all__ = [
    "FRIDAY",
    "AutoPostPolicy",
    "BankConnection",
    "CanonicalEventEnvelope",
    "ChannelStatus",
    "ContractStatus",
    "CredentialVaultPort",
    "FiscalPeriodCheckerPort",
    "GatewayContract",
    "GateLayerResult",
    "Holiday",
    "HolidayCalendar",
    "InstallationSettings",
    "IntegrationMapping",
    "IntegrationsRegistryPort",
    "MappingKind",
    "MappingStatus",
    "MappingSuggestion",
    "PaymentChannel",
    "PolicyGateConfig",
    "PolicyGateDecision",
    "Store",
    "SuggestionMode",
    "SuggestionStatus",
    "TrancheLeg",
    "evaluate_policy_gate",
    "validate_tranche_legs",
]
