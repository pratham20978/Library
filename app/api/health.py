"""Health, readiness, metrics, and tool listing (arch §26, §44).

Three probes with three different meanings, which matters to a load balancer:

* `/health` — the process is alive. Never touches a dependency, so it stays fast
  and cannot be made to fail by a downstream outage. A Kubernetes liveness probe
  that fails when Postgres blips restarts a perfectly healthy pod.
* `/ready` — the process can serve. Checks the database and that the gateway has
  a tool registry. A hub whose integrations are all down is still *ready*: it
  serves `hub.*` and reports the outage, which is arch §45's whole point.
* `/api/health` — per-integration status (arch §25), the operator-facing view.

These are unauthenticated by design: an orchestrator probes before it has a
credential, and none of them disclose anything an attacker could use. Per-integration
detail behind `/api/health` is scoped.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import Runtime, require
from app.auth.permissions import SCOPE_INTEGRATIONS_READ
from app.core.logging import get_logger
from app.core.metrics import CONTENT_TYPE, render_metrics

__all__ = ["router"]

log = get_logger(__name__)

router = APIRouter(tags=["health"])

ReadAccess = Annotated[Any, Depends(require(SCOPE_INTEGRATIONS_READ))]


@router.get("/health", summary="Liveness probe")
async def liveness() -> dict[str, Any]:
    """The process is running. Deliberately checks nothing else."""
    from app import __version__

    return {"status": "ok", "version": __version__}


@router.get("/ready", summary="Readiness probe")
async def readiness(runtime: Runtime, response: Response) -> dict[str, Any]:
    """Whether this process can serve traffic.

    Reports `503` only for hub-level problems. Unhealthy *integrations* are
    reported, not fatal — the hub still serves every other one (arch §45).
    """
    database_ok = await runtime.database.ping()
    integrations = runtime.manager.health_snapshot()
    serving = sum(1 for report in integrations.values() if report.is_serving)

    ready = database_ok
    if not ready:
        response.status_code = 503

    return {
        "ready": ready,
        "database": "ok" if database_ok else "unavailable",
        "redis": "connected" if runtime.redis is not None else "not configured",
        "tools": len(runtime.registry),
        "integrations": {"total": len(integrations), "serving": serving},
    }


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics(runtime: Runtime) -> Response:
    """Prometheus exposition format (arch §44)."""
    if not runtime.settings.metrics_enabled:
        return Response(status_code=404)

    from app.core.metrics import AUDIT_DROPPED, SESSION_COUNT

    # Refresh the gauges that only this endpoint can sample cheaply.
    SESSION_COUNT.set(len(runtime.pool.describe()))
    AUDIT_DROPPED.set(runtime.audit.dropped_events)
    return Response(content=render_metrics(), media_type=CONTENT_TYPE)


@router.get("/api/health", summary="Integration health", tags=["integrations"])
async def integration_health(
    runtime: Runtime,
    _: ReadAccess,
    probe: bool = Query(default=True, description="Contact upstreams. Set false to read cached status."),
) -> dict[str, Any]:
    """`GET /api/health` — per-integration status (arch §25, §26)."""
    if probe:
        statuses = await runtime.lifecycle.health()
    else:
        statuses = {
            integration_id: {
                "status": report.status.value,
                "detail": report.detail,
                "latency_ms": report.latency_ms,
                "tools": report.tool_count,
            }
            for integration_id, report in sorted(runtime.manager.health_snapshot().items())
        }
    healthy = sum(1 for item in statuses.values() if item["status"] == "HEALTHY")
    return {"integrations": statuses, "healthy": healthy, "total": len(statuses)}


@router.get("/api/tools", summary="List registered tools", tags=["integrations"])
async def list_tools(
    runtime: Runtime,
    _: ReadAccess,
    integration: str | None = Query(default=None, description="Restrict to one integration."),
) -> dict[str, Any]:
    """`GET /api/tools` — the tool registry (arch §26, §51)."""
    tools = runtime.registry.for_integration(integration) if integration else runtime.registry.all()
    return {
        "tools": [item.summary() for item in tools],
        "count": len(tools),
        "exposure_mode": runtime.gateway.exposure_mode.value,
    }


@router.get("/api/status", summary="Hub status", tags=["integrations"])
async def hub_status(runtime: Runtime, _: ReadAccess) -> dict[str, Any]:
    """A single view of what the hub is doing right now.

    Shares its implementation with `mcp-hub status` (arch §72).
    """
    return runtime.status_payload()
