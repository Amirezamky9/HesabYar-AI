"""Tool/resource/prompt scope registry (US-601).

The middleware enforces scopes centrally, so a tool's required scopes
must be known before its handler runs. Every HesabYar tool, resource
and prompt is registered here — through :func:`register_hesabyar_tool`
(and its resource/prompt siblings in US-604) — with the exact scopes it
needs. A name absent from the registry has no scope record: the
middleware lets the SDK dispatch it (an unknown tool fails with
``METHOD_NOT_FOUND`` there) and audits the attempt as an error.

US-601 registers no production tools: the registry starts empty and
the real tools land in US-602–US-605. Tests register fixture tools
through the same helper, so the enforcement path they exercise is the
production path.

US-606: resource URIs are templates (``hesabyar://companies/{company}/...``),
so the middleware matches a concrete URI against the registered
templates (:func:`ToolRegistry.match_resource_uri`) and enforces
resource scopes centrally — the handler check stays as defence in
depth, from the same registry record.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from mcp.server.mcpserver import MCPServer

#: Template parameter in a resource URI template, e.g. ``{company}``.
_TEMPLATE_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _template_to_regex(template: str) -> re.Pattern[str]:
    """Compile a resource URI template to a full-match regex.

    Literal segments match verbatim; each ``{param}`` becomes a named
    group matching one URI segment (``[^/]+``) — a parameter never
    spans a slash, so a template cannot match a URI of another shape.
    """
    parts: list[str] = []
    pos = 0
    for match in _TEMPLATE_PARAM.finditer(template):
        parts.append(re.escape(template[pos : match.start()]))
        parts.append(f"(?P<{match.group(1)}>[^/]+)")
        pos = match.end()
    parts.append(re.escape(template[pos:]))
    return re.compile(f"^{''.join(parts)}$")


class ToolRegistry:
    """Required scopes per tool, resource and prompt name."""

    def __init__(self) -> None:
        self._tools: dict[str, frozenset[str]] = {}
        self._resources: dict[str, frozenset[str]] = {}
        self._prompts: dict[str, frozenset[str]] = {}
        self._resource_patterns: dict[str, re.Pattern[str]] = {}

    # -- tools ----------------------------------------------------------

    def register_tool(self, name: str, scopes: Iterable[str]) -> None:
        """Record the scopes a tool requires (exact match, no wildcards)."""
        self._tools[name] = frozenset(scopes)

    def scopes_for_tool(self, name: str) -> frozenset[str] | None:
        """Required scopes for ``name``, or None when the tool is unknown."""
        return self._tools.get(name)

    def tool_names(self) -> frozenset[str]:
        """Names of all registered tools."""
        return frozenset(self._tools)

    # -- resources ------------------------------------------------------

    def register_resource(self, uri: str, scopes: Iterable[str]) -> None:
        """Record the scopes a resource requires."""
        self._resources[uri] = frozenset(scopes)
        self._resource_patterns[uri] = _template_to_regex(uri)

    def scopes_for_resource(self, uri: str) -> frozenset[str] | None:
        """Required scopes for the template ``uri`` (exact match), or None."""
        return self._resources.get(uri)

    def resource_templates(self) -> frozenset[str]:
        """URI templates of all registered resources."""
        return frozenset(self._resources)

    def match_resource_uri(self, uri: str) -> tuple[frozenset[str], dict[str, str]] | None:
        """Match a concrete resource URI against the registered templates.

        Returns the (required scopes, template parameters) of the
        first matching template, or None when no template matches.
        Templates are tried in registration order; HesabYar's
        templates are disjoint by their static segments, so at most
        one matches any URI.
        """
        for template, scopes in self._resources.items():
            match = self._resource_patterns[template].match(uri)
            if match is not None:
                return scopes, match.groupdict()
        return None

    def scopes_for_resource_uri(self, uri: str) -> frozenset[str] | None:
        """Required scopes for a concrete resource URI, or None when unknown."""
        matched = self.match_resource_uri(uri)
        return matched[0] if matched is not None else None

    # -- prompts --------------------------------------------------------

    def register_prompt(self, name: str, scopes: Iterable[str]) -> None:
        """Record the scopes a prompt requires."""
        self._prompts[name] = frozenset(scopes)

    def scopes_for_prompt(self, name: str) -> frozenset[str] | None:
        """Required scopes for ``name``, or None when the prompt is unknown."""
        return self._prompts.get(name)

    def prompt_names(self) -> frozenset[str]:
        """Names of all registered prompts."""
        return frozenset(self._prompts)


def register_hesabyar_tool(
    server: MCPServer[Any],
    registry: ToolRegistry,
    fn: Callable[..., Any],
    *,
    name: str,
    scopes: Iterable[str],
    description: str,
) -> None:
    """Register a tool on ``server`` AND record its scopes in ``registry``.

    The two registrations are one operation on purpose: a tool that is
    callable but has no scope record would bypass the middleware's
    scope check. Tool authors must use this helper (never
    ``server.tool`` directly) — US-602+ tools included.
    """
    registry.register_tool(name, scopes)
    server.tool(name=name, description=description)(fn)
