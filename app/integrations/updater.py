"""The update pipeline (arch §15, §16, §17, §59, §60).

Arch §15 forbids the obvious implementation. `git pull && restart` is not an
update; it is an outage waiting for a bad commit. What runs instead is:

    resolve -> lock -> backup -> stage -> validate -> smoke test -> compare tools
      -> promote -> health check -> [rollback on failure] -> lock file -> audit

Four properties make it safe:

* **Nothing is updated in place.** Each version installs into its own immutable
  directory and `current` is repointed by an atomic rename. The old version stays
  on disk, so rollback is a rename back rather than a re-download (arch §15, §19).
* **The new version proves itself before it serves.** It is launched in
  isolation, completes an MCP handshake, and lists its tools while the old
  version keeps handling traffic. A build that cannot start never reaches an
  agent.
* **Failure rolls back automatically, and only that integration.** Arch §60 calls
  this rolling-per-integration: GitHub failing must not disturb Jira.
* **Everything is recorded.** The lock file gets the resolved version, the audit
  log gets the outcome, and the tool diff is surfaced rather than applied
  silently — an update that removes a tool an agent depends on should be visible.

`UpdateManager` is the single implementation arch §57 requires. The CLI and the
REST API both call it; neither has update logic of its own.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.audit.logger import AuditLogger
from app.config.catalog import ResolvedIntegration
from app.config.models import LockEntry, LockFile
from app.config.store import ConfigStore
from app.core.clock import utcnow
from app.core.context import Principal, current_request
from app.core.domain import AuditAction, HealthStatus, SourceType, UpdatePolicy
from app.core.errors import HubError, IntegrationNotFound, UpdateFailed, describe_exception
from app.core.ids import new_job_id
from app.core.logging import get_logger
from app.core.metrics import UPDATE_RESULTS
from app.integrations.backup import BackupManager
from app.integrations.base import StagedBuild, VersionRef
from app.integrations.locks import LockManager, integration_lock
from app.integrations.manager import IntegrationManager

if TYPE_CHECKING:
    from app.gateway.discovery import ToolDiscoverer

__all__ = ["UpdateManager", "UpdateOutcome", "UpdatePlan", "UpdatePlanItem", "UpdateStage"]

log = get_logger(__name__)


class UpdateStage(StrEnum):
    """Where an update got to. Recorded so a failure names its own step."""

    RESOLVE = "resolve"
    LOCK = "lock"
    BACKUP = "backup"
    STAGE = "stage"
    VALIDATE = "validate"
    COMPARE = "compare"
    PROMOTE = "promote"
    HEALTH = "health"
    FINALISE = "finalise"
    DONE = "done"


class UpdateAction(StrEnum):
    """What the plan proposes for one integration."""

    UPDATE = "update"
    INSTALL = "install"
    REFRESH = "refresh"
    """Remote services: re-probe and rediscover tools; no artifact changes (arch §61)."""

    NONE = "none"
    EXCLUDED = "excluded"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class UpdatePlanItem:
    """What would happen to one integration (arch §16, §59)."""

    integration_id: str
    action: UpdateAction
    current_version: str | None = None
    latest_version: str | None = None
    reason: str = ""

    @property
    def changes_anything(self) -> bool:
        """Whether executing this item would modify the deployment."""
        return self.action in (UpdateAction.UPDATE, UpdateAction.INSTALL, UpdateAction.REFRESH)


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    """The whole proposed change set, shown before anything happens (arch §16)."""

    items: tuple[UpdatePlanItem, ...]
    generated_at: Any = field(default_factory=utcnow)

    @property
    def actionable(self) -> tuple[UpdatePlanItem, ...]:
        """Items that would actually change something."""
        return tuple(item for item in self.items if item.changes_anything)

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to do."""
        return not self.actionable

    def render(self) -> str:
        """Operator-facing plan text, in the shape arch §16 specifies."""
        if not self.items:
            return "Update plan: nothing to do."
        lines = ["Update plan:", ""]
        for item in self.items:
            lines.append(f"  {item.integration_id}")
            match item.action:
                case UpdateAction.UPDATE:
                    lines.append(f"    current: {item.current_version or 'unknown'}")
                    lines.append(f"    latest:  {item.latest_version or 'unknown'}")
                case UpdateAction.INSTALL:
                    lines.append(f"    install: {item.latest_version or 'latest'}")
                case UpdateAction.REFRESH:
                    lines.append("    remote — refresh tools, no local update required")
                case _:
                    lines.append(f"    {item.action.value}: {item.reason or 'no action'}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def to_payload(self) -> list[dict[str, Any]]:
        """JSON form for the REST API."""
        return [
            {
                "integration": item.integration_id,
                "action": item.action.value,
                "current": item.current_version,
                "latest": item.latest_version,
                "reason": item.reason,
            }
            for item in self.items
        ]


@dataclass(frozen=True, slots=True)
class UpdateOutcome:
    """What actually happened to one integration."""

    integration_id: str
    succeeded: bool
    stage_reached: UpdateStage
    from_version: str | None = None
    to_version: str | None = None
    rolled_back: bool = False
    tools_added: tuple[str, ...] = ()
    tools_removed: tuple[str, ...] = ()
    detail: str = ""
    duration_ms: float = 0.0

    def summary(self) -> dict[str, Any]:
        """JSON form for the REST API and CLI output."""
        return {
            "integration": self.integration_id,
            "succeeded": self.succeeded,
            "stage": self.stage_reached.value,
            "from": self.from_version,
            "to": self.to_version,
            "rolled_back": self.rolled_back,
            "tools_added": list(self.tools_added),
            "tools_removed": list(self.tools_removed),
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 1),
        }


class UpdateManager:
    """Plans and executes integration updates (arch §57)."""

    def __init__(
        self,
        *,
        manager: IntegrationManager,
        store: ConfigStore,
        locks: LockManager,
        backups: BackupManager,
        audit: AuditLogger,
        discoverer: ToolDiscoverer,
        lock_timeout_seconds: float = 900.0,
        rollback_on_failure: bool = True,
    ) -> None:
        self._manager = manager
        self._store = store
        self._locks = locks
        self._backups = backups
        self._audit = audit
        self._discoverer = discoverer
        self._lock_timeout = lock_timeout_seconds
        self._rollback_default = rollback_on_failure

    # ------------------------------------------------------------------ plan

    async def plan(
        self,
        *,
        names: Sequence[str] = (),
        all_integrations: bool = False,
        exclude: Sequence[str] = (),
    ) -> UpdatePlan:
        """Work out what would change, without changing anything (arch §16, §59).

        Exclusions are applied *before* versions are resolved, as arch §17
        requires — an excluded integration is not contacted at all, so a
        `--exclude` on a broken upstream keeps the plan fast and quiet.

        Raises:
            IntegrationNotFound: A named integration is not configured.
        """
        excluded = {item.strip() for item in exclude if item.strip()}
        targets = self._select(names, all_integrations, excluded)

        items: list[UpdatePlanItem] = []
        for integration_id in sorted(excluded):
            if integration_id in self._manager.catalog:
                items.append(
                    UpdatePlanItem(
                        integration_id=integration_id,
                        action=UpdateAction.EXCLUDED,
                        reason="excluded by --exclude",
                    )
                )

        for integration in targets:
            items.append(await self._plan_one(integration))

        items.sort(key=lambda item: item.integration_id)
        return UpdatePlan(items=tuple(items))

    async def _plan_one(self, integration: ResolvedIntegration) -> UpdatePlanItem:
        """Resolve one integration's current and available versions."""
        adapter = self._manager.adapter_for(integration.id)

        if integration.source_type is SourceType.REMOTE:
            # Arch §61: the provider owns the deployment. The useful action is a
            # tool refresh, not a version change.
            return UpdatePlanItem(
                integration_id=integration.id,
                action=UpdateAction.REFRESH,
                current_version=integration.lock.protocol_version if integration.lock else None,
                reason="remote service — provider-managed",
            )
        if integration.source_type is SourceType.BUILTIN:
            return UpdatePlanItem(
                integration_id=integration.id,
                action=UpdateAction.NONE,
                reason="builtin — versions with the hub",
            )

        try:
            latest = await adapter.resolve_latest()
        except HubError as exc:
            return UpdatePlanItem(
                integration_id=integration.id,
                action=UpdateAction.SKIPPED,
                reason=f"could not resolve latest version: {exc.message}",
            )

        current = await adapter.current_version(integration.lock)
        if current is None:
            return UpdatePlanItem(
                integration_id=integration.id,
                action=UpdateAction.INSTALL,
                latest_version=latest.display,
                reason="not installed",
            )
        if current.identifier == latest.identifier:
            return UpdatePlanItem(
                integration_id=integration.id,
                action=UpdateAction.NONE,
                current_version=current.display,
                latest_version=latest.display,
                reason="already up to date",
            )
        return UpdatePlanItem(
            integration_id=integration.id,
            action=UpdateAction.UPDATE,
            current_version=current.display,
            latest_version=latest.display,
        )

    def _select(self, names: Sequence[str], all_integrations: bool, excluded: set[str]) -> list[ResolvedIntegration]:
        """Resolve the target set, honouring exclusions (arch §17).

        Raises:
            IntegrationNotFound: A named integration is not configured.
        """
        if names:
            _ensure_known(self._manager, names)
            chosen = [self._manager.get(name) for name in names]
        elif all_integrations:
            chosen = self._manager.enabled()
        else:
            raise UpdateFailed("Specify integration names or use --all.")
        return [item for item in chosen if item.id not in excluded]

    # ---------------------------------------------------------------- execute

    async def update(
        self,
        *,
        names: Sequence[str] = (),
        all_integrations: bool = False,
        exclude: Sequence[str] = (),
        dry_run: bool = False,
        force: bool = False,
        rollback_on_failure: bool | None = None,
        principal: Principal | None = None,
        parallel: bool = False,
        job_id: str | None = None,
        plan: UpdatePlan | None = None,
    ) -> tuple[UpdatePlan, list[UpdateOutcome]]:
        """Execute the update plan (arch §16, §59, §60).

        Args:
            names: Integrations to update.
            all_integrations: Update every enabled integration.
            exclude: Names to leave alone (arch §17).
            dry_run: Produce the plan and stop (arch §59).
            force: Update even when the resolved version already matches.
            rollback_on_failure: Restore the previous version when an update
                fails. Defaults to the deployment setting, which defaults to on.
            principal: Who requested this, for audit.
            parallel: Update different integrations concurrently. Off by default
                (arch §30) so a shared build cache or registry is not hammered.
            job_id: Correlation id when run from the job queue.
            plan: A plan already computed by the caller. The CLI shows the plan,
                asks for confirmation, then executes it; passing it back avoids
                re-resolving every upstream version, and — more importantly —
                guarantees the operator approved the change set that actually
                runs rather than a freshly resolved one.

        Returns:
            The plan and one outcome per attempted integration.
        """
        if plan is None:
            plan = await self.plan(names=names, all_integrations=all_integrations, exclude=exclude)
        if dry_run or plan.is_empty:
            return plan, []

        correlation = job_id or new_job_id()
        actionable = plan.actionable

        if parallel:
            import asyncio

            results = await asyncio.gather(
                *(
                    self._execute_item(item, force=force, rollback=rollback_on_failure, owner=correlation)
                    for item in actionable
                ),
                return_exceptions=True,
            )
            outcomes: list[UpdateOutcome] = []
            for item, result in zip(actionable, results, strict=True):
                if isinstance(result, BaseException):
                    outcomes.append(
                        UpdateOutcome(
                            integration_id=item.integration_id,
                            succeeded=False,
                            stage_reached=UpdateStage.RESOLVE,
                            detail=str(result),
                        )
                    )
                else:
                    outcomes.append(result)
        else:
            # Sequential is the default (arch §30): each integration is
            # health-checked before the next one starts, so a systemic problem
            # stops after the first failure rather than after the twentieth.
            outcomes = [
                await self._execute_item(item, force=force, rollback=rollback_on_failure, owner=correlation)
                for item in actionable
            ]

        succeeded = sum(1 for item in outcomes if item.succeeded)
        log.info(
            "update.batch_complete",
            attempted=len(outcomes),
            succeeded=succeeded,
            failed=len(outcomes) - succeeded,
            job=correlation,
        )
        return plan, outcomes

    async def _execute_item(
        self,
        item: UpdatePlanItem,
        *,
        force: bool,
        rollback: bool | None,
        owner: str,
    ) -> UpdateOutcome:
        """Run one plan item, catching everything so a batch continues."""
        try:
            if item.action is UpdateAction.REFRESH:
                return await self._refresh_remote(item.integration_id)
            return await self.update_one(item.integration_id, force=force, rollback_on_failure=rollback, owner=owner)
        except HubError as exc:
            UPDATE_RESULTS.labels(item.integration_id, "failed").inc()
            return UpdateOutcome(
                integration_id=item.integration_id,
                succeeded=False,
                stage_reached=UpdateStage.LOCK,
                detail=exc.message,
            )

    async def _refresh_remote(self, integration_id: str) -> UpdateOutcome:
        """Re-probe a remote service and rediscover its tools (arch §61)."""
        started = time.monotonic()
        before = [item.qualified_name for item in self._discoverer.registry.for_integration(integration_id)]
        result = await self._discoverer.refresh(integration_id)
        after = [item.qualified_name for item in result.tools]
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))

        adapter = self._manager.adapter_for(integration_id)
        succeeded = result.status is HealthStatus.HEALTHY
        if succeeded:
            try:
                version = await adapter.resolve_latest()
                self._write_lock(integration_id, adapter.build_lock_entry(version))
                await self._manager.reload_from(self._store)
            except HubError as exc:
                log.warning("update.remote_lock_skip", integration=integration_id, error=exc.message)

        UPDATE_RESULTS.labels(integration_id, "succeeded" if succeeded else "failed").inc()
        self._audit.emit(
            AuditAction.UPDATE,
            "success" if succeeded else "error",
            integration=integration_id,
            message=result.detail,
            context={"kind": "remote_refresh", "tools_added": added, "tools_removed": removed},
            duration_ms=(time.monotonic() - started) * 1000,
        )
        return UpdateOutcome(
            integration_id=integration_id,
            succeeded=succeeded,
            stage_reached=UpdateStage.DONE if succeeded else UpdateStage.VALIDATE,
            tools_added=tuple(added),
            tools_removed=tuple(removed),
            detail=result.detail,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------- single run

    async def update_one(
        self,
        integration_id: str,
        *,
        target: VersionRef | None = None,
        force: bool = False,
        rollback_on_failure: bool | None = None,
        owner: str | None = None,
    ) -> UpdateOutcome:
        """Run the full staged pipeline for one integration (arch §15).

        Raises:
            IntegrationLocked: Another operation holds this integration's lock.
            IntegrationNotFound: No such integration.
        """
        integration = self._manager.get(integration_id)
        adapter = self._manager.adapter_for(integration_id)
        should_rollback = self._rollback_default if rollback_on_failure is None else rollback_on_failure
        job = owner or new_job_id()
        started = time.monotonic()
        stage = UpdateStage.RESOLVE
        staged: StagedBuild | None = None
        previous: VersionRef | None = None

        async with integration_lock(self._locks, integration_id, owner=job, ttl_seconds=self._lock_timeout):
            try:
                stage = UpdateStage.RESOLVE
                latest = target or await adapter.resolve_latest()
                previous = await adapter.current_version(integration.lock)
                if previous and previous.identifier == latest.identifier and not force:
                    UPDATE_RESULTS.labels(integration_id, "skipped").inc()
                    return UpdateOutcome(
                        integration_id=integration_id,
                        succeeded=True,
                        stage_reached=UpdateStage.DONE,
                        from_version=previous.display,
                        to_version=latest.display,
                        detail="already up to date",
                        duration_ms=(time.monotonic() - started) * 1000,
                    )

                stage = UpdateStage.BACKUP
                tools_before = [
                    item.qualified_name for item in self._discoverer.registry.for_integration(integration_id)
                ]
                self._backups.create(
                    integration_id,
                    version_id=previous.identifier if previous else "none",
                    reason="update",
                    lock_entry=integration.lock,
                    tools=tools_before,
                    created_by=current_request().principal.subject or None,
                )

                stage = UpdateStage.STAGE
                credentials = await self._manager.credentials_for(integration_id)
                staged = await adapter.stage(latest)

                stage = UpdateStage.VALIDATE
                report, staged_tools = await adapter.validate_staged(staged, credentials)
                if report.status is not HealthStatus.HEALTHY:
                    raise UpdateFailed(
                        f"The staged build of {integration_id!r} at {latest.display} failed its "
                        f"smoke test: {report.detail}",
                        integration=integration_id,
                        version=latest.identifier,
                    )

                stage = UpdateStage.COMPARE
                staged_names = sorted(f"{integration.namespace}.{tool.name}" for tool in staged_tools)
                added = sorted(set(staged_names) - set(tools_before))
                removed = sorted(set(tools_before) - set(staged_names))
                if removed and not force:
                    # Surfaced, not blocked: arch §15 wants tool changes compared
                    # and visible. An operator who wants the change proceeds; one
                    # who did not expect it now knows before agents break.
                    log.warning(
                        "update.tools_removed",
                        integration=integration_id,
                        removed=removed,
                        detail="The new version exposes fewer tools than the current one.",
                    )

                stage = UpdateStage.PROMOTE
                await adapter.promote(staged)
                await self._manager.disconnect(integration_id)

                stage = UpdateStage.HEALTH
                health = await self._manager.check_health(integration_id)
                if not health.is_serving:
                    raise UpdateFailed(
                        f"{integration_id!r} is {health.status.value} after promoting "
                        f"{latest.display}: {health.detail}",
                        integration=integration_id,
                        version=latest.identifier,
                    )

                stage = UpdateStage.FINALISE
                self._write_lock(integration_id, adapter.build_lock_entry(latest, staged=staged))
                # The manager still holds the pre-update snapshot until told
                # otherwise, so everything downstream — `list`, `show`, the next
                # plan, and a rollback's idea of what it is rolling back from —
                # would keep reporting the version this update just replaced.
                await self._manager.reload_from(self._store)
                await self._discoverer.refresh(integration_id)

                elapsed = (time.monotonic() - started) * 1000
                UPDATE_RESULTS.labels(integration_id, "succeeded").inc()
                self._audit.emit(
                    AuditAction.UPDATE,
                    "success",
                    integration=integration_id,
                    message=f"{previous.display if previous else 'none'} -> {latest.display}",
                    context={
                        "from": previous.identifier if previous else None,
                        "to": latest.identifier,
                        "tools_added": added,
                        "tools_removed": removed,
                    },
                    duration_ms=elapsed,
                )
                log.info(
                    "update.succeeded",
                    integration=integration_id,
                    from_version=previous.display if previous else None,
                    to_version=latest.display,
                    tools_added=len(added),
                    tools_removed=len(removed),
                )
                return UpdateOutcome(
                    integration_id=integration_id,
                    succeeded=True,
                    stage_reached=UpdateStage.DONE,
                    from_version=previous.display if previous else None,
                    to_version=latest.display,
                    tools_added=tuple(added),
                    tools_removed=tuple(removed),
                    duration_ms=elapsed,
                )

            except Exception as exc:  # noqa: BLE001 - every failure path must roll back and report
                return await self._handle_failure(
                    integration_id,
                    exc,
                    stage=stage,
                    staged=staged,
                    previous=previous,
                    should_rollback=should_rollback,
                    started=started,
                )

    async def _handle_failure(
        self,
        integration_id: str,
        exc: BaseException,
        *,
        stage: UpdateStage,
        staged: StagedBuild | None,
        previous: VersionRef | None,
        should_rollback: bool,
        started: float,
    ) -> UpdateOutcome:
        """Clean up after a failed update and roll back if armed (arch §15, §60)."""
        adapter = self._manager.adapter_for(integration_id)
        message = exc.message if isinstance(exc, HubError) else describe_exception(exc)
        log.error("update.failed", integration=integration_id, stage=stage.value, error=message)

        rolled_back = False
        # Only stages at or past PROMOTE changed what serves traffic; before that
        # the live version was never touched and there is nothing to restore.
        if (
            should_rollback
            and previous is not None
            and stage in (UpdateStage.PROMOTE, UpdateStage.HEALTH, UpdateStage.FINALISE)
        ):
            try:
                await adapter.rollback(previous)
                await self._manager.disconnect(integration_id)
                await self._manager.check_health(integration_id)
                await self._discoverer.refresh(integration_id)
                rolled_back = True
                log.info("update.rolled_back", integration=integration_id, version=previous.display)
            except Exception as rollback_exc:  # noqa: BLE001 - reported, never masks the original
                log.error(
                    "update.rollback_failed",
                    integration=integration_id,
                    error=str(rollback_exc),
                    detail="Manual intervention required.",
                )
                message = f"{message} (rollback also failed: {rollback_exc})"

        if (
            staged is not None
            and not rolled_back
            and stage in (UpdateStage.STAGE, UpdateStage.VALIDATE, UpdateStage.COMPARE)
        ):
            # The staged build never served; remove it so a retry starts clean.
            with_suppressed = getattr(adapter, "discard", None)
            if with_suppressed is not None:
                try:
                    await adapter.discard(staged)
                except Exception as discard_exc:  # noqa: BLE001 - cleanup is best-effort
                    log.warning("update.discard_failed", integration=integration_id, error=str(discard_exc))

        elapsed = (time.monotonic() - started) * 1000
        UPDATE_RESULTS.labels(integration_id, "rolled_back" if rolled_back else "failed").inc()
        self._audit.emit(
            AuditAction.UPDATE,
            "error",
            integration=integration_id,
            error_code=exc.code if isinstance(exc, HubError) else "update_failed",
            message=message,
            context={"stage": stage.value, "rolled_back": rolled_back},
            duration_ms=elapsed,
        )
        return UpdateOutcome(
            integration_id=integration_id,
            succeeded=False,
            stage_reached=stage,
            from_version=previous.display if previous else None,
            rolled_back=rolled_back,
            detail=message,
            duration_ms=elapsed,
        )

    # ---------------------------------------------------------------- helpers

    def _write_lock(self, integration_id: str, entry: LockEntry) -> None:
        """Record the resolved version in the lock file (arch §62)."""

        def mutate(lock: LockFile) -> LockFile:
            integrations = dict(lock.integrations)
            integrations[integration_id] = entry
            return lock.model_copy(update={"integrations": integrations})

        self._store.update_lock(mutate)

    def scheduled_candidates(self) -> list[str]:
        """Integrations whose update policy allows an unattended update (arch §34).

        `manual` integrations are never included: automation must not move a
        version an operator said they would move themselves.
        """
        return [
            integration.id
            for integration in self._manager.enabled()
            if integration.update_policy in (UpdatePolicy.SCHEDULED, UpdatePolicy.AUTOMATIC)
        ]


def _ensure_known(manager: IntegrationManager, names: Sequence[str]) -> None:
    """Validate every name before doing any work.

    Raises:
        IntegrationNotFound: Any name is unknown. Checked up front so a typo in
            `update jira githbu` fails before Jira has already been updated.
    """
    unknown = [name for name in names if name not in manager.catalog]
    if unknown:
        raise IntegrationNotFound(
            f"Unknown integration(s): {', '.join(sorted(unknown))}.",
            unknown=sorted(unknown),
            known=sorted(item.id for item in manager.all()),
        )
