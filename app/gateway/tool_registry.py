"""The tool registry (arch §11, §51, §52).

Everything the hub knows about a tool lives in one `ToolDescriptor`: what the
upstream said about it, what the hub concluded about its risk, and whether policy
lets it through. The registry indexes those by qualified name, which is what
routing, exposure, and the `mcp-hub tools` output all read.

Two properties matter more than the data structure:

* **Replacement is atomic per integration.** Rediscovery swaps a whole
  integration's tool set in one assignment. A tools/list arriving mid-refresh
  sees the old set or the new one, never a half-populated mix where some of an
  upstream's tools have vanished.
* **A qualified name maps to exactly one integration.** Namespaces are validated
  as unique when the catalog loads, so `jira.search` can only ever mean one
  thing. Collisions are refused at configuration time rather than resolved by
  whichever integration was discovered last (arch §11).

The registry holds no credentials and performs no I/O — it is a projection of
what discovery found, and rebuilding it is always safe.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import mcp.types as types

from app.core.clock import utcnow
from app.core.domain import ExposureMode, RiskLevel
from app.core.errors import ToolNotFound
from app.core.ids import parse_qualified_name, qualify
from app.core.logging import get_logger

__all__ = ["ToolDescriptor", "ToolRegistry"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """One discovered tool, with the hub's own judgements attached (arch §51)."""

    integration_id: str
    namespace: str
    tool_name: str
    """Name as the upstream reports it — what gets sent back on `tools/call`."""

    qualified_name: str
    """Name agents see, `<namespace>.<tool_name>` (arch §11)."""

    tool: types.Tool
    """The upstream's own definition, kept verbatim for schema fidelity."""

    risk: RiskLevel
    risk_source: str
    """`manifest`, `annotation`, `heuristic`, or `default` (arch §52)."""

    requires_confirmation: bool
    """Whether policy or the manifest demands a human accept (arch §53)."""

    policy_allowed: bool = True
    """Whether policy would permit this tool at all. Drives SELECTIVE exposure."""

    enabled: bool = True
    """Whether the integration is enabled and healthy enough to route to."""

    discovered_at: Any = field(default_factory=utcnow)

    @property
    def description(self) -> str:
        """The upstream's description, or an empty string."""
        return self.tool.description or ""

    def as_exposed_tool(self) -> types.Tool:
        """Render for `tools/list`, under the qualified name (arch §11).

        The description gains a short annotation when confirmation is required,
        so a model can anticipate the extra round trip instead of treating the
        elicitation as a failure.
        """
        description = self.description
        if self.requires_confirmation:
            note = "Requires human confirmation before it runs."
            description = f"{description}\n\n{note}" if description else note
        return self.tool.model_copy(update={"name": self.qualified_name, "description": description or None})

    def to_row(self) -> dict[str, Any]:
        """Column values for the `tool_registry` table (arch §51)."""
        return {
            "integration_id": self.integration_id,
            "tool_name": self.tool_name,
            "qualified_name": self.qualified_name,
            "title": self.tool.title,
            "description": self.description or None,
            "input_schema": self.tool.input_schema,
            "output_schema": self.tool.output_schema,
            "annotations": (
                self.tool.annotations.model_dump(mode="json", exclude_none=True) if self.tool.annotations else None
            ),
            "risk_level": self.risk,
            "risk_source": self.risk_source,
            "enabled": self.enabled,
            "requires_confirmation": self.requires_confirmation,
        }

    def summary(self) -> dict[str, Any]:
        """Compact form for the REST API and `mcp-hub tools`."""
        return {
            "qualified_name": self.qualified_name,
            "integration": self.integration_id,
            "tool": self.tool_name,
            "risk": self.risk.value,
            "risk_source": self.risk_source,
            "requires_confirmation": self.requires_confirmation,
            "allowed": self.policy_allowed,
            "enabled": self.enabled,
            "description": (self.description[:160] + "…") if len(self.description) > 160 else self.description,
        }


class ToolRegistry:
    """Indexes discovered tools by qualified name and by integration."""

    def __init__(self) -> None:
        self._by_integration: dict[str, tuple[ToolDescriptor, ...]] = {}
        self._index: dict[str, ToolDescriptor] = {}

    # ---------------------------------------------------------------- writes

    def replace_integration(self, integration_id: str, tools: Sequence[ToolDescriptor]) -> None:
        """Swap in a whole integration's tool set (arch §12 `refresh_tools`)."""
        ordered = tuple(sorted(tools, key=lambda item: item.qualified_name))
        self._by_integration[integration_id] = ordered
        self._reindex()
        log.info("tool_registry.updated", integration=integration_id, tools=len(ordered))

    def remove_integration(self, integration_id: str) -> int:
        """Drop an integration's tools, e.g. on disable or removal."""
        removed = self._by_integration.pop(integration_id, ())
        if removed:
            self._reindex()
            log.info("tool_registry.removed", integration=integration_id, tools=len(removed))
        return len(removed)

    def set_enabled(self, integration_id: str, enabled: bool) -> None:
        """Flip routability for an integration without discarding what was discovered.

        Keeping the descriptors means re-enabling is instant and `mcp-hub tools`
        can still show what a disabled integration *would* expose.
        """
        current = self._by_integration.get(integration_id)
        if not current:
            return
        self._by_integration[integration_id] = tuple(replace(item, enabled=enabled) for item in current)
        self._reindex()

    def _reindex(self) -> None:
        index: dict[str, ToolDescriptor] = {}
        for tools in self._by_integration.values():
            for descriptor in tools:
                index[descriptor.qualified_name] = descriptor
        self._index = index

    # ----------------------------------------------------------------- reads

    def get(self, qualified_name: str) -> ToolDescriptor:
        """Resolve a qualified name to its descriptor.

        Raises:
            ToolNotFound: No such tool, or its integration is not routable. The
                message deliberately does not distinguish the two: an agent
                probing for hidden tools should learn nothing from the difference.
        """
        descriptor = self._index.get(qualified_name)
        if descriptor is None or not descriptor.enabled:
            raise ToolNotFound(
                f"No tool named {qualified_name!r} is available.",
                requested=qualified_name,
            )
        return descriptor

    def find(self, qualified_name: str) -> ToolDescriptor | None:
        """Look up without raising, including disabled tools."""
        return self._index.get(qualified_name)

    def for_integration(self, integration_id: str) -> tuple[ToolDescriptor, ...]:
        """Every tool discovered for one integration."""
        return self._by_integration.get(integration_id, ())

    def all(self) -> list[ToolDescriptor]:
        """Every known tool, in qualified-name order."""
        return sorted(self._index.values(), key=lambda item: item.qualified_name)

    def integrations(self) -> tuple[str, ...]:
        """Integrations with discovered tools."""
        return tuple(sorted(self._by_integration))

    def exposed(self, mode: ExposureMode) -> list[types.Tool]:
        """The tool list an agent should see, per the exposure mode (arch §13).

        `DISCOVERY` returns nothing here — the router injects its own small set
        of navigation tools instead, which is the whole point of that mode.
        """
        match mode:
            case ExposureMode.FULL:
                candidates = [item for item in self.all() if item.enabled]
            case ExposureMode.SELECTIVE:
                candidates = [item for item in self.all() if item.enabled and item.policy_allowed]
            case ExposureMode.DISCOVERY:
                candidates = []
        return [item.as_exposed_tool() for item in candidates]

    def digest(self, integration_id: str | None = None) -> str:
        """A stable digest of the tool set, for change detection (arch §15).

        Covers names, schemas, and risk, so an update that silently changes a
        tool's input schema is as visible as one that adds or removes a tool.
        """
        scope = self._by_integration.get(integration_id, ()) if integration_id is not None else tuple(self.all())
        payload = [
            {
                "name": item.qualified_name,
                "schema": item.tool.input_schema,
                "risk": item.risk.value,
            }
            for item in sorted(scope, key=lambda item: item.qualified_name)
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def diff(self, integration_id: str, previous: Iterable[str]) -> tuple[list[str], list[str]]:
        """Compare current tool names against a previous set (arch §15, §16).

        Returns:
            `(added, removed)` qualified names, so an update plan can show what
            actually changed rather than just that something did.
        """
        current = {item.qualified_name for item in self.for_integration(integration_id)}
        before = set(previous)
        return sorted(current - before), sorted(before - current)

    def __len__(self) -> int:
        return len(self._index)

    def __iter__(self) -> Iterator[ToolDescriptor]:
        return iter(self.all())

    def __contains__(self, qualified_name: object) -> bool:
        return qualified_name in self._index


def build_descriptor(
    *,
    integration_id: str,
    namespace: str,
    tool: types.Tool,
    risk: RiskLevel,
    risk_source: str,
    requires_confirmation: bool,
    policy_allowed: bool,
    enabled: bool,
) -> ToolDescriptor:
    """Assemble a descriptor, validating the qualified name (arch §11).

    Raises:
        ValidationFailed: The upstream name or the resulting qualified name
            breaks the MCP tool-name rules — a 130-character qualified name would
            be rejected by clients, so it is caught here where it can be reported
            against the integration that produced it.
    """
    qualified = qualify(namespace, tool.name)
    return ToolDescriptor(
        integration_id=integration_id,
        namespace=namespace,
        tool_name=tool.name,
        qualified_name=qualified,
        tool=tool,
        risk=risk,
        risk_source=risk_source,
        requires_confirmation=requires_confirmation,
        policy_allowed=policy_allowed,
        enabled=enabled,
    )


def split_qualified(qualified_name: str) -> tuple[str, str]:
    """Split an agent-supplied name into namespace and upstream tool name.

    Raises:
        ToolNotFound: The name carries no namespace. Reported as "not found"
            rather than "malformed" so probing reveals nothing.
    """
    from app.core.errors import ValidationFailed

    try:
        parsed = parse_qualified_name(qualified_name)
    except ValidationFailed as exc:
        raise ToolNotFound(f"No tool named {qualified_name!r} is available.", requested=qualified_name) from exc
    return parsed.namespace, parsed.tool
