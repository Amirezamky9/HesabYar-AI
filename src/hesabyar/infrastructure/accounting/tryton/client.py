"""Synchronous Tryton 8.2 REST client (httpx).

Talks to Tryton ONLY through the documented REST API
(``/<db>/rest/model/<name>`` etc., see ``trytond/protocols/rest.py``):
search, read, create, update, delete, model actions, and reports. No SQL,
no JSON-RPC, no direct database access anywhere in this module.

Authentication is a user-application key (``res.user.application`` with
``application='rest'``), sent as ``Authorization: Bearer <key>``. A session
token does NOT work on the REST endpoints.

Transaction context (notably the current company) travels in the
``X-Tryton-Context`` header as base64url JSON.

All payloads are translated through the Tryton JSON wire format
(:mod:`contracts`): Decimal/date/datetime values become ``{"__class__": ...}``
dicts on the wire and are decoded back on receipt.
"""

from __future__ import annotations

from typing import Any

import httpx

from hesabyar.infrastructure.accounting.tryton import contracts
from hesabyar.infrastructure.accounting.tryton.contracts import Domain, Record
from hesabyar.infrastructure.accounting.tryton.errors import (
    TrytonAdapterError,
    translate_http_error,
    translate_transport_error,
)


class TrytonRestClient:
    """Minimal REST transport for one Tryton database."""

    def __init__(
        self,
        base_url: str,
        database: str,
        api_key: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._database = database
        self._api_key = api_key
        # trust_env=False: the Tryton server is a configured internal
        # endpoint. Environment proxies must never intercept engine traffic
        # (the Bearer key must not leak to a proxy), and malformed proxy env
        # vars must not break engine calls.
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TrytonRestClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low-level request -------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        operation: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        headers: dict[str, str] = {}
        if context:
            headers["X-Tryton-Context"] = contracts.encode_context(context)
        try:
            response = self._client.request(
                method,
                path,
                params=params,
                json=contracts.encode_wire(payload) if payload is not None else None,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise translate_transport_error(exc, operation) from exc
        if response.status_code == 204:
            return None
        if response.is_success:
            if not response.content:
                return None
            return contracts.decode_wire(response.json())
        raise translate_http_error(response.status_code, response.text, operation)

    def _model_path(self, model: str, record_id: int | None = None, action: str | None = None) -> str:
        path = f"/{self._database}/rest/model/{model}"
        if record_id is not None:
            path += f"/{record_id}"
        if action is not None:
            path += f"/{action}"
        return path

    @staticmethod
    def _fields_params(fields: list[str] | None) -> dict[str, Any]:
        # httpx repeats a list-valued param as ?f=a&f=b, which is what
        # Tryton expects for fields.
        return {"f": fields} if fields else {}

    # -- model operations --------------------------------------------------

    def search(
        self,
        model: str,
        domain: Domain | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: list[list[str]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[Record]:
        """Search records (``GET /<db>/rest/model/<name>``)."""
        params: dict[str, Any] = self._fields_params(fields)
        if domain:
            params["d"] = contracts.encode_domain(domain)
        if limit is not None:
            params["s"] = limit
        if offset:
            params["p"] = offset
        if order:
            params["o"] = contracts.encode_order(order)
        result = self._request(
            "GET", self._model_path(model), f"search {model}", params=params, context=context
        )
        if not isinstance(result, list):
            raise TrytonAdapterError(
                f"Unexpected search response for {model}: expected a list.",
                code="ENGINE_ERROR",
            )
        return result

    def read(
        self,
        model: str,
        record_id: int,
        fields: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Record | None:
        """Read one record by id; returns None when it does not exist (404)."""
        try:
            result = self._request(
                "GET",
                self._model_path(model, record_id),
                f"read {model} {record_id}",
                params=self._fields_params(fields),
                context=context,
            )
        except TrytonAdapterError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(result, dict):
            raise TrytonAdapterError(
                f"Unexpected read response for {model} {record_id}: expected an object.",
                code="ENGINE_ERROR",
            )
        return result

    def create(
        self,
        model: str,
        values: dict[str, Any],
        fields: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Record:
        """Create one record; returns the created record (server-read-back)."""
        result = self._request(
            "POST",
            self._model_path(model),
            f"create {model}",
            params=self._fields_params(fields),
            payload=values,
            context=context,
        )
        if not isinstance(result, dict):
            raise TrytonAdapterError(
                f"Unexpected create response for {model}: expected an object.",
                code="ENGINE_ERROR",
            )
        return result

    def write(
        self,
        model: str,
        record_id: int,
        values: dict[str, Any],
        fields: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Record | None:
        """Update one record; returns the updated record, or None on 404/204."""
        try:
            result = self._request(
                "PUT",
                self._model_path(model, record_id),
                f"write {model} {record_id}",
                params=self._fields_params(fields),
                payload=values,
                context=context,
            )
        except TrytonAdapterError as exc:
            if exc.status_code == 404:
                return None
            raise
        if result is None:
            return None
        if not isinstance(result, dict):
            raise TrytonAdapterError(
                f"Unexpected write response for {model} {record_id}: expected an object.",
                code="ENGINE_ERROR",
            )
        return result

    def delete(
        self,
        model: str,
        record_id: int,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Delete one record; returns False when it did not exist (404)."""
        try:
            self._request(
                "DELETE",
                self._model_path(model, record_id),
                f"delete {model} {record_id}",
                context=context,
            )
        except TrytonAdapterError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    def action(
        self,
        model: str,
        action: str,
        record_id: int | None = None,
        data: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Call a model action (``POST /<db>/rest/model/<name>/[id/]<action>``).

        Only actions exposed in the model's ``__rpc__`` are callable; other
        names return 403 from the server.
        """
        return self._request(
            "POST",
            self._model_path(model, record_id, action),
            f"action {model}.{action}",
            params=self._fields_params(fields),
            payload=data or {},
            context=context,
        )

    # -- health ------------------------------------------------------------

    def ping(self) -> bool:
        """True when the server answers a minimal REST read; never raises."""
        try:
            self.search("res.user", fields=["login"], limit=1)
        except Exception:
            return False
        return True
