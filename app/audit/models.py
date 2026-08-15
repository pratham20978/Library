"""The audit event (arch §24).

One immutable record per auditable action. Built through the constructors below
rather than assembled field by field at each call site, so every record of a
given kind carries the same fields and redaction is applied in exactly one place.

Nothing here may contain credential material. `arguments` is passed through
`redact_arguments` on construction — not by the caller, and not later — because
"the caller redacts it" is a rule that holds until the one call site that forgets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Self

from app.core.clock import utcnow
from app.core.context import RequestContext, current_request
from app.core.domain import AuditAction, PolicyDecision, RiskLevel
from app.core.redaction import redact_arguments

__all__ = ["AuditEvent", "AuditStatus"]

AuditStatus = Literal["success", "denied", "error", "pending"]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One auditable action, ready to persist."""

    action: AuditAction
    status: AuditStatus
    timestamp: datetime = field(default_factory=utcnow)
    request_id: str = ""
    trace_id: str | None = None
    user_id: str | None = None
    integration: str | None = None
    tool: str | None = None
    risk_level: RiskLevel | None = None
    decision: PolicyDecision | None = None
    duration_ms: float | None = None
    error_code: str | None = None
    message: str | None = None
    arguments: dict[str, Any] | None = None
    """Already-redacted argument metadata. Never raw arguments."""

    context: dict[str, Any] | None = None
    """Extra structured detail: versions, counts, stage names."""

    client_ip: str | None = None

    @classmethod
    def build(
        cls,
        action: AuditAction,
        status: AuditStatus,
        *,
        request: RequestContext | None = None,
        raw_arguments: dict[str, Any] | None = None,
        **fields: Any,
    ) -> Self:
        """Construct an event, filling identity from the active request context.

        `raw_arguments` is redacted here. Pass the real arguments; never
        pre-redact and never pass them through `arguments` directly.
        """
        ctx = request or current_request()
        return cls(
            action=action,
            status=status,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            user_id=ctx.principal.subject or None,
            client_ip=ctx.client_ip,
            arguments=redact_arguments(raw_arguments) if raw_arguments else None,
            **fields,
        )

    def to_row(self) -> dict[str, Any]:
        """Column values for the `audit_logs` table."""
        return {
            "timestamp": self.timestamp,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "action": self.action.value,
            "status": self.status,
            "user_id": self.user_id,
            "integration": self.integration,
            "tool": self.tool,
            "risk_level": self.risk_level.value if self.risk_level else None,
            "decision": self.decision.value if self.decision else None,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "message": self.message,
            "arguments": self.arguments,
            "context": self.context,
            "client_ip": self.client_ip,
        }

    def to_log_fields(self) -> dict[str, Any]:
        """Fields for the structured log mirror, dropping empties."""
        row = self.to_row()
        row["timestamp"] = self.timestamp.isoformat()
        return {key: value for key, value in row.items() if value is not None}
