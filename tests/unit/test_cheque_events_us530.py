"""US-530: ADR 0004 + the 15 cheque decisions (static checks).

ADR 0004 approves the canonical cheque event names and the internal
(dashboard-only) cycle events; docs/cheque-decisions.md records all
15 §5 §9.1 decisions with their build defaults. These tests read the
documents — they guard the approval itself, not behaviour. US-533
wired the canonical names into ingest (enum + webhook mapping +
pipeline branch), so the handoff test below asserts they ARE
ingestible now.
"""

from __future__ import annotations

from pathlib import Path

from hesabyar.domain.events.cheque_events import (
    CHEQUE_CANONICAL_EVENT_TYPES,
    CHEQUE_INTERNAL_EVENT_TYPES,
)
from hesabyar.domain.events.envelope import CanonicalEventType
from hesabyar.domain.events.store_webhooks import (
    STORE_WEBHOOK_MAPPINGS,
    StoreWebhookTopic,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_0004 = REPO_ROOT / "docs" / "adr" / "0004-cheque-events.md"
DECISIONS = REPO_ROOT / "docs" / "cheque-decisions.md"


def test_adr_0004_accepted_and_names_canonical_events() -> None:
    text = ADR_0004.read_text(encoding="utf-8")
    assert "- **Status:** Accepted" in text
    assert "cheque.received.registered" in text
    assert "cheque.received.cancelled" in text
    # The US-501 contract is named, not redesigned.
    assert "X-HesabYar-Event-Type" in text
    assert "payload_hash" in text
    assert "schema_version" in text
    # Internal (non-canonical, dashboard-only) cycle events are listed.
    assert "cheque.held" in text
    assert "cheque.in_clearing" in text
    assert "cheque.cleared" in text
    assert "cheque.bounced" in text


def test_cheque_event_constants_match_adr_0004() -> None:
    assert CHEQUE_CANONICAL_EVENT_TYPES == (
        "cheque.received.registered",
        "cheque.received.cancelled",
    )
    assert set(CHEQUE_INTERNAL_EVENT_TYPES) == {
        "cheque.held",
        "cheque.in_clearing",
        "cheque.cleared",
        "cheque.bounced",
        "cheque.returned",
        "cheque.endorsed",
        "cheque.issued",
        "cheque.voided",
        "cheque.expired_unreceived",
        "cheque.rejected_mismatch",
        "cheque.called_in",
    }
    # Internal names never collide with canonical names.
    assert not set(CHEQUE_INTERNAL_EVENT_TYPES) & set(
        CHEQUE_CANONICAL_EVENT_TYPES
    )
    assert not set(CHEQUE_INTERNAL_EVENT_TYPES) & {
        t.value for t in CanonicalEventType
    }


def test_canonical_names_ingestible_since_us533() -> None:
    """Handoff resolved: US-533 wired the enum members + cleared pending_adr.

    ADR 0004 approved the names; US-533 made them ingestible — the
    canonical members exist on the enum and the webhook mapping
    points at them with no pending ADR.
    """
    assert "cheque.received.registered" in {t.value for t in CanonicalEventType}
    assert "cheque.received.cancelled" in {t.value for t in CanonicalEventType}
    mapping = STORE_WEBHOOK_MAPPINGS[StoreWebhookTopic.CHEQUE_REGISTERED.value]
    assert mapping.pending_adr is False
    assert mapping.canonical_types == (CanonicalEventType.CHEQUE_RECEIVED_REGISTERED,)
    assert mapping.proposed_targets == ()
    cancelled = STORE_WEBHOOK_MAPPINGS[StoreWebhookTopic.CHEQUE_CANCELLED.value]
    assert cancelled.pending_adr is False
    assert cancelled.canonical_types == (CanonicalEventType.CHEQUE_RECEIVED_CANCELLED,)
    assert cancelled.proposed_targets == ()


def test_cheque_decisions_covers_all_15() -> None:
    text = DECISIONS.read_text(encoding="utf-8")
    for number in range(1, 16):
        assert f"## #{number} " in text, f"decision #{number} missing"


def test_cheque_decisions_build_defaults() -> None:
    text = DECISIONS.read_text(encoding="utf-8")
    # #1 first journal at held; #2 bounce default AR; #6 7-day arrival;
    # #7 auto-post default draft; #14 storage default vault.
    assert "registered → held" in text
    assert "1.2.1" in text
    assert "1.2.9 Bounced Cheques" in text
    assert "7 days" in text
    assert "draft" in text.lower()
    assert "vault" in text
    # #5 re-presentment stays DISABLED; #3, #4, #12 stay gated.
    assert "DISABLED" in text
    for gated in ("#3", "#4", "#12"):
        assert "ACCOUNTANT-GATED" in text
        assert gated in text
