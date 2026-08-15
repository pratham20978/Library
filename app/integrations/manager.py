"""The integration manager — one place that owns adapters and their state.

Everything above this layer asks the manager for an integration rather than
constructing an adapter, resolving credentials, and opening a session itself.
That keeps three things in one place that are wrong when they drift apart:

* **Adapter construction.** Each integration gets exactly one adapter instance,
  built from its manifest through the registry, never by name (arch §49).
* **Credential resolution.** A session is only ever opened with credentials
  resolved for the *calling principal*, so a per-user integration cannot be
  reached with someone else's token (arch §20).
* **Session keying.** The pool key comes from the credential fingerprint the
  manager computed, not from anything a caller passes in.

The manager holds a `ResolvedCatalog` snapshot. Configuration changes produce a
new snapshot via `reload`, and in-flight requests keep using the one they
started with — configuration never mutates underneath a running call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from app.config.catalog import ResolvedCatalog, ResolvedIntegration
from app.config.settings import Settings
from app.config.store import ConfigStore
from app.core.context import Principal, current_principal
from app.core.domain import HealthStatus
from app.core.errors import IntegrationDisabled, IntegrationNotFound, IntegrationUnavailable, describe_exception
from app.core.logging import get_logger
from app.gateway.sessions import SessionKey, SessionPool, UpstreamSession
from app.integrations.adapters import adapter_registry
from app.integrations.base import AdapterContext, HealthReport, IntegrationAdapter
from app.secrets.manager import SecretManager, UpstreamCredentials

__all__ = ["IntegrationManager"]

log = get_logger(__name__)


class IntegrationManager:
    """Owns adapters, credentials, and upstream sessions for every integration."""

    def __init__(
        self,
        *,
        settings: Settings,
        catalog: ResolvedCatalog,
        secrets: SecretManager,
        pool: SessionPool,
    ) -> None:
        self._settings = settings
        self._catalog = catalog
        self._secrets = secrets
        self._pool = pool
        self._adapters: dict[str, IntegrationAdapter] = {}
        self._health: dict[str, HealthReport] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- catalog

    @property
    def catalog(self) -> ResolvedCatalog:
        """The current configuration snapshot."""
        return self._catalog

    @property
    def secrets(self) -> SecretManager:
        """The credential resolver, for callers that manage secrets directly."""
        return self._secrets

    @property
    def pool(self) -> SessionPool:
        """The session pool, for status reporting and invalidation."""
        return self._pool

    async def reload_from(self, store: ConfigStore) -> None:
        """Re-read configuration from disk and adopt it.

        Every writer of `integrations.yaml` or the lock file has to follow the
        write with this, or the manager keeps serving a snapshot that predates
        its own change — which is how an integration ends up reported as "not
        installed" immediately after being installed.
        """
        await self.reload(ResolvedCatalog.load(store, exposure_override=self._settings.tool_exposure_mode))

    async def reload(self, catalog: ResolvedCatalog) -> None:
        """Adopt a new configuration snapshot.

        Adapters are rebuilt only for integrations whose definition actually
        changed, and their sessions closed, so reloading configuration does not
        needlessly restart every upstream process.
        """
        async with self._lock:
            previous = self._catalog
            self._catalog = catalog
            changed: list[str] = []
            for integration_id, adapter in list(self._adapters.items()):
                new = catalog.integrations.get(integration_id)
                old = previous.integrations.get(integration_id)
                if new is None or old is None or new.manifest != old.manifest:
                    del self._adapters[integration_id]
                    changed.append(integration_id)
                else:
                    adapter.manifest = new.manifest  # keep the instance, refresh the view
        for integration_id in changed:
            await self._pool.invalidate(integration_id)
        if changed:
            log.info("integration_manager.reloaded", rebuilt=changed)

    def get(self, integration_id: str) -> ResolvedIntegration:
        """Look up one integration's merged configuration.

        Raises:
            IntegrationNotFound: No such integration is configured.
        """
        return self._catalog.get(integration_id)

    def enabled(self) -> list[ResolvedIntegration]:
        """Every integration the operator wants serving."""
        return self._catalog.enabled()

    def all(self) -> list[ResolvedIntegration]:
        """Every configured integration."""
        return self._catalog.all()

    # ---------------------------------------------------------------- adapters

    def adapter_for(self, integration_id: str) -> IntegrationAdapter:
        """The adapter handling `integration_id` (arch §49).

        Raises:
            IntegrationNotFound: No such integration.
            ValidationFailed: No adapter is registered for its source type.
        """
        adapter = self._adapters.get(integration_id)
        if adapter is not None:
            return adapter
        integration = self.get(integration_id)
        adapter = adapter_registry.create(self._context_for(integration))
        self._adapters[integration_id] = adapter
        return adapter

    def _context_for(self, integration: ResolvedIntegration) -> AdapterContext:
        """Build the adapter's view of the filesystem and its budgets."""
        source = integration.manifest.source
        return AdapterContext(
            manifest=integration.manifest,
            root=self._settings.integrations_dir / integration.id,
            cache_dir=self._settings.cache_dir / integration.id,
            logs_dir=self._settings.logs_dir,
            connect_timeout_seconds=min(
                source.startup_timeout_seconds, self._settings.upstream_connect_timeout_seconds
            ),
            request_timeout_seconds=source.request_timeout_seconds,
            allow_mutable_tags=self._settings.mutable_image_tags_allowed,
        )

    # ------------------------------------------------------------- credentials

    async def credentials_for(self, integration_id: str, principal: Principal | None = None) -> UpstreamCredentials:
        """Resolve this caller's credentials for one upstream (arch §20).

        Raises:
            SecretNotFound: A required credential is missing, or a required
                per-user credential was requested without an authenticated
                caller.
        """
        integration = self.get(integration_id)
        return await self._secrets.resolve_auth(
            integration.manifest.auth,
            principal=_subject(principal),
            extra=integration.manifest.secrets,
        )

    async def missing_secrets(self, integration_id: str, principal: Principal | None = None) -> list[str]:
        """Required credentials this caller does not have (arch §25).

        Health reports `AUTH_REQUIRED` from this rather than waiting for a call
        to fail with an upstream 401.
        """
        integration = self.get(integration_id)
        refs = list(integration.manifest.secrets)
        if integration.manifest.auth.secret is not None:
            refs.append(integration.manifest.auth.secret)
        return await self._secrets.check_requirements(refs, principal=_subject(principal))

    # ---------------------------------------------------------------- sessions

    @asynccontextmanager
    async def session_for(
        self, integration_id: str, principal: Principal | None = None
    ) -> AsyncIterator[UpstreamSession]:
        """Borrow a live session to an upstream, scoped to this caller.

        Raises:
            IntegrationNotFound: No such integration.
            IntegrationDisabled: The integration is switched off.
            IntegrationUnavailable: The upstream could not be reached.
        """
        integration = self.get(integration_id)
        if not integration.enabled:
            raise IntegrationDisabled(
                f"Integration {integration_id!r} is disabled. Run `mcp-hub enable {integration_id}` to turn it on.",
                integration=integration_id,
            )

        credentials = await self.credentials_for(integration_id, principal)
        adapter = self.adapter_for(integration_id)
        key = SessionKey(integration_id=integration_id, credential_identity=credentials.identity)

        async def factory() -> Any:
            return await adapter.transport_factory(credentials)

        session = await self._pool.acquire(
            key,
            factory,
            request_timeout_seconds=integration.manifest.source.request_timeout_seconds,
            connect_timeout_seconds=adapter.context.connect_timeout_seconds,
        )
        try:
            yield session
        finally:
            self._pool.release(session)

    async def disconnect(self, integration_id: str) -> int:
        """Close every session for an integration (arch §18)."""
        return await self._pool.invalidate(integration_id)

    # ------------------------------------------------------------------ health

    async def check_health(self, integration_id: str, principal: Principal | None = None) -> HealthReport:
        """Probe one integration and cache the result (arch §25).

        Configuration is checked before the network: a disabled integration or
        one missing credentials has a definite status already, and probing it
        would just produce a slower, less specific failure.
        """
        integration = self.get(integration_id)
        if not integration.enabled:
            return self._remember(integration_id, HealthReport(status=HealthStatus.DISABLED))
        if integration.needs_install:
            return self._remember(
                integration_id,
                HealthReport(
                    status=HealthStatus.UPDATE_REQUIRED,
                    detail=f"Not installed. Run `mcp-hub install {integration_id}`.",
                ),
            )

        try:
            missing = await self.missing_secrets(integration_id, principal)
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
            return self._remember(integration_id, HealthReport(status=HealthStatus.AUTH_REQUIRED, detail=str(exc)))
        if missing:
            return self._remember(
                integration_id,
                HealthReport(
                    status=HealthStatus.AUTH_REQUIRED,
                    detail=f"Missing credential(s): {', '.join(missing)}",
                ),
            )

        credentials: UpstreamCredentials | None = None
        try:
            credentials = await self.credentials_for(integration_id, principal)
            adapter = self.adapter_for(integration_id)
            report = await adapter.health_check(credentials)
        except Exception as exc:  # noqa: BLE001 - one bad upstream must not fail the sweep
            report = HealthReport(status=HealthStatus.UNAVAILABLE, detail=describe_exception(exc))
        return self._remember(integration_id, self._classify(integration, credentials, report))

    @staticmethod
    def _classify(
        integration: ResolvedIntegration,
        credentials: UpstreamCredentials | None,
        report: HealthReport,
    ) -> HealthReport:
        """Refine a failed probe into `AUTH_REQUIRED` where that is the real cause (arch §25).

        An integration that declares authentication, resolved no credential, and
        then failed to complete a handshake is almost certainly refusing us for
        want of a token rather than being down. The distinction matters because
        it is the operator's next action: one means "store a credential", the
        other means "the upstream is broken".

        It cannot be decided before the probe. An *optional* per-user credential
        is legitimately absent — an OAuth flow may supply the identity another
        way — so absence alone proves nothing until the provider declines to
        talk to us.
        """
        if report.status is not HealthStatus.UNAVAILABLE:
            return report
        if credentials is None or not credentials.is_anonymous:
            return report
        if integration.manifest.auth.type == "none":
            return report

        secret = integration.manifest.auth.secret
        hint = (
            f" No credential is configured — store one with `mcp-hub secrets set {secret.name}`."
            if secret is not None
            else " No credential is configured for this integration."
        )
        return HealthReport(
            status=HealthStatus.AUTH_REQUIRED,
            detail=report.detail + hint,
            latency_ms=report.latency_ms,
        )

    async def check_all(
        self, principal: Principal | None = None, *, integrations: Sequence[str] | None = None
    ) -> dict[str, HealthReport]:
        """Probe several integrations concurrently (arch §25).

        Bounded by `discovery_concurrency` so a hub with thirty integrations does
        not open thirty upstream connections at once on startup.
        """
        targets = list(integrations) if integrations is not None else [item.id for item in self.all()]
        limit = asyncio.Semaphore(self._settings.discovery_concurrency)

        async def probe(integration_id: str) -> tuple[str, HealthReport]:
            async with limit:
                return integration_id, await self.check_health(integration_id, principal)

        results = await asyncio.gather(*(probe(item) for item in targets), return_exceptions=True)
        reports: dict[str, HealthReport] = {}
        for outcome in results:
            if isinstance(outcome, BaseException):
                log.error("health.sweep_failed", error=str(outcome))
                continue
            integration_id, report = outcome
            reports[integration_id] = report
        return reports

    def _remember(self, integration_id: str, report: HealthReport) -> HealthReport:
        self._health[integration_id] = report
        return report

    def last_health(self, integration_id: str) -> HealthReport | None:
        """The most recent probe result, without probing again."""
        return self._health.get(integration_id)

    def health_snapshot(self) -> dict[str, HealthReport]:
        """Every cached health result, for `/api/health` and the CLI."""
        return dict(self._health)

    def is_serving(self, integration_id: str) -> bool:
        """Whether this integration should currently be routed to.

        Optimistic before the first probe: an integration that is enabled and
        installed is assumed serving until something says otherwise, so a hub
        that has just started does not report every tool as unavailable.
        """
        try:
            integration = self.get(integration_id)
        except IntegrationNotFound:
            return False
        if not integration.enabled or integration.needs_install:
            return False
        report = self._health.get(integration_id)
        return report.is_serving if report is not None else True

    async def require_serving(self, integration_id: str) -> ResolvedIntegration:
        """Fetch an integration, refusing if it cannot serve (arch §45).

        Raises:
            IntegrationNotFound: No such integration.
            IntegrationDisabled: Switched off.
            IntegrationUnavailable: Enabled but not currently usable.
        """
        integration = self.get(integration_id)
        if not integration.enabled:
            raise IntegrationDisabled(f"Integration {integration_id!r} is disabled.", integration=integration_id)
        report = self._health.get(integration_id)
        if report is not None and not report.is_serving:
            raise IntegrationUnavailable(
                f"Integration {integration_id!r} is {report.status.value.lower()}: {report.detail}",
                integration=integration_id,
                status=report.status.value,
            )
        return integration


def _subject(principal: Principal | None) -> str | None:
    """The identity per-user credentials key off, or `None` for a shared lookup.

    Falls back to the ambient request principal when the caller passes none, so
    an operation reached from a bound request (an API call, an MCP tool call, a
    CLI command) resolves *that* caller's credentials without every intermediate
    layer having to thread the principal through by hand. Outside any request
    the ambient principal is `ANONYMOUS`, which yields `None` — a shared lookup,
    exactly as before.
    """
    resolved = principal or current_principal()
    return resolved.subject if resolved.is_authenticated else None
