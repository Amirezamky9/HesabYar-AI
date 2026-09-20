"""Functional Result type for railway-oriented error handling in domain logic."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")
U = TypeVar("U")
E = TypeVar("E")
F = TypeVar("F")


class Result[T, E](ABC):
    """Abstract base class for functional Result[T, E]."""

    @property
    @abstractmethod
    def is_success(self) -> bool:
        """Return True if this Result represents a successful outcome."""
        ...

    @property
    @abstractmethod
    def is_failure(self) -> bool:
        """Return True if this Result represents a failure outcome."""
        ...

    @property
    @abstractmethod
    def value(self) -> T:
        """Return the inner value if successful, or raise ValueError if failure."""
        ...

    @property
    @abstractmethod
    def error(self) -> E:
        """Return the inner error if failure, or raise ValueError if success."""
        ...

    @abstractmethod
    def map(self, fn: Callable[[T], U]) -> Result[U, E]:
        """Apply fn to the value if Success, returning a new Success. Pass through Failure."""
        ...

    @abstractmethod
    def bind(self, fn: Callable[[T], Result[U, E]]) -> Result[U, E]:
        """Monadic bind / flat_map: apply fn to the value if Success. Pass through Failure."""
        ...

    @abstractmethod
    def map_err(self, fn: Callable[[E], F]) -> Result[T, F]:
        """Apply fn to the error if Failure, returning a new Failure. Pass through Success."""
        ...

    @abstractmethod
    def unwrap_or(self, default: T) -> T:
        """Return inner value if Success, otherwise default."""
        ...

    @abstractmethod
    def unwrap(self) -> T:
        """Return inner value if Success.

        If Failure:
          - If the contained error is an Exception instance, re-raise that exact exception.
          - If the contained error is a non-exception value, raise ValueError(str(self.error)).
        """
        ...

    @abstractmethod
    def __bool__(self) -> bool:
        """Truthiness matches is_success."""
        ...

    @classmethod
    def ok(cls, value: T) -> Success[T]:
        """Construct a Success result."""
        return Success(value)

    @classmethod
    def fail(cls, error: E) -> Failure[E]:
        """Construct a Failure result."""
        return Failure(error)


class Success[T](Result[T, Any]):
    """Represents a successful computation holding a value of type T."""

    __slots__ = ("_value",)

    def __init__(self, value: T) -> None:
        self._value = value

    @property
    def is_success(self) -> bool:
        return True

    @property
    def is_failure(self) -> bool:
        return False

    @property
    def value(self) -> T:
        return self._value

    @property
    def error(self) -> Any:
        raise ValueError("Cannot access .error on a Success result.")

    def map(self, fn: Callable[[T], U]) -> Result[U, Any]:
        return Success(fn(self._value))

    def bind(self, fn: Callable[[T], Result[U, Any]]) -> Result[U, Any]:
        return fn(self._value)

    def map_err(self, fn: Callable[[Any], F]) -> Result[T, F]:
        return self  # type: ignore[return-value]

    def unwrap_or(self, default: T) -> T:
        return self._value

    def unwrap(self) -> T:
        """Return inner value."""
        return self._value

    def __bool__(self) -> bool:
        return True

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Success):
            return self._value == other._value
        return False

    def __repr__(self) -> str:
        return f"Success({self._value!r})"


class Failure[E](Result[Any, E]):
    """Represents a failed computation holding an error of type E."""

    __slots__ = ("_error",)

    def __init__(self, error: E) -> None:
        self._error = error

    @property
    def is_success(self) -> bool:
        return False

    @property
    def is_failure(self) -> bool:
        return True

    @property
    def value(self) -> Any:
        cause = self._error if isinstance(self._error, Exception) else None
        raise ValueError(f"Cannot access .value on a Failure result: {self._error}") from cause

    @property
    def error(self) -> E:
        return self._error

    def map(self, fn: Callable[[Any], U]) -> Result[U, E]:
        return self  # type: ignore[return-value]

    def bind(self, fn: Callable[[Any], Result[U, E]]) -> Result[U, E]:
        return self  # type: ignore[return-value]

    def map_err(self, fn: Callable[[E], F]) -> Result[Any, F]:
        return Failure(fn(self._error))

    def unwrap_or(self, default: T) -> T:
        return default

    def unwrap(self) -> Any:
        """Unwrap error: re-raise exact exception if Exception, else raise ValueError(str(self.error))."""
        if isinstance(self._error, Exception):
            raise self._error
        raise ValueError(str(self._error))

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Failure):
            return self._error == other._error
        return False

    def __repr__(self) -> str:
        return f"Failure({self._error!r})"
