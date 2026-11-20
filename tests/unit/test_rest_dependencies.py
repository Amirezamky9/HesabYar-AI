"""Unit tests for the REST dependencies (US-516).

Covers the ``get_db_session`` teardown: against a remote database the
connection can die between the last query and the close, and the
close's rollback then raises — that must never fail the request.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from hesabyar.interfaces.rest import dependencies


def _settings() -> Any:
    return SimpleNamespace(database=SimpleNamespace(sqlalchemy_url="sqlite://"))


class _FakeSession:
    def __init__(self, close_error: Exception | None = None) -> None:
        self.closed = False
        self._close_error = close_error

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def _drive(session: _FakeSession, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(dependencies, "sessionmaker", lambda *args, **kwargs: lambda: session)
    generator = dependencies.get_db_session(_settings())
    yielded = next(generator)
    assert yielded is session
    # Exhausting the generator runs the teardown.
    with pytest.raises(StopIteration):
        next(generator)


def test_db_session_teardown_closes_and_disposes(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    engine = _FakeEngine()
    _drive(session, engine, monkeypatch)
    assert session.closed is True
    assert engine.disposed is True


def test_db_session_teardown_swallows_a_dead_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The connection died mid-request (Supabase pooler drops it):
    # close() raises OperationalError from the rollback. The request
    # must not 500 — the teardown failure is swallowed.
    session = _FakeSession(close_error=OperationalError("rollback", None, Exception("SSL EOF")))
    engine = _FakeEngine()
    _drive(session, engine, monkeypatch)
    assert session.closed is True
    assert engine.disposed is True
