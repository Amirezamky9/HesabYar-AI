"""Domain models for the tax-policy registry (US-402, spec §12.2).

Tax rules are versioned, effective-dated and traceable to an official
source snapshot. An annual VAT rate, exemption or threshold is never a
timeless constant: it lives on a :class:`TaxPolicyVersion` with its
rule year, performance year, filing year and effective dates as
separate fields (docs/CURRENT_RULES_1405.md §3 — e.g. the Article 100
ceiling is a 1405 rule about performance year 1404, filed in 1405).

Verification labels are the shared vocabulary of
docs/CURRENT_RULES_1405.md §1 (see
:class:`hesabyar.domain.standards.models.VerificationStatus`): only a
``VERIFIED_TEXT`` version may be used for production; anything else —
``CORROBORATED_CURRENT``, ``UNVERIFIED``, ``DISCOVERY_ONLY``,
``BLOCKED_SOURCE_GAP``, ``TRANSITIONAL`` — is blocked for production
by the decision service, which fails closed with a typed error naming
the missing official source pin.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique
from typing import NewType

from hesabyar.domain.standards.models import VerificationStatus

__all__ = [
    "RuleKey",
    "TaxDecision",
    "TaxPolicyVersion",
    "TaxPolicyVersionDetail",
    "TaxSource",
    "TaxSourceDetail",
    "UsageContext",
    "ValueUnit",
    "VerificationStatus",
]

RuleKey = NewType("RuleKey", str)
"""Stable rule identifier, e.g. 'vat_general_rate'."""


@unique
class ValueUnit(StrEnum):
    """Unit of a policy parameter value.

    - PERCENT: a percentage figure as written in the source (``10``
      means 10%, not 0.10) — e.g. the general VAT rate.
    - IRR: an amount in Iranian rials — e.g. the Article 84 exemption.
    """

    PERCENT = "percent"
    IRR = "irr"


@unique
class UsageContext(StrEnum):
    """Why a tax decision is being resolved.

    - REFERENCE: a read-only lookup (dashboard, report, what-if). Any
      active version resolves; the decision records the version's
      verification status so callers can see it is unverified.
    - PRODUCTION: the decision will drive a real tax computation or
      filing. Only a ``VERIFIED_TEXT`` version resolves; anything else
      is blocked with a typed error (the CURRENT_RULES_1405.md gate).
    """

    REFERENCE = "reference"
    PRODUCTION = "production"


@dataclass(frozen=True)
class TaxSource:
    """One corroborating or official source document in the registry.

    A source is a document (a circular, a consolidated law text, a
    parliament/budget report) that versions cite. ``source_key`` is the
    stable slug; ``sha256`` pins the snapshotted bytes once an official
    snapshot exists (``None`` while the source is only corroboration —
    exactly why the 1405 candidates are ``CORROBORATED_CURRENT`` and
    blocked for production).
    """

    id: str
    source_key: str
    title: str
    authority: str | None = None
    source_url: str | None = None
    snapshot_uri: str | None = None
    sha256: str | None = None
    notes: str | None = None
    versions_count: int = 0


@dataclass(frozen=True)
class TaxPolicyVersion:
    """One versioned tax rule with its annual parameter.

    ``rule_year``, ``performance_year`` and ``filing_year`` are
    separate persisted fields (spec §12.2): a rule enacted for 1405
    may govern performance year 1404 and be filed in 1405. The
    effective dates (Gregorian) drive resolution for a given date.
    ``parameter_value`` is exact (``Decimal`` — never a binary float)
    and meaningless without ``value_unit``.
    """

    id: str
    source_id: str
    source_key: str
    rule_key: RuleKey
    rule_name: str
    version_id: str
    parameter_value: Decimal
    value_unit: ValueUnit
    rule_year: int
    performance_year: int | None = None
    filing_year: int | None = None
    effective_from: datetime.date | None = None
    effective_to: datetime.date | None = None
    status: str = "active"
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verified_by: str | None = None
    verified_at: datetime.datetime | None = None
    source_authority: str | None = None
    source_url: str | None = None
    snapshot_uri: str | None = None
    sha256: str | None = None
    corroboration_urls: tuple[str, ...] = ()
    notes: str | None = None

    def is_effective_on(self, on: datetime.date) -> bool:
        """True when this version covers ``on`` (inclusive both ends)."""
        if self.effective_from is not None and on < self.effective_from:
            return False
        if self.effective_to is not None and on > self.effective_to:
            return False
        return True

    @property
    def is_production_ready(self) -> bool:
        """Only a VERIFIED_TEXT version may drive production use."""
        return self.verification_status == VerificationStatus.VERIFIED_TEXT


@dataclass(frozen=True)
class TaxSourceDetail:
    """One tax source with its policy versions (a domain read model)."""

    source: TaxSource
    versions: tuple[TaxPolicyVersion, ...]


@dataclass(frozen=True)
class TaxPolicyVersionDetail:
    """One policy version with its source (a domain read model)."""

    version: TaxPolicyVersion
    source: TaxSource


@dataclass(frozen=True)
class TaxDecision:
    """A recorded tax-policy resolution for a date + company + rule.

    The decision snapshots the resolving version (row id, version id,
    parameter value/unit, verification status, source key) so it stays
    auditable even if the version row is later deleted or superseded.
    ``usage_context`` records whether the resolution was a reference
    lookup or a production decision.
    """

    id: str
    tenant_id: str
    company_id: str | None
    rule_key: RuleKey
    decision_date: datetime.date
    usage_context: UsageContext
    policy_version_id: str | None
    version_id: str
    parameter_value: Decimal
    value_unit: ValueUnit
    verification_status: VerificationStatus
    source_key: str
    created_at: datetime.datetime
