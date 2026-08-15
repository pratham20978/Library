"""The lifecycle service (arch §14, §18, §19, §57, §63, §72).

Arch §72 is the requirement this module exists to satisfy: *every command must
operate against the same underlying integration-management service used by the
REST API. Do not create separate business logic for CLI and API.* So `mcp-hub
enable jira` and `POST /api/integrations/jira/enable` both land here, and there
is only one implementation of what enabling means.

Each operation follows the same shape, because each one can leave the deployment
inconsistent if it half-succeeds:

    validate -> lock -> back up -> mutate config -> reconcile runtime -> audit

Ordering within removal is deliberate and follows arch §18 exactly: routing is
disabled *before* sessions are disconnected, and sessions before artifacts are
deleted, so no request can be mid-flight into something that no longer exists.
Credentials shared with other integrations are never deleted — arch §18 is
explicit, and the ownership recorded at write time is what makes that decision
possible rather than a guess.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.audit.logger import AuditLogger
from app.config.catalog import ResolvedCatalog
from app.config.models import Catalog, CatalogEntry, LockFile
from app.config.settings import Settings
from app.config.store import ConfigStore
from app.core.context import Principal, current_principal
from app.core.domain import AuditAction, HealthStatus, SourceType
from app.core.errors import HubError, IntegrationNotFound, RollbackFailed, ValidationFailed, describe_exception
from app.core.ids import new_job_id
from app.core.logging import get_logger
from app.gateway.discovery import ToolDiscoverer
from app.integrations.backup import BackupManager
from app.integrations.base import VersionRef
from app.integrations.locks import LockManager, integration_lock
from app.integrations.manager import IntegrationManager
from app.integrations.updater import UpdateManager

__all__ = ["IntegrationSummary", "LifecycleService", "ReconcileReport"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IntegrationSummary:
    """One row of `mcp-hub list` / `GET /api/integrations`."""

    id: str
    name: str
    namespace: str
    source_type: str
    trust: str
    enabled: bool
    installed: bool
    version: str | None
    health: str
    health_detail: str
    tool_count: int
    update_policy: str

    def to_payload(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "id": self.id,
            "name": self.name,
            "namespace": self.namespace,
            "source_type": self.source_type,
            "trust": self.trust,
            "enabled": self.enabled,
            "installed": self.installed,
            "version": self.version,
            "health": self.health,
            "health_detail": self.health_detail,
            "tools": self.tool_count,
            "update_policy": self.update_policy,
        }


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What reconciliation changed (arch §63)."""

    enabled: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    installed: tuple[str, ...] = ()
    refreshed: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()

    @property
    def changed(self) -> bool:
        """Whether anything actually differed from the desired state."""
        return bool(self.enabled or self.disabled or self.installed or self.refreshed)

    def render(self) -> str:
        """Operator-facing summary."""
        if not self.changed and not self.failed:
            return "Already reconciled — actual state matches desired state."
        lines: list[str] = []
        for label, items in (
            ("enabled", self.enabled),
            ("disabled", self.disabled),
            ("installed", self.installed),
            ("refreshed", self.refreshed),
        ):
            if items:
                lines.append(f"  {label}: {', '.join(items)}")
        for integration_id, reason in self.failed:
            lines.append(f"  failed: {integration_id} — {reason}")
        return "Reconciled:\n" + "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "enabled": list(self.enabled),
            "disabled": list(self.disabled),
            "installed": list(self.installed),
            "refreshed": list(self.refreshed),
            "failed": [{"integration": key, "reason": value} for key, value in self.failed],
            "changed": self.changed,
        }


class LifecycleService:
    """Install, enable, disable, remove, roll back, and reconcile integrations."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: ConfigStore,
        manager: IntegrationManager,
        updater: UpdateManager,
        discoverer: ToolDiscoverer,
        backups: BackupManager,
        locks: LockManager,
        audit: AuditLogger,
    ) -> None:
        self._settings = settings
        self._store = store
        self._manager = manager
        self._updater = updater
        self._discoverer = discoverer
        self._backups = backups
        self._locks = locks
        self._audit = audit

    # ------------------------------------------------------------------ views

    def list_integrations(self, *, enabled_only: bool = False) -> list[IntegrationSummary]:
        """Every configured integration with its current state (arch §14)."""
        rows: list[IntegrationSummary] = []
        for integration in self._manager.all():
            if enabled_only and not integration.enabled:
                continue
            report = self._manager.last_health(integration.id)
            rows.append(
                IntegrationSummary(
                    id=integration.id,
                    name=integration.manifest.name,
                    namespace=integration.namespace,
                    source_type=integration.source_type.value,
                    trust=integration.trust.value,
                    enabled=integration.enabled,
                    installed=integration.is_installed,
                    version=integration.lock.version_id if integration.lock else None,
                    health=(report.status if report else integration.baseline_status()).value,
                    health_detail=report.detail if report else "",
                    tool_count=len(self._discoverer.registry.for_integration(integration.id)),
                    update_policy=integration.update_policy.value,
                )
            )
        return rows

    def describe(self, integration_id: str) -> dict[str, Any]:
        """Full detail for one integration (`GET /api/integrations/{name}`)."""
        integration = self._manager.get(integration_id)
        report = self._manager.last_health(integration_id)
        tools = self._discoverer.registry.for_integration(integration_id)
        return {
            "id": integration.id,
            "name": integration.manifest.name,
            "description": integration.manifest.description,
            "namespace": integration.namespace,
            "enabled": integration.enabled,
            "origin": integration.manifest.source.origin,
            "source": integration.manifest.source.model_dump(mode="json", exclude_none=True),
            "trust": integration.trust.value,
            "update_policy": integration.update_policy.value,
            "capabilities": list(integration.manifest.capabilities),
            "installed": integration.is_installed,
            "lock": integration.lock.model_dump(mode="json") if integration.lock else None,
            "health": {
                "status": (report.status if report else integration.baseline_status()).value,
                "detail": report.detail if report else "",
                "latency_ms": report.latency_ms if report else None,
            },
            "tools": [item.summary() for item in tools],
            "policy": integration.policy.model_dump(mode="json", exclude_none=True),
            "rollback_points": [item.summary() for item in self._backups.list(integration_id)],
        }

    # --------------------------------------------------------------- install

    async def install(
        self,
        integration_id: str,
        *,
        enable: bool = True,
        version: VersionRef | None = None,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        """Install an integration and, by default, switch it on (arch §14).

        Raises:
            IntegrationNotFound: No manifest or catalog entry defines it.
            IntegrationLocked: Another operation holds its lock.
            HubError: Acquiring or building the artifact failed.
        """
        integration = self._manager.get(integration_id)
        job = new_job_id()
        started = time.monotonic()

        async with integration_lock(
            self._locks, integration_id, owner=job, ttl_seconds=self._settings.lock_timeout_seconds
        ):
            adapter = self._manager.adapter_for(integration_id)
            try:
                if integration.source_type.is_local:
                    resolved = await adapter.install(version)
                    staged_path = adapter.context.current_link
                    self._write_lock(
                        integration_id,
                        adapter.build_lock_entry(resolved).model_copy(update={"installed_path": str(staged_path)}),
                    )
                else:
                    # Remote and builtin have no artifact; recording the resolved
                    # protocol version is what "installed" means for them.
                    resolved = version or await adapter.resolve_latest()
                    self._write_lock(integration_id, adapter.build_lock_entry(resolved))
            except HubError as exc:
                self._audit.emit(
                    AuditAction.INSTALL,
                    "error",
                    integration=integration_id,
                    error_code=exc.code,
                    message=exc.message,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                raise

        # Reload before enabling, not after. `enable` refuses an integration it
        # believes is not installed, and that belief comes from the manager's
        # catalog snapshot — which still predates the lock entry just written.
        await self._reload()

        if enable:
            await self.enable(integration_id, principal=principal, audit_action=False)

        result = await self._discoverer.refresh(integration_id)
        self._audit.emit(
            AuditAction.INSTALL,
            "success",
            integration=integration_id,
            message=f"installed {resolved.display}",
            context={"version": resolved.identifier, "tools": len(result.tools), "enabled": enable},
            duration_ms=(time.monotonic() - started) * 1000,
        )
        log.info("lifecycle.installed", integration=integration_id, version=resolved.display, enabled=enable)
        return {
            "integration": integration_id,
            "version": resolved.display,
            "enabled": enable,
            "tools": len(result.tools),
            "status": result.status.value,
        }

    # ------------------------------------------------------- enable / disable

    async def enable(
        self,
        integration_id: str,
        *,
        principal: Principal | None = None,
        audit_action: bool = True,
    ) -> dict[str, Any]:
        """Switch an integration on and discover its tools (arch §14).

        Raises:
            IntegrationNotFound: No such integration.
            ValidationFailed: It needs installing first.
        """
        integration = self._manager.get(integration_id)
        if integration.needs_install:
            raise ValidationFailed(
                f"Integration {integration_id!r} is not installed. Run `mcp-hub install {integration_id}` first.",
                integration=integration_id,
            )

        self._set_enabled(integration_id, True)
        await self._reload()
        self._discoverer.registry.set_enabled(integration_id, True)
        result = await self._discoverer.discover(integration_id, principal=principal)

        if audit_action:
            self._audit.emit(
                AuditAction.ENABLE,
                "success",
                integration=integration_id,
                context={"tools": len(result.tools), "status": result.status.value},
            )
        log.info("lifecycle.enabled", integration=integration_id, tools=len(result.tools))
        return {
            "integration": integration_id,
            "enabled": True,
            "tools": len(result.tools),
            "status": result.status.value,
        }

    async def disable(self, integration_id: str, *, principal: Principal | None = None) -> dict[str, Any]:
        """Switch an integration off, stopping its traffic (arch §14).

        Sessions are closed and tools stop being routed, but nothing is deleted —
        re-enabling is immediate and needs no reinstall.
        """
        self._manager.get(integration_id)
        self._set_enabled(integration_id, False)
        await self._reload()

        removed = self._discoverer.registry.remove_integration(integration_id)
        closed = await self._manager.disconnect(integration_id)

        self._audit.emit(
            AuditAction.DISABLE,
            "success",
            integration=integration_id,
            context={"tools_withdrawn": removed, "sessions_closed": closed},
        )
        log.info("lifecycle.disabled", integration=integration_id, tools_withdrawn=removed)
        return {
            "integration": integration_id,
            "enabled": False,
            "tools_withdrawn": removed,
            "sessions_closed": closed,
        }

    # ---------------------------------------------------------------- remove

    async def remove(
        self,
        integration_id: str,
        *,
        purge_secrets: bool = True,
        purge_backups: bool = False,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        """Remove an integration entirely (arch §18).

        Runs arch §18's order exactly: stop routing, disconnect sessions, back up
        configuration, delete artifacts, delete only this integration's own
        secrets, update the catalog and lock file, and audit.

        Args:
            purge_secrets: Delete credentials owned solely by this integration.
                Shared credentials are never touched, whatever this is set to.
            purge_backups: Also delete its rollback points. Off by default so a
                removal stays reversible for a while.

        Raises:
            IntegrationNotFound: No such integration.
            IntegrationLocked: Another operation holds its lock.
        """
        integration = self._manager.get(integration_id)
        job = new_job_id()
        started = time.monotonic()

        async with integration_lock(
            self._locks, integration_id, owner=job, ttl_seconds=self._settings.lock_timeout_seconds
        ):
            # 1-2. Stop routing before anything else can be torn out from under it.
            withdrawn = self._discoverer.registry.remove_integration(integration_id)
            # 3. Disconnect sessions.
            closed = await self._manager.disconnect(integration_id)

            # 4. Back up configuration before deleting it.
            self._backups.create(
                integration_id,
                version_id=integration.lock.version_id if integration.lock else "none",
                reason="remove",
                lock_entry=integration.lock,
                created_by=(principal or current_principal()).subject or None,
            )

            # 5. Remove runtime artifacts.
            adapter = self._manager.adapter_for(integration_id)
            await adapter.uninstall()

            # 6. Remove secrets belonging only to this integration (arch §18).
            secrets_removed = 0
            if purge_secrets:
                secrets_removed = await self._manager.secrets.delete_for_integration(integration_id)

            # 7-8. Update the catalog and the lock file.
            self._remove_from_catalog(integration_id)
            self._remove_from_lock(integration_id)

            if purge_backups:
                self._backups.purge(integration_id)

        await self._reload()

        # 10. Audit.
        self._audit.emit(
            AuditAction.REMOVE,
            "success",
            integration=integration_id,
            context={
                "tools_withdrawn": withdrawn,
                "sessions_closed": closed,
                "secrets_removed": secrets_removed,
                "backups_purged": purge_backups,
            },
            duration_ms=(time.monotonic() - started) * 1000,
        )
        log.info("lifecycle.removed", integration=integration_id, secrets_removed=secrets_removed)
        return {
            "integration": integration_id,
            "removed": True,
            "tools_withdrawn": withdrawn,
            "sessions_closed": closed,
            "secrets_removed": secrets_removed,
        }

    # -------------------------------------------------------------- rollback

    async def rollback(
        self,
        integration_id: str,
        *,
        version: str | None = None,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        """Restore a previous version (arch §19).

        Args:
            version: The version identifier to restore. Defaults to whatever the
                most recent rollback point recorded.

        Raises:
            RollbackFailed: There is no rollback point, or the target version's
                directory is gone.
            IntegrationLocked: Another operation holds its lock.
        """
        integration = self._manager.get(integration_id)
        adapter = self._manager.adapter_for(integration_id)
        job = new_job_id()
        started = time.monotonic()

        target_id = version
        record = None
        if target_id is None:
            record = self._backups.latest(integration_id)
            if record is None or not record.version_id or record.version_id == "none":
                raise RollbackFailed(
                    f"No rollback point recorded for {integration_id!r}. "
                    "A rollback point is created before each update; there has been none yet.",
                    integration=integration_id,
                )
            target_id = record.version_id

        current = await adapter.current_version(integration.lock)
        target = VersionRef(identifier=target_id, kind=current.kind if current else "version")

        async with integration_lock(
            self._locks, integration_id, owner=job, ttl_seconds=self._settings.lock_timeout_seconds
        ):
            try:
                await adapter.rollback(target)
                await self._manager.disconnect(integration_id)

                restored = self._backups.restore_lock_entry(record) if record else None
                self._write_lock(integration_id, restored or adapter.build_lock_entry(target))
            except HubError as exc:
                self._audit.emit(
                    AuditAction.ROLLBACK,
                    "error",
                    integration=integration_id,
                    error_code=exc.code,
                    message=exc.message,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                raise

        await self._reload()
        health = await self._manager.check_health(integration_id)
        result = await self._discoverer.refresh(integration_id)

        self._audit.emit(
            AuditAction.ROLLBACK,
            "success" if health.is_serving else "error",
            integration=integration_id,
            message=f"{current.display if current else 'unknown'} -> {target.display}",
            context={"health": health.status.value, "tools": len(result.tools)},
            duration_ms=(time.monotonic() - started) * 1000,
        )
        log.info(
            "lifecycle.rolled_back",
            integration=integration_id,
            to_version=target.display,
            health=health.status.value,
        )
        return {
            "integration": integration_id,
            "from": current.display if current else None,
            "to": target.display,
            "health": health.status.value,
            "tools": len(result.tools),
        }

    def rollback_points(self, integration_id: str) -> list[dict[str, Any]]:
        """Available rollback points for one integration."""
        return [item.summary() for item in self._backups.list(integration_id)]

    # -------------------------------------------------------------- reconcile

    async def reconcile(self, *, principal: Principal | None = None) -> ReconcileReport:
        """Drive actual runtime state toward the desired configuration (arch §63).

        Reads the catalog fresh from disk, so an operator who hand-edited
        `integrations.yaml` gets it applied without a restart. Each integration
        is reconciled independently: one failure is recorded and the rest proceed.
        """
        started = time.monotonic()
        await self._reload()

        enabled: list[str] = []
        disabled: list[str] = []
        installed: list[str] = []
        refreshed: list[str] = []
        failed: list[tuple[str, str]] = []

        for integration in self._manager.all():
            registry_has_tools = bool(self._discoverer.registry.for_integration(integration.id))
            try:
                if integration.enabled:
                    if integration.needs_install:
                        await self.install(integration.id, enable=True, principal=principal)
                        installed.append(integration.id)
                        continue
                    if not registry_has_tools:
                        result = await self._discoverer.discover(integration.id, principal=principal)
                        if result.ok:
                            enabled.append(integration.id)
                        else:
                            failed.append((integration.id, result.detail))
                    else:
                        refreshed.append(integration.id)
                elif registry_has_tools:
                    self._discoverer.registry.remove_integration(integration.id)
                    await self._manager.disconnect(integration.id)
                    disabled.append(integration.id)
            except HubError as exc:
                failed.append((integration.id, exc.message))
            except Exception as exc:  # noqa: BLE001 - one integration must not abort the sweep
                failed.append((integration.id, describe_exception(exc)))

        report = ReconcileReport(
            enabled=tuple(enabled),
            disabled=tuple(disabled),
            installed=tuple(installed),
            refreshed=tuple(refreshed),
            failed=tuple(failed),
        )
        self._audit.emit(
            AuditAction.RECONCILE,
            "success" if not failed else "error",
            context=report.to_payload(),
            duration_ms=(time.monotonic() - started) * 1000,
        )
        log.info("lifecycle.reconciled", **{k: len(v) for k, v in report.to_payload().items() if isinstance(v, list)})
        return report

    # ------------------------------------------------------------------ health

    async def health(
        self, *, integrations: Sequence[str] | None = None, principal: Principal | None = None
    ) -> dict[str, dict[str, Any]]:
        """Probe integrations and return their statuses (arch §25)."""
        reports = await self._manager.check_all(principal, integrations=integrations)
        from app.core.metrics import INTEGRATION_HEALTH

        for integration_id, report in reports.items():
            INTEGRATION_HEALTH.labels(integration_id, report.status.value).set(1.0 if report.is_serving else 0.0)
        return {
            integration_id: {
                "status": report.status.value,
                "detail": report.detail,
                "latency_ms": round(report.latency_ms, 1) if report.latency_ms else None,
                "tools": report.tool_count,
                "protocol_version": report.protocol_version,
                "server_version": report.server_version,
            }
            for integration_id, report in sorted(reports.items())
        }

    async def refresh(
        self, *, integrations: Sequence[str] | None = None, principal: Principal | None = None
    ) -> dict[str, Any]:
        """Rediscover tools for some or all integrations (arch §12)."""
        if integrations:
            results = {
                integration_id: await self._discoverer.refresh(integration_id, principal=principal)
                for integration_id in integrations
            }
        else:
            results = await self._discoverer.discover_all(principal=principal)

        self._audit.emit(
            AuditAction.TOOL_REFRESH,
            "success",
            context={"integrations": sorted(results), "tools": sum(len(r.tools) for r in results.values())},
        )
        return {
            integration_id: {"status": result.status.value, "tools": len(result.tools), "detail": result.detail}
            for integration_id, result in sorted(results.items())
        }

    # ------------------------------------------------------------- config I/O

    def _set_enabled(self, integration_id: str, enabled: bool) -> None:
        """Flip the catalog's `enabled` flag through a validated model (arch §38)."""

        def mutate(catalog: Catalog) -> Catalog:
            entries = dict(catalog.integrations)
            existing = entries.get(integration_id, CatalogEntry())
            entries[integration_id] = existing.model_copy(update={"enabled": enabled})
            return catalog.model_copy(update={"integrations": entries})

        self._store.update_catalog(mutate)

    def _remove_from_catalog(self, integration_id: str) -> None:
        def mutate(catalog: Catalog) -> Catalog:
            entries = {k: v for k, v in catalog.integrations.items() if k != integration_id}
            return catalog.model_copy(update={"integrations": entries})

        self._store.update_catalog(mutate)

    def _remove_from_lock(self, integration_id: str) -> None:
        def mutate(lock: LockFile) -> LockFile:
            entries = {k: v for k, v in lock.integrations.items() if k != integration_id}
            return lock.model_copy(update={"integrations": entries})

        self._store.update_lock(mutate)

    def _write_lock(self, integration_id: str, entry: Any) -> None:
        def mutate(lock: LockFile) -> LockFile:
            entries = dict(lock.integrations)
            entries[integration_id] = entry
            return lock.model_copy(update={"integrations": entries})

        self._store.update_lock(mutate)

    async def _reload(self) -> None:
        """Re-read configuration from disk and hand the manager a new snapshot."""
        await self._manager.reload_from(self._store)

    # -------------------------------------------------------------- diagnostics

    async def doctor(self) -> list[tuple[str, str, str]]:
        """Environment and configuration checks (arch §36).

        Returns:
            `(status, subject, detail)` triples where status is `OK`, `WARN`, or
            `FAIL`. Returning data rather than printing keeps the CLI and the API
            rendering the same results.
        """
        import shutil
        import sys

        checks: list[tuple[str, str, str]] = []

        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        checks.append(("OK" if sys.version_info >= (3, 12) else "FAIL", "Python", f"{version} (need >= 3.12)"))

        try:
            import mcp

            checks.append(("OK", "MCP SDK", f"mcp {getattr(mcp, '__version__', 'installed')}"))
        except ImportError:  # pragma: no cover - the package could not import at all
            checks.append(("FAIL", "MCP SDK", "not importable"))

        for binary, needed_by in (
            ("docker", "container isolation and docker sources"),
            ("git", "git sources"),
            ("npm", "npm sources"),
            ("uv", "python sources (falls back to pip)"),
        ):
            present = shutil.which(binary) is not None
            required = self._requires_binary(binary)
            status = "OK" if present else ("FAIL" if required else "WARN")
            detail = f"{'found' if present else 'not found'} — needed for {needed_by}"
            checks.append((status, binary, detail))

        checks.append(
            (
                "OK" if self._settings.redis_url else "WARN",
                "Redis",
                self._settings.redis_url or "not configured — locks and rate limits are process-local",
            )
        )
        checks.append(
            (
                "OK" if not self._settings.database_url.startswith("sqlite") else "WARN",
                "Database",
                "PostgreSQL" if not self._settings.database_url.startswith("sqlite") else "SQLite (development only)",
            )
        )
        checks.append(
            (
                "OK" if self._settings.auth_secret.get_secret_value() else "FAIL",
                "Auth secret",
                "set" if self._settings.auth_secret.get_secret_value() else "MCP_HUB_AUTH_SECRET is not set",
            )
        )

        try:
            catalog = ResolvedCatalog.load(self._store)
            checks.append(("OK", "Configuration", f"{len(catalog)} integrations resolved"))
        except HubError as exc:
            checks.append(("FAIL", "Configuration", exc.message))

        checks.append(
            (
                "OK" if self._store.lock_path.exists() else "WARN",
                "Lock file",
                str(self._store.lock_path) if self._store.lock_path.exists() else "not written yet",
            )
        )

        for integration_id, report in sorted((await self.health()).items()):
            status = {
                HealthStatus.HEALTHY.value: "OK",
                HealthStatus.DEGRADED.value: "WARN",
                HealthStatus.DISABLED.value: "OK",
                HealthStatus.AUTH_REQUIRED.value: "WARN",
                HealthStatus.UPDATE_REQUIRED.value: "WARN",
            }.get(report["status"], "FAIL")
            # A healthy or disabled integration has nothing to explain, so the
            # status stands alone rather than trailing an empty `: `.
            detail = str(report["detail"] or "")
            checks.append((status, integration_id, f"{report['status']}: {detail}" if detail else report["status"]))

        return checks

    def _requires_binary(self, binary: str) -> bool:
        """Whether any enabled integration actually needs this tool present."""
        needed: dict[str, set[SourceType]] = {
            "docker": {SourceType.DOCKER},
            "git": {SourceType.GIT},
            "npm": {SourceType.NPM},
            "uv": set(),  # python sources fall back to pip
        }
        wanted = needed.get(binary, set())
        for integration in self._manager.enabled():
            if integration.source_type in wanted:
                return True
            # Any locally-run integration configured for container isolation
            # needs a container runtime, whatever its source kind is.
            contained = integration.manifest.runtime.isolation == "container"
            if binary == "docker" and contained and integration.source_type.is_local:
                return True
        return False


def _unknown(catalog: ResolvedCatalog, names: Sequence[str]) -> list[str]:
    """Names not present in the catalog."""
    return sorted(name for name in names if name not in catalog)


def ensure_known(catalog: ResolvedCatalog, names: Sequence[str]) -> None:
    """Validate names before starting work.

    Raises:
        IntegrationNotFound: Any name is unknown.
    """
    unknown = _unknown(catalog, names)
    if unknown:
        raise IntegrationNotFound(
            f"Unknown integration(s): {', '.join(unknown)}.",
            unknown=unknown,
            known=sorted(item.id for item in catalog.all()),
        )
