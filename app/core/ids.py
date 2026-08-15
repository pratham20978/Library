"""Identifier minting and the qualified-name grammar.

Two unrelated jobs live here because both are about *names the whole system
agrees on*: opaque unique ids, and the `integration.tool` grammar that arch §11
makes the hub's central naming contract.

Qualified names are parsed and built in exactly one place so a namespace can
never be assembled with the wrong separator, and so the MCP tool-name rules
(SEP-986: `[A-Za-z0-9._-]`, ≤128 chars) are enforced once rather than hoped for.
"""

from __future__ import annotations

import re
import secrets
import uuid
from typing import Final, NamedTuple

__all__ = [
    "MAX_TOOL_NAME_LENGTH",
    "NAMESPACE_SEPARATOR",
    "QualifiedName",
    "is_valid_namespace",
    "new_id",
    "new_job_id",
    "new_request_id",
    "new_token",
    "parse_qualified_name",
    "qualify",
    "validate_namespace",
]

NAMESPACE_SEPARATOR: Final = "."
"""Separator between namespace and tool name (arch §11). SEP-986 permits `.`."""

MAX_TOOL_NAME_LENGTH: Final = 128
"""SEP-986's limit. The *qualified* name must fit, not just the upstream name."""

_NAMESPACE_RE: Final = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
"""Namespaces are lowercase and dot-free: a dot would make parsing ambiguous.

Deliberately narrower than SEP-986 allows for tool names, because a namespace
is also a config key, a directory name, and a CLI argument.
"""

_UPSTREAM_NAME_RE: Final = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
"""SEP-986's tool-name grammar, applied to names we receive from upstreams."""


def new_id() -> str:
    """A random opaque identifier (UUID4 hex, no dashes)."""
    return uuid.uuid4().hex


def new_request_id() -> str:
    """A request id, prefixed so it is recognisable in a log line."""
    return f"req_{uuid.uuid4().hex[:24]}"


def new_job_id() -> str:
    """A job id, prefixed so it is recognisable in a log line."""
    return f"job_{uuid.uuid4().hex[:24]}"


def new_token(*, nbytes: int = 32) -> str:
    """A cryptographically secure URL-safe token.

    Used for bootstrap admin credentials and confirmation nonces. Callers wrap
    the result in `Secret` before it goes anywhere near a log.
    """
    return secrets.token_urlsafe(nbytes)


class QualifiedName(NamedTuple):
    """A parsed `integration.tool` pair.

    `tool` keeps any dots the upstream used — only the *first* separator marks
    the namespace boundary, so an upstream tool legitimately called `a.b` round
    trips as `ns.a.b` rather than being silently mangled.
    """

    namespace: str
    """The integration's namespace, e.g. `jira`."""

    tool: str
    """The upstream tool name, e.g. `search_issues` or `get.thing`."""

    def __str__(self) -> str:
        return f"{self.namespace}{NAMESPACE_SEPARATOR}{self.tool}"


def is_valid_namespace(namespace: str) -> bool:
    """Whether `namespace` is a legal integration namespace."""
    return _NAMESPACE_RE.match(namespace) is not None


def validate_namespace(namespace: str) -> str:
    """Return `namespace` if legal, else explain precisely why it is not.

    Raises:
        ValidationFailed: The namespace breaks the grammar.
    """
    from app.core.errors import ValidationFailed

    if not is_valid_namespace(namespace):
        raise ValidationFailed(
            "Namespace must be 1-64 characters of lowercase letters, digits, "
            "'_' or '-', starting and ending alphanumeric, with no dots.",
            namespace=namespace,
        )
    return namespace


def qualify(namespace: str, tool: str) -> str:
    """Build the agent-visible name for `tool` inside `namespace` (arch §11).

    Raises:
        ValidationFailed: The namespace is illegal, the upstream name breaks
            SEP-986, or the combination exceeds the 128-character limit.
    """
    from app.core.errors import ValidationFailed

    validate_namespace(namespace)
    if not _UPSTREAM_NAME_RE.match(tool):
        raise ValidationFailed(
            "Upstream tool name is not a valid MCP tool name (SEP-986 allows "
            "A-Z, a-z, 0-9, '_', '-' and '.', 1-128 characters).",
            namespace=namespace,
            tool=tool,
        )
    qualified = f"{namespace}{NAMESPACE_SEPARATOR}{tool}"
    if len(qualified) > MAX_TOOL_NAME_LENGTH:
        raise ValidationFailed(
            f"Qualified tool name exceeds the {MAX_TOOL_NAME_LENGTH}-character MCP limit.",
            namespace=namespace,
            tool=tool,
            length=len(qualified),
        )
    return qualified


def parse_qualified_name(qualified: str) -> QualifiedName:
    """Split an agent-supplied name back into namespace and upstream tool.

    Raises:
        ValidationFailed: The name carries no namespace or an illegal one. The
            gateway turns this into `tool_not_found` rather than echoing it, so
            a probing agent learns nothing about what exists.
    """
    from app.core.errors import ValidationFailed

    namespace, separator, tool = qualified.partition(NAMESPACE_SEPARATOR)
    if not separator or not tool:
        raise ValidationFailed(
            "Tool names must be qualified as '<integration>.<tool>'.",
            requested=qualified,
        )
    validate_namespace(namespace)
    return QualifiedName(namespace=namespace, tool=tool)
