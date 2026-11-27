"""Bank auto-coding rules schemas (US-565, Phase 5-7 ۵خ W3).

Amounts are whole Rial everywhere (spec §19 rule 7): the UI takes
تومان from the operator and converts ×10 at the boundary, like the
rules engine's conditions. Pattern fragments travel as the operator
typed them; the service normalises them (Persian digits, ي/ك, case,
whitespace) before storage.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

from hesabyar.domain.bank.models import BankRule, BankRuleSuggestion
from hesabyar.interfaces.rest.schemas.bank import BankStatementMatchResponse


class BankRuleCreateRequest(BaseModel):
    """Create a bank rule (born in SUGGEST mode — promotion is the only path to AUTO)."""

    company_id: str = Field(description="HesabYar company row id")
    name: str = Field(min_length=1, max_length=200, description="Operator-facing rule name")
    target_account_id: int = Field(gt=0, description="Engine account id the rule codes to")
    bank_key: str = Field(default="", description="Bank key the rule applies to; empty = every bank")
    description_contains: str | None = Field(
        default=None,
        max_length=256,
        description="Description substring the line must contain (NULL = no condition)",
    )
    counterparty_contains: str | None = Field(
        default=None,
        max_length=256,
        description="Counterparty name substring (NULL = no condition)",
    )
    amount_min_irr: int | None = Field(
        default=None,
        ge=0,
        description="Minimum line magnitude in whole Rial, inclusive (NULL = no lower bound)",
    )
    amount_max_irr: int | None = Field(
        default=None,
        ge=0,
        description="Maximum line magnitude in whole Rial, inclusive (NULL = no upper bound)",
    )
    direction: str = Field(default="any", description="Money direction: 'in', 'out' or 'any'")
    target_party_id: int | None = Field(default=None, gt=0, description="Engine party id the rule codes to, when known")
    target_kind: str = Field(default="account", description="'account' or 'misc_income'")
    priority: int = Field(default=100, description="Firing order inside one company's set (lower runs first)")
    enabled: bool = Field(default=True, description="A disabled rule never fires")


class BankRuleUpdateRequest(BaseModel):
    """Update a bank rule; only the given fields change.

    An explicitly-null pattern field CLEARS that condition (the router
    passes exactly the fields the client set). Changing the pattern or
    the target resets the rule's confirmations and drops an AUTO rule
    back to SUGGEST — the trust was earned for the old definition.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    bank_key: str | None = Field(default=None)
    description_contains: str | None = Field(default=None, max_length=256)
    counterparty_contains: str | None = Field(default=None, max_length=256)
    amount_min_irr: int | None = Field(default=None, ge=0)
    amount_max_irr: int | None = Field(default=None, ge=0)
    direction: str | None = Field(default=None)
    target_account_id: int | None = Field(default=None, gt=0)
    target_party_id: int | None = Field(default=None, gt=0)
    target_kind: str | None = Field(default=None)
    priority: int | None = Field(default=None)
    enabled: bool | None = Field(default=None)


class BankRuleResponse(BaseModel):
    """A user-defined bank auto-coding rule (Suggest → Auto)."""

    id: str = Field(description="Rule UUID")
    tenant_id: str = Field(description="Owning tenant id")
    company_id: str = Field(description="HesabYar company row id")
    bank_key: str = Field(description="Bank key, or '' for any bank")
    name: str = Field(description="Operator-facing rule name")
    description_contains: str | None = Field(default=None)
    counterparty_contains: str | None = Field(default=None)
    amount_min_irr: int | None = Field(default=None)
    amount_max_irr: int | None = Field(default=None)
    direction: str = Field(description="'in', 'out' or 'any'")
    target_account_id: int = Field(description="Engine account id the rule codes to")
    target_party_id: int | None = Field(default=None, description="Engine party id, when known")
    target_kind: str = Field(description="'account' or 'misc_income'")
    mode: str = Field(description="'suggest' or 'auto'")
    confirmations: int = Field(description="Confirmed suggestions behind this rule")
    auto_eligible: bool = Field(description="True when a human may promote this rule to AUTO (threshold met)")
    priority: int = Field(description="Firing order (lower runs first)")
    enabled: bool = Field(description="A disabled rule never fires")
    created_by: str | None = Field(default=None)
    created_at: datetime.datetime | None = Field(default=None)
    updated_at: datetime.datetime | None = Field(default=None)

    @classmethod
    def from_rule(cls, rule: BankRule) -> BankRuleResponse:
        return cls(
            id=rule.id,
            tenant_id=rule.tenant_id,
            company_id=rule.company_id,
            bank_key=rule.bank_key,
            name=rule.name,
            description_contains=rule.description_contains,
            counterparty_contains=rule.counterparty_contains,
            amount_min_irr=rule.amount_min_irr,
            amount_max_irr=rule.amount_max_irr,
            direction=rule.direction,
            target_account_id=rule.target_account_id,
            target_party_id=rule.target_party_id,
            target_kind=rule.target_kind,
            mode=rule.mode.value,
            confirmations=rule.confirmations,
            auto_eligible=rule.auto_eligible,
            priority=rule.priority,
            enabled=rule.enabled,
            created_by=rule.created_by,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )


class BankRuleSuggestionResponse(BaseModel):
    """One suggestion a bank rule made for one statement line."""

    id: str = Field(description="Suggestion UUID")
    tenant_id: str = Field(description="Owning tenant id")
    rule_id: str | None = Field(default=None, description="Bank rule id (NULL when the rule was deleted)")
    rule_name: str = Field(description="Rule name snapshot")
    company_id: str = Field(description="HesabYar company row id")
    statement_id: int = Field(description="Engine statement id of the suggested line")
    statement_line_id: int = Field(description="Engine statement line id")
    line_description: str = Field(description="Statement line description snapshot")
    line_amount_irr: int = Field(description="Signed line amount in whole Rial")
    target_account_id: int = Field(description="Engine account id the rule suggested")
    target_party_id: int | None = Field(default=None, description="Engine party id, when known")
    status: str = Field(description="'pending', 'confirmed' or 'rejected'")
    confirmed_by: str | None = Field(default=None)
    confirmed_at: datetime.datetime | None = Field(default=None)
    created_at: datetime.datetime | None = Field(default=None)

    @classmethod
    def from_suggestion(cls, suggestion: BankRuleSuggestion) -> BankRuleSuggestionResponse:
        return cls(
            id=suggestion.id,
            tenant_id=suggestion.tenant_id,
            rule_id=suggestion.rule_id,
            rule_name=suggestion.rule_name,
            company_id=suggestion.company_id,
            statement_id=suggestion.statement_id,
            statement_line_id=suggestion.statement_line_id,
            line_description=suggestion.line_description,
            line_amount_irr=suggestion.line_amount_irr,
            target_account_id=suggestion.target_account_id,
            target_party_id=suggestion.target_party_id,
            status=suggestion.status.value,
            confirmed_by=suggestion.confirmed_by,
            confirmed_at=suggestion.confirmed_at,
            created_at=suggestion.created_at,
        )


class ConfirmSuggestionResponse(BaseModel):
    """What confirming a suggestion produced: the match + the rule's new state."""

    match: BankStatementMatchResponse = Field(description="The CONFIRMED match record")
    rule: BankRuleResponse | None = Field(
        default=None,
        description="The rule after the confirmation (NULL when the rule was deleted)",
    )


class UndoAutoCodeRequest(BaseModel):
    """Undo a wrong auto-code: the corrected coding is required.

    The engine line cannot be left uncoded (Tryton requires an account
    on every statement line), so undo always means "correct the
    mapping", never "clear it".
    """

    corrected_account_id: int = Field(gt=0, description="Engine account id to code the line to")
    corrected_party_id: int | None = Field(default=None, gt=0, description="Engine party id, when known")


class UndoAutoCodeResponse(BaseModel):
    """What undoing an auto-code produced: the corrected match + the undone rule."""

    match: BankStatementMatchResponse = Field(description="The CONFIRMED manual match record (the corrected coding)")
    undone_rule_id: str = Field(description="Rule id of the undone auto-code")
    undone_match_id: str = Field(description="Match id of the undone auto-code")
