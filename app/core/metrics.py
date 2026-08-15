"""Prometheus metrics (arch §44).

Everything is registered at import time against the default registry, so a
metric exists at zero before its first observation — a dashboard querying
`mcp_hub_tool_calls_total` should see `0`, not an empty result, while the hub is
idle.

Label cardinality is bounded deliberately. `integration` and `tool` come from
the configured catalog and discovered tool set, which are finite and operator-
controlled. Nothing here is labelled by principal or by argument value; those are
unbounded and belong in the audit log, which is built to hold them.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

__all__ = [
    "AUDIT_DROPPED",
    "CONTENT_TYPE",
    "DISCOVERY_TOOLS",
    "INTEGRATION_HEALTH",
    "POLICY_DECISIONS",
    "SESSION_COUNT",
    "TOOL_CALLS",
    "TOOL_LATENCY",
    "UPDATE_RESULTS",
    "render_metrics",
]

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
"""Content type the `/metrics` endpoint must return."""

TOOL_CALLS = Counter(
    "mcp_hub_tool_calls_total",
    "Tool invocations routed through the hub.",
    labelnames=("integration", "tool", "outcome"),
)
"""`outcome` is `success`, `denied`, `error`, or `confirm_required`."""

TOOL_LATENCY = Histogram(
    "mcp_hub_tool_duration_seconds",
    "Wall-clock time for a tool call, measured at the hub boundary.",
    labelnames=("integration", "tool"),
    # Tuned for MCP: sub-second for local servers, tens of seconds for remote
    # services doing real work (Figma design context, Jira JQL over big projects).
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

POLICY_DECISIONS = Counter(
    "mcp_hub_policy_decisions_total",
    "Policy engine verdicts.",
    labelnames=("integration", "decision", "rule"),
)

INTEGRATION_HEALTH = Gauge(
    "mcp_hub_integration_healthy",
    "1 when an integration is serving, 0 otherwise.",
    labelnames=("integration", "status"),
)

SESSION_COUNT = Gauge(
    "mcp_hub_upstream_sessions",
    "Live upstream MCP sessions held by the pool.",
)

DISCOVERY_TOOLS = Gauge(
    "mcp_hub_tools_registered",
    "Tools currently registered per integration.",
    labelnames=("integration",),
)

UPDATE_RESULTS = Counter(
    "mcp_hub_updates_total",
    "Integration update attempts.",
    labelnames=("integration", "result"),
)
"""`result` is `succeeded`, `failed`, `rolled_back`, or `skipped`."""

AUDIT_DROPPED = Gauge(
    "mcp_hub_audit_events_dropped",
    "Audit events logged but never persisted, since process start.",
)


def render_metrics(registry: CollectorRegistry | None = None) -> bytes:
    """Render the current metrics in Prometheus text format."""
    if registry is None:
        return generate_latest()
    return generate_latest(registry)
