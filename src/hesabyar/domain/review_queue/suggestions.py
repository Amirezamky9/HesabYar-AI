"""Derived action suggestions for review items (US-552, Phase 5-6 ۵ح).

Every item shows the system's suggested action with a Persian reason.
The suggestion is DERIVED from the item's type + payload (severity,
subtype, channel) — never hard-coded per item and never stored: the
stored ``suggested_action`` free text is the filer's note, while this
module computes the structured suggestion at read time, so a rule
change re-suggests the whole queue immediately.

The derivation returns an action CODE (always one of the type's
permitted §7.2 actions — enforced by tests) plus a stable reason KEY;
the Persian reason sentence lives in the frontend
(``features/review-queue/review-queue-data.ts``), like every other
user-facing label — this layer stays English.

Rules (type first, then payload overrides):

- ``mapping`` → ``complete_mapping_retry``: the event cannot process
  until the mapping exists.
- ``settlement_variance`` → subtype ``fee_mismatch`` →
  ``fix_fee_mapping`` (the fee mapping is the defect); critical →
  ``escalate_to_gateway`` (a large variance needs the gateway);
  otherwise ``accept_variance``.
- ``cheque_exception`` → a bounced subtype (``cheque_bounced``,
  ``issued_cheque_bounced``) → ``record_bounce``; otherwise
  ``contact_customer`` (investigate with the customer first).
- ``bank_unmatched`` → ``manual_match``: only a manual match
  resolves the line (an unmatched deposit never auto-posts).
- ``packaging_reorder`` → ``approve_purchase``: the reorder point
  was hit; the purchase request waits on approval.
- ``installment_overdue`` → ``send_reminder``: dunning first —
  collateral activation is never the default.
- ``manual_import_review`` → ``approve_preview``: the batch passed
  validation; approval is the expected path.
- ``cashbox_variance`` → critical → ``escalate_to_manager``;
  otherwise ``approve_variance_entry`` (the count stands unless
  doubted — recount is for a suspect count).
- ``inbox_dead_letter`` → ``replay``: the dead letter's whole point.
- ``sms_delivery_failed`` → ``retry_reminder``: a transient
  failure — retry first.
- ``reminder`` → a customer-facing subtype (``cheque_due_soon``,
  ``installment_overdue``) → ``contact_customer`` (follow up with
  the customer/issuer first); otherwise ``acknowledge`` (the
  operator acts on the reminder — counts the box, renews the
  eNamad — and closes it).
"""

from __future__ import annotations

from dataclasses import dataclass

from hesabyar.domain.review_queue.models import (
    ReviewItem,
    ReviewItemType,
    ReviewSeverity,
)

#: Cheque subtypes that mean "the cheque bounced" (filed by the
#: cheque module, US-531/532): the bounce must be recorded, not
#: investigated first.
_BOUNCED_SUBTYPES = frozenset({"cheque_bounced", "issued_cheque_bounced"})

#: Reminder subtypes (US-564) whose follow-up is with a customer or
#: cheque issuer: the suggestion is to contact them, not merely to
#: acknowledge the reminder. The other subtypes (cashbox_uncounted,
#: enmad_expiry) are the operator's own work — acknowledge.
_CUSTOMER_FOLLOW_UP_SUBTYPES = frozenset({"cheque_due_soon", "installment_overdue"})


@dataclass(frozen=True)
class SuggestedAction:
    """The system's derived suggestion for one review item.

    ``action_code`` is one of the item type's permitted §7.2
    actions; ``reason_key`` is a stable key the frontend maps to a
    Persian reason sentence.
    """

    action_code: str
    reason_key: str


def derive_suggestion(item: ReviewItem) -> SuggestedAction:
    """Derive the suggested action + reason key for an item.

    Pure function of the item's type + payload (severity, subtype).
    The returned action is always permitted for the item's type.
    """
    item_type = item.item_type
    if item_type is ReviewItemType.MAPPING:
        return SuggestedAction("complete_mapping_retry", "mapping_blocks_event")
    if item_type is ReviewItemType.SETTLEMENT_VARIANCE:
        if item.subtype == "fee_mismatch":
            return SuggestedAction("fix_fee_mapping", "variance_is_fee_mismatch")
        if item.severity is ReviewSeverity.CRITICAL:
            return SuggestedAction("escalate_to_gateway", "large_variance_needs_gateway")
        return SuggestedAction("accept_variance", "small_variance_accept")
    if item_type is ReviewItemType.CHEQUE_EXCEPTION:
        if item.subtype in _BOUNCED_SUBTYPES:
            return SuggestedAction("record_bounce", "cheque_bounced")
        return SuggestedAction("contact_customer", "cheque_needs_customer")
    if item_type is ReviewItemType.BANK_UNMATCHED:
        return SuggestedAction("manual_match", "line_needs_match")
    if item_type is ReviewItemType.PACKAGING_REORDER:
        return SuggestedAction("approve_purchase", "stock_below_reorder")
    if item_type is ReviewItemType.INSTALLMENT_OVERDUE:
        return SuggestedAction("send_reminder", "installment_overdue_remind")
    if item_type is ReviewItemType.MANUAL_IMPORT_REVIEW:
        return SuggestedAction("approve_preview", "batch_preview_ready")
    if item_type is ReviewItemType.CASHBOX_VARIANCE:
        if item.severity is ReviewSeverity.CRITICAL:
            return SuggestedAction("escalate_to_manager", "large_variance_needs_manager")
        return SuggestedAction("approve_variance_entry", "variance_needs_approval")
    if item_type is ReviewItemType.INBOX_DEAD_LETTER:
        return SuggestedAction("replay", "event_dead_lettered")
    if item_type is ReviewItemType.SMS_DELIVERY_FAILED:
        return SuggestedAction("retry_reminder", "sms_send_failed")
    if item_type is ReviewItemType.REMINDER:
        if item.subtype in _CUSTOMER_FOLLOW_UP_SUBTYPES:
            return SuggestedAction("contact_customer", "reminder_needs_follow_up")
        return SuggestedAction("acknowledge", "reminder_needs_attention")
    raise AssertionError(f"unhandled review item type {item_type!r}")
