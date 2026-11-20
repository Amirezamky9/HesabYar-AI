"""Installment domain errors (US-540, Phase 5-5 ۵ج).

Every error carries a stable machine-readable ``code`` so the REST
layer can map it to a problem-details response without string
matching, and a ``details`` mapping for the values a caller or an
operator needs to recover (never PII beyond what the caller supplied).

The linkage guard (the user's key rule — batch 1 item 4) has its own
error, :class:`InstallmentLinkageError`: a receipt without BOTH the
original invoice ref and the accounting-document (journal move) ref
is rejected with a typed 422, never stored.
"""

from __future__ import annotations

from hesabyar.domain.common.errors import InvariantViolationError


class InstallmentError(InvariantViolationError):
    """Base class for installment-domain failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.details: dict[str, object] = details or {}


class InstallmentValidationError(InstallmentError):
    """A plan / leg / receipt / contract payload failed validation (fail-closed).

    ``details["field_errors"]`` maps field name -> human-readable
    problem; the REST layer renders it as a 422.
    """

    def __init__(self, message: str, *, field_errors: dict[str, str]) -> None:
        super().__init__(
            message, code="INSTALLMENT_VALIDATION_FAILED", details={"field_errors": field_errors}
        )
        self.field_errors = field_errors


class InstallmentPlanNotFoundError(InstallmentError):
    """No installment plan with the requested id exists in this tenant/installation."""

    def __init__(self, plan_id: str) -> None:
        super().__init__(
            f"Installment plan {plan_id!r} does not exist.",
            code="INSTALLMENT_PLAN_NOT_FOUND",
            details={"plan_id": plan_id},
        )


class InstallmentContractNotFoundError(InstallmentError):
    """No installment sale contract with the requested id exists in this tenant/installation."""

    def __init__(self, contract_id: str) -> None:
        super().__init__(
            f"Installment sale contract {contract_id!r} does not exist.",
            code="INSTALLMENT_CONTRACT_NOT_FOUND",
            details={"contract_id": contract_id},
        )


class InstallmentPlanExistsError(InstallmentError):
    """A plan already exists for this invoice (one plan per invoice, ever)."""

    def __init__(self, invoice_id: str, *, existing_plan_id: str) -> None:
        super().__init__(
            f"An installment plan already exists for invoice {invoice_id!r}.",
            code="INSTALLMENT_PLAN_EXISTS",
            details={"invoice_id": invoice_id, "existing_plan_id": existing_plan_id},
        )


class InstallmentLinkageError(InstallmentError):
    """THE LINKAGE GUARD: a receipt without both refs is rejected.

    Every installment receipt MUST reference the original invoice
    (``invoice_id``) AND the accounting document (``journal_move_id``).
    ``details["missing"]`` lists the refs that were absent; the REST
    layer renders this as a typed 422
    (``INSTALLMENT_RECEIPT_LINKAGE_REQUIRED``).
    """

    def __init__(self, *, missing: tuple[str, ...]) -> None:
        missing_list = ", ".join(sorted(missing))
        super().__init__(
            "An installment receipt must reference both the original "
            f"invoice and the accounting document (journal); missing: {missing_list}.",
            code="INSTALLMENT_RECEIPT_LINKAGE_REQUIRED",
            details={"missing": list(missing)},
        )
        self.missing = missing


class InstallmentInvoiceMismatchError(InstallmentError):
    """A receipt's invoice ref does not match the plan's invoice.

    The receipt names an invoice, but it is not the invoice the plan
    belongs to — the linkage is wrong, not merely missing. Typed 422.
    """

    def __init__(self, *, receipt_invoice_id: str, plan_invoice_id: str) -> None:
        super().__init__(
            "The receipt's invoice does not match the plan's invoice: "
            f"receipt names {receipt_invoice_id!r}, the plan belongs to "
            f"{plan_invoice_id!r}.",
            code="INSTALLMENT_INVOICE_MISMATCH",
            details={
                "receipt_invoice_id": receipt_invoice_id,
                "plan_invoice_id": plan_invoice_id,
            },
        )


class InstallmentLateFeeAccountMissingError(InstallmentError):
    """A late fee cannot be journaled: the contract lacks an account.

    The late-fee journal needs BOTH sides — the penalty account
    (credit: finance income) and the receivable account (debit:
    what the customer owes). ``details["missing"]`` names the
    account(s) not configured on the contract. Typed 422: the
    operator configures the accounts and retries — the fee is never
    journaled to a guessed account, and a settlement that would
    keep the fee is refused until the accounts exist.
    """

    def __init__(self, *, contract_id: str, missing: tuple[str, ...]) -> None:
        missing_list = ", ".join(sorted(missing))
        super().__init__(
            "The installment sale contract cannot post a late-fee journal: "
            f"missing account(s): {missing_list}.",
            code="INSTALLMENT_LATE_FEE_ACCOUNT_MISSING",
            details={"contract_id": contract_id, "missing": list(missing)},
        )
        self.contract_id = contract_id
        self.missing = missing


class InstallmentSettlementAmountError(InstallmentError):
    """An early settlement's amount does not cover the payoff quote.

    A FULL settlement must pay at least the payoff total (remaining
    principal + income due + late fees due − advance held); less
    than that is a partial payment, not a settlement, and must say
    so (``settlement_type: "partial"``). Typed 422.
    """

    def __init__(self, *, amount_irr: int, payoff_irr: int) -> None:
        super().__init__(
            "The settlement amount does not cover the payoff quote: "
            f"amount {amount_irr:,} Rial, payoff {payoff_irr:,} Rial.",
            code="INSTALLMENT_SETTLEMENT_AMOUNT_INSUFFICIENT",
            details={"amount_irr": amount_irr, "payoff_irr": payoff_irr},
        )


class InstallmentAlreadySettledError(InstallmentError):
    """The plan has no open legs — there is nothing left to settle.

    Every leg is paid (or the schedule was already closed by an
    earlier settlement). A second settlement is a 409, never a
    no-op receipt.
    """

    def __init__(self, plan_id: str) -> None:
        super().__init__(
            f"Installment plan {plan_id!r} has no open legs to settle.",
            code="INSTALLMENT_PLAN_ALREADY_SETTLED",
            details={"plan_id": plan_id},
        )


class InstallmentPaymentMethodError(InstallmentError):
    """A receipt's payment method is not supported by this story.

    US-540 records cash / bank-transfer / other receipts; US-542
    adds cheque receipts (a cheque-paid leg enters
    ``pending_clearance`` until the cheque clears) and US-543
    adds BNPL receipts (BNPL plans only). An unknown method still
    fails closed: typed 422.
    """

    def __init__(self, method: str, *, reason: str) -> None:
        super().__init__(
            f"Payment method {method!r} is not supported for installment receipts: {reason}",
            code="INSTALLMENT_PAYMENT_METHOD_UNSUPPORTED",
            details={"payment_method": method, "reason": reason},
        )


class InstallmentChequeNotReceivableError(InstallmentError):
    """A cheque receipt names a cheque that cannot receive a payment (US-542).

    Receivable means: a RECEIVED commercial (``payment``) cheque in
    ``held`` or ``in_clearing`` — the leaf has been accepted (سند ۱
    exists, so the receipt's ``journal_move_id`` honestly references
    it) and the cheque can still clear. A ``registered`` cheque has
    no journal yet (intake creates none until ``held``); a cleared /
    bounced / returned / endorsed / voided / expired / rejected
    cheque can no longer be the instrument of a new receipt; an
    issued or collateral cheque is never a customer payment. Typed 422.
    """

    def __init__(self, cheque_id: str, *, state: str, kind: str, purpose: str) -> None:
        super().__init__(
            f"Cheque {cheque_id!r} is not receivable for an installment "
            f"receipt: {kind}/{purpose} in state {state!r} (a receipt "
            "needs a received commercial cheque in 'held' or 'in_clearing').",
            code="INSTALLMENT_CHEQUE_NOT_RECEIVABLE",
            details={
                "cheque_id": cheque_id,
                "state": state,
                "kind": kind,
                "purpose": purpose,
            },
        )


class InstallmentChequeLinkError(InstallmentError):
    """A cheque receipt's cheque is already linked to a different leg (US-542).

    The cheque ↔ leg link is single-leg by design (one cheque pays
    one leg); a cheque already linked to another leg cannot back a
    receipt for this leg. Typed 422.
    """

    def __init__(self, cheque_id: str, *, linked_leg_id: str, leg_id: str) -> None:
        super().__init__(
            f"Cheque {cheque_id!r} already pays leg {linked_leg_id!r}; "
            f"it cannot also pay leg {leg_id!r}.",
            code="INSTALLMENT_CHEQUE_LINK_MISMATCH",
            details={
                "cheque_id": cheque_id,
                "linked_leg_id": linked_leg_id,
                "leg_id": leg_id,
            },
        )


class InstallmentChequeAlreadyUsedError(InstallmentError):
    """A cheque receipt names a cheque that already backs a receipt (US-542).

    One cheque backs at most one receipt — reusing the same cheque
    for a second receipt would double-count the instrument. Typed 422.
    """

    def __init__(self, cheque_id: str, *, existing_receipt_id: str) -> None:
        super().__init__(
            f"Cheque {cheque_id!r} already backs receipt "
            f"{existing_receipt_id!r}; one cheque backs at most one receipt.",
            code="INSTALLMENT_CHEQUE_ALREADY_USED",
            details={"cheque_id": cheque_id, "existing_receipt_id": existing_receipt_id},
        )


class InstallmentChequeAmountError(InstallmentError):
    """A cheque receipt's amount exceeds its cheque's amount (US-542).

    A receipt backed by a cheque cannot pay more than the cheque
    itself; the applied amount (``applied_amount_irr`` on the
    cheque) is the receipt's allocated slice, capped at the leg's
    balance. Typed 422.
    """

    def __init__(self, *, receipt_amount_irr: int, cheque_amount_irr: int) -> None:
        super().__init__(
            "The receipt amount "
            f"({receipt_amount_irr:,} Rial) exceeds its cheque's amount "
            f"({cheque_amount_irr:,} Rial).",
            code="INSTALLMENT_CHEQUE_AMOUNT_EXCEEDED",
            details={
                "receipt_amount_irr": receipt_amount_irr,
                "cheque_amount_irr": cheque_amount_irr,
            },
        )


class InstallmentBnplPlanRequiredError(InstallmentError):
    """A BNPL receipt was recorded against an INTERNAL plan (US-543).

    BNPL and internal installments are separate worlds: a leg paid
    through a BNPL gateway belongs to a BNPL plan (kind ``bnpl``),
    never to an internal plan. Typed 422.
    """

    def __init__(self, *, plan_id: str) -> None:
        super().__init__(
            "A BNPL receipt requires a BNPL plan: plan "
            f"{plan_id!r} is an internal plan. Create the sale as a "
            "BNPL plan (kind 'bnpl' with its gateway) instead.",
            code="INSTALLMENT_BNPL_PLAN_REQUIRED",
            details={"plan_id": plan_id},
        )


class InstallmentBnplPlanMethodError(InstallmentError):
    """A non-BNPL receipt was recorded against a BNPL plan (US-543).

    A BNPL plan's legs are paid by the gateway only — a cash / bank
    / other receipt on it would mix customer money into a
    gateway-receivable plan. Typed 422.
    """

    def __init__(self, *, plan_id: str, method: str) -> None:
        super().__init__(
            f"Plan {plan_id!r} is a BNPL plan: only BNPL receipts are "
            f"accepted, not {method!r}. The gateway pays this plan's "
            "legs; the customer never pays the seller directly.",
            code="INSTALLMENT_BNPL_PLAN_METHOD_NOT_ALLOWED",
            details={"plan_id": plan_id, "payment_method": method},
        )


class InstallmentBnplGatewayMismatchError(InstallmentError):
    """A BNPL receipt names a different gateway than its plan (US-543).

    A BNPL plan pins exactly one gateway channel; every receipt on
    it must come through that gateway. Typed 422.
    """

    def __init__(self, *, plan_id: str, plan_gateway: str, receipt_gateway: str) -> None:
        super().__init__(
            f"Plan {plan_id!r} is a {plan_gateway!r} BNPL plan; the "
            f"receipt names gateway {receipt_gateway!r}. One sale goes "
            "through one gateway.",
            code="INSTALLMENT_BNPL_GATEWAY_MISMATCH",
            details={
                "plan_id": plan_id,
                "plan_gateway": plan_gateway,
                "receipt_gateway": receipt_gateway,
            },
        )


class InstallmentReceiptNotFoundError(InstallmentError):
    """No installment receipt with the requested id exists in this tenant/installation."""

    def __init__(self, receipt_id: str) -> None:
        super().__init__(
            f"Installment receipt {receipt_id!r} does not exist.",
            code="INSTALLMENT_RECEIPT_NOT_FOUND",
            details={"receipt_id": receipt_id},
        )


class InstallmentNotBnplReceiptError(InstallmentError):
    """A BNPL-only operation was applied to a non-BNPL receipt (US-543).

    Only a receipt recorded with ``payment_method == bnpl`` has a
    gateway payment to reject or cancel. Typed 422.
    """

    def __init__(self, *, receipt_id: str, method: str) -> None:
        super().__init__(
            f"Receipt {receipt_id!r} is a {method!r} receipt, not a "
            "BNPL receipt — there is no gateway payment to reject.",
            code="INSTALLMENT_NOT_A_BNPL_RECEIPT",
            details={"receipt_id": receipt_id, "payment_method": method},
        )


class InstallmentBnplAlreadyRejectedError(InstallmentError):
    """A BNPL receipt was rejected twice (US-543).

    The first rejection already returned the legs to ``expected``
    and filed the review item; a second rejection has nothing left
    to do. Typed 409.
    """

    def __init__(self, *, receipt_id: str, status: str) -> None:
        super().__init__(
            f"BNPL receipt {receipt_id!r} was already {status}; it cannot "
            "be rejected again.",
            code="INSTALLMENT_BNPL_ALREADY_REJECTED",
            details={"receipt_id": receipt_id, "bnpl_status": status},
        )


class InstallmentBnplUnavailableError(InstallmentError):
    """The BNPL path is not wired (no settlement service) — a server misconfiguration.

    BNPL receipts need the settlement engine (US-521) to create the
    gateway receivable; without it the service refuses rather than
    recording a receipt with no gateway payment behind it. Typed 503.
    """

    def __init__(self) -> None:
        super().__init__(
            "BNPL receipts are unavailable: the settlement engine is not "
            "wired into the installment service.",
            code="INSTALLMENT_BNPL_UNAVAILABLE",
            details={},
        )


class InstallmentBnplEarlySettlementError(InstallmentError):
    """An early settlement (or payoff quote) was requested for a BNPL plan.

    Added at the US-541 merge: a BNPL plan's legs are owed to the
    gateway, not the seller — there is no payoff to quote and no
    settlement to close with the seller. Early settlement is the
    gateway's business, never the seller's books. Typed 422.
    """

    def __init__(self, *, plan_id: str) -> None:
        super().__init__(
            f"Plan {plan_id!r} is a BNPL plan: it has no payoff quote "
            "and cannot settle early with the seller — the customer "
            "owes the gateway, not the seller.",
            code="INSTALLMENT_BNPL_EARLY_SETTLEMENT_NOT_ALLOWED",
            details={"plan_id": plan_id},
        )
