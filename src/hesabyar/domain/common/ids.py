"""Strongly-typed entity identifiers enforcing tenant boundaries and domain semantics."""

from __future__ import annotations

from typing import Any, Self
from uuid import UUID, uuid4

from hesabyar.domain.common.errors import InvalidIdentifierError


class EntityId:
    """Base class for all strongly-typed domain entity identifiers."""

    __slots__ = ("_value",)

    def __init__(self, value: str | UUID | EntityId) -> None:
        if isinstance(value, EntityId):
            if type(value) is not type(self):
                raise InvalidIdentifierError(
                    f"Cannot construct {self.__class__.__name__} from distinct identifier type {type(value).__name__}."
                )
            self._value = value._value
        elif isinstance(value, UUID):
            self._value = str(value)
        elif isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                raise InvalidIdentifierError(f"{self.__class__.__name__} value cannot be empty or blank.")
            self._value = cleaned
        else:
            raise InvalidIdentifierError(
                f"{self.__class__.__name__} expects str, UUID, or {self.__class__.__name__}, "
                f"got {type(value).__name__}."
            )

    @property
    def value(self) -> str:
        """Return the raw string representation of the identifier."""
        return self._value

    @classmethod
    def generate(cls) -> Self:
        """Generate a new unique identifier using UUID4."""
        return cls(str(uuid4()))

    @classmethod
    def from_string(cls, value: str) -> Self:
        """Create an identifier from a string value."""
        return cls(value)

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, self.__class__):
            return self._value == other._value
        return False

    def __hash__(self) -> int:
        return hash((self.__class__, self._value))

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        def validate(v: Any) -> Any:
            if isinstance(v, cls):
                return v
            if isinstance(v, (str, UUID)):
                return cls(v)
            raise ValueError(f"Cannot parse {cls.__name__} from {type(v).__name__}")

        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_after_validator_function(cls, core_schema.str_schema()),
            python_schema=core_schema.no_info_plain_validator_function(validate),
            serialization=core_schema.plain_serializer_function_ser_schema(lambda v: v.value),
        )


class TenantId(EntityId):
    """Identifies an isolated tenant (organization/business) in the multi-tenant system."""


class ActorId(EntityId):
    """Identifies an actor (human user, service account, or system component) performing actions."""


class VoucherId(EntityId):
    """Identifies an accounting voucher (Sanad)."""


class AccountId(EntityId):
    """Identifies a general ledger account (Kol or Moein)."""


class TafsiliId(EntityId):
    """Identifies a floating subsidiary ledger entity (Tafsili)."""


class FiscalYearId(EntityId):
    """Identifies an accounting fiscal year."""


class PostingPeriodId(EntityId):
    """Identifies an accounting posting period within a fiscal year."""


class InvoiceId(EntityId):
    """Identifies an electronic invoice (Moadian or commercial invoice)."""
