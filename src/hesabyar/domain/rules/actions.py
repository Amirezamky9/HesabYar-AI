"""The closed action set's parameter validation (US-561; §6.4).

:func:`parse_action` is the single gate: an action kind outside
:data:`RuleActionKind` is rejected here (and again by the executor,
defense in depth), and each kind's parameters are validated against
its own schema. A rule only ever stores a validated action.

Per-kind parameters (v1)
------------------------
- ``create_draft`` — ``{"document": "journal", "description": str?,
  "lines": [{"account_code": str, "debit": int | {"var": path},
  "credit": int | {"var": path}, "description": str?}, ...]}``.
  At least two lines; exactly one side per line; amounts are whole
  Rial (``MoneyIRR`` digits=0) — literals or ``{"var": path}``
  resolved from the event facts at execution time. Invoice drafts
  are NOT in v1: they need party/address resolution the engine
  does not expose yet (see ``docs/rules-engine.md``).
- ``post_if_policy_allows`` — ``{"draft_kind": "journal"?,
  "draft_id": int?, "draft_id_field": "payload.path"?}``. The draft
  to post comes from ``draft_id``, from the payload field named by
  ``draft_id_field``, or — when neither is given — from the
  execution context (the draft the event already produced). No
  draft resolves → the action is skipped, never invented. Invoice
  posting is NOT in v1: no invoice finalize use case exists yet.
- ``notify`` — ``{"recipients": [str, ...], "channels":
  ["in_app", "email", "sms"]?, "subject": str, "message": str}``.
  ``{{dotted.path}}`` placeholders in subject/message render from
  the event facts. The ``sms`` channel is accepted in the schema
  so US-564 can light it up through the notification port; the v1
  adapter reports it ``unsupported`` (and ``email`` ``skipped``
  until an SMTP backend exists) — the delivery outcome is always
  recorded, never silently dropped.
- ``assign`` — ``{"assignee": str, "review_item_id": str?}``.
  The target defaults to the execution context's review item.
- ``tag`` — ``{"tag": str, "review_item_id": str?}``.
- ``set_field`` — ``{"target": "review_item", "field": "severity",
  "value": "info" | "warning" | "critical",
  "review_item_id": str?}``. Draft-document fields are NOT in v1
  (the engine exposes no draft update); the ``target`` value is
  validated so the extension point is explicit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hesabyar.domain.rules.conditions import validate_field_path
from hesabyar.domain.rules.errors import RuleDefinitionError
from hesabyar.domain.rules.models import RuleAction, RuleActionKind

#: Notification channels the schema accepts. The v1 adapter delivers
#: ``in_app`` (recorded in the execution log) and reports ``email``
#: skipped (no SMTP backend) and ``sms`` unsupported (US-564).
NOTIFY_CHANNELS: frozenset[str] = frozenset({"in_app", "email", "sms"})

#: ``set_field`` targets and their whitelisted fields (v1).
SET_FIELD_TARGETS: dict[str, dict[str, tuple[str, ...]]] = {
    "review_item": {"severity": ("info", "warning", "critical")},
}

#: Max length of a tag (keeps the review item's tag list readable).
MAX_TAG_LENGTH = 64


def _require_str(params: Mapping[str, Any], key: str, *, kind: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuleDefinitionError(
            f'Action "{kind}" needs a non-empty string parameter "{key}".',
            details={"kind": kind, "param": key},
        )
    return value.strip()


def _optional_str(params: Mapping[str, Any], key: str, *, kind: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuleDefinitionError(
            f'Action "{kind}" parameter "{key}" must be a non-empty string when given.',
            details={"kind": kind, "param": key},
        )
    return value.strip()


def _validate_amount(value: Any, *, kind: str, where: str) -> int | dict[str, str]:
    """Validate a draft-line amount: a positive int (Rial) or a ``var`` ref."""
    if isinstance(value, bool):
        raise RuleDefinitionError(
            f'Action "{kind}": {where} must be a positive integer (Rial) or {{"var": "path"}}.',
            details={"kind": kind, "where": where},
        )
    if isinstance(value, int):
        if value <= 0:
            raise RuleDefinitionError(
                f'Action "{kind}": {where} must be a positive integer (Rial).',
                details={"kind": kind, "where": where},
            )
        return value
    if isinstance(value, dict) and list(value.keys()) == ["var"]:
        validate_field_path(value["var"])
        return {"var": value["var"]}
    raise RuleDefinitionError(
        f'Action "{kind}": {where} must be a positive integer (Rial) or {{"var": "path"}}.',
        details={"kind": kind, "where": where},
    )


def _validate_create_draft(params: Mapping[str, Any]) -> dict[str, Any]:
    kind = RuleActionKind.CREATE_DRAFT.value
    document = params.get("document", "journal")
    if document != "journal":
        raise RuleDefinitionError(
            'Action "create_draft" only supports document "journal" in v1; '
            f"got {document!r} (invoice drafts need party/address "
            "resolution — see docs/rules-engine.md).",
            details={"kind": kind, "document": document},
        )
    lines = params.get("lines")
    if not isinstance(lines, list) or len(lines) < 2:
        raise RuleDefinitionError(
            'Action "create_draft" needs at least two journal lines.',
            details={"kind": kind},
        )
    validated_lines: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        where = f"line {index}"
        if not isinstance(line, Mapping):
            raise RuleDefinitionError(
                f'Action "create_draft": {where} must be an object.',
                details={"kind": kind, "line": index},
            )
        account_code = line.get("account_code")
        if not isinstance(account_code, str) or not account_code.strip():
            raise RuleDefinitionError(
                f'Action "create_draft": {where} needs a non-empty "account_code".',
                details={"kind": kind, "line": index},
            )
        has_debit = "debit" in line and line["debit"] is not None
        has_credit = "credit" in line and line["credit"] is not None
        if has_debit == has_credit:
            raise RuleDefinitionError(
                f'Action "create_draft": {where} needs exactly one of "debit" or "credit".',
                details={"kind": kind, "line": index},
            )
        validated: dict[str, Any] = {"account_code": account_code.strip()}
        if has_debit:
            validated["debit"] = _validate_amount(line["debit"], kind=kind, where=f"{where} debit")
        else:
            validated["credit"] = _validate_amount(line["credit"], kind=kind, where=f"{where} credit")
        description = line.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise RuleDefinitionError(
                    f'Action "create_draft": {where} "description" must be a string.',
                    details={"kind": kind, "line": index},
                )
            validated["description"] = description
        validated_lines.append(validated)
    validated_params: dict[str, Any] = {"document": "journal", "lines": validated_lines}
    description = params.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise RuleDefinitionError(
                'Action "create_draft": "description" must be a string.',
                details={"kind": kind},
            )
        validated_params["description"] = description
    return validated_params


def _validate_post(params: Mapping[str, Any]) -> dict[str, Any]:
    kind = RuleActionKind.POST_IF_POLICY_ALLOWS.value
    draft_kind = params.get("draft_kind", "journal")
    if draft_kind != "journal":
        raise RuleDefinitionError(
            'Action "post_if_policy_allows" only supports draft_kind '
            f'"journal" in v1; got {draft_kind!r} (no invoice finalize '
            "use case exists yet — see docs/rules-engine.md).",
            details={"kind": kind, "draft_kind": draft_kind},
        )
    validated: dict[str, Any] = {"draft_kind": "journal"}
    draft_id = params.get("draft_id")
    if draft_id is not None:
        if isinstance(draft_id, bool) or not isinstance(draft_id, int) or draft_id <= 0:
            raise RuleDefinitionError(
                'Action "post_if_policy_allows": "draft_id" must be a positive integer.',
                details={"kind": kind},
            )
        validated["draft_id"] = draft_id
    draft_id_field = params.get("draft_id_field")
    if draft_id_field is not None:
        validate_field_path(draft_id_field)
        validated["draft_id_field"] = draft_id_field
    if draft_id is not None and draft_id_field is not None:
        raise RuleDefinitionError(
            'Action "post_if_policy_allows": give "draft_id" or "draft_id_field", not both.',
            details={"kind": kind},
        )
    return validated


def _validate_notify(params: Mapping[str, Any]) -> dict[str, Any]:
    kind = RuleActionKind.NOTIFY.value
    recipients = params.get("recipients")
    if (
        not isinstance(recipients, list)
        or not recipients
        or any(not isinstance(r, str) or not r.strip() for r in recipients)
    ):
        raise RuleDefinitionError(
            'Action "notify" needs a non-empty "recipients" list of strings.',
            details={"kind": kind},
        )
    channels = params.get("channels", ["in_app", "email"])
    if not isinstance(channels, list) or not channels or any(channel not in NOTIFY_CHANNELS for channel in channels):
        raise RuleDefinitionError(
            f'Action "notify": "channels" must be a non-empty list drawn from {sorted(NOTIFY_CHANNELS)!r}.',
            details={"kind": kind, "channels": channels},
        )
    subject = _require_str(params, "subject", kind=kind)
    message = _require_str(params, "message", kind=kind)
    return {
        "recipients": [r.strip() for r in recipients],
        "channels": list(channels),
        "subject": subject,
        "message": message,
    }


def _validate_review_item_action(params: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    """Shared validation for actions targeting a review item."""
    validated: dict[str, Any] = {}
    review_item_id = params.get("review_item_id")
    if review_item_id is not None:
        if not isinstance(review_item_id, str) or not review_item_id.strip():
            raise RuleDefinitionError(
                f'Action "{kind}": "review_item_id" must be a non-empty string.',
                details={"kind": kind},
            )
        validated["review_item_id"] = review_item_id.strip()
    return validated


def _validate_assign(params: Mapping[str, Any]) -> dict[str, Any]:
    kind = RuleActionKind.ASSIGN.value
    validated = _validate_review_item_action(params, kind=kind)
    validated["assignee"] = _require_str(params, "assignee", kind=kind)
    return validated


def _validate_tag(params: Mapping[str, Any]) -> dict[str, Any]:
    kind = RuleActionKind.TAG.value
    validated = _validate_review_item_action(params, kind=kind)
    tag = _require_str(params, "tag", kind=kind)
    if len(tag) > MAX_TAG_LENGTH:
        raise RuleDefinitionError(
            f'Action "tag": "tag" must be at most {MAX_TAG_LENGTH} characters.',
            details={"kind": kind, "tag": tag},
        )
    validated["tag"] = tag
    return validated


def _validate_set_field(params: Mapping[str, Any]) -> dict[str, Any]:
    kind = RuleActionKind.SET_FIELD.value
    validated = _validate_review_item_action(params, kind=kind)
    target = params.get("target", "review_item")
    if target not in SET_FIELD_TARGETS:
        raise RuleDefinitionError(
            'Action "set_field": "target" must be one of '
            f"{sorted(SET_FIELD_TARGETS)!r} in v1; got {target!r} "
            "(draft-document fields need an engine draft update — "
            "see docs/rules-engine.md).",
            details={"kind": kind, "target": target},
        )
    fields = SET_FIELD_TARGETS[target]
    field_name = params.get("field")
    if field_name not in fields:
        raise RuleDefinitionError(
            f'Action "set_field": "field" for target "{target}" must be one of {sorted(fields)!r}; got {field_name!r}.',
            details={"kind": kind, "field": field_name},
        )
    value = params.get("value")
    if value not in fields[field_name]:
        raise RuleDefinitionError(
            f'Action "set_field": "value" for field "{field_name}" must be '
            f"one of {fields[field_name]!r}; got {value!r}.",
            details={"kind": kind, "field": field_name, "value": value},
        )
    validated["target"] = target
    validated["field"] = field_name
    validated["value"] = value
    return validated


_VALIDATORS = {
    RuleActionKind.CREATE_DRAFT: _validate_create_draft,
    RuleActionKind.POST_IF_POLICY_ALLOWS: _validate_post,
    RuleActionKind.NOTIFY: _validate_notify,
    RuleActionKind.ASSIGN: _validate_assign,
    RuleActionKind.TAG: _validate_tag,
    RuleActionKind.SET_FIELD: _validate_set_field,
}


def parse_action(raw: Any) -> RuleAction:
    """Validate a raw action dict into a :class:`RuleAction`.

    The closed set is enforced here: an unknown ``kind`` raises
    :class:`RuleDefinitionError` — it is never stored, and the
    executor re-validates before running anything, so a row written
    by hand (or by a future build with a new kind) cannot execute
    an action this build does not know.
    """
    if not isinstance(raw, Mapping):
        raise RuleDefinitionError(
            f"An action must be a JSON object; got {type(raw).__name__}.",
            details={"action": raw},
        )
    kind_raw = raw.get("kind")
    if not isinstance(kind_raw, str):
        raise RuleDefinitionError(
            f'An action needs a string "kind" from the closed set {sorted(k.value for k in RuleActionKind)!r}.',
            details={"action": raw},
        )
    try:
        kind = RuleActionKind(kind_raw)
    except ValueError as exc:
        raise RuleDefinitionError(
            f"Unknown action kind {kind_raw!r}: actions come from the "
            f"closed set {sorted(k.value for k in RuleActionKind)!r} only.",
            details={"kind": kind_raw},
        ) from exc
    params = raw.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise RuleDefinitionError(
            f'Action "{kind.value}": "params" must be a JSON object.',
            details={"kind": kind.value},
        )
    return RuleAction(kind=kind, params=_VALIDATORS[kind](params))
