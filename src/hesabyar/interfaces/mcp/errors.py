"""Typed MCP errors (US-601).

HesabYar's MCP layer signals authorization failures as JSON-RPC errors
in the server-error range (-32000..-32099): the MCP spec defines no
dedicated auth error codes, and a denied tool call is a protocol-level
refusal, not a tool result. The audit middleware maps these codes back
to the ``denied`` outcome; every other failure is ``error``.

Error messages never carry key material, and never distinguish
"unknown key" from "revoked key" — the HTTP layer already answers one
generic 401 for every auth failure, and the MCP layer keeps the same
shape.
"""

from __future__ import annotations

from mcp.shared.exceptions import MCPError

#: Generic server failure (JSON-RPC server-error range).
MCP_ERROR_INTERNAL = -32000
#: No authenticated identity could be established for the call.
MCP_ERROR_UNAUTHENTICATED = -32001
#: The authenticated identity may not make this call (missing scope or
#: a company outside its grant).
MCP_ERROR_FORBIDDEN = -32002


def unauthenticated(message: str = "Authentication required: a valid API key is required.") -> MCPError:
    """Build the MCP error for a call with no authenticated identity."""
    return MCPError(MCP_ERROR_UNAUTHENTICATED, message)


def forbidden(message: str) -> MCPError:
    """Build the MCP error for a call the authenticated identity may not make."""
    return MCPError(MCP_ERROR_FORBIDDEN, message)


def internal_error(message: str = "Internal error.") -> MCPError:
    """Build the MCP error for a server-side failure (audit failure, ...)."""
    return MCPError(MCP_ERROR_INTERNAL, message)


def is_auth_error(error: MCPError) -> bool:
    """True when ``error`` is one of HesabYar's auth/authorization errors."""
    return error.code in (MCP_ERROR_UNAUTHENTICATED, MCP_ERROR_FORBIDDEN)


class CompanyAccessError(Exception):
    """A caller-supplied company id failed the grant check.

    Raised by :func:`hesabyar.interfaces.mcp.auth.resolve_company_row`;
    the middleware audits it (``error_code`` distinguishes the two
    fail-closed reasons) and translates it to an ``MCP_ERROR_FORBIDDEN``
    protocol error. Never carries key material.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        #: ``company_not_found`` (unknown, or another tenant's) or
        #: ``company_forbidden`` (the key is restricted elsewhere).
        self.error_code = error_code
        self.message = message
