"""Cheque lifecycle (US-531 §5 §2.1 + US-532 §2.2/§2.3, §2.4).

Pure functions — no I/O. The state machine, the journal designs
(سند ۱–۱۳), the pre-acceptance verdict and the installment-leg hook
all live here so the application service and the tests share exactly
one definition of "what may happen next".

Journal designs are expressed as ACCOUNT ROLES (Persian chart
names from §2.4). The engine module (``account_check_ir``) resolves
each role to the installation's actual Tryton account and posts the
move in the same Tryton transaction as the state change; the
HesabYar layer never builds move lines itself. The design is
recorded on every transition result so the audit trail (report 17)
can show which سند each transition produced.

Collateral designs are memorandum-only (§2.2): they touch the 7.x
memorandum accounts and NEVER a commercial account — that is the
segregation rule, and it is tested at the engine level (move-line
account codes) as well as here.
"""

from __future__ import annotations

from dataclasses import dataclass

from hesabyar.domain.cheques.errors import ChequeTransitionNotAllowedError
from hesabyar.domain.cheques.models import (
    BROWN_COLOR_CODE,
    WHITE_COLOR_CODE,
    ChequeKind,
    ChequePurpose,
    ChequeState,
    InstallmentLegStatus,
    RedBrownPolicy,
)

#: Transition names — the same strings the REST endpoints, the
#: engine ``transition_rest`` action and the audit trail use.
TRANSITION_CONFIRM_RECEIPT = "confirm_receipt"
TRANSITION_DEPOSIT = "deposit"
TRANSITION_CLEAR = "clear"
TRANSITION_BOUNCE = "bounce"
TRANSITION_ENDORSE = "endorse"
TRANSITION_RETURN = "return"
TRANSITION_EXPIRE = "expire"
TRANSITION_REJECT_MISMATCH = "reject_mismatch"
TRANSITION_ISSUE = "issue"
TRANSITION_VOID = "void"
TRANSITION_CALL_IN = "call_in"
TRANSITION_UNEXPECTED_CALL_IN = "unexpected_call_in"
TRANSITION_ENDORSED_BOUNCE = "endorsed_bounce"
#: Site cancellation of an intake before held (US-533): registered
#: -> voided, no journal. After held a cancellation never voids —
#: it goes to the review queue instead.
TRANSITION_VOID_INTAKE = "void_intake"

#: (from_state, transition) -> to_state.
TRANSITIONS: dict[tuple[ChequeState, str], ChequeState] = {
    (ChequeState.REGISTERED, TRANSITION_CONFIRM_RECEIPT): ChequeState.HELD,
    (ChequeState.REGISTERED, TRANSITION_EXPIRE): ChequeState.EXPIRED_UNRECEIVED,
    (ChequeState.REGISTERED, TRANSITION_REJECT_MISMATCH): ChequeState.REJECTED_MISMATCH,
    (ChequeState.REGISTERED, TRANSITION_ISSUE): ChequeState.ISSUED,
    (ChequeState.REGISTERED, TRANSITION_VOID_INTAKE): ChequeState.VOIDED,
    (ChequeState.HELD, TRANSITION_DEPOSIT): ChequeState.IN_CLEARING,
    (ChequeState.HELD, TRANSITION_ENDORSE): ChequeState.ENDORSED,
    (ChequeState.HELD, TRANSITION_RETURN): ChequeState.RETURNED,
    (ChequeState.HELD, TRANSITION_CALL_IN): ChequeState.CALLED_IN,
    (ChequeState.IN_CLEARING, TRANSITION_CLEAR): ChequeState.CLEARED,
    (ChequeState.IN_CLEARING, TRANSITION_BOUNCE): ChequeState.BOUNCED,
    (ChequeState.BOUNCED, TRANSITION_RETURN): ChequeState.RETURNED,
    (ChequeState.ISSUED, TRANSITION_CLEAR): ChequeState.CLEARED,
    (ChequeState.ISSUED, TRANSITION_BOUNCE): ChequeState.BOUNCED,
    (ChequeState.ISSUED, TRANSITION_VOID): ChequeState.VOIDED,
    (ChequeState.ISSUED, TRANSITION_RETURN): ChequeState.RETURNED,
    (ChequeState.ISSUED, TRANSITION_UNEXPECTED_CALL_IN): ChequeState.CALLED_IN,
    (ChequeState.CALLED_IN, TRANSITION_DEPOSIT): ChequeState.IN_CLEARING,
    (ChequeState.CALLED_IN, TRANSITION_ENDORSE): ChequeState.ENDORSED,
    (ChequeState.CALLED_IN, TRANSITION_RETURN): ChequeState.RETURNED,
    (ChequeState.CALLED_IN, TRANSITION_CLEAR): ChequeState.CLEARED,
    (ChequeState.ENDORSED, TRANSITION_ENDORSED_BOUNCE): ChequeState.BOUNCED,
}

#: Transitions the design deliberately disables, with the reason the
#: error carries (§9.1 #5: re-presentment of a bounced cheque).
DISABLED_TRANSITIONS: dict[tuple[ChequeState, str], str] = {
    (ChequeState.BOUNCED, TRANSITION_DEPOSIT): (
        "Re-presentment of a bounced cheque (bounced -> in_clearing) is "
        "DISABLED by design (§9.1 #5, UNVERIFIED with the accountant/bank). "
        "Only bounced -> returned plus a replacement payment (cash or a "
        "new cheque) is supported."
    ),
}


@dataclass(frozen=True, slots=True)
class JournalDesign:
    """One سند from the §2.4 table, as debit/credit account roles."""

    number: str
    transition: str
    debit_role: str
    credit_role: str
    #: True when the design allows an alternative debit role (bounce
    #: destination per installation setting; prepayment credit).
    debit_alternative_role: str | None = None
    credit_alternative_role: str | None = None
    #: Bounce reversals are always dated in the CURRENT period (§2.1).
    current_period_only: bool = False
    #: The simultaneous companion move of a two-move transition
    #: (``call_in``: سند ۹الف memorandum reversal alongside ۹ب).
    companion_number: str | None = None
    companion_debit_role: str | None = None
    companion_credit_role: str | None = None
    #: Note when the commercial counterpart of this design is
    #: accountant-gated and deliberately NOT designed here (§9.1).
    gated_note: str | None = None


#: §2.4 journal table, commercial designs (سند ۱–۶ + ۱۰–۱۳).
#: The memorandum variants (سند ۷/۸ + issued mirrors) live in
#: ``COLLATERAL_JOURNAL_DESIGNS``; ``journal_design_for`` picks by
#: (transition, kind, purpose).
JOURNAL_DESIGNS: dict[str, JournalDesign] = {
    TRANSITION_CONFIRM_RECEIPT: JournalDesign(
        number="۱",
        transition=TRANSITION_CONFIRM_RECEIPT,
        debit_role="اسناد دریافتنی نزد صندوق",
        credit_role="حساب‌های دریافتنی",
        credit_alternative_role="پیش‌دریافت مشتری",
    ),
    TRANSITION_DEPOSIT: JournalDesign(
        number="۲",
        transition=TRANSITION_DEPOSIT,
        debit_role="اسناد دریافتنی در جریان وصول",
        credit_role="اسناد دریافتنی نزد صندوق",
    ),
    TRANSITION_CLEAR: JournalDesign(
        number="۳",
        transition=TRANSITION_CLEAR,
        debit_role="موجودی نقد و بانک",
        credit_role="اسناد دریافتنی در جریان وصول",
    ),
    TRANSITION_BOUNCE: JournalDesign(
        number="۴",
        transition=TRANSITION_BOUNCE,
        debit_role="حساب‌های دریافتنی",
        credit_role="اسناد دریافتنی در جریان وصول",
        debit_alternative_role="چک‌های برگشتی/مطالبات غیرجاری",
        current_period_only=True,
    ),
    TRANSITION_ENDORSE: JournalDesign(
        number="۵",
        transition=TRANSITION_ENDORSE,
        debit_role="حساب‌های پرداختنی",
        credit_role="اسناد دریافتنی نزد صندوق",
    ),
    TRANSITION_RETURN: JournalDesign(
        number="۶",
        transition=TRANSITION_RETURN,
        debit_role="حساب‌های دریافتنی",
        credit_role="اسناد دریافتنی نزد صندوق",
    ),
    TRANSITION_ISSUE: JournalDesign(
        number="۱۰",
        transition=TRANSITION_ISSUE,
        debit_role="حساب‌های پرداختنی",
        credit_role="اسناد پرداختنی",
    ),
    TRANSITION_VOID: JournalDesign(
        number="۱۳",
        transition=TRANSITION_VOID,
        debit_role="اسناد پرداختنی",
        credit_role="حساب‌های پرداختنی",
    ),
    TRANSITION_CALL_IN: JournalDesign(
        number="۹ب",
        transition=TRANSITION_CALL_IN,
        debit_role="اسناد دریافتنی نزد صندوق",
        credit_role="حساب‌های دریافتنی",
        companion_number="۹الف",
        companion_debit_role="طرف حساب‌های انتظامی",
        companion_credit_role="اسناد تضمینی دریافتی",
    ),
    TRANSITION_UNEXPECTED_CALL_IN: JournalDesign(
        number="۷-معکوس",
        transition=TRANSITION_UNEXPECTED_CALL_IN,
        debit_role="طرف حساب اسناد تضمینی نزد دیگران",
        credit_role="اسناد تضمینی نزد دیگران",
        gated_note=(
            "The commercial journal for a called-in issued collateral "
            "cheque is accountant-gated (§9.1 #4) and is NOT designed "
            "here — only the memorandum reversal is posted by this "
            "transition; the accountant posts the commercial journal."
        ),
    ),
}

#: Memorandum (off-balance-sheet) designs (§2.2 + issued mirrors).
#: Keyed by (transition, kind): a collateral journal touches ONLY
#: the 7.x memorandum accounts — never a commercial account.
COLLATERAL_JOURNAL_DESIGNS: dict[tuple[str, str], JournalDesign] = {
    (TRANSITION_CONFIRM_RECEIPT, "received"): JournalDesign(
        number="۷",
        transition=TRANSITION_CONFIRM_RECEIPT,
        debit_role="اسناد تضمینی دریافتی",
        credit_role="طرف حساب‌های انتظامی",
    ),
    (TRANSITION_RETURN, "received"): JournalDesign(
        number="۸",
        transition=TRANSITION_RETURN,
        debit_role="طرف حساب‌های انتظامی",
        credit_role="اسناد تضمینی دریافتی",
    ),
    (TRANSITION_ISSUE, "issued"): JournalDesign(
        number="۷",
        transition=TRANSITION_ISSUE,
        debit_role="اسناد تضمینی نزد دیگران",
        credit_role="طرف حساب اسناد تضمینی نزد دیگران",
    ),
    (TRANSITION_RETURN, "issued"): JournalDesign(
        number="۸",
        transition=TRANSITION_RETURN,
        debit_role="طرف حساب اسناد تضمینی نزد دیگران",
        credit_role="اسناد تضمینی نزد دیگران",
    ),
}

#: Issued-commercial designs that differ from the received ones
#: (سند ۱۱/۱۲ vs سند ۳/۴). Keyed by transition; ``journal_design_for``
#: picks them when kind == "issued".
ISSUED_JOURNAL_DESIGNS: dict[str, JournalDesign] = {
    TRANSITION_CLEAR: JournalDesign(
        number="۱۱",
        transition=TRANSITION_CLEAR,
        debit_role="اسناد پرداختنی",
        credit_role="موجودی نقد و بانک",
    ),
    TRANSITION_BOUNCE: JournalDesign(
        number="۱۲",
        transition=TRANSITION_BOUNCE,
        debit_role="اسناد پرداختنی",
        credit_role="حساب‌های پرداختنی",
        current_period_only=True,
    ),
}

#: Transitions that create NO journal (§2.4): intake, expiry,
#: mismatch rejection, voiding a site-cancelled intake, returning
#: an already-bounced cheque (the claim was re-established by سند ۴
#: and stays until paid), and a bounced endorsed cheque (the
#: reverse-of-endorsement journal is accountant-gated, §9.1 #3 —
#: no journal is guessed).
NO_JOURNAL_TRANSITIONS: frozenset[str] = frozenset(
    {TRANSITION_EXPIRE, TRANSITION_REJECT_MISMATCH, TRANSITION_ENDORSED_BOUNCE, TRANSITION_VOID_INTAKE}
)


def target_state(from_state: ChequeState, transition: str) -> ChequeState:
    """Return the state ``transition`` leads to, or raise.

    Raises :class:`ChequeTransitionNotAllowedError` for unknown
    transitions, transitions from the wrong state, and deliberately
    disabled transitions (with the design reason).
    """
    disabled = DISABLED_TRANSITIONS.get((from_state, transition))
    if disabled is not None:
        raise ChequeTransitionNotAllowedError(
            from_state.value, transition, reason=disabled
        )
    target = TRANSITIONS.get((from_state, transition))
    if target is None:
        raise ChequeTransitionNotAllowedError(
            from_state.value,
            transition,
            reason=(
                f"No {transition!r} transition exists from state "
                f"{from_state.value!r}."
            ),
        )
    return target


def journal_design_for(
    transition: str,
    kind: ChequeKind | str = ChequeKind.RECEIVED,
    purpose: ChequePurpose | str = ChequePurpose.PAYMENT,
) -> JournalDesign | None:
    """Return the §2.4 journal design for a transition, or None when
    the transition creates no journal.

    The design depends on the cheque: collateral cheques use the
    memorandum designs (سند ۷/۸, 7.x accounts only), issued
    commercial cheques use سند ۱۱/۱۲ for clear/bounce, and
    ``call_in`` carries its companion (سند ۹الف) on the design.
    ``return`` from ``bounced`` creates no journal — the caller
    knows the from-state and the design function does not, so the
    design still returns سند ۶ for ``return`` and the engine
    decides by from-state.
    """
    kind_value = kind.value if isinstance(kind, ChequeKind) else kind
    purpose_value = (
        purpose.value if isinstance(purpose, ChequePurpose) else purpose
    )
    if purpose_value == ChequePurpose.COLLATERAL.value:
        collateral = COLLATERAL_JOURNAL_DESIGNS.get((transition, kind_value))
        if collateral is not None:
            return collateral
    if kind_value == ChequeKind.ISSUED.value:
        issued = ISSUED_JOURNAL_DESIGNS.get(transition)
        if issued is not None:
            return issued
    return JOURNAL_DESIGNS.get(transition)


def assert_transition_scope(
    kind: ChequeKind | str,
    purpose: ChequePurpose | str,
    from_state: ChequeState | str,
    transition: str,
) -> None:
    """Refuse a transition outside its (kind, purpose, state) scope.

    The mirror of the engine's ``_check_scope`` (§2.2/§2.3): a
    commercial transition on a collateral cheque, an issued
    transition on a received cheque, or a transition from the
    wrong state for that direction is refused here — before
    anything reaches the engine. A called-in received collateral
    cheque has purpose ``payment`` by then, so the §2.1
    transitions apply to it; an unexpectedly called-in issued
    collateral cheque clears from ``called_in`` only after the
    accountant has posted its commercial journal.
    """
    kind_value = kind.value if isinstance(kind, ChequeKind) else kind
    purpose_value = (
        purpose.value if isinstance(purpose, ChequePurpose) else purpose
    )
    state_value = (
        from_state.value if isinstance(from_state, ChequeState) else from_state
    )
    received = kind_value == ChequeKind.RECEIVED.value
    issued = kind_value == ChequeKind.ISSUED.value
    payment = purpose_value == ChequePurpose.PAYMENT.value
    collateral = purpose_value == ChequePurpose.COLLATERAL.value
    allowed = False
    if transition == TRANSITION_CONFIRM_RECEIPT:
        allowed = received and state_value == ChequeState.REGISTERED.value
    elif transition in (TRANSITION_DEPOSIT, TRANSITION_ENDORSE):
        allowed = received and payment and state_value in (
            ChequeState.HELD.value,
            ChequeState.CALLED_IN.value,
        )
    elif transition == TRANSITION_CLEAR:
        allowed = (received and payment and state_value == ChequeState.IN_CLEARING.value) or (
            issued and payment and state_value in (
                ChequeState.ISSUED.value,
                ChequeState.CALLED_IN.value,
            )
        )
    elif transition == TRANSITION_BOUNCE:
        allowed = (received and payment and state_value == ChequeState.IN_CLEARING.value) or (
            issued and payment and state_value == ChequeState.ISSUED.value
        )
    elif transition == TRANSITION_RETURN:
        allowed = (received and state_value in (
            ChequeState.HELD.value,
            ChequeState.BOUNCED.value,
        )) or (
            issued and collateral and state_value == ChequeState.ISSUED.value
        )
    elif transition in (TRANSITION_EXPIRE, TRANSITION_REJECT_MISMATCH):
        allowed = received and state_value == ChequeState.REGISTERED.value
    elif transition == TRANSITION_ISSUE:
        allowed = issued and state_value == ChequeState.REGISTERED.value
    elif transition == TRANSITION_VOID:
        allowed = issued and payment and state_value == ChequeState.ISSUED.value
    elif transition == TRANSITION_CALL_IN:
        allowed = received and collateral and state_value == ChequeState.HELD.value
    elif transition == TRANSITION_UNEXPECTED_CALL_IN:
        allowed = issued and collateral and state_value == ChequeState.ISSUED.value
    elif transition == TRANSITION_ENDORSED_BOUNCE:
        allowed = received and payment and state_value == ChequeState.ENDORSED.value
    if not allowed:
        raise ChequeTransitionNotAllowedError(
            state_value,
            transition,
            reason=(
                f"Transition {transition!r} does not apply to a "
                f"{kind_value} / {purpose_value} cheque in state "
                f"{state_value!r} (§5 §2.2/§2.3 scope)."
            ),
        )


# ---------------------------------------------------------------------------
# Pre-acceptance (§2.1): item matching + issuer credit, before held
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreAcceptanceInput:
    """Inputs to the pre-acceptance decision, all from the Sayyad
    inquiry receipts (manual until US-534 wires the APIs)."""

    #: The four Sayyad items (sayad_id, amount, due date, payee
    #: identity) all matched the leaf. A single mismatch means
    #: NEVER confirm (§2.1) — the caller checks this first.
    items_matched: bool
    #: CBI issuer colour code 1–5 (1=white … 5=red); None = not inquired.
    color_code: int | None = None
    #: Issuer's bounced-cheque count from the back-cheques inquiry.
    back_cheques_count: int | None = None
    #: Issuer's in-transit (Sayyad) commitments, Rial; None = unknown.
    in_transit_commitments_irr: int | None = None


@dataclass(frozen=True, slots=True)
class PreAcceptanceVerdict:
    """The pre-acceptance decision: accept, hold for a manager, or refuse."""

    decision: str  # "accept" | "manager_approval_required" | "reject"
    warnings: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.decision == "accept"


def evaluate_pre_acceptance(
    pre: PreAcceptanceInput,
    *,
    red_brown_policy: RedBrownPolicy,
    max_in_transit_commitments_irr: int | None,
) -> PreAcceptanceVerdict:
    """Decide whether a registered cheque may be confirmed to held.

    Fail-closed: a mismatch refuses outright (the caller must use
    ``registered -> rejected_mismatch`` instead). A red/brown colour
    or in-transit commitments above the installation threshold
    follows the installation policy (reject, or manager approval).
    A white colour is NOT a guarantee — it always carries a warning
    in the UI (§2.1).
    """
    warnings: list[str] = []
    reasons: list[str] = []
    if not pre.items_matched:
        return PreAcceptanceVerdict(
            decision="reject",
            reasons=("Sayyad items do not match the leaf — never confirm on mismatch (§2.1).",),
        )
    if pre.color_code is not None and not (1 <= pre.color_code <= 5):
        return PreAcceptanceVerdict(
            decision="reject",
            reasons=(f"Issuer colour code {pre.color_code} is outside the 1–5 scale.",),
        )
    decision = "accept"
    if pre.color_code is not None and pre.color_code >= BROWN_COLOR_CODE:
        colour = "brown" if pre.color_code == BROWN_COLOR_CODE else "red"
        reasons.append(f"Issuer colour is {colour} (code {pre.color_code}).")
        if red_brown_policy is RedBrownPolicy.REJECT:
            decision = "reject"
        else:
            decision = "manager_approval_required"
    if (
        pre.in_transit_commitments_irr is not None
        and max_in_transit_commitments_irr is not None
        and pre.in_transit_commitments_irr > max_in_transit_commitments_irr
    ):
        reasons.append(
            "Issuer in-transit commitments "
            f"({pre.in_transit_commitments_irr:,} Rial) exceed the installation "
            f"threshold ({max_in_transit_commitments_irr:,} Rial)."
        )
        if red_brown_policy is RedBrownPolicy.REJECT:
            decision = "reject"
        elif decision == "accept":
            decision = "manager_approval_required"
    if pre.color_code == WHITE_COLOR_CODE:
        warnings.append(
            "رنگ سفید تضمین نیست — a white issuer colour is history only, "
            "not a guarantee this leaf will clear (§2.1)."
        )
    if pre.color_code is None:
        warnings.append("Issuer colour was not inquired before acceptance (manual fallback).")
    if pre.back_cheques_count:
        warnings.append(
            f"Issuer has {pre.back_cheques_count} bounced cheque(s) on record."
        )
    return PreAcceptanceVerdict(decision=decision, warnings=tuple(warnings), reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Installment-leg hook (§2.1 + batch 1)
# ---------------------------------------------------------------------------


def leg_status_effect(transition: str) -> InstallmentLegStatus | None:
    """The leg-status effect of a cheque transition, if any.

    - ``clear`` marks the linked leg PAID (and, in ۵ج, moves that
      leg's financing-profit share from unrealised to realised);
    - ``bounce`` marks the linked leg UNPAID (back to
      ``pending_clearance`` in ۵ج terms);
    - every other transition leaves the leg untouched (until clearing
      the leg stays ``pending_clearance`` and no overdue dunning is
      sent for it, unless installation policy says otherwise).

    The service persists the effect on the cheque row
    (``installment_leg_status``); the ۵ج installment module consumes
    it. The two-way test with a REAL installment_leg is a deferred
    cross-phase test for ۵ج — this function is the hook, not the
    integration.
    """
    if transition == TRANSITION_CLEAR:
        return InstallmentLegStatus.PAID
    if transition == TRANSITION_BOUNCE:
        return InstallmentLegStatus.UNPAID
    return None
