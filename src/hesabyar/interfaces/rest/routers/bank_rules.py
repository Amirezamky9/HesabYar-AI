"""Bank auto-coding rules routes (US-565, Phase 5-7 ۵خ W3).

Thin routes over the bank-rules service. Bank rules are W3 rules
(phased design §1.3 ۵خ): rule configuration (CRUD, enable/disable,
promote/demote, suggestions list, reject) needs ``rules:read`` /
``rules:manage`` — the same scopes as the US-561 rules engine.
Confirming a suggestion writes a statement line through the engine
(like a manual match), so it needs ``banking:reconcile`` and an
``Idempotency-Key`` header, like every other bank mutation. The undo
endpoint lives with the statement-line routes in ``routers/bank.py``.

- ``GET /bank/rules`` — list rules (filters: company, enabled, mode),
  in firing order.
- ``POST /bank/rules`` — create a rule (born in SUGGEST mode; there
  is no way to create an AUTO rule directly).
- ``GET /bank/rules/{rule_id}`` — one rule.
- ``PUT /bank/rules/{rule_id}`` — update a rule (only the given
  fields change; an explicit null clears a pattern condition;
  changing the pattern or target resets the confirmations and drops
  an AUTO rule back to SUGGEST).
- ``DELETE /bank/rules/{rule_id}`` — delete a rule (its suggestions
  survive, rule_id NULL).
- ``POST /bank/rules/{rule_id}/enable`` / ``.../disable`` — enable/disable.
- ``POST /bank/rules/{rule_id}/promote`` — promote SUGGEST → AUTO
  (needs 3 confirmed suggestions; 409 below threshold).
- ``POST /bank/rules/{rule_id}/demote`` — demote AUTO → SUGGEST
  (confirmations reset to zero; the rule must re-earn trust).
- ``GET /bank/rules/suggestions`` — list suggestions (filters:
  rule_id, status, company), newest first.
- ``POST /bank/rules/suggestions/{id}/confirm`` — confirm a
  suggestion: the coding is applied to the line and the rule's
  confirmation count grows by one.
- ``POST /bank/rules/suggestions/{id}/reject`` — reject a suggestion:
  the line never re-suggests from that rule.

Route order matters: the static path (``/bank/rules/suggestions``)
is registered before ``/bank/rules/{rule_id}`` so FastAPI matches it
first. See ``docs/bank-rules.md``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Path, Query
from sqlalchemy.orm import Session

from hesabyar.application.commands.bank_rules import BankRulesService
from hesabyar.application.queries.statements import BankStatementQueryService
from hesabyar.domain.accounting.refs import BankStatementRef
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_bank_rules_service,
    get_bank_statement_query_service,
    get_db_session,
    require_company_row_by_id,
    require_scopes,
    require_visible_company,
)
from hesabyar.interfaces.rest.errors import translate_errors
from hesabyar.interfaces.rest.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    require_idempotency_key,
)
from hesabyar.interfaces.rest.schemas.bank import BankStatementMatchResponse
from hesabyar.interfaces.rest.schemas.bank_rules import (
    BankRuleCreateRequest,
    BankRuleResponse,
    BankRuleSuggestionResponse,
    BankRuleUpdateRequest,
    ConfirmSuggestionResponse,
)

router = APIRouter(tags=["bank-rules"])


# -- static paths first (before /bank/rules/{rule_id}) ------------------------


@router.get(
    "/bank/rules/suggestions",
    response_model=list[BankRuleSuggestionResponse],
    summary="List bank-rule suggestions, newest first",
)
def list_bank_rule_suggestions(
    rule_id: str | None = Query(default=None, description="Filter to one rule"),
    status: str | None = Query(default=None, description="Filter by status: 'pending', 'confirmed' or 'rejected'"),
    company: str | None = Query(default=None, description="Filter by HesabYar company row id"),
    client: AuthenticatedClient = Depends(require_scopes("rules:read")),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> list[BankRuleSuggestionResponse]:
    """Return the tenant's bank-rule suggestions (a bad status filter is a 422)."""
    with translate_errors():
        suggestions = service.list_suggestions(client.tenant_id, rule_id=rule_id, status=status, company_id=company)
    return [BankRuleSuggestionResponse.from_suggestion(s) for s in suggestions]


@router.post(
    "/bank/rules/suggestions/{suggestion_id}/confirm",
    response_model=ConfirmSuggestionResponse,
    summary="Confirm a suggestion (apply the coding, count toward promotion)",
)
def confirm_bank_rule_suggestion(
    suggestion_id: str = Path(..., description="Suggestion id (UUID)"),
    idempotency_key: str | None = Header(
        default=None,
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Unique key for this mutation; retries replay the original response",
    ),
    client: AuthenticatedClient = Depends(require_scopes("banking:reconcile")),
    session: Session = Depends(get_db_session),
    service: BankRulesService = Depends(get_bank_rules_service),
    query_service: BankStatementQueryService = Depends(get_bank_statement_query_service),
) -> ConfirmSuggestionResponse:
    """Confirm a pending suggestion: the line is coded and the rule is credited.

    The statement must still be a draft and the line must have no live
    match (409 otherwise). The rule's confirmation count grows by one;
    at the threshold (3) the rule becomes promotable to AUTO.
    """
    require_idempotency_key(idempotency_key)
    with translate_errors():
        suggestion = service.get_suggestion(client.tenant_id, suggestion_id)
    row = require_company_row_by_id(session, client, suggestion.company_id)
    with translate_errors():
        view = query_service.get_bank_statement(BankStatementRef(suggestion.statement_id))
    require_visible_company(
        session,
        client,
        view.company,
        suggestion.statement_id,
        resource_kind="Bank statement",
        error_code="LEDGER_STATEMENT_NOT_FOUND",
    )
    with translate_errors():
        result = service.confirm_suggestion(
            tenant_id=client.tenant_id,
            suggestion_id=suggestion_id,
            company_id=row.id,
            actor=client.client_name,
        )
    session.commit()
    return ConfirmSuggestionResponse(
        match=BankStatementMatchResponse.from_match(result.match),
        rule=BankRuleResponse.from_rule(result.rule) if result.rule is not None else None,
    )


@router.post(
    "/bank/rules/suggestions/{suggestion_id}/reject",
    response_model=BankRuleSuggestionResponse,
    summary="Reject a suggestion (the line never re-suggests from that rule)",
)
def reject_bank_rule_suggestion(
    suggestion_id: str = Path(..., description="Suggestion id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> BankRuleSuggestionResponse:
    """Reject a pending suggestion (no engine write — a decision record only)."""
    with translate_errors():
        suggestion = service.reject_suggestion(
            tenant_id=client.tenant_id,
            suggestion_id=suggestion_id,
            actor=client.client_name,
        )
    session.commit()
    return BankRuleSuggestionResponse.from_suggestion(suggestion)


# -- rule CRUD ------------------------------------------------------------------


@router.get(
    "/bank/rules",
    response_model=list[BankRuleResponse],
    summary="List bank rules in firing order",
)
def list_bank_rules(
    company: str | None = Query(default=None, description="Filter by HesabYar company row id"),
    enabled: bool | None = Query(default=None, description="Filter by enabled flag"),
    mode: str | None = Query(default=None, description="Filter by mode: 'suggest' or 'auto'"),
    client: AuthenticatedClient = Depends(require_scopes("rules:read")),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> list[BankRuleResponse]:
    """Return the tenant's bank rules (a bad mode filter is a 422)."""
    with translate_errors():
        rules = service.list_rules(client.tenant_id, company_id=company, enabled=enabled, mode=mode)
    return [BankRuleResponse.from_rule(r) for r in rules]


@router.post(
    "/bank/rules",
    response_model=BankRuleResponse,
    status_code=201,
    summary="Create a bank rule (born in SUGGEST mode)",
)
def create_bank_rule(
    body: BankRuleCreateRequest,
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> BankRuleResponse:
    """Create a bank rule (validated before it is stored).

    The rule's company must belong to the caller's tenant. At least
    one pattern condition is required — a rule with no condition
    would match every line.
    """
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        rule = service.create_rule(
            tenant_id=client.tenant_id,
            company_id=row.id,
            name=body.name,
            target_account_id=body.target_account_id,
            bank_key=body.bank_key,
            description_contains=body.description_contains,
            counterparty_contains=body.counterparty_contains,
            amount_min_irr=body.amount_min_irr,
            amount_max_irr=body.amount_max_irr,
            direction=body.direction,
            target_party_id=body.target_party_id,
            target_kind=body.target_kind,
            priority=body.priority,
            enabled=body.enabled,
            actor=client.client_id,
        )
    session.commit()
    return BankRuleResponse.from_rule(rule)


@router.get(
    "/bank/rules/{rule_id}",
    response_model=BankRuleResponse,
    summary="Get one bank rule",
)
def get_bank_rule(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:read")),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> BankRuleResponse:
    """Return one bank rule (404 when it is not in this tenant)."""
    with translate_errors():
        rule = service.get_rule(client.tenant_id, rule_id)
    return BankRuleResponse.from_rule(rule)


@router.put(
    "/bank/rules/{rule_id}",
    response_model=BankRuleResponse,
    summary="Update a bank rule",
)
def update_bank_rule(
    rule_id: str,
    body: BankRuleUpdateRequest,
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> BankRuleResponse:
    """Update a bank rule (only the given fields change).

    Only the fields the client actually set are passed on — an
    explicitly-null pattern field clears that condition. Changing
    the pattern or the target resets the confirmations and drops an
    AUTO rule back to SUGGEST.
    """
    fields: dict[str, Any] = {key: value for key, value in body.model_dump().items() if key in body.model_fields_set}
    with translate_errors():
        rule = service.update_rule(client.tenant_id, rule_id, fields)
    session.commit()
    return BankRuleResponse.from_rule(rule)


@router.delete(
    "/bank/rules/{rule_id}",
    status_code=204,
    summary="Delete a bank rule",
)
def delete_bank_rule(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> None:
    """Delete a bank rule (its suggestions survive, rule_id NULL)."""
    with translate_errors():
        service.delete_rule(client.tenant_id, rule_id)
    session.commit()


@router.post(
    "/bank/rules/{rule_id}/enable",
    response_model=BankRuleResponse,
    summary="Enable a bank rule",
)
def enable_bank_rule(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> BankRuleResponse:
    """Enable a bank rule (404 when it is not in this tenant)."""
    with translate_errors():
        rule = service.set_enabled(client.tenant_id, rule_id, enabled=True)
    session.commit()
    return BankRuleResponse.from_rule(rule)


@router.post(
    "/bank/rules/{rule_id}/disable",
    response_model=BankRuleResponse,
    summary="Disable a bank rule",
)
def disable_bank_rule(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> BankRuleResponse:
    """Disable a bank rule (it stops firing — live and suggested)."""
    with translate_errors():
        rule = service.set_enabled(client.tenant_id, rule_id, enabled=False)
    session.commit()
    return BankRuleResponse.from_rule(rule)


@router.post(
    "/bank/rules/{rule_id}/promote",
    response_model=BankRuleResponse,
    summary="Promote a SUGGEST rule to AUTO (needs 3 confirmed suggestions)",
)
def promote_bank_rule(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> BankRuleResponse:
    """Promote a rule to AUTO — a human decision, at the threshold.

    Below 3 confirmed suggestions (or an already-AUTO rule) this is a
    409 ``BANK_RULE_NOT_PROMOTABLE``.
    """
    with translate_errors():
        rule = service.promote_rule(client.tenant_id, rule_id)
    session.commit()
    return BankRuleResponse.from_rule(rule)


@router.post(
    "/bank/rules/{rule_id}/demote",
    response_model=BankRuleResponse,
    summary="Demote an AUTO rule back to SUGGEST (confirmations reset)",
)
def demote_bank_rule(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: BankRulesService = Depends(get_bank_rules_service),
) -> BankRuleResponse:
    """Demote a rule to SUGGEST — the trust is re-earned from zero.

    A non-AUTO rule is a 409 ``BANK_RULE_NOT_PROMOTABLE``.
    """
    with translate_errors():
        rule = service.demote_rule(client.tenant_id, rule_id)
    session.commit()
    return BankRuleResponse.from_rule(rule)
