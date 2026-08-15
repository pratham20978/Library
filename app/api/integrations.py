"""Integration management endpoints (arch §26).

Every route here delegates to `LifecycleService` or `UpdateManager` — the same
objects the CLI calls (arch §72). These handlers do three things and nothing
else: check the scope, translate HTTP into a service call, and shape the
response.

Operations that can take minutes go through the job queue rather than blocking
the request (arch §29). `update` and `install` return `202 Accepted` with a job
id; `enable`, `disable`, and `rollback` are fast enough to answer inline.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Response, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentPrincipal, Runtime, require
from app.auth.permissions import SCOPE_INTEGRATIONS_READ, SCOPE_INTEGRATIONS_WRITE
from app.core.domain import JobKind
from app.core.logging import get_logger

__all__ = ["router"]

log = get_logger(__name__)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

ReadAccess = Annotated[Any, Depends(require(SCOPE_INTEGRATIONS_READ))]
WriteAccess = Annotated[Any, Depends(require(SCOPE_INTEGRATIONS_WRITE))]


class UpdateRequest(BaseModel):
    """Body for an update request (arch §16, §59)."""

    dry_run: bool = Field(default=False, description="Produce the plan and change nothing.")
    force: bool = Field(default=False, description="Update even when already at the target version.")
    rollback_on_failure: bool | None = Field(
        default=None, description="Override the deployment's automatic-rollback setting."
    )
    exclude: list[str] = Field(default_factory=list, description="Integrations to leave alone (arch §17).")
    all: bool = Field(default=False, description="Update every enabled integration.")
    integrations: list[str] = Field(default_factory=list, description="Explicit integrations to update.")


class RollbackRequest(BaseModel):
    """Body for a rollback request (arch §19)."""

    version: str | None = Field(
        default=None, description="Version to restore. Defaults to the most recent rollback point."
    )


class RemoveRequest(BaseModel):
    """Body for a removal request (arch §18)."""

    purge_secrets: bool = Field(default=True, description="Delete credentials owned solely by this integration.")
    purge_backups: bool = Field(default=False, description="Also delete its rollback points.")


@router.get("", summary="List integrations")
async def list_integrations(
    runtime: Runtime,
    _: ReadAccess,
    enabled_only: bool = Query(default=False, description="Only integrations that are switched on."),
) -> dict[str, Any]:
    """`GET /api/integrations` (arch §26)."""
    rows = runtime.lifecycle.list_integrations(enabled_only=enabled_only)
    return {"integrations": [row.to_payload() for row in rows], "count": len(rows)}


@router.get("/{name}", summary="Describe one integration")
async def describe_integration(name: str, runtime: Runtime, _: ReadAccess) -> dict[str, Any]:
    """`GET /api/integrations/{name}` (arch §26)."""
    return runtime.lifecycle.describe(name)


@router.post("/{name}/enable", summary="Enable an integration")
async def enable_integration(
    name: str, runtime: Runtime, principal: CurrentPrincipal, _: WriteAccess
) -> dict[str, Any]:
    """`POST /api/integrations/{name}/enable` (arch §26)."""
    return await runtime.lifecycle.enable(name, principal=principal)


@router.post("/{name}/disable", summary="Disable an integration")
async def disable_integration(
    name: str, runtime: Runtime, principal: CurrentPrincipal, _: WriteAccess
) -> dict[str, Any]:
    """`POST /api/integrations/{name}/disable` (arch §26)."""
    return await runtime.lifecycle.disable(name, principal=principal)


@router.post("/{name}/update", status_code=status.HTTP_202_ACCEPTED, summary="Update an integration")
async def update_integration(
    name: str,
    runtime: Runtime,
    response: Response,
    _: WriteAccess,
    body: Annotated[UpdateRequest, Body()] = UpdateRequest(),
) -> dict[str, Any]:
    """`POST /api/integrations/{name}/update` (arch §26, §29).

    A dry run answers inline because it does no work. A real update is queued,
    since cloning and building outlast any reasonable HTTP timeout.
    """
    if body.dry_run:
        response.status_code = status.HTTP_200_OK
        plan = await runtime.updater.plan(names=[name])
        return {"dry_run": True, "plan": plan.to_payload(), "rendered": plan.render()}

    job = await runtime.jobs.submit(
        JobKind.UPDATE,
        integrations=(name,),
        parameters={
            "force": body.force,
            "rollback_on_failure": body.rollback_on_failure,
        },
    )
    return {"job": job.to_payload(), "poll": f"/api/jobs/{job.id}"}


@router.post("/{name}/rollback", summary="Roll back an integration")
async def rollback_integration(
    name: str,
    runtime: Runtime,
    principal: CurrentPrincipal,
    _: WriteAccess,
    body: Annotated[RollbackRequest, Body()] = RollbackRequest(),
) -> dict[str, Any]:
    """`POST /api/integrations/{name}/rollback` (arch §19, §26)."""
    return await runtime.lifecycle.rollback(name, version=body.version, principal=principal)


@router.get("/{name}/rollback-points", summary="List rollback points")
async def list_rollback_points(name: str, runtime: Runtime, _: ReadAccess) -> dict[str, Any]:
    """Available restore points for an integration (arch §19)."""
    return {"integration": name, "rollback_points": runtime.lifecycle.rollback_points(name)}


@router.post("/{name}/install", status_code=status.HTTP_202_ACCEPTED, summary="Install an integration")
async def install_integration(
    name: str,
    runtime: Runtime,
    _: WriteAccess,
    enable: bool = Query(default=True, description="Switch it on once installed."),
) -> dict[str, Any]:
    """Install an integration (arch §14, §29)."""
    job = await runtime.jobs.submit(JobKind.INSTALL, integrations=(name,), parameters={"enable": enable})
    return {"job": job.to_payload(), "poll": f"/api/jobs/{job.id}"}


@router.delete("/{name}", summary="Remove an integration")
async def remove_integration(
    name: str,
    runtime: Runtime,
    principal: CurrentPrincipal,
    _: WriteAccess,
    body: Annotated[RemoveRequest, Body()] = RemoveRequest(),
) -> dict[str, Any]:
    """`DELETE /api/integrations/{name}` (arch §18, §26).

    Runs inline rather than as a job: removal is fast, and an operator deleting
    something should see the outcome rather than a job id to chase.
    """
    return await runtime.lifecycle.remove(
        name,
        purge_secrets=body.purge_secrets,
        purge_backups=body.purge_backups,
        principal=principal,
    )


@router.post("/{name}/refresh", summary="Rediscover an integration's tools")
async def refresh_integration(
    name: str, runtime: Runtime, principal: CurrentPrincipal, _: WriteAccess
) -> dict[str, Any]:
    """Re-read tool definitions from one upstream (arch §12)."""
    return await runtime.lifecycle.refresh(integrations=[name], principal=principal)
