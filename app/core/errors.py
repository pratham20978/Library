"""The hub's exception hierarchy.

Every failure the hub raises deliberately is a `HubError`. Each one carries a
stable machine-readable `code` (what an API client or agent branches on) and a
`message` safe to show a caller. Anything that escapes as a bare `Exception` is
a bug, and the surfaces that face the network say so by converting unknown
exceptions into `INTERNAL_ERROR` without echoing their text.

The `code` values are part of the hub's public contract: they appear in REST
error bodies, MCP tool errors, and audit records. Treat renames as breaking.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

__all__ = [
    "ConfirmationRequired",
    "HubError",
    "IntegrationDisabled",
    "IntegrationLocked",
    "IntegrationNotFound",
    "IntegrationUnavailable",
    "InvalidConfiguration",
    "NotAuthenticated",
    "NotAuthorized",
    "PolicyViolation",
    "RateLimited",
    "RollbackFailed",
    "SecretNotFound",
    "SupplyChainRejected",
    "ToolNotFound",
    "UpdateFailed",
    "UpstreamError",
    "ValidationFailed",
    "describe_exception",
]


class HubError(Exception):
    """Base class for every deliberate hub failure.

    Args:
        message: Caller-safe description. Never interpolate secret material.
        details: Structured, caller-safe context (ids, names, counts). Values
            are surfaced verbatim in API responses and audit records, so they
            must already be redacted.
    """

    code: str = "hub_error"
    """Stable machine-readable identifier for this failure class."""

    http_status: int = 500
    """Status the REST layer uses when this escapes a request handler."""

    def __init__(self, message: str, /, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        """Render as the wire body shared by the REST API and MCP errors."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r}, details={self.details!r})"


# --------------------------------------------------------------------------- config


class InvalidConfiguration(HubError):
    """Catalog, policy, or settings input the hub refuses to run with."""

    code = "invalid_configuration"
    http_status = 400


class ValidationFailed(HubError):
    """A request or manifest failed schema/semantic validation."""

    code = "validation_failed"
    http_status = 422


# --------------------------------------------------------------------------- integrations


class IntegrationNotFound(HubError):
    """No integration is registered under the requested name."""

    code = "integration_not_found"
    http_status = 404


class IntegrationDisabled(HubError):
    """The integration exists but is switched off, so it routes no traffic."""

    code = "integration_disabled"
    http_status = 409


class IntegrationUnavailable(HubError):
    """The integration is enabled but its upstream cannot serve requests.

    This is the graceful-degradation path (arch §45): one broken upstream must
    never take the hub down, so callers get this instead of a 5xx from the hub.
    """

    code = "integration_unavailable"
    http_status = 503


class IntegrationLocked(HubError):
    """Another lifecycle operation holds this integration's lock (arch §30)."""

    code = "integration_locked"
    http_status = 409


# --------------------------------------------------------------------------- lifecycle


class UpdateFailed(HubError):
    """An update aborted. If rollback was armed, the prior version is restored."""

    code = "update_failed"
    http_status = 500


class RollbackFailed(HubError):
    """A rollback could not restore the prior version — needs an operator."""

    code = "rollback_failed"
    http_status = 500


class SupplyChainRejected(HubError):
    """An artifact failed provenance/security gating and was not installed."""

    code = "supply_chain_rejected"
    http_status = 400


# --------------------------------------------------------------------------- gateway


class ToolNotFound(HubError):
    """No enabled tool is registered under the requested qualified name."""

    code = "tool_not_found"
    http_status = 404


class UpstreamError(HubError):
    """The upstream MCP server returned a protocol-level error."""

    code = "upstream_error"
    http_status = 502


# --------------------------------------------------------------------------- security


class NotAuthenticated(HubError):
    """The caller presented no credential, or one the hub could not verify."""

    code = "not_authenticated"
    http_status = 401


class NotAuthorized(HubError):
    """The caller is known but lacks the scope for this operation."""

    code = "not_authorized"
    http_status = 403


class PolicyViolation(HubError):
    """The policy engine denied the call (arch §23)."""

    code = "policy_violation"
    http_status = 403


class ConfirmationRequired(HubError):
    """A human confirmation is required and could not be obtained (arch §53).

    Raised when the tool needs confirmation but the client cannot be asked —
    it declares no elicitation capability. Never treated as "allow".
    """

    code = "confirmation_required"
    http_status = 403


class RateLimited(HubError):
    """The caller exceeded a configured rate limit."""

    code = "rate_limited"
    http_status = 429

    def __init__(self, message: str, /, *, retry_after_seconds: float | None = None, **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after_seconds = retry_after_seconds


class SecretNotFound(HubError):
    """A required secret is absent from every configured provider."""

    code = "secret_not_found"
    http_status = 424


# --------------------------------------------------------------------------- rendering


_MAX_GROUP_DEPTH = 6


def describe_exception(exception: BaseException, *, limit: int = 3) -> str:
    """Render an exception for an operator, unwrapping exception groups.

    Every MCP transport in the SDK is built on anyio task groups, so a failed
    upstream handshake arrives as an `ExceptionGroup` whose own text is
    "unhandled errors in a TaskGroup (1 sub-exception)" — accurate, and useless
    to whoever has to fix it. This digs out the leaves, so a health report names
    the 401 or the DNS failure that actually happened.

    Args:
        exception: What went wrong.
        limit: How many leaf causes to name before summarising the rest. A group
            of thirty identical connection errors should not fill an audit row.

    Returns:
        A single line, safe to put in a health detail or an audit record. It may
        contain an upstream's own error text, which is why callers must not use
        it where a message reaches an unauthenticated client (arch §45).
    """
    leaves = list(_leaf_exceptions(exception))
    parts = [_describe_one(leaf) for leaf in leaves[:limit]]
    if len(leaves) > limit:
        parts.append(f"(+{len(leaves) - limit} more)")
    return "; ".join(parts) or type(exception).__name__


def _describe_one(exception: BaseException) -> str:
    """`TypeName: message`, falling back to the cause when there is no message.

    A `code` attribute is appended when the exception carries one. The SDK's
    `MCPError` renders every JSON-RPC failure as the same sentence, so the code
    is the only thing distinguishing "invalid params" from "internal error" —
    and it is what an operator will search for.
    """
    text = str(exception).strip()
    if not text and exception.__cause__ is not None:
        return f"{type(exception).__name__}: {_describe_one(exception.__cause__)}"

    code = getattr(exception, "code", None)
    if isinstance(code, int | str) and str(code) not in text:
        text = f"{text} [code {code}]" if text else f"code {code}"
    return f"{type(exception).__name__}: {text}" if text else type(exception).__name__


def _leaf_exceptions(exception: BaseException, depth: int = 0) -> Iterator[BaseException]:
    """Flatten nested exception groups down to the failures that carry meaning."""
    if isinstance(exception, BaseExceptionGroup) and depth < _MAX_GROUP_DEPTH:
        for nested in exception.exceptions:
            yield from _leaf_exceptions(nested, depth + 1)
        return
    yield exception
