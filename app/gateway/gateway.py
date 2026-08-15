"""The gateway — the pipeline every tool call runs through (arch §23).

This is where the hub's guarantees are actually enforced, in this order:

    authenticate -> resolve tool -> integration serving? -> policy
      -> confirmation -> rate limit -> upstream -> audit

Three decisions shape the whole module.

**Failures become results, not exceptions.** Arch §45 requires one broken
upstream to leave the rest of the hub working. So a `HubError` is rendered as a
`CallToolResult` with `is_error=True` carrying a stable machine-readable code —
the agent learns `integration_unavailable` and can route around it, while the
connection and every other integration stay up. Unexpected exceptions are caught
too, logged with detail, and returned as `internal_error` without their text: a
stack trace is for the operator, not the model.

**Confirmation is a resumable exchange, not a flag.** A tool needing human
approval returns `InputRequiredResult` on the first round and only runs once the
answer comes back accepted (arch §53). The hub keeps no pending state — the SDK
seals it into `requestState` — so confirmations survive restarts and work across
replicas.

**Every path audits.** Allowed, denied, confirmed, rate-limited, and failed calls
all produce a record with the same shape (arch §24), because an audit trail with
holes in it is not one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import mcp.types as types

from app.audit.logger import AuditLogger
from app.core.context import Principal
from app.core.domain import AuditAction, ExposureMode, PolicyDecision
from app.core.errors import HubError, PolicyViolation, RateLimited
from app.core.logging import get_logger
from app.core.metrics import POLICY_DECISIONS, TOOL_CALLS, TOOL_LATENCY
from app.gateway.discovery import ToolDiscoverer
from app.gateway.tool_registry import ToolDescriptor, ToolRegistry
from app.gateway.tool_router import HUB_NAMESPACE, HubTools, SessionActivation
from app.integrations.manager import IntegrationManager
from app.policy.confirmation import (
    build_confirmation_request,
    client_supports_confirmation,
    read_confirmation,
)
from app.policy.engine import PolicyEngine, PolicyRequest

__all__ = ["Gateway", "ToolCallOutcome"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    """What a tool call produced, and whether the tool list changed."""

    result: types.CallToolResult | types.InputRequiredResult
    tools_changed: bool = False


class Gateway:
    """Routes tool calls from agents to upstream MCP servers."""

    def __init__(
        self,
        *,
        manager: IntegrationManager,
        registry: ToolRegistry,
        policy: PolicyEngine,
        audit: AuditLogger,
        discoverer: ToolDiscoverer,
        exposure_mode: ExposureMode = ExposureMode.SELECTIVE,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._policy = policy
        self._audit = audit
        self._discoverer = discoverer
        self._exposure_mode = exposure_mode
        self._hub_tools = HubTools(manager=manager, registry=registry, discoverer=discoverer)

    @property
    def exposure_mode(self) -> ExposureMode:
        """How many tool schemas agents currently see (arch §13)."""
        return self._exposure_mode

    def set_exposure_mode(self, mode: ExposureMode) -> None:
        """Change the exposure mode at runtime."""
        self._exposure_mode = mode
        log.info("gateway.exposure_mode_changed", mode=mode.value)

    @property
    def registry(self) -> ToolRegistry:
        """The tool registry, for status surfaces."""
        return self._registry

    @property
    def manager(self) -> IntegrationManager:
        """The integration manager, for status surfaces."""
        return self._manager

    # ------------------------------------------------------------- tools/list

    def list_tools(self, *, principal: Principal, activation: SessionActivation) -> list[types.Tool]:
        """The tools this caller should see (arch §11, §13).

        Policy is evaluated per caller, so two agents with different scopes get
        different lists from the same registry.
        """
        tools = self._hub_tools.exposed_tools(self._exposure_mode, activation)
        log.debug(
            "gateway.tools_listed",
            principal=principal.subject or "anonymous",
            mode=self._exposure_mode.value,
            count=len(tools),
        )
        return tools

    # ------------------------------------------------------------- tools/call

    async def call_tool(
        self,
        params: types.CallToolRequestParams,
        *,
        principal: Principal,
        activation: SessionActivation,
        session: Any = None,
    ) -> ToolCallOutcome:
        """Run one tool call through the full pipeline.

        Args:
            params: The incoming `tools/call` parameters, including any
                confirmation answer from a previous round.
            principal: The authenticated caller.
            activation: This session's discovery-mode activations.
            session: The MCP `ServerSession`, used to check whether the client
                can be shown a confirmation prompt.

        Returns:
            The result to send back, and whether the tool list changed.
        """
        started = time.monotonic()
        qualified = params.name
        arguments = dict(params.arguments or {})

        try:
            if self._hub_tools.owns(qualified):
                result, changed = await self._hub_tools.call(
                    qualified, arguments, principal=principal, activation=activation
                )
                self._audit.emit(
                    AuditAction.TOOL_CALL,
                    "success",
                    integration=HUB_NAMESPACE,
                    tool=qualified,
                    duration_ms=(time.monotonic() - started) * 1000,
                    raw_arguments=arguments,
                )
                TOOL_CALLS.labels(HUB_NAMESPACE, qualified, "success").inc()
                return ToolCallOutcome(result=result, tools_changed=changed)

            return await self._call_integration_tool(
                params, qualified, arguments, principal=principal, session=session, started=started
            )

        except HubError as exc:
            return ToolCallOutcome(result=self._error_result(exc, qualified, arguments, started))
        except Exception as exc:
            log.exception("gateway.unhandled_error", tool=qualified, error=str(exc))
            self._audit.emit(
                AuditAction.TOOL_CALL,
                "error",
                tool=qualified,
                error_code="internal_error",
                message=f"{type(exc).__name__}",
                duration_ms=(time.monotonic() - started) * 1000,
                raw_arguments=arguments,
            )
            TOOL_CALLS.labels("unknown", qualified, "error").inc()
            # The exception text is deliberately withheld: it can carry paths,
            # queries, or values the caller should not see.
            return ToolCallOutcome(
                result=_error(
                    "internal_error",
                    "The hub failed to process this call. The failure has been logged.",
                )
            )

    async def _call_integration_tool(
        self,
        params: types.CallToolRequestParams,
        qualified: str,
        arguments: dict[str, Any],
        *,
        principal: Principal,
        session: Any,
        started: float,
    ) -> ToolCallOutcome:
        """The main path: policy, confirmation, upstream, audit."""
        descriptor = self._registry.get(qualified)
        integration = await self._manager.require_serving(descriptor.integration_id)

        request = PolicyRequest(
            principal=principal,
            integration_id=integration.id,
            namespace=integration.namespace,
            tool_name=descriptor.tool_name,
            qualified_name=qualified,
            risk=descriptor.risk,
            arguments=arguments,
            manifest_confirmation_verbs=integration.manifest.confirmation_required,
        )

        try:
            outcome = await self._policy.evaluate(request, integration.policy)
        except RateLimited as exc:
            self._audit.emit(
                AuditAction.POLICY_VIOLATION,
                "denied",
                integration=integration.id,
                tool=qualified,
                risk_level=descriptor.risk,
                error_code=exc.code,
                message=exc.message,
                duration_ms=(time.monotonic() - started) * 1000,
                raw_arguments=arguments,
            )
            TOOL_CALLS.labels(integration.id, qualified, "denied").inc()
            raise

        POLICY_DECISIONS.labels(integration.id, outcome.decision.value, outcome.rule).inc()

        if outcome.decision is PolicyDecision.DENY:
            self._audit.emit(
                AuditAction.POLICY_VIOLATION,
                "denied",
                integration=integration.id,
                tool=qualified,
                risk_level=descriptor.risk,
                decision=outcome.decision,
                error_code="policy_violation",
                message=outcome.reason,
                context={"rule": outcome.rule},
                duration_ms=(time.monotonic() - started) * 1000,
                raw_arguments=arguments,
            )
            TOOL_CALLS.labels(integration.id, qualified, "denied").inc()
            raise PolicyViolation(outcome.reason, tool=qualified, rule=outcome.rule)

        if outcome.decision is PolicyDecision.CONFIRM:
            pending = self._handle_confirmation(
                params,
                descriptor,
                arguments,
                reason=outcome.reason,
                session=session,
                started=started,
            )
            if pending is not None:
                return pending

        return await self._forward(
            descriptor, integration.id, arguments, principal=principal, qualified=qualified, started=started
        )

    # ----------------------------------------------------------- confirmation

    def _handle_confirmation(
        self,
        params: types.CallToolRequestParams,
        descriptor: ToolDescriptor,
        arguments: dict[str, Any],
        *,
        reason: str,
        session: Any,
        started: float,
    ) -> ToolCallOutcome | None:
        """Ask for, or read, a human confirmation (arch §53).

        Returns:
            An outcome to return immediately — either the elicitation to send or
            a refusal — or `None` when the call has been approved and should
            proceed.
        """
        qualified = descriptor.qualified_name
        verdict = read_confirmation(params, qualified_name=qualified)

        if not verdict.responded:
            if session is None or not client_supports_confirmation(session):
                # No way to ask, so no way to approve. Never treated as consent —
                # this is the bypass arch §53 exists to close.
                self._audit.emit(
                    AuditAction.CONFIRMATION_DENIED,
                    "denied",
                    integration=descriptor.integration_id,
                    tool=qualified,
                    risk_level=descriptor.risk,
                    error_code="confirmation_required",
                    message="Client cannot display a confirmation prompt.",
                    duration_ms=(time.monotonic() - started) * 1000,
                    raw_arguments=arguments,
                )
                TOOL_CALLS.labels(descriptor.integration_id, qualified, "denied").inc()
                return ToolCallOutcome(
                    result=_error(
                        "confirmation_required",
                        f"{qualified} requires human confirmation ({reason}), but this client "
                        "does not support elicitation. Connect with a client that does, or ask "
                        "an operator to permit this tool explicitly in config/policies.yaml.",
                    )
                )

            self._audit.emit(
                AuditAction.CONFIRMATION_REQUESTED,
                "pending",
                integration=descriptor.integration_id,
                tool=qualified,
                risk_level=descriptor.risk,
                decision=PolicyDecision.CONFIRM,
                message=reason,
                raw_arguments=arguments,
            )
            TOOL_CALLS.labels(descriptor.integration_id, qualified, "confirm_required").inc()
            return ToolCallOutcome(
                result=build_confirmation_request(
                    qualified_name=qualified,
                    risk=descriptor.risk,
                    reason=reason,
                    arguments=arguments,
                    integration=descriptor.integration_id,
                )
            )

        if not verdict.accepted:
            self._audit.emit(
                AuditAction.CONFIRMATION_DENIED,
                "denied",
                integration=descriptor.integration_id,
                tool=qualified,
                risk_level=descriptor.risk,
                decision=PolicyDecision.CONFIRM,
                error_code="confirmation_declined",
                message=verdict.reason,
                duration_ms=(time.monotonic() - started) * 1000,
                raw_arguments=arguments,
            )
            TOOL_CALLS.labels(descriptor.integration_id, qualified, "denied").inc()
            return ToolCallOutcome(result=_error("confirmation_declined", verdict.reason))

        log.info("gateway.confirmed", tool=qualified, integration=descriptor.integration_id)
        return None

    # --------------------------------------------------------------- forward

    async def _forward(
        self,
        descriptor: ToolDescriptor,
        integration_id: str,
        arguments: dict[str, Any],
        *,
        principal: Principal,
        qualified: str,
        started: float,
    ) -> ToolCallOutcome:
        """Send the call upstream and record what happened (arch §24).

        The upstream tool name is used on the wire — the namespace exists for the
        agent's benefit and would be meaningless to the upstream server.
        """
        async with self._manager.session_for(integration_id, principal) as session:
            result = await session.call_tool(descriptor.tool_name, arguments)

        elapsed_ms = (time.monotonic() - started) * 1000
        TOOL_LATENCY.labels(integration_id, qualified).observe(elapsed_ms / 1000)

        if result.is_error:
            # An upstream tool reporting failure is a normal result, not a hub
            # error: the model should see it and adapt.
            self._audit.emit(
                AuditAction.TOOL_CALL,
                "error",
                integration=integration_id,
                tool=qualified,
                risk_level=descriptor.risk,
                decision=PolicyDecision.ALLOW,
                error_code="upstream_tool_error",
                message=_first_text(result)[:500],
                duration_ms=elapsed_ms,
                raw_arguments=arguments,
            )
            TOOL_CALLS.labels(integration_id, qualified, "error").inc()
        else:
            self._audit.emit(
                AuditAction.TOOL_CALL,
                "success",
                integration=integration_id,
                tool=qualified,
                risk_level=descriptor.risk,
                decision=PolicyDecision.ALLOW,
                duration_ms=elapsed_ms,
                raw_arguments=arguments,
            )
            TOOL_CALLS.labels(integration_id, qualified, "success").inc()

        return ToolCallOutcome(result=result)

    # ---------------------------------------------------------------- errors

    def _error_result(
        self, exc: HubError, qualified: str, arguments: dict[str, Any], started: float
    ) -> types.CallToolResult:
        """Render a hub error as a tool result (arch §45).

        Policy denials are already audited by the code that raised them; anything
        else is recorded here so no failure path goes unlogged.
        """
        if exc.code not in ("policy_violation", "rate_limited"):
            self._audit.emit(
                AuditAction.UPSTREAM_ERROR if exc.code == "upstream_error" else AuditAction.TOOL_CALL,
                "error",
                integration=str(exc.details.get("integration") or "") or None,
                tool=qualified,
                error_code=exc.code,
                message=exc.message,
                duration_ms=(time.monotonic() - started) * 1000,
                raw_arguments=arguments,
            )
            TOOL_CALLS.labels(str(exc.details.get("integration") or "unknown"), qualified, "error").inc()

        message = exc.message
        if isinstance(exc, RateLimited) and exc.retry_after_seconds:
            message = f"{message} (retry after {exc.retry_after_seconds:.1f}s)"
        return _error(exc.code, message, details=exc.details)


# --------------------------------------------------------------------------- helpers


def _error(code: str, message: str, *, details: dict[str, Any] | None = None) -> types.CallToolResult:
    """Build an error result carrying a machine-readable code.

    The code goes in `structured_content` as well as the text, so a programmatic
    client can branch on `integration_unavailable` without parsing prose.
    """
    payload: dict[str, Any] = {"error": code, "message": message}
    if details:
        payload["details"] = details
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{code}: {message}")],
        structured_content=payload,
        is_error=True,
    )


def _first_text(result: types.CallToolResult) -> str:
    """The first text block of a result, for audit messages."""
    for block in result.content:
        if isinstance(block, types.TextContent):
            return block.text
    return ""
