"""Administrative endpoints (arch §26, §29, §64).

Arch §64 requires these to be protected separately from tool access, so every
route demands a write-level scope and none of them is reachable with the
`tools:call` scope an agent would hold.

Three groups live here: batch update operations (arch §16, §17), job status
(arch §29), and audit/secret administration (arch §21, §24). Secrets are
write-only through this API — there is a `PUT` and a `DELETE` and deliberately no
`GET`, because arch §21 forbids returning credential material and an endpoint
that could is one misconfiguration away from doing so.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, status
from pydantic import BaseModel, Field, SecretStr

from app.api.deps import CurrentPrincipal, Runtime, require
from app.api.integrations import UpdateRequest
from app.auth.permissions import (
    SCOPE_ADMIN,
    SCOPE_AUDIT_READ,
    SCOPE_INTEGRATIONS_READ,
    SCOPE_INTEGRATIONS_WRITE,
    SCOPE_SECRETS_WRITE,
)
from app.core.domain import AuditAction, JobKind
from app.core.logging import get_logger
from app.core.redaction import Secret

__all__ = ["router"]

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["admin"])

WriteAccess = Annotated[Any, Depends(require(SCOPE_INTEGRATIONS_WRITE))]
ReadAccess = Annotated[Any, Depends(require(SCOPE_INTEGRATIONS_READ))]
AuditAccess = Annotated[Any, Depends(require(SCOPE_AUDIT_READ))]
SecretAccess = Annotated[Any, Depends(require(SCOPE_SECRETS_WRITE))]
AdminAccess = Annotated[Any, Depends(require(SCOPE_ADMIN))]


class SecretRequest(BaseModel):
    """Body for storing a credential (arch §21)."""

    value: SecretStr = Field(description="The credential. Never echoed back.")
    principal: str | None = Field(
        default=None,
        description="Store as this user's own credential (arch §20). Omit for a deployment-wide one.",
    )
    integration: str | None = Field(
        default=None,
        description="Record sole ownership, so removal can clean it up (arch §18).",
    )


# --------------------------------------------------------------------------- updates


@router.post("/update", status_code=status.HTTP_202_ACCEPTED, summary="Update selected integrations")
async def update_selected(
    runtime: Runtime,
    _: WriteAccess,
    body: Annotated[UpdateRequest, Body()] = UpdateRequest(),
) -> dict[str, Any]:
    """`POST /api/update` — update named integrations (arch §26)."""
    if body.dry_run:
        plan = await runtime.updater.plan(names=body.integrations, all_integrations=body.all, exclude=body.exclude)
        return {"dry_run": True, "plan": plan.to_payload(), "rendered": plan.render()}

    job = await runtime.jobs.submit(
        JobKind.UPDATE,
        integrations=tuple(body.integrations),
        parameters={
            "all": body.all,
            "exclude": body.exclude,
            "force": body.force,
            "rollback_on_failure": body.rollback_on_failure,
        },
    )
    return {"job": job.to_payload(), "poll": f"/api/jobs/{job.id}"}


@router.post("/update/all", status_code=status.HTTP_202_ACCEPTED, summary="Update every integration")
async def update_all(
    runtime: Runtime,
    _: WriteAccess,
    exclude: list[str] = Query(default=[], description="Integrations to leave alone (arch §17)."),
    dry_run: bool = Query(default=False, description="Produce the plan and change nothing."),
) -> dict[str, Any]:
    """`POST /api/update/all` (arch §16, §17, §26)."""
    if dry_run:
        plan = await runtime.updater.plan(all_integrations=True, exclude=exclude)
        return {"dry_run": True, "plan": plan.to_payload(), "rendered": plan.render()}

    job = await runtime.jobs.submit(JobKind.UPDATE, parameters={"all": True, "exclude": exclude})
    return {"job": job.to_payload(), "poll": f"/api/jobs/{job.id}"}


@router.post("/reconcile", summary="Reconcile desired and actual state")
async def reconcile(runtime: Runtime, principal: CurrentPrincipal, _: WriteAccess) -> dict[str, Any]:
    """`POST /api/reconcile` (arch §63)."""
    report = await runtime.lifecycle.reconcile(principal=principal)
    return report.to_payload()


@router.post("/refresh", summary="Rediscover tools")
async def refresh_tools(
    runtime: Runtime,
    principal: CurrentPrincipal,
    _: WriteAccess,
    integrations: list[str] = Query(default=[], description="Restrict to these integrations."),
) -> dict[str, Any]:
    """Re-read tool definitions from upstreams (arch §12)."""
    return await runtime.lifecycle.refresh(integrations=integrations or None, principal=principal)


# --------------------------------------------------------------------------- jobs


@router.get("/jobs/{job_id}", summary="Job status")
async def job_status(job_id: str, runtime: Runtime, _: ReadAccess) -> dict[str, Any]:
    """`GET /api/jobs/{job_id}` (arch §29).

    Raises:
        IntegrationNotFound: No such job.
    """
    job = await runtime.jobs.get(job_id)
    if job is None:
        from app.core.errors import HubError

        raise HubError(f"No job with id {job_id!r}.", job=job_id) from None
    return job.to_payload()


@router.get("/jobs", summary="Recent jobs")
async def recent_jobs(
    runtime: Runtime,
    _: ReadAccess,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Recently submitted jobs, newest first."""
    return {"jobs": await runtime.jobs.recent(limit=limit)}


# --------------------------------------------------------------------------- audit


@router.get("/audit", summary="Read the audit trail")
async def read_audit(
    runtime: Runtime,
    _: AuditAccess,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user_id: str | None = Query(default=None),
    integration: str | None = Query(default=None),
    action: AuditAction | None = Query(default=None),
    since: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """`GET /api/audit` (arch §24, §26)."""
    records = await runtime.audit.query(
        limit=limit,
        offset=offset,
        user_id=user_id,
        integration=integration,
        action=action,
        since=since,
    )
    return {
        "records": [
            {
                "id": record.id,
                "timestamp": record.timestamp.isoformat(),
                "request_id": record.request_id,
                "action": record.action,
                "status": record.status,
                "user_id": record.user_id,
                "integration": record.integration,
                "tool": record.tool,
                "risk_level": record.risk_level,
                "decision": record.decision,
                "duration_ms": record.duration_ms,
                "error_code": record.error_code,
                "message": record.message,
                # Already redacted when the event was built (arch §24).
                "arguments": record.arguments,
                "context": record.context,
            }
            for record in records
        ],
        "count": len(records),
    }


# --------------------------------------------------------------------------- secrets


@router.put("/admin/secrets/{name}", summary="Store a credential")
async def store_secret(
    name: str,
    runtime: Runtime,
    _: SecretAccess,
    body: Annotated[SecretRequest, Body()],
) -> dict[str, Any]:
    """Store a credential (arch §20, §21).

    There is no matching `GET`. Credentials leave the hub only into an upstream
    request, never into an API response.
    """
    provider = await runtime.secrets.store(
        name,
        Secret(body.value.get_secret_value()),
        principal=body.principal,
        integration=body.integration,
    )
    log.info(
        "api.secret_stored",
        secret=name,
        provider=provider,
        per_user=bool(body.principal),
        integration=body.integration,
    )
    return {"stored": name, "provider": provider, "per_user": bool(body.principal)}


@router.delete("/admin/secrets/{name}", summary="Delete a credential")
async def delete_secret(
    name: str,
    runtime: Runtime,
    _: SecretAccess,
    principal: str | None = Query(default=None, description="Delete this user's own credential."),
) -> dict[str, Any]:
    """Delete a credential (arch §21)."""
    removed = await runtime.secrets.delete(name, principal=principal)
    return {"deleted": name, "removed": removed}


@router.get("/admin/secrets", summary="List credential names")
async def list_secret_names(
    runtime: Runtime,
    _: SecretAccess,
    principal: str | None = Query(default=None),
) -> dict[str, Any]:
    """List which credentials exist. Names only, never values (arch §21)."""
    return {"providers": await runtime.secrets.describe(principal=principal)}


# --------------------------------------------------------------------------- config


@router.get("/admin/config", summary="Effective configuration")
async def read_config(runtime: Runtime, _: AdminAccess) -> dict[str, Any]:
    """The resolved configuration, with secrets omitted (arch §64).

    Renders from the same models the YAML is parsed into, so what an operator
    sees here is exactly what the hub is acting on.
    """
    catalog = runtime.manager.catalog
    return {
        "exposure_mode": catalog.exposure_mode.value,
        "require_authentication": catalog.policies.require_authentication,
        "integrations": {
            integration.id: {
                "enabled": integration.enabled,
                "namespace": integration.namespace,
                "source": integration.manifest.source.model_dump(mode="json", exclude_none=True),
                "trust": integration.trust.value,
                "update_policy": integration.update_policy.value,
                # Only whether a credential is required and where from — never a value.
                "auth": {
                    "type": integration.manifest.auth.type,
                    "per_user": integration.manifest.auth.is_per_user,
                    "secret_name": (
                        integration.manifest.auth.secret.name if integration.manifest.auth.secret else None
                    ),
                },
            }
            for integration in catalog.all()
        },
        "settings": {
            "env": runtime.settings.env,
            "database": "postgresql" if "postgres" in runtime.settings.database_url else "sqlite",
            "redis": runtime.redis is not None,
            "secret_providers": list(runtime.secrets.provider_names),
            "rollback_on_failure": runtime.settings.rollback_on_failure,
            "allow_parallel_updates": runtime.settings.allow_parallel_updates,
        },
    }


@router.post("/admin/reload", summary="Reload configuration from disk")
async def reload_config(runtime: Runtime, _: AdminAccess) -> dict[str, Any]:
    """Re-read the YAML contract without restarting (arch §63)."""
    catalog = await runtime.reload_configuration()
    return {"reloaded": True, "integrations": len(catalog), "exposure_mode": catalog.exposure_mode.value}


@router.get("/admin/doctor", summary="Run environment diagnostics")
async def doctor(runtime: Runtime, _: AdminAccess) -> dict[str, Any]:
    """`mcp-hub doctor` over HTTP (arch §36)."""
    checks = await runtime.lifecycle.doctor()
    return {
        "checks": [{"status": status_, "subject": subject, "detail": detail} for status_, subject, detail in checks],
        "ok": all(status_ != "FAIL" for status_, _, _ in checks),
    }
