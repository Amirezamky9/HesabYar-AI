"""Commands for the tax-policy bounded context (US-402).

A command is the already-validated input to a write: the decision
service resolves the version and builds this command, and the
repository persists it. Commands carry domain types only — no ORM,
no infrastructure.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.domain.tax_policy.models import RuleKey, UsageContext, ValueUnit


@dataclass(frozen=True)
class RecordTaxDecision:
    """Record a resolved tax-policy decision.

    All fields are the resolution's snapshot: the resolving version's
    row id, version id, parameter value/unit, verification status and
    source key are denormalized into the decision so it stays
    auditable even if the version row is later deleted or superseded.
    The repository assigns the decision id and timestamp.
    """

    tenant_id: str
    company_id: str | None
    rule_key: RuleKey
    decision_date: datetime.date
    usage_context: UsageContext
    policy_version_id: str
    version_id: str
    parameter_value: Decimal
    value_unit: ValueUnit
    verification_status: VerificationStatus
    source_key: str
