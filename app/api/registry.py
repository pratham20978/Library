"""MCP Registry endpoints (arch §7, §26, §56).

Searching and inspecting are read operations against an external directory.
*Installing* from that directory is not, and arch §56 puts a mandatory step
between them:

    registry -> metadata -> security inspection -> user confirmation
      -> staging install -> test -> enable

`GET /api/registry/search` and `/inspect` cover the first three. The install
endpoint refuses to proceed on a community-tier server unless the caller passes
`confirm=true`, having seen the disclosure — arch §7 requires explicit
confirmation before installing an unknown or community server, and a flag the
caller must consciously set is how that is expressed over HTTP.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import Runtime, require
from app.auth.permissions import SCOPE_INTEGRATIONS_READ, SCOPE_INTEGRATIONS_WRITE
from app.core.errors import ValidationFailed
from app.core.logging import get_logger

__all__ = ["router"]

log = get_logger(__name__)

router = APIRouter(prefix="/api/registry", tags=["registry"])

ReadAccess = Annotated[Any, Depends(require(SCOPE_INTEGRATIONS_READ))]
WriteAccess = Annotated[Any, Depends(require(SCOPE_INTEGRATIONS_WRITE))]


class RegistryInstallRequest(BaseModel):
    """Body for installing a server found in the registry (arch §56)."""

    integration_id: str | None = Field(
        default=None, description="Hub id to install under. Derived from the registry name when omitted."
    )
    confirm: bool = Field(
        default=False,
        description="Explicit acknowledgement of the disclosure. Required for community-tier servers.",
    )
    enable: bool = Field(default=False, description="Switch it on after installing.")


@router.get("/search", summary="Search the MCP Registry")
async def search(
    runtime: Runtime,
    _: ReadAccess,
    q: str = Query(default="", description="Search terms."),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, description="Pagination cursor."),
) -> dict[str, Any]:
    """`GET /api/registry/search?q=jira` (arch §7, §26)."""
    results = await runtime.registry_client.search_servers(q, limit=limit, cursor=cursor)
    return {
        "query": q,
        "servers": [
            {
                "name": server.name,
                "description": server.description,
                "version": server.version,
                "repository": server.repository_url,
                "deprecated": server.is_deprecated,
                "installable": server.preferred_installation() is not None,
            }
            for server in results.servers
        ],
        "count": len(results),
        "next_cursor": results.next_cursor,
    }


@router.get("/servers/{name:path}", summary="Inspect a registry entry")
async def inspect(name: str, runtime: Runtime, _: ReadAccess) -> dict[str, Any]:
    """The pre-install disclosure arch §54 requires, without installing."""
    disclosure = await runtime.registry_client.validate_server(name)
    return {
        **disclosure.to_payload(),
        "rendered": disclosure.render(),
        "manifest": (disclosure.manifest.model_dump(mode="json", exclude_none=True) if disclosure.manifest else None),
    }


@router.get("/servers/{name:path}/versions", summary="List published versions")
async def versions(name: str, runtime: Runtime, _: ReadAccess) -> dict[str, Any]:
    """Published versions of a registry entry (arch §7)."""
    return {"server": name, "versions": await runtime.registry_client.get_versions(name)}


@router.post("/install", status_code=status.HTTP_202_ACCEPTED, summary="Install from the registry")
async def install(
    runtime: Runtime,
    _: WriteAccess,
    name: str = Query(description="Canonical registry name of the server."),
    body: Annotated[RegistryInstallRequest, Body()] = RegistryInstallRequest(),
) -> dict[str, Any]:
    """Install a server from the registry (arch §56).

    Raises:
        ValidationFailed: The server is community-tier and `confirm` was not set.
            The response carries the disclosure so the caller can show it and
            retry with acknowledgement.
    """
    disclosure = await runtime.registry_client.validate_server(name, integration_id=body.integration_id)

    if disclosure.requires_confirmation and not body.confirm:
        raise ValidationFailed(
            f"{disclosure.server_name} is a community server and will run third-party code on this "
            "host. Review the disclosure and retry with confirm=true.",
            disclosure=disclosure.to_payload(),
        )

    assert disclosure.manifest is not None
    path = runtime.store.write_manifest(disclosure.manifest)
    await runtime.reload_configuration()
    log.info(
        "registry.manifest_written",
        server=disclosure.server_name,
        integration=disclosure.integration_id,
        path=str(path),
        trust=disclosure.trust.value,
    )

    from app.core.domain import JobKind

    job = await runtime.jobs.submit(
        JobKind.INSTALL,
        integrations=(disclosure.integration_id,),
        parameters={"enable": body.enable, "from_registry": disclosure.server_name},
    )
    return {
        "integration": disclosure.integration_id,
        "manifest": str(path),
        "disclosure": disclosure.to_payload(),
        "job": job.to_payload(),
        "poll": f"/api/jobs/{job.id}",
    }
