"""Scopes and the checks that read them (arch §22, §64).

Arch §64 requires administrative operations to be protected separately from tool
access: an agent holding a token good enough to call `jira.get_issue` must not be
able to uninstall an integration with it. Scopes are how that separation is
expressed.

The vocabulary is small on purpose. Every scope names something an operator would
actually want to grant or withhold; anything finer belongs in `config/policies.yaml`,
which can name individual tools. `admin` is a wildcard satisfying every check —
convenient for a bootstrap operator, and exactly what should not be handed to an
agent.
"""

from __future__ import annotations

from typing import Final

from app.core.context import Principal
from app.core.errors import NotAuthorized

__all__ = [
    "AGENT_SCOPES",
    "ALL_SCOPES",
    "SCOPE_ADMIN",
    "SCOPE_AUDIT_READ",
    "SCOPE_INTEGRATIONS_READ",
    "SCOPE_INTEGRATIONS_WRITE",
    "SCOPE_SECRETS_WRITE",
    "SCOPE_TOOLS_CALL",
    "SCOPE_TOOLS_REFRESH",
    "require_scope",
]

SCOPE_ADMIN: Final = "admin"
"""Everything. Grant to operators, never to an agent."""

SCOPE_TOOLS_CALL: Final = "tools:call"
"""Invoke integration tools through the MCP endpoint."""

SCOPE_TOOLS_REFRESH: Final = "tools:refresh"
"""Trigger tool rediscovery. Reconnects to upstreams, so it is not free."""

SCOPE_INTEGRATIONS_READ: Final = "integrations:read"
"""List integrations, health, and tool metadata."""

SCOPE_INTEGRATIONS_WRITE: Final = "integrations:write"
"""Install, update, enable, disable, remove, roll back."""

SCOPE_SECRETS_WRITE: Final = "secrets:write"
"""Store or delete credentials. Never grants the ability to read one back."""

SCOPE_AUDIT_READ: Final = "audit:read"
"""Read the audit trail."""

ALL_SCOPES: Final = (
    SCOPE_ADMIN,
    SCOPE_TOOLS_CALL,
    SCOPE_TOOLS_REFRESH,
    SCOPE_INTEGRATIONS_READ,
    SCOPE_INTEGRATIONS_WRITE,
    SCOPE_SECRETS_WRITE,
    SCOPE_AUDIT_READ,
)

AGENT_SCOPES: Final = (SCOPE_TOOLS_CALL, SCOPE_INTEGRATIONS_READ)
"""A sensible default for a token issued to an agent: call tools, read status,
change nothing."""


def require_scope(principal: Principal, scope: str, *, action: str = "") -> None:
    """Assert that `principal` holds `scope`.

    Raises:
        NotAuthorized: The caller lacks it. The message names the missing scope,
            which is safe — knowing a scope exists reveals nothing, and an
            operator debugging a 403 needs it.
    """
    if principal.has_scope(scope):
        return
    what = action or f"this operation (requires {scope!r})"
    raise NotAuthorized(
        f"{principal} is not permitted to perform {what}. Missing scope: {scope!r}.",
        required_scope=scope,
        subject=principal.subject or None,
    )


def require_any_scope(principal: Principal, *scopes: str, action: str = "") -> None:
    """Assert that `principal` holds at least one of `scopes`.

    Raises:
        NotAuthorized: The caller holds none of them.
    """
    if any(principal.has_scope(scope) for scope in scopes):
        return
    what = action or "this operation"
    raise NotAuthorized(
        f"{principal} is not permitted to perform {what}. Requires one of: {', '.join(sorted(scopes))}.",
        required_scopes=sorted(scopes),
        subject=principal.subject or None,
    )
