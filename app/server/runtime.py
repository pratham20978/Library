"""The composition root.

Every component the hub needs is constructed here, once, in dependency order,
and torn down in reverse. Nothing else in the codebase constructs a database, a
secret manager, or a session pool — they receive what they need, which is what
makes the whole system testable against fakes.

`HubRuntime` is also what makes arch §72 true. The MCP endpoint, the REST API,
and the CLI each build a runtime and call the same `LifecycleService` and
`UpdateManager`. There is no second implementation of "enable an integration"
for the command line to drift away from.

Startup is ordered so that a failure is survivable: infrastructure first, then
configuration, then discovery. Discovery contacts upstreams and is deliberately
best-effort — arch §45 requires the hub to come up and serve whatever is healthy,
so an unreachable Jira delays nothing and blocks nobody.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from app.audit.logger import AuditLogger
from app.auth.tokens import HubTokenVerifier, TokenIssuer
from app.config.catalog import ResolvedCatalog
from app.config.settings import Settings, load_settings
from app.config.store import ConfigStore
from app.core.logging import configure_logging, get_logger
from app.database.session import Database
from app.gateway.discovery import ToolDiscoverer
from app.gateway.gateway import Gateway
from app.gateway.sessions import SessionPool
from app.gateway.tool_registry import ToolRegistry
from app.integrations.backup import BackupManager
from app.integrations.lifecycle import LifecycleService
from app.integrations.locks import LockManager, build_lock_manager
from app.integrations.manager import IntegrationManager
from app.integrations.updater import UpdateManager
from app.jobs.queue import Job, JobQueue
from app.policy.engine import PolicyEngine
from app.policy.ratelimit import RateLimiter, build_rate_limiter
from app.registry.client import RegistryClient
from app.secrets.manager import SecretManager, build_secret_manager

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.server.mcp_server import HubMcpServer

__all__ = ["HubRuntime"]

log = get_logger(__name__)


@dataclass
class HubRuntime:
    """Owns every long-lived component for one process."""

    settings: Settings
    store: ConfigStore
    database: Database
    secrets: SecretManager
    audit: AuditLogger
    rate_limiter: RateLimiter
    locks: LockManager
    policy: PolicyEngine
    registry: ToolRegistry
    pool: SessionPool
    manager: IntegrationManager
    discoverer: ToolDiscoverer
    gateway: Gateway
    updater: UpdateManager
    lifecycle: LifecycleService
    backups: BackupManager
    verifier: HubTokenVerifier
    jobs: JobQueue
    registry_client: RegistryClient
    redis: Any = None
    _mcp_server: HubMcpServer | None = None
    _started: bool = False

    # ------------------------------------------------------------------ build

    @classmethod
    async def create(cls, settings: Settings | None = None, **overrides: object) -> Self:
        """Construct every component in dependency order.

        Nothing here contacts an upstream or opens a listening socket; that is
        `start`'s job. Building and starting are separate so a CLI command can
        build a runtime, run one operation, and never pay for tool discovery.
        """
        settings = settings or load_settings(**overrides)
        configure_logging(level=settings.log_level, json_output=settings.json_logs)

        store = ConfigStore.from_settings(settings)
        database = Database(settings.database_url, echo=settings.database_echo, is_production=settings.is_production)
        redis = await _connect_redis(settings.redis_url)

        secrets = build_secret_manager(settings, session_factory=database.session_factory)
        audit = AuditLogger(session_factory=database.session_factory)
        rate_limiter = build_rate_limiter(redis)
        locks = build_lock_manager(redis)

        catalog = ResolvedCatalog.load(store, exposure_override=settings.tool_exposure_mode)
        policy = PolicyEngine(
            rate_limiter=rate_limiter,
            require_authentication=catalog.policies.require_authentication and settings.auth_required,
            global_rate_limit=catalog.policies.global_rate_limit,
        )

        pool = SessionPool(
            max_sessions=settings.upstream_max_sessions,
            idle_timeout_seconds=settings.upstream_session_idle_seconds,
        )
        manager = IntegrationManager(settings=settings, catalog=catalog, secrets=secrets, pool=pool)
        tool_registry = ToolRegistry()
        discoverer = ToolDiscoverer(
            manager=manager,
            registry=tool_registry,
            policy=policy,
            concurrency=settings.discovery_concurrency,
        )
        gateway = Gateway(
            manager=manager,
            registry=tool_registry,
            policy=policy,
            audit=audit,
            discoverer=discoverer,
            exposure_mode=catalog.exposure_mode,
        )
        backups = BackupManager(settings.backups_dir, retention=settings.backup_retention)
        updater = UpdateManager(
            manager=manager,
            store=store,
            locks=locks,
            backups=backups,
            audit=audit,
            discoverer=discoverer,
            lock_timeout_seconds=settings.lock_timeout_seconds,
            rollback_on_failure=settings.rollback_on_failure,
        )
        lifecycle = LifecycleService(
            settings=settings,
            store=store,
            manager=manager,
            updater=updater,
            discoverer=discoverer,
            backups=backups,
            locks=locks,
            audit=audit,
        )
        verifier = HubTokenVerifier(
            secret=settings.auth_secret.get_secret_value(),
            admin_tokens=tuple(token.get_secret_value() for token in settings.admin_tokens),
        )
        jobs = JobQueue(
            session_factory=database.session_factory,
            concurrency=1 if not settings.allow_parallel_updates else 2,
        )
        registry_client = RegistryClient(
            base_url=settings.registry_url,
            timeout_seconds=settings.registry_timeout_seconds,
            cache_ttl_seconds=settings.registry_cache_ttl_seconds,
        )

        runtime = cls(
            settings=settings,
            store=store,
            database=database,
            secrets=secrets,
            audit=audit,
            rate_limiter=rate_limiter,
            locks=locks,
            policy=policy,
            registry=tool_registry,
            pool=pool,
            manager=manager,
            discoverer=discoverer,
            gateway=gateway,
            updater=updater,
            lifecycle=lifecycle,
            backups=backups,
            verifier=verifier,
            jobs=jobs,
            registry_client=registry_client,
            redis=redis,
        )
        runtime._register_job_handlers()
        return runtime

    def _register_job_handlers(self) -> None:
        """Bind long-running lifecycle operations to the job queue (arch §29).

        Handlers are thin: they translate a job's parameters into a call on the
        same service the CLI uses, so a queued update and a command-line update
        run identical code (arch §72).
        """
        from app.core.domain import JobKind

        async def run_update(job: Job) -> dict[str, Any]:
            plan, outcomes = await self.updater.update(
                names=job.integrations,
                all_integrations=bool(job.parameters.get("all")),
                exclude=tuple(job.parameters.get("exclude") or ()),
                force=bool(job.parameters.get("force")),
                rollback_on_failure=job.parameters.get("rollback_on_failure"),
                parallel=self.settings.allow_parallel_updates,
                job_id=job.id,
            )
            return {
                "plan": plan.to_payload(),
                "outcomes": [outcome.summary() for outcome in outcomes],
                "succeeded": sum(1 for outcome in outcomes if outcome.succeeded),
                "failed": sum(1 for outcome in outcomes if not outcome.succeeded),
            }

        async def run_install(job: Job) -> dict[str, Any]:
            results = [
                await self.lifecycle.install(integration_id, enable=bool(job.parameters.get("enable", True)))
                for integration_id in job.integrations
            ]
            return {"installed": results}

        async def run_remove(job: Job) -> dict[str, Any]:
            results = [
                await self.lifecycle.remove(
                    integration_id,
                    purge_secrets=bool(job.parameters.get("purge_secrets", True)),
                    purge_backups=bool(job.parameters.get("purge_backups", False)),
                )
                for integration_id in job.integrations
            ]
            return {"removed": results}

        async def run_rollback(job: Job) -> dict[str, Any]:
            results = [
                await self.lifecycle.rollback(integration_id, version=job.parameters.get("version"))
                for integration_id in job.integrations
            ]
            return {"rolled_back": results}

        async def run_health(job: Job) -> dict[str, Any]:
            return await self.lifecycle.health(integrations=job.integrations or None)

        async def run_refresh(job: Job) -> dict[str, Any]:
            return await self.lifecycle.refresh(integrations=job.integrations or None)

        for kind, handler in (
            (JobKind.UPDATE, run_update),
            (JobKind.INSTALL, run_install),
            (JobKind.REMOVE, run_remove),
            (JobKind.ROLLBACK, run_rollback),
            (JobKind.HEALTH_CHECK, run_health),
            (JobKind.TOOL_REFRESH, run_refresh),
        ):
            self.jobs.register(kind, handler)

    # -------------------------------------------------------------- lifecycle

    async def start(self, *, discover: bool = True) -> None:
        """Bring the runtime up.

        Args:
            discover: Contact upstreams and populate the tool registry. CLI
                commands that only touch configuration skip it.
        """
        if self._started:
            return

        if not self.settings.is_production:
            await self.database.ensure_schema()
        await self.audit.start()
        await self.pool.start()
        await self.jobs.start()

        if discover:
            # Best-effort by design (arch §45): a hub with one dead upstream must
            # still come up and serve the others.
            results = await self.discoverer.discover_all()
            healthy = sum(1 for item in results.values() if item.ok)
            log.info(
                "runtime.started",
                integrations=len(results),
                healthy=healthy,
                tools=len(self.registry),
                exposure_mode=self.gateway.exposure_mode.value,
            )
            self._publish_metrics()
        else:
            log.info("runtime.started", discovery="skipped")

        self._started = True

    async def stop(self) -> None:
        """Tear everything down in reverse order."""
        if not self._started:
            await self._close_infrastructure()
            return
        await self.jobs.stop()
        await self.pool.stop()
        await self.audit.stop()
        await self._close_infrastructure()
        self._started = False
        log.info("runtime.stopped")

    async def _close_infrastructure(self) -> None:
        await self.secrets.clear_cache()
        if self.redis is not None:
            with __import__("contextlib").suppress(Exception):
                await self.redis.aclose()
        await self.database.dispose()

    def _publish_metrics(self) -> None:
        """Seed gauges so a dashboard has values before the first tool call."""
        from app.core.metrics import DISCOVERY_TOOLS, INTEGRATION_HEALTH, SESSION_COUNT

        for integration in self.manager.all():
            DISCOVERY_TOOLS.labels(integration.id).set(len(self.registry.for_integration(integration.id)))
            report = self.manager.last_health(integration.id)
            if report is not None:
                INTEGRATION_HEALTH.labels(integration.id, report.status.value).set(1.0 if report.is_serving else 0.0)
        SESSION_COUNT.set(len(self.pool.describe()))

    # ------------------------------------------------------------ accessors

    @property
    def mcp_server(self) -> HubMcpServer:
        """The MCP endpoint, built on first use."""
        if self._mcp_server is None:
            from app.server.mcp_server import HubMcpServer

            self._mcp_server = HubMcpServer(
                self.gateway,
                request_state_keys=self.settings.request_state_keys,
            )
        return self._mcp_server

    def token_issuer(self) -> TokenIssuer:
        """Mint hub tokens.

        Raises:
            NotAuthenticated: No signing secret is configured.
        """
        return TokenIssuer(
            self.settings.auth_secret.get_secret_value(),
            ttl_seconds=self.settings.token_ttl_seconds,
        )

    def status_payload(self) -> dict[str, Any]:
        """A single view of what this process is doing right now.

        Built here because it spans components no single service owns — the
        gateway's exposure mode, the session pool, the secret providers. Both
        `GET /api/status` and `mcp-hub status` render this, so the two can never
        disagree about the hub's state (arch §72).
        """
        from app import __version__

        integrations = self.lifecycle.list_integrations()
        return {
            "version": __version__,
            "environment": self.settings.env,
            "mcp_endpoint": self.settings.mcp_path,
            "exposure_mode": self.gateway.exposure_mode.value,
            "integrations": {
                "total": len(integrations),
                "enabled": sum(1 for item in integrations if item.enabled),
                "healthy": sum(1 for item in integrations if item.health == "HEALTHY"),
            },
            "tools": len(self.registry),
            "sessions": self.pool.describe(),
            "secret_providers": list(self.secrets.provider_names),
            "coordination": "redis" if self.redis is not None else "in-process",
        }

    async def reload_configuration(self) -> ResolvedCatalog:
        """Re-read the YAML contract and adopt it."""
        catalog = ResolvedCatalog.load(self.store, exposure_override=self.settings.tool_exposure_mode)
        await self.manager.reload(catalog)
        self.gateway.set_exposure_mode(catalog.exposure_mode)
        return catalog

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()


async def _connect_redis(url: str | None) -> Redis | None:
    """Connect to Redis, or return `None` and let the fallbacks apply.

    A configured-but-unreachable Redis is a warning rather than a startup
    failure: the hub degrades to process-local locks and rate limits, which is
    strictly better than refusing to serve at all (arch §45). The warning says
    exactly what was lost.
    """
    if not url:
        return None
    try:
        from redis.asyncio import Redis

        client: Redis = Redis.from_url(url, decode_responses=False)
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - degrade rather than refuse to start
        log.error(
            "runtime.redis_unavailable",
            error=str(exc),
            detail="Falling back to process-local locks and rate limits. Multi-replica coordination is NOT active.",
        )
        return None
    log.info("runtime.redis_connected")
    return client
