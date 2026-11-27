"""Unit tests for the rule condition DSL (US-561, Phase 5-7 ۵خ W2).

The DSL is a small safe JSONLogic-style language: comparisons on
number/string/date values combined with and/or/not. There is no
``eval`` and no arbitrary code — anything outside the grammar is
rejected at parse time, and evaluation never raises on data (a
missing field or a type mismatch is False, fail-closed).
"""

from __future__ import annotations

import pytest

from hesabyar.domain.rules.conditions import (
    MAX_CONDITION_DEPTH,
    MAX_CONDITION_NODES,
    evaluate_conditions,
    parse_conditions,
    resolve_field,
)
from hesabyar.domain.rules.errors import RuleDefinitionError

FACTS = {
    "event_type": "sales.order.completed",
    "amount_irr": 5_000_000,
    "channel": "zarinpal",
    "party": "party-7",
    "company": "1",
    "occurred_at": "2026-11-20",
    "payload": {
        "total_amount": 5_000_000,
        "currency": "IRR",
        "customer_name": "Test Customer",
        "is_gift": False,
        "occurred_at": "2026-11-20T10:00:00Z",
    },
}


def test_empty_conditions_always_match() -> None:
    assert evaluate_conditions(parse_conditions({}), FACTS) is True
    assert evaluate_conditions(parse_conditions(None), FACTS) is True


def test_numeric_comparisons() -> None:
    assert evaluate_conditions(parse_conditions({">=": [{"var": "amount_irr"}, 1_000_000]}), FACTS) is True
    assert evaluate_conditions(parse_conditions({"<": [{"var": "amount_irr"}, 1_000_000]}), FACTS) is False
    assert evaluate_conditions(parse_conditions({"==": [{"var": "payload.total_amount"}, 5_000_000]}), FACTS) is True
    assert evaluate_conditions(parse_conditions({"!=": [{"var": "amount_irr"}, 5_000_000]}), FACTS) is False
    # Floats compare numerically against ints.
    assert evaluate_conditions(parse_conditions({">": [{"var": "amount_irr"}, 4_999_999.5]}), FACTS) is True


def test_string_comparisons() -> None:
    assert evaluate_conditions(parse_conditions({"==": [{"var": "channel"}, "zarinpal"]}), FACTS) is True
    assert evaluate_conditions(parse_conditions({"!=": [{"var": "channel"}, "melli"]}), FACTS) is True
    assert evaluate_conditions(parse_conditions({">": [{"var": "channel"}, "aaa"]}), FACTS) is True


def test_date_comparisons() -> None:
    assert evaluate_conditions(parse_conditions({">=": [{"var": "occurred_at"}, "2026-11-01"]}), FACTS) is True
    assert evaluate_conditions(parse_conditions({"<": [{"var": "occurred_at"}, "2026-11-01"]}), FACTS) is False
    assert evaluate_conditions(parse_conditions({"==": [{"var": "occurred_at"}, "2026-11-20"]}), FACTS) is True
    # A bare date never equals a datetime (fail-closed, not coerced).
    assert (
        evaluate_conditions(
            parse_conditions({"==": [{"var": "occurred_at"}, "2026-11-20T00:00:00Z"]}),
            FACTS,
        )
        is False
    )


def test_in_operator() -> None:
    assert (
        evaluate_conditions(
            parse_conditions({"in": [{"var": "channel"}, ["zarinpal", "melli"]]}),
            FACTS,
        )
        is True
    )
    assert (
        evaluate_conditions(
            parse_conditions({"in": [{"var": "channel"}, ["melli", "sadad"]]}),
            FACTS,
        )
        is False
    )
    assert (
        evaluate_conditions(
            parse_conditions({"not_in": [{"var": "channel"}, ["melli"]]}),
            FACTS,
        )
        is True
    )
    # Substring form: needle in a string haystack.
    assert (
        evaluate_conditions(
            parse_conditions({"in": ["Test", {"var": "payload.customer_name"}]}),
            FACTS,
        )
        is True
    )


def test_logic_combinators() -> None:
    big_and_zarinpal = {
        "and": [
            {">=": [{"var": "amount_irr"}, 1_000_000]},
            {"==": [{"var": "channel"}, "zarinpal"]},
        ]
    }
    assert evaluate_conditions(parse_conditions(big_and_zarinpal), FACTS) is True
    assert (
        evaluate_conditions(
            parse_conditions(
                {
                    "or": [
                        {"==": [{"var": "channel"}, "melli"]},
                        {"==": [{"var": "channel"}, "zarinpal"]},
                    ]
                }
            ),
            FACTS,
        )
        is True
    )
    assert (
        evaluate_conditions(
            parse_conditions({"not": {"==": [{"var": "channel"}, "melli"]}}),
            FACTS,
        )
        is True
    )
    assert evaluate_conditions(parse_conditions({"and": []}), FACTS) is True
    assert evaluate_conditions(parse_conditions({"or": []}), FACTS) is False


def test_missing_field_never_matches() -> None:
    # Even != fails closed on a missing field: a rule cannot fire on
    # an absence it did not ask about.
    assert evaluate_conditions(parse_conditions({"==": [{"var": "payload.nope"}, 1]}), FACTS) is False
    assert evaluate_conditions(parse_conditions({"!=": [{"var": "payload.nope"}, 1]}), FACTS) is False
    assert evaluate_conditions(parse_conditions({"not_in": [{"var": "payload.nope"}, [1]]}), FACTS) is False
    assert evaluate_conditions(parse_conditions({"in": [{"var": "payload.nope"}, [1]]}), FACTS) is False


def test_type_mismatch_is_false() -> None:
    assert evaluate_conditions(parse_conditions({"==": [{"var": "amount_irr"}, "5000000"]}), FACTS) is False
    assert evaluate_conditions(parse_conditions({">": [{"var": "amount_irr"}, "100"]}), FACTS) is False
    # Booleans are not numbers.
    assert evaluate_conditions(parse_conditions({"==": [{"var": "payload.is_gift"}, 0]}), FACTS) is False
    assert evaluate_conditions(parse_conditions({"==": [{"var": "payload.is_gift"}, False]}), FACTS) is True


def test_unknown_operator_rejected() -> None:
    with pytest.raises(RuleDefinitionError, match="Unknown condition operator"):
        parse_conditions({"xor": [{"var": "channel"}, "zarinpal"]})
    with pytest.raises(RuleDefinitionError, match="Unknown condition operator"):
        parse_conditions({"contains": [{"var": "channel"}, "zar"]})


def test_malformed_trees_rejected() -> None:
    with pytest.raises(RuleDefinitionError):
        parse_conditions("amount > 1000")  # not an object — no expression strings
    with pytest.raises(RuleDefinitionError):
        parse_conditions({"==": [{"var": "amount_irr"}]})  # one operand
    with pytest.raises(RuleDefinitionError):
        parse_conditions({"and": {"==": [{"var": "x"}, 1]}})  # and needs a list
    with pytest.raises(RuleDefinitionError):
        parse_conditions({"==": [{"var": "amount_irr"}, 1], "!=": [{"var": "channel"}, "x"]})  # two operator keys
    with pytest.raises(RuleDefinitionError):
        parse_conditions({"==": [{"eval": "1+1"}, 2]})  # no eval operand
    with pytest.raises(RuleDefinitionError):
        parse_conditions({"==": [{"var": "amount_irr", "extra": 1}, 2]})
    with pytest.raises(RuleDefinitionError):
        parse_conditions({"==": [{"var": ""}, 2]})  # empty path
    with pytest.raises(RuleDefinitionError):
        parse_conditions({"==": [{"var": "a b"}, 2]})  # space in a segment
    with pytest.raises(RuleDefinitionError):
        parse_conditions({"==": [{"var": "a.b.c.d.e.f"}, 2]})  # too deep
    with pytest.raises(RuleDefinitionError):
        parse_conditions({"==": [{"var": "amount_irr"}, {"nested": "dict"}]})
    with pytest.raises(RuleDefinitionError):
        parse_conditions({"in": [{"var": "channel"}, [{"dict": 1}]]})


def test_node_and_depth_limits() -> None:
    too_many = {"and": [{"==": [{"var": "amount_irr"}, i]} for i in range(MAX_CONDITION_NODES)]}
    with pytest.raises(RuleDefinitionError, match="exceeds .* nodes"):
        parse_conditions(too_many)
    deep: dict = {"==": [{"var": "amount_irr"}, 1]}
    for _ in range(MAX_CONDITION_DEPTH + 1):
        deep = {"not": deep}
    with pytest.raises(RuleDefinitionError, match="max depth"):
        parse_conditions(deep)


def test_evaluation_never_raises_on_unvalidated_data() -> None:
    # Defense in depth: a stored tree that somehow bypassed validation
    # evaluates False instead of crashing the engine.
    assert evaluate_conditions({"bogus": []}, FACTS) is False
    assert evaluate_conditions({"==": [{"var": "x"}]}, FACTS) is False
    assert evaluate_conditions({"and": "not-a-list"}, FACTS) is False


def test_resolve_field() -> None:
    assert resolve_field(FACTS, "payload.total_amount") == 5_000_000
    assert resolve_field(FACTS, "channel") == "zarinpal"
    assert resolve_field(FACTS, "payload.missing") is not None  # sentinel, not None
    from hesabyar.domain.rules.conditions import FIELD_MISSING

    assert resolve_field(FACTS, "payload.missing") is FIELD_MISSING
    assert resolve_field(FACTS, "amount_irr.deeper") is FIELD_MISSING
