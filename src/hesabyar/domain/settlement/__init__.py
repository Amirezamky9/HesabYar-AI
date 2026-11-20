"""Settlement engine domain (US-521, Phase 5-3 Wave A).

Phased design §3: versioned gateway contracts (in
``domain.integrations``) drive the expected settlement schedule for
every gateway payment — one tranche per contract leg, derived at
payment time, with the §3.3 state machine, the 11 exception types,
fee tracking, and the file-import model for the matching stories
(US-522/523) to build on.
"""

from hesabyar.domain.settlement.derivation import derive_payment_amounts, derive_tranches
from hesabyar.domain.settlement.matching import (
    DepositMatchDecision,
    DepositToMatch,
    EventCandidate,
    MatchAllocation,
    MatchSuggestion,
    TieOutResult,
    TrancheCandidate,
    batch_match_allocations,
    compute_tieout,
    match_deposit,
)
from hesabyar.domain.settlement.models import (
    BankDeposit,
    GatewayPayment,
    GatewayPaymentStatus,
    PaymentSource,
    SettlementEvent,
    SettlementException,
    SettlementExceptionResolution,
    SettlementExceptionSeverity,
    SettlementExceptionStatus,
    SettlementExceptionType,
    SettlementFeeRecord,
    SettlementFileImport,
    SettlementFileRow,
    SettlementMatch,
    SettlementPosting,
    SettlementPostingKind,
    SettlementPostingStatus,
    SettlementTranche,
    TrancheState,
)

__all__ = [
    "BankDeposit",
    "DepositMatchDecision",
    "DepositToMatch",
    "EventCandidate",
    "GatewayPayment",
    "GatewayPaymentStatus",
    "MatchAllocation",
    "MatchSuggestion",
    "PaymentSource",
    "SettlementEvent",
    "SettlementException",
    "SettlementExceptionResolution",
    "SettlementExceptionSeverity",
    "SettlementExceptionStatus",
    "SettlementExceptionType",
    "SettlementFeeRecord",
    "SettlementFileImport",
    "SettlementFileRow",
    "SettlementMatch",
    "SettlementPosting",
    "SettlementPostingKind",
    "SettlementPostingStatus",
    "SettlementTranche",
    "TieOutResult",
    "TrancheCandidate",
    "TrancheState",
    "batch_match_allocations",
    "compute_tieout",
    "derive_payment_amounts",
    "derive_tranches",
    "match_deposit",
]
