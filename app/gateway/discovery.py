"""Dynamic tool discovery (arch §12).

Arch §12 forbids hard-coding third-party tool definitions, so the hub asks each
upstream what it can do and builds its registry from the answer:

    connect -> initialize -> tools/list -> classify -> namespace -> apply policy

Everything downstream depends on that being the *only* source of tool
definitions. When Atlassian ships a new tool, a refresh makes it appear; when one
disappears, a refresh removes it. No code change, no manifest edit.

Discovery is deliberately fault-tolerant. One unreachable upstream produces an
empty tool set for that integration and a recorded health status — never an
exception that aborts the sweep. Arch §45 requires the hub to keep serving
whatever else is healthy, and startup is exactly when a single misconfigured
integration would otherwise take everything down.

Which principal discovery runs as matters. Tool *lists* are usually identical
across users, so the default sweep uses deployment-wide credentials; an
integration whose credentials are strictly per-user is discovered lazily, on the
first call by a caller who actually has them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

import mcp.types as types

from app.config.catalog import ResolvedIntegration
from app.core.context import ANONYMOUS, Principal
from app.core.domain import HealthStatus, RiskLevel
from app.core.errors import HubError, ValidationFailed, describe_exception
from app.core.logging import get_logger
from app.gateway.tool_registry import ToolDescriptor, ToolRegistry, build_descriptor
from app.integrations.manager import IntegrationManager
from app.policy.classifier import classify_tool
from app.policy.engine import PolicyEngine, PolicyRequest

__all__ = ["DiscoveryResult", "ToolDiscoverer"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """What one integration's discovery produced."""

    integration_id: str
    tools: tuple[ToolDescriptor, ...]
    status: HealthStatus
    detail: str = ""
    skipped: tuple[str, ...] = ()
    """Upstream tools that could not be exposed, with the reason appended."""

    @property
    def ok(self) -> bool:
        """Whether discovery reached the upstream successfully."""
        return self.status is HealthStatus.HEALTHY


class ToolDiscoverer:
    """Discovers, classifies, and registers upstream tools."""

    def __init__(
        self,
        *,
        manager: IntegrationManager,
        registry: ToolRegistry,
        policy: PolicyEngine,
        concurrency: int = 8,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._policy = policy
        self._concurrency = concurrency

    @property
    def registry(self) -> ToolRegistry:
        """The registry discovery writes into, for before/after comparisons."""
        return self._registry

    # ------------------------------------------------------------------ sweep

    async def discover_all(self, *, principal: Principal | None = None) -> dict[str, DiscoveryResult]:
        """Discover every enabled integration, in parallel and fault-isolated."""
        targets = list(self._manager.enabled())
        limit = asyncio.Semaphore(self._concurrency)

        async def one(integration: ResolvedIntegration) -> DiscoveryResult:
            async with limit:
                return await self.discover(integration.id, principal=principal)

        outcomes = await asyncio.gather(*(one(item) for item in targets), return_exceptions=True)
        results: dict[str, DiscoveryResult] = {}
        for integration, outcome in zip(targets, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # `discover` already contains its own failures; reaching here
                # means something unexpected, which must still not abort startup.
                log.error("discovery.unexpected_failure", integration=integration.id, error=str(outcome))
                results[integration.id] = DiscoveryResult(
                    integration_id=integration.id,
                    tools=(),
                    status=HealthStatus.UNAVAILABLE,
                    detail=str(outcome),
                )
                continue
            results[integration.id] = outcome

        total = sum(len(item.tools) for item in results.values())
        healthy = sum(1 for item in results.values() if item.ok)
        log.info(
            "discovery.complete",
            integrations=len(results),
            healthy=healthy,
            unavailable=len(results) - healthy,
            tools=total,
        )
        return results

    async def discover(self, integration_id: str, *, principal: Principal | None = None) -> DiscoveryResult:
        """Discover one integration's tools and register them.

        Never raises for an upstream problem: the failure is recorded as a status
        on the result and the integration keeps its previous tool set rather than
        losing it to a transient outage.
        """
        integration = self._manager.get(integration_id)
        if not integration.enabled:
            self._registry.set_enabled(integration_id, False)
            return DiscoveryResult(integration_id, (), HealthStatus.DISABLED, "Integration is disabled.")

        try:
            async with self._manager.session_for(integration_id, principal) as session:
                upstream_tools = await session.list_tools()
        except HubError as exc:
            log.warning("discovery.failed", integration=integration_id, error=exc.message, code=exc.code)
            return DiscoveryResult(
                integration_id,
                self._registry.for_integration(integration_id),
                _status_for(exc),
                exc.message,
            )
        except Exception as exc:  # noqa: BLE001 - one upstream must not fail the hub (arch §45)
            log.error("discovery.error", integration=integration_id, error=str(exc))
            return DiscoveryResult(
                integration_id,
                self._registry.for_integration(integration_id),
                HealthStatus.UNAVAILABLE,
                describe_exception(exc),
            )

        descriptors, skipped = self._build(integration, upstream_tools, principal or ANONYMOUS)
        self._registry.replace_integration(integration_id, descriptors)
        if skipped:
            log.warning("discovery.tools_skipped", integration=integration_id, skipped=list(skipped))
        return DiscoveryResult(
            integration_id,
            tuple(descriptors),
            HealthStatus.HEALTHY,
            f"{len(descriptors)} tools",
            skipped=tuple(skipped),
        )

    # ------------------------------------------------------------ construction

    def _build(
        self,
        integration: ResolvedIntegration,
        upstream_tools: Sequence[types.Tool],
        principal: Principal,
    ) -> tuple[list[ToolDescriptor], list[str]]:
        """Classify and namespace what the upstream reported (arch §11, §52)."""
        descriptors: list[ToolDescriptor] = []
        skipped: list[str] = []
        manifest = integration.manifest

        for tool in upstream_tools:
            annotations = tool.annotations
            classification = classify_tool(
                tool.name,
                manifest=manifest,
                description=tool.description,
                read_only_hint=annotations.read_only_hint if annotations else None,
                destructive_hint=annotations.destructive_hint if annotations else None,
            )

            # Ask policy what it would do with this tool, without charging a rate
            # limit — this is exposure, not invocation.
            outcome = self._policy.preview(
                PolicyRequest(
                    principal=principal,
                    integration_id=integration.id,
                    namespace=integration.namespace,
                    tool_name=tool.name,
                    qualified_name=f"{integration.namespace}.{tool.name}",
                    risk=classification.risk,
                    arguments={},
                    manifest_confirmation_verbs=manifest.confirmation_required,
                ),
                integration.policy,
            )

            try:
                descriptors.append(
                    build_descriptor(
                        integration_id=integration.id,
                        namespace=integration.namespace,
                        tool=tool,
                        risk=classification.risk,
                        risk_source=classification.source,
                        requires_confirmation=outcome.needs_confirmation,
                        policy_allowed=outcome.allowed,
                        enabled=True,
                    )
                )
            except ValidationFailed as exc:
                # A name the MCP spec will not accept once namespaced. Skipping
                # one tool is right; failing the whole integration is not.
                skipped.append(f"{tool.name}: {exc.message}")

        return descriptors, skipped

    # ------------------------------------------------------------- refreshing

    async def refresh(self, integration_id: str, *, principal: Principal | None = None) -> DiscoveryResult:
        """Rediscover one integration (arch §12 `refresh_tools`).

        Sessions are dropped first so the refresh reflects a newly promoted
        version rather than talking to the process the old version left running.
        """
        await self._manager.disconnect(integration_id)
        return await self.discover(integration_id, principal=principal)

    def reclassify(self, principal: Principal) -> None:
        """Recompute policy verdicts for already-discovered tools.

        Policy is per-caller — scopes and principal restrictions differ between
        users — so what a given agent may see is decided against *their*
        identity, without re-contacting any upstream.
        """
        for integration_id in self._registry.integrations():
            try:
                integration = self._manager.get(integration_id)
            except HubError:
                continue
            updated: list[ToolDescriptor] = []
            for descriptor in self._registry.for_integration(integration_id):
                outcome = self._policy.preview(
                    PolicyRequest(
                        principal=principal,
                        integration_id=integration.id,
                        namespace=integration.namespace,
                        tool_name=descriptor.tool_name,
                        qualified_name=descriptor.qualified_name,
                        risk=descriptor.risk,
                        arguments={},
                        manifest_confirmation_verbs=integration.manifest.confirmation_required,
                    ),
                    integration.policy,
                )
                from dataclasses import replace

                updated.append(
                    replace(
                        descriptor,
                        policy_allowed=outcome.allowed,
                        requires_confirmation=outcome.needs_confirmation,
                    )
                )
            self._registry.replace_integration(integration_id, updated)


def _status_for(exc: HubError) -> HealthStatus:
    """Map a discovery failure onto the health vocabulary (arch §25)."""
    match exc.code:
        case "integration_disabled":
            return HealthStatus.DISABLED
        case "secret_not_found":
            return HealthStatus.AUTH_REQUIRED
        case _:
            return HealthStatus.UNAVAILABLE


def risk_of(descriptor: ToolDescriptor) -> RiskLevel:
    """The risk a descriptor carries. Present for symmetry with policy code."""
    return descriptor.risk
