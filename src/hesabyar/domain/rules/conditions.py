"""The rule condition DSL (Phase 5-7 ۵خ W2, US-561; phased design §6.4).

A small safe JSONLogic-style language over event facts — comparisons
on number/string/date values combined with ``and`` / ``or`` /
``not``. There is no ``eval`` and no arbitrary code: a condition is
a JSON tree walked by :func:`evaluate_conditions`, and anything
outside the grammar is rejected by :func:`parse_conditions` before
a rule is ever stored.

Grammar
-------
::

    condition := {}                                  # always true
              |  {"and": [condition, ...]}
              |  {"or":  [condition, ...]}
              |  {"not": condition}
              |  {"==": [operand, operand]}  (also !=, >, >=, <, <=)
              |  {"in": [operand, operand]}  (also "not_in")
    operand   := {"var": "dotted.path"} | <JSON scalar>
              |  [<JSON scalar>, ...]        # only as the "in" haystack

Field paths are dotted segments of ``[A-Za-z0-9_-]+`` (max depth 5),
resolved against the facts dict built by
:func:`hesabyar.domain.rules.engine.build_facts`. Payload fields live
under ``payload.<path>``; the envelope/context facts are top-level
(``event_type``, ``amount_irr``, ``channel``, ``party``,
``company``, ``occurred_at``).

Semantics
---------
- A missing field never matches — even ``!=``: a condition on data
  the event does not carry fails closed, so a rule cannot fire on
  an absence it did not ask about.
- Numbers compare numerically (ints and floats; booleans are not
  numbers here). Strings compare lexicographically — except ISO
  date/datetime strings on both sides, which compare as dates.
- Type mismatches compare False (``!=`` included).
- ``in`` is membership in a list, or substring when both sides are
  strings; ``not_in`` is its negation (a missing field is False
  for ``in`` and False for ``not_in`` too — fail-closed).
- ``and`` of zero conditions is True; ``or`` of zero is False.

Limits (abuse guard): at most 64 nodes, depth at most 8.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping
from typing import Any

from hesabyar.domain.rules.errors import RuleDefinitionError

#: Abuse guards for the DSL tree.
MAX_CONDITION_NODES = 64
MAX_CONDITION_DEPTH = 8
MAX_FIELD_DEPTH = 5

#: Allowed comparison operators (JSONLogic names, plus ``not_in``).
COMPARISON_OPERATORS: frozenset[str] = frozenset({"==", "!=", ">", ">=", "<", "<=", "in", "not_in"})

#: Allowed logic combinators.
LOGIC_OPERATORS: frozenset[str] = frozenset({"and", "or", "not"})

_FIELD_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$")

#: Sentinel for a field the facts do not carry (public: renderers
#: and executors compare against it too).
FIELD_MISSING = object()


def _is_number(value: Any) -> bool:
    """True for int/float values (booleans excluded — they are not amounts)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_scalar(value: Any) -> bool:
    """True for a JSON scalar literal (str/int/float/bool/None)."""
    return value is None or isinstance(value, (str, int, float, bool))


def validate_field_path(path: Any) -> str:
    """Validate a ``var`` field path (dotted segments, bounded depth)."""
    if not isinstance(path, str) or not path:
        raise RuleDefinitionError(
            "A condition field path must be a non-empty string.",
            details={"path": path},
        )
    segments = path.split(".")
    if len(segments) > MAX_FIELD_DEPTH:
        raise RuleDefinitionError(
            f"Field path {path!r} is too deep (max {MAX_FIELD_DEPTH} segments).",
            details={"path": path},
        )
    for segment in segments:
        if not _FIELD_SEGMENT.match(segment):
            raise RuleDefinitionError(
                f"Field path {path!r} has an invalid segment {segment!r}: "
                "segments may only contain letters, digits, '_' and '-'.",
                details={"path": path},
            )
    return path


def _validate_operand(operand: Any, *, depth: int, counter: list[int]) -> None:
    """Validate one comparison operand (a ``var`` ref or a scalar/list)."""
    counter[0] += 1
    if counter[0] > MAX_CONDITION_NODES:
        raise RuleDefinitionError(
            f"Condition tree exceeds {MAX_CONDITION_NODES} nodes.",
            details={"max_nodes": MAX_CONDITION_NODES},
        )
    if isinstance(operand, dict):
        keys = list(operand.keys())
        if keys != ["var"]:
            raise RuleDefinitionError(
                f'A condition operand object must be exactly {{"var": "path"}}; got keys {keys!r}.',
                details={"operand": operand},
            )
        validate_field_path(operand["var"])
        return
    if isinstance(operand, list):
        for item in operand:
            if not _is_scalar(item):
                raise RuleDefinitionError(
                    "A condition list operand may only contain JSON scalars.",
                    details={"operand": operand},
                )
        return
    if not _is_scalar(operand):
        raise RuleDefinitionError(
            'A condition operand must be a {"var": ...} reference, a JSON '
            f"scalar, or a list of scalars; got {type(operand).__name__}.",
            details={"operand": operand},
        )


def _validate_condition(node: Any, *, depth: int, counter: list[int]) -> None:
    """Recursively validate a condition tree (raises RuleDefinitionError)."""
    counter[0] += 1
    if counter[0] > MAX_CONDITION_NODES:
        raise RuleDefinitionError(
            f"Condition tree exceeds {MAX_CONDITION_NODES} nodes.",
            details={"max_nodes": MAX_CONDITION_NODES},
        )
    if depth > MAX_CONDITION_DEPTH:
        raise RuleDefinitionError(
            f"Condition tree exceeds max depth {MAX_CONDITION_DEPTH}.",
            details={"max_depth": MAX_CONDITION_DEPTH},
        )
    if not isinstance(node, dict):
        raise RuleDefinitionError(
            f"A condition must be a JSON object; got {type(node).__name__}.",
            details={"condition": node},
        )
    if not node:
        return  # {} means "no conditions" — always true.
    if len(node) != 1:
        raise RuleDefinitionError(
            f"A condition object must have exactly one operator key; got {sorted(node.keys())!r}.",
            details={"condition": node},
        )
    operator, value = next(iter(node.items()))
    if operator in LOGIC_OPERATORS:
        if operator == "not":
            _validate_condition(value, depth=depth + 1, counter=counter)
            return
        if not isinstance(value, list):
            raise RuleDefinitionError(
                f'Operator "{operator}" needs a list of conditions.',
                details={"condition": node},
            )
        for child in value:
            _validate_condition(child, depth=depth + 1, counter=counter)
        return
    if operator in COMPARISON_OPERATORS:
        if not isinstance(value, list) or len(value) != 2:
            raise RuleDefinitionError(
                f'Operator "{operator}" needs exactly two operands.',
                details={"condition": node},
            )
        for operand in value:
            _validate_operand(operand, depth=depth + 1, counter=counter)
        return
    raise RuleDefinitionError(
        f"Unknown condition operator {operator!r}; allowed operators are "
        f"{sorted(LOGIC_OPERATORS | COMPARISON_OPERATORS)!r}.",
        details={"operator": operator},
    )


def parse_conditions(raw: Any) -> dict[str, Any]:
    """Validate a raw conditions tree; return it unchanged when valid.

    ``None`` is treated as "no conditions" (``{}``). Anything outside
    the grammar raises :class:`RuleDefinitionError` — a rule with an
    invalid condition tree is never stored.
    """
    if raw is None:
        return {}
    _validate_condition(raw, depth=0, counter=[0])
    if not isinstance(raw, dict):
        raise RuleDefinitionError(
            "Conditions must be a JSON object.",
            details={"conditions": raw},
        )
    return dict(raw)


def resolve_field(facts: Mapping[str, Any], path: str) -> Any:
    """Resolve a dotted path against the facts (``FIELD_MISSING`` when absent)."""
    current: Any = facts
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return FIELD_MISSING
        current = current[segment]
    return current


def _parse_iso(value: str) -> datetime.date | datetime.datetime | None:
    """Parse an ISO date/datetime string, or None when it is not one."""
    if not _ISO_DATE.match(value):
        return None
    try:
        if "T" in value or " " in value:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _comparable(value: Any) -> tuple[str, Any] | None:
    """Coerce a value to a comparable (kind, value) pair, or None.

    Kinds: ``number``, ``date``, ``string``, ``bool``. Two values
    compare only when their kinds match.
    """
    if isinstance(value, bool):
        return ("bool", value)
    if _is_number(value):
        return ("number", value)
    if isinstance(value, str):
        parsed = _parse_iso(value)
        if parsed is not None:
            return ("date", parsed)
        return ("string", value)
    return None


def _compare(operator: str, left: Any, right: Any) -> bool:
    """Compare two resolved operands (fail-closed on mismatch)."""
    if left is FIELD_MISSING or right is FIELD_MISSING:
        return False
    if operator in ("in", "not_in"):
        if isinstance(right, list):
            found = any(_compare("==", left, item) for item in right)
            return found if operator == "in" else not found
        if isinstance(left, str) and isinstance(right, str):
            found = left in right
            return found if operator == "in" else not found
        return False
    left_cmp = _comparable(left)
    right_cmp = _comparable(right)
    if left_cmp is None or right_cmp is None or left_cmp[0] != right_cmp[0]:
        return False
    kind = left_cmp[0]
    left_value, right_value = left_cmp[1], right_cmp[1]
    if kind == "date":
        # A bare date and a datetime never compare equal; normalize a
        # date to midnight UTC only when the other side is a date too.
        # Mixed date/datetime pairs compare False (fail-closed).
        if isinstance(left_value, datetime.datetime) != isinstance(right_value, datetime.datetime):
            return False
        if isinstance(left_value, datetime.datetime) and isinstance(right_value, datetime.datetime):
            left_naive = left_value.tzinfo is None
            right_naive = right_value.tzinfo is None
            if left_naive != right_naive:
                return False
    if operator == "==":
        return bool(left_value == right_value)
    if operator == "!=":
        return bool(left_value != right_value)
    if operator == ">":
        return bool(left_value > right_value)
    if operator == ">=":
        return bool(left_value >= right_value)
    if operator == "<":
        return bool(left_value < right_value)
    if operator == "<=":
        return bool(left_value <= right_value)
    return False  # unreachable for validated trees


def _resolve_operand(operand: Any, facts: Mapping[str, Any]) -> Any:
    """Resolve an operand: a ``var`` lookup, a list, or a literal."""
    if isinstance(operand, dict):
        return resolve_field(facts, operand["var"])
    if isinstance(operand, list):
        return [_resolve_operand(item, facts) if isinstance(item, dict) else item for item in operand]
    return operand


def evaluate_conditions(condition: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    """Evaluate a validated condition tree against the facts.

    The tree must already be validated (see :func:`parse_conditions`);
    evaluation never raises on data — an unresolvable or mistyped
    comparison is False, and an unknown operator is False too, so a
    stored rule can never crash evaluation.
    """
    if not condition:
        return True
    if len(condition) != 1:
        return False
    operator, value = next(iter(condition.items()))
    if operator == "and":
        if not isinstance(value, list):
            return False
        return all(isinstance(child, Mapping) and evaluate_conditions(child, facts) for child in value)
    if operator == "or":
        if not isinstance(value, list):
            return False
        return any(isinstance(child, Mapping) and evaluate_conditions(child, facts) for child in value)
    if operator == "not":
        if not isinstance(value, Mapping):
            return False
        return not evaluate_conditions(value, facts)
    if operator in COMPARISON_OPERATORS:
        if not isinstance(value, list) or len(value) != 2:
            return False
        left = _resolve_operand(value[0], facts)
        right = _resolve_operand(value[1], facts)
        return _compare(operator, left, right)
    return False
