"""Request identity and correlation, carried without threading it by hand.

Every inbound request — HTTP, MCP, CLI, or a worker job — runs inside a
`RequestContext`. It answers two questions the rest of the hub keeps asking:
*who is calling* (so per-user credentials and authorization resolve) and *what
chain of work is this part of* (so logs and audit rows correlate).

It lives in a `ContextVar` because the alternative is an extra parameter on
every function between the transport and the audit writer, and one missed
hand-off silently attributes a call to the wrong principal. Set it once at each
entry point with `bind_request`; read it anywhere with `current_request`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from typing import Final

from app.core.ids import new_request_id

__all__ = [
    "ANONYMOUS",
    "Principal",
    "RequestContext",
    "bind_request",
    "current_principal",
    "current_request",
    "require_principal",
]


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller.

    `subject` is the stable per-user identity that per-user credentials key off
    (arch §20) — get it wrong and one user's token serves another's request, so
    it is set only by the authentication layer.
    """

    subject: str
    """Stable unique id for the caller. Empty only for `ANONYMOUS`."""

    display_name: str | None = None
    """Human-readable label for operator-facing output. Never used for lookup."""

    scopes: frozenset[str] = field(default_factory=frozenset)
    """Granted authorization scopes, e.g. `admin`, `tools:call`."""

    is_service_account: bool = False
    """True when this is a shared service identity rather than a person.

    Service accounts use the hub's shared credentials; humans use their own.
    """

    @property
    def is_authenticated(self) -> bool:
        """Whether a credential was actually verified for this caller."""
        return bool(self.subject)

    def has_scope(self, scope: str) -> bool:
        """Whether this caller holds `scope`, honouring the `admin` wildcard."""
        return "admin" in self.scopes or scope in self.scopes

    def __str__(self) -> str:
        return self.display_name or self.subject or "anonymous"


ANONYMOUS: Final = Principal(subject="")
"""The unauthenticated caller. Never resolves per-user credentials."""


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Per-request identity and correlation ids.

    Immutable: derive a modified copy with `with_principal` rather than
    mutating, so a nested operation can never rewrite its caller's identity.
    """

    request_id: str
    """Unique per inbound request. Echoed to clients and stamped on audit rows."""

    principal: Principal = ANONYMOUS
    """Who is calling."""

    trace_id: str | None = None
    """Correlates work spanning several requests (a job and the call that queued it)."""

    source: str = "unknown"
    """Entry point that created this context: `mcp`, `api`, `cli`, or `worker`."""

    client_ip: str | None = None
    """Peer address, when the transport knows one. Audit-only."""

    def with_principal(self, principal: Principal) -> RequestContext:
        """Return a copy that has been authenticated as `principal`."""
        return replace(self, principal=principal)

    def child(self, *, source: str) -> RequestContext:
        """Derive a context for downstream work, keeping the correlation chain.

        The new context gets its own `request_id` but inherits `trace_id` (or
        seeds it from this request), so a job started by an API call stays
        joinable to it in the audit log.
        """
        return RequestContext(
            request_id=new_request_id(),
            principal=self.principal,
            trace_id=self.trace_id or self.request_id,
            source=source,
            client_ip=self.client_ip,
        )


_current: ContextVar[RequestContext] = ContextVar("mcp_hub_request_context")


def current_request() -> RequestContext:
    """The active context, or a fresh anonymous one outside any request.

    Never raises: logging and audit paths call this from places that may not be
    inside a bound request (startup, background reaping), and they should record
    *something* rather than fail.
    """
    try:
        return _current.get()
    except LookupError:
        return RequestContext(request_id=new_request_id(), source="detached")


def current_principal() -> Principal:
    """The active caller, or `ANONYMOUS`."""
    return current_request().principal


def require_principal() -> Principal:
    """The active caller, refusing to proceed if unauthenticated.

    Raises:
        NotAuthenticated: No verified credential is bound to this request.
    """
    from app.core.errors import NotAuthenticated

    principal = current_principal()
    if not principal.is_authenticated:
        raise NotAuthenticated("This operation requires an authenticated caller.")
    return principal


@contextmanager
def bind_request(context: RequestContext) -> Iterator[RequestContext]:
    """Make `context` current for the duration of the block.

    Always restores the previous value, including on exception, so a failed
    request cannot leak its identity into whatever runs next on the same task.
    """
    token: Token[RequestContext] = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)
