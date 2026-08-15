"""Persistent schema (arch §27).

PostgreSQL in production, SQLite for local work and tests. Portability is kept
by two deliberate choices: JSON columns declare a `JSONB` variant that only
PostgreSQL sees, and every timestamp is stored timezone-aware so SQLite's naive
default cannot creep in.

What lives here versus in YAML is a firm split. The YAML files are the *desired*
and *resolved* contract — human-authored, reviewable in a diff, and the source of
truth for what should exist. These tables hold *observed* state and history:
health probes, discovered tools, audit records, job progress, rollback points.
Losing the database costs history and requires a rediscovery; it does not change
what the hub is configured to do.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.clock import utcnow
from app.core.domain import HealthStatus, JobKind, JobStatus, RiskLevel, SourceType

__all__ = [
    "AuditLog",
    "Base",
    "HealthRecord",
    "IntegrationRecord",
    "IntegrationVersion",
    "JobRecord",
    "PolicyRecord",
    "RollbackPoint",
    "StoredSecret",
    "ToolRecord",
    "UpdateHistory",
    "User",
]

# `JSONB` on PostgreSQL for indexing and containment queries; plain `JSON`
# elsewhere so the same models run on SQLite unchanged.
JsonColumn = JSON().with_variant(JSONB, "postgresql")

_TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    """Declarative base for every table."""


def _timestamp(**kwargs: Any) -> Mapped[datetime]:
    """A timezone-aware timestamp column defaulting to now."""
    return mapped_column(_TZ, default=utcnow, **kwargs)


# --------------------------------------------------------------------------- identity


class User(Base):
    """A caller the hub can authenticate and attribute credentials to (arch §20)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    """Stable external identity — an OAuth subject or an issued token's subject."""

    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    email: Mapped[str | None] = mapped_column(String(320), default=None, index=True)
    scopes: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    is_service_account: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _timestamp()
    last_seen_at: Mapped[datetime | None] = mapped_column(_TZ, default=None)


class StoredSecret(Base):
    """An encrypted credential (arch §21, §27 `integration_credentials`).

    `ciphertext` is Fernet-sealed by `EncryptedDatabaseProvider`; plaintext never
    reaches this table. `principal` being non-null makes the row a per-user
    credential. `integration` records sole ownership, which is what lets removal
    delete an integration's own secrets while leaving shared ones alone (arch §18).
    """

    __tablename__ = "integration_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    """Composite storage key: `user:<subject>:<name>` or `shared:<name>`."""

    name: Mapped[str] = mapped_column(String(255), index=True)
    principal: Mapped[str | None] = mapped_column(String(255), default=None, index=True)
    integration: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _timestamp()
    updated_at: Mapped[datetime] = _timestamp()

    __table_args__ = (Index("ix_credentials_principal_name", "principal", "name"),)


# --------------------------------------------------------------------------- integrations


class IntegrationRecord(Base):
    """Observed state for one configured integration (arch §27 `integrations`).

    A mirror of the YAML contract plus runtime facts, so the dashboard and audit
    joins do not have to parse YAML. The YAML files stay authoritative.
    """

    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    source_type: Mapped[SourceType] = mapped_column(String(32))
    trust: Mapped[str] = mapped_column(String(32), default="community")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    current_version_id: Mapped[str | None] = mapped_column(String(128), default=None)
    last_status: Mapped[HealthStatus] = mapped_column(String(32), default=HealthStatus.DISABLED)
    last_checked_at: Mapped[datetime | None] = mapped_column(_TZ, default=None)
    tool_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _timestamp()
    updated_at: Mapped[datetime] = _timestamp()

    versions: Mapped[list[IntegrationVersion]] = relationship(
        back_populates="integration", cascade="all, delete-orphan", lazy="selectin"
    )
    tools: Mapped[list[ToolRecord]] = relationship(
        back_populates="integration", cascade="all, delete-orphan", lazy="selectin"
    )


class IntegrationVersion(Base):
    """One installed version, kept for rollback (arch §15, §27, §55).

    Provenance columns are the supply-chain record arch §55 requires: which
    repository, which commit, which digest, when, and by whom.
    """

    __tablename__ = "integration_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"), index=True)
    version_id: Mapped[str] = mapped_column(String(128), index=True)
    """Commit sha, package version, or image digest — whatever pins this source."""

    source_type: Mapped[SourceType] = mapped_column(String(32))
    repository: Mapped[str | None] = mapped_column(String(512), default=None)
    branch: Mapped[str | None] = mapped_column(String(255), default=None)
    commit_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    package_version: Mapped[str | None] = mapped_column(String(128), default=None)
    image_digest: Mapped[str | None] = mapped_column(String(128), default=None)
    install_path: Mapped[str | None] = mapped_column(String(1024), default=None)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    tool_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    installed_by: Mapped[str | None] = mapped_column(String(255), default=None)
    installed_at: Mapped[datetime] = _timestamp()

    integration: Mapped[IntegrationRecord] = relationship(back_populates="versions")

    __table_args__ = (UniqueConstraint("integration_id", "version_id", name="uq_version_per_integration"),)


class HealthRecord(Base):
    """One health probe result (arch §25, §27 `integration_health`)."""

    __tablename__ = "integration_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    integration_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[HealthStatus] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    latency_ms: Mapped[float | None] = mapped_column(Float, default=None)
    tool_count: Mapped[int] = mapped_column(Integer, default=0)
    protocol_version: Mapped[str | None] = mapped_column(String(32), default=None)
    server_version: Mapped[str | None] = mapped_column(String(128), default=None)
    checked_at: Mapped[datetime] = _timestamp(index=True)

    __table_args__ = (Index("ix_health_integration_time", "integration_id", "checked_at"),)


class ToolRecord(Base):
    """A tool discovered from an upstream (arch §51).

    Rebuilt by discovery rather than hand-maintained — arch §12 forbids
    hard-coding third-party tool definitions, so this table is a cache of what
    `tools/list` actually returned, with the hub's own classification attached.
    """

    __tablename__ = "tool_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"), index=True)
    tool_name: Mapped[str] = mapped_column(String(128))
    """Name as the upstream reports it."""

    qualified_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    """Name agents see, `<namespace>.<tool_name>` (arch §11)."""

    title: Mapped[str | None] = mapped_column(String(255), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, default=None)
    annotations: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, default=None)
    risk_level: Mapped[RiskLevel] = mapped_column(String(16), default=RiskLevel.WRITE, index=True)
    risk_source: Mapped[str] = mapped_column(String(32), default="heuristic")
    """`manifest`, `annotation`, or `heuristic` — how the risk was determined (arch §52)."""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    discovered_at: Mapped[datetime] = _timestamp()
    last_called_at: Mapped[datetime | None] = mapped_column(_TZ, default=None)
    call_count: Mapped[int] = mapped_column(Integer, default=0)

    integration: Mapped[IntegrationRecord] = relationship(back_populates="tools")

    __table_args__ = (UniqueConstraint("integration_id", "tool_name", name="uq_tool_per_integration"),)


class PolicyRecord(Base):
    """A policy override stored in the database (arch §27 `policies`).

    `config/policies.yaml` remains the reviewable source of truth. Rows here are
    for runtime changes made through the admin API that must survive a restart;
    the engine layers them over the file, and an operator can promote one into
    YAML at any time.
    """

    __tablename__ = "policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    integration_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    rule: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(255), default=None)
    updated_at: Mapped[datetime] = _timestamp()


# --------------------------------------------------------------------------- audit & jobs


class AuditLog(Base):
    """One recorded action (arch §24).

    Append-only by convention. `arguments` holds redacted metadata, never raw
    tool arguments — `redact_arguments` runs before anything is written here.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = _timestamp(index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    """`success`, `denied`, `error`, or `pending`."""

    user_id: Mapped[str | None] = mapped_column(String(255), default=None, index=True)
    integration: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    tool: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    risk_level: Mapped[str | None] = mapped_column(String(16), default=None)
    decision: Mapped[str | None] = mapped_column(String(16), default=None)
    duration_ms: Mapped[float | None] = mapped_column(Float, default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    message: Mapped[str | None] = mapped_column(Text, default=None)
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, default=None)
    context: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, default=None)
    client_ip: Mapped[str | None] = mapped_column(String(64), default=None)

    __table_args__ = (
        Index("ix_audit_user_time", "user_id", "timestamp"),
        Index("ix_audit_integration_time", "integration", "timestamp"),
    )


class JobRecord(Base):
    """A queued lifecycle operation (arch §29, §27 `update_jobs`)."""

    __tablename__ = "update_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[JobKind] = mapped_column(String(32), index=True)
    status: Mapped[JobStatus] = mapped_column(String(24), default=JobStatus.QUEUED, index=True)
    integrations: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    parameters: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(String(512), default=None)
    requested_by: Mapped[str | None] = mapped_column(String(255), default=None)
    request_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = _timestamp(index=True)
    started_at: Mapped[datetime | None] = mapped_column(_TZ, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(_TZ, default=None)


class UpdateHistory(Base):
    """The outcome of one update attempt (arch §27 `update_history`)."""

    __tablename__ = "update_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    integration_id: Mapped[str] = mapped_column(String(64), index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    from_version: Mapped[str | None] = mapped_column(String(128), default=None)
    to_version: Mapped[str | None] = mapped_column(String(128), default=None)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rolled_back: Mapped[bool] = mapped_column(Boolean, default=False)
    stage_reached: Mapped[str | None] = mapped_column(String(48), default=None)
    """Last pipeline stage entered — where a failure actually happened (arch §15)."""

    tools_added: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    tools_removed: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    duration_ms: Mapped[float | None] = mapped_column(Float, default=None)
    started_at: Mapped[datetime] = _timestamp()
    finished_at: Mapped[datetime | None] = mapped_column(_TZ, default=None)


class RollbackPoint(Base):
    """A restorable snapshot taken before a mutating operation (arch §19, §39).

    The backup directory holds configuration, lock state, and tool metadata —
    never credential material, which arch §19 forbids restoring from a backup.
    """

    __tablename__ = "rollback_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    integration_id: Mapped[str] = mapped_column(String(64), index=True)
    version_id: Mapped[str] = mapped_column(String(128))
    backup_path: Mapped[str] = mapped_column(String(1024))
    reason: Mapped[str] = mapped_column(String(64), default="update")
    lock_entry: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, default=None)
    tool_snapshot: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonColumn, default=None)
    created_by: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = _timestamp(index=True)

    __table_args__ = (Index("ix_rollback_integration_time", "integration_id", "created_at"),)
