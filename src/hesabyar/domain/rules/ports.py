"""Ports for the rules engine (US-561; phased design §6.4/§6.7).

The application layer talks to persistence and to the other bounded
contexts ONLY through these ports — it never imports infrastructure
(spec §6.3). Each effect of the closed action set has its own port,
so the executor is testable with fakes and each production adapter
can evolve independently:

- :class:`RuleRepositoryPort` — rules, executions, rule-set configs.
- :class:`RulePolicyGatePort` — the 3-layer policy gate (F3) for
  ``post_if_policy_allows``. The gate is the ONLY path to posting:
  the executor never posts without an ``allowed`` verdict from this
  port, and a missing/unwired gate fails closed (no post).
- :class:`DraftCreationPort` — ``create_draft`` (journal drafts).
- :class:`DraftPostingPort` — posting a journal draft after the gate.
- :class:`NotificationPort` — ``notify`` (in-app + email in v1; the
  SMS channel extends through this same port in US-564).

Review-queue effects (``assign`` / ``tag`` / ``set_field``) reuse
the existing :class:`ReviewQueuePort` /
:class:`ReviewAuditPort` from ``hesabyar.domain.review_queue`` —
no new port is needed for them.
"""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from hesabyar.domain.events.envelope import CanonicalEventType
from hesabyar.domain.rules.models import (
    NotificationDelivery,
    Rule,
    RuleDraftLine,
    RuleExecution,
    RuleGateVerdict,
    RuleSetConfig,
)


class RuleRepositoryPort(ABC):
    """Rules, executions and rule-set configs (SQLAlchemy impl flushes; the caller commits)."""

    @abstractmethod
    def save_rule(self, rule: Rule) -> Rule:
        """Insert or update a rule; returns it with timestamps set."""

    @abstractmethod
    def get_rule(self, tenant_id: str, rule_id: str) -> Rule | None:
        """One rule of a tenant, or None (also when another tenant's)."""

    @abstractmethod
    def list_rules(
        self,
        tenant_id: str,
        *,
        installation_id: str | None = None,
        event_type: CanonicalEventType | None = None,
        enabled: bool | None = None,
    ) -> Sequence[Rule]:
        """A tenant's rules, in priority order (ties: created_at, id).

        ``installation_id=None`` lists only tenant-wide rules (rows
        with a NULL installation); pass an installation id to list
        that installation's rules.
        """

    @abstractmethod
    def delete_rule(self, tenant_id: str, rule_id: str) -> bool:
        """Delete a rule; False when it does not exist in this tenant.

        Executions survive the rule (``rule_id`` is set NULL) — the
        audit log is append-only.
        """

    @abstractmethod
    def save_execution(self, execution: RuleExecution) -> RuleExecution:
        """Append one execution record (append-only); returns it."""

    @abstractmethod
    def get_execution(self, tenant_id: str, execution_id: str) -> RuleExecution | None:
        """One execution of a tenant, or None."""

    @abstractmethod
    def list_executions(
        self,
        tenant_id: str,
        *,
        rule_id: str | None = None,
        event_id: str | None = None,
        limit: int = 50,
    ) -> Sequence[RuleExecution]:
        """A tenant's executions, newest first."""

    @abstractmethod
    def get_set_config(self, tenant_id: str, event_type: CanonicalEventType) -> RuleSetConfig | None:
        """A rule set's stored config, or None (the default applies)."""

    @abstractmethod
    def save_set_config(self, config: RuleSetConfig) -> RuleSetConfig:
        """Insert or replace a rule set's config."""

    @abstractmethod
    def list_set_configs(self, tenant_id: str) -> Sequence[RuleSetConfig]:
        """A tenant's stored rule-set configs (may be empty)."""


class RulePolicyGatePort(Protocol):
    """The 3-layer policy gate (F3) as the rules engine sees it.

    Read-only: evaluating the gate never posts, never writes. The
    production adapter wraps :class:`PolicyGateService` (US-503);
    the adapter's fail-closed rules apply — no installation context,
    no company, or no amount all deny.
    """

    def evaluate(
        self,
        *,
        installation_id: str | None,
        event_type: str,
        company_engine_id: int | None,
        event_date: datetime.date | None,
        amount_irr: int | None,
        channel_key: str | None,
    ) -> RuleGateVerdict:
        """Decide whether the event's draft may auto-post."""
        ...


class DraftCreationPort(Protocol):
    """Create a draft journal from a rule's template (``create_draft``)."""

    def create_journal_draft(
        self,
        *,
        company_engine_id: int,
        date: datetime.date,
        description: str,
        lines: Sequence[RuleDraftLine],
    ) -> str:
        """Create the draft in the engine; return its reference string."""
        ...


class DraftPostingPort(Protocol):
    """Post a journal draft (``post_if_policy_allows``, after the gate)."""

    def post_journal_draft(
        self,
        *,
        company_engine_id: int,
        draft_engine_id: int,
    ) -> str:
        """Post the draft in the engine; return the posted reference string."""
        ...


class NotificationPort(Protocol):
    """Deliver a ``notify`` action (in-app + email in v1).

    The port returns per-channel results — a channel the deployment
    cannot deliver (no SMTP backend, SMS before US-564) reports
    ``skipped``/``unsupported`` with a reason; delivery is never
    claimed when it did not happen. ``facts`` is the event's fact
    dict, for ``{{placeholder}}`` rendering (the adapter renders;
    the executor passes the raw templates through).
    """

    def send(
        self,
        *,
        recipients: Sequence[str],
        channels: Sequence[str],
        subject: str,
        message: str,
        facts: Mapping[str, Any],
    ) -> NotificationDelivery:
        """Send the notification; return per-channel delivery results."""
        ...
