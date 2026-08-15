"""The hub's shared vocabulary.

These enums are the words every layer uses to talk about the same things, so
they live below every layer that uses them. They are all `str` enums: they
serialise to readable YAML/JSON, compare equal to their wire form, and survive
a round trip through the database without a translation table.

Nothing here performs I/O or imports from another `app` package. Keep it that
way — this module is the bottom of the import graph.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AuditAction",
    "ExposureMode",
    "HealthStatus",
    "JobKind",
    "JobStatus",
    "PolicyDecision",
    "RiskLevel",
    "SourceType",
    "TransportKind",
    "TrustTier",
    "UpdatePolicy",
]


class SourceType(StrEnum):
    """Where an integration's MCP server comes from (arch §8).

    This is the only thing the core gateway may branch on when it needs
    source-specific behaviour, and even then it does so through the adapter
    registry rather than an `if` — see arch §49.
    """

    REMOTE = "remote"
    """A provider-hosted MCP service reached over HTTP. Nothing is installed."""

    GIT = "git"
    """A repository cloned, built, and run as a local process or container."""

    NPM = "npm"
    """A package from the npm registry, run in an isolated Node environment."""

    PYTHON = "python"
    """A distribution from PyPI, run in an isolated Python environment."""

    DOCKER = "docker"
    """A container image, run with a constrained runtime profile."""

    BUILTIN = "builtin"
    """Implemented inside the hub itself; no external artifact."""

    @property
    def is_local(self) -> bool:
        """Whether this source materialises artifacts under `runtime/`.

        Remote and builtin integrations have no local install, so the whole
        stage/promote/rollback machinery does not apply to them.
        """
        return self not in (SourceType.REMOTE, SourceType.BUILTIN)


class TransportKind(StrEnum):
    """How the hub speaks MCP to one upstream."""

    STREAMABLE_HTTP = "streamable-http"
    """Streamable HTTP — the hub's own transport, and the default upstream."""

    STDIO = "stdio"
    """A child process speaking MCP over stdin/stdout."""

    IN_MEMORY = "in-memory"
    """A server object in this process. Used by builtins and by tests."""


class TrustTier(StrEnum):
    """How much the hub trusts an integration's provenance (arch §46).

    Installation gating reads this: anything below `LOCAL_OFFICIAL` needs an
    explicit human decision before its code is fetched or run.
    """

    REMOTE_OFFICIAL = "remote_official"
    """Vendor-operated remote service. No third-party code executes locally."""

    LOCAL_OFFICIAL = "local_official"
    """Vendor-published server we run ourselves, isolated."""

    COMMUNITY = "community"
    """Third-party code. Requires explicit trust before install."""

    BUILTIN = "builtin"
    """The hub's own code."""

    @property
    def requires_explicit_trust(self) -> bool:
        """Whether installing this tier demands an operator confirmation."""
        return self is TrustTier.COMMUNITY


class HealthStatus(StrEnum):
    """An integration's serving state (arch §25)."""

    HEALTHY = "HEALTHY"
    """Initialised, tools listed, serving."""

    DEGRADED = "DEGRADED"
    """Reachable but failing some checks, or slow past its budget."""

    UNAVAILABLE = "UNAVAILABLE"
    """Enabled but unreachable. Its tools stop being routed."""

    DISABLED = "DISABLED"
    """Switched off by configuration. Not an error."""

    UPDATE_REQUIRED = "UPDATE_REQUIRED"
    """Running, but pinned below the resolved desired version."""

    AUTH_REQUIRED = "AUTH_REQUIRED"
    """Reachable, but no usable credential is configured."""

    @property
    def can_serve(self) -> bool:
        """Whether tools from this integration should be routed to."""
        return self in (HealthStatus.HEALTHY, HealthStatus.DEGRADED, HealthStatus.UPDATE_REQUIRED)


class RiskLevel(StrEnum):
    """What a tool can do, worst case (arch §52).

    Ordered: `DESTRUCTIVE` and `ADMIN` outrank `WRITE`, which outranks `READ`.
    A manifest's explicit classification always beats the name heuristic.
    """

    READ = "READ"
    """Observes state. Safe to retry."""

    WRITE = "WRITE"
    """Creates or modifies state. Not necessarily reversible."""

    DESTRUCTIVE = "DESTRUCTIVE"
    """Removes state. Assume it cannot be undone."""

    ADMIN = "ADMIN"
    """Changes permissions, billing, or configuration of the upstream itself."""

    @property
    def rank(self) -> int:
        """Severity order, for picking the stricter of two classifications."""
        return _RISK_RANK[self]

    def __lt__(self, other: RiskLevel) -> bool:  # type: ignore[override]
        return self.rank < other.rank


_RISK_RANK: dict[RiskLevel, int] = {
    RiskLevel.READ: 0,
    RiskLevel.WRITE: 1,
    RiskLevel.DESTRUCTIVE: 2,
    RiskLevel.ADMIN: 3,
}


class PolicyDecision(StrEnum):
    """What the policy engine concluded about one tool call (arch §23)."""

    ALLOW = "allow"
    """Proceed."""

    CONFIRM = "confirm"
    """Proceed only after a human accepts an elicitation."""

    DENY = "deny"
    """Refuse. Never downgraded to `CONFIRM` by anything downstream."""


class ExposureMode(StrEnum):
    """How many tool schemas the hub shows an agent (arch §13)."""

    FULL = "full"
    """Every tool of every healthy integration."""

    SELECTIVE = "selective"
    """Only tools an allow policy admits. The default."""

    DISCOVERY = "discovery"
    """A handful of hub router tools; integrations activate on demand."""


class UpdatePolicy(StrEnum):
    """Who decides when an integration moves to a new version (arch §34)."""

    MANUAL = "manual"
    """Only an explicit operator command. The default."""

    SCHEDULED = "scheduled"
    """A cadence proposes updates; each still runs the full staged pipeline."""

    AUTOMATIC = "automatic"
    """Updates apply without prompting, still staged and health-gated."""


class JobKind(StrEnum):
    """Long-running work that must not block an HTTP handler (arch §29)."""

    INSTALL = "install"
    UPDATE = "update"
    REMOVE = "remove"
    ROLLBACK = "rollback"
    HEALTH_CHECK = "health_check"
    TOOL_REFRESH = "tool_refresh"


class JobStatus(StrEnum):
    """A job's position in its lifecycle."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is possible."""
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


class AuditAction(StrEnum):
    """Everything the hub writes an audit record for (arch §24)."""

    INSTALL = "install"
    UPDATE = "update"
    REMOVE = "remove"
    ROLLBACK = "rollback"
    ENABLE = "enable"
    DISABLE = "disable"
    TOOL_CALL = "tool_call"
    TOOL_REFRESH = "tool_refresh"
    AUTH_SUCCESS = "auth_success"
    AUTH_FAILURE = "auth_failure"
    AUTHZ_FAILURE = "authz_failure"
    POLICY_VIOLATION = "policy_violation"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONFIRMATION_DENIED = "confirmation_denied"
    UPSTREAM_ERROR = "upstream_error"
    RECONCILE = "reconcile"
