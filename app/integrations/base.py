"""The integration adapter contract (arch §48, §49).

An adapter answers source-specific questions — *where does this server come
from, what version is available, how do I launch it* — and nothing else. It owns
no policy, writes no audit records, and never decides whether an update should
happen. That separation is what arch §49 is really about: the gateway knows
about "an MCP server", not about Jira, and adding a source kind means adding an
adapter, never editing the core.

The lifecycle is deliberately split into small primitives rather than one
`update()` that does everything:

    resolve_latest  ->  stage  ->  validate  ->  promote  ->  activate
                                     |
                                     +-- discard (on any failure)

`UpdateManager` sequences them, holds the lock, takes backups, runs health
checks, and rolls back. An adapter that only implements the primitives cannot
accidentally skip a safety step, because it never controls the order.

Arch §48's named methods (`install`, `update`, `rollback`, `health_check`, …)
are all present on the base class, implemented in terms of those primitives, so
the documented interface exists without duplicating the pipeline.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from app.config.models import IntegrationManifest, LockEntry
from app.core.clock import utcnow
from app.core.domain import HealthStatus, SourceType, TransportKind
from app.core.errors import HubError, describe_exception
from app.core.logging import get_logger

if TYPE_CHECKING:
    from mcp.client._transport import Transport

    from app.secrets.manager import UpstreamCredentials

__all__ = [
    "AdapterContext",
    "HealthReport",
    "IntegrationAdapter",
    "StagedBuild",
    "VersionRef",
]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class VersionRef:
    """An exact, resolvable version of an integration's server (arch §33).

    Whichever field identifies this source kind is set; `identifier` is the one
    that ends up in the lock file and in operator output.
    """

    identifier: str
    """The pin: a commit sha, package version, image digest, or protocol version."""

    kind: str
    """What `identifier` is — `commit`, `version`, `digest`, or `protocol`."""

    display: str = ""
    """Short human-readable form, e.g. an abbreviated sha."""

    metadata: Mapping[str, str] = field(default_factory=dict)
    """Extra provenance recorded alongside: branch, tag, registry, image."""

    def __post_init__(self) -> None:
        if not self.display:
            object.__setattr__(self, "display", self.identifier[:12] if self.kind == "commit" else self.identifier)

    def __str__(self) -> str:
        return self.display


@dataclass(frozen=True, slots=True)
class StagedBuild:
    """A version prepared but not yet serving (arch §15).

    Staging happens in a directory the running version never touches, so a
    failed build or a failed smoke test costs nothing and needs no cleanup of
    live state. `promote` is the only step that changes what serves traffic.
    """

    version: VersionRef
    path: Path
    """Immutable version directory, `runtime/integrations/<id>/versions/<ref>/`."""

    command: tuple[str, ...] = ()
    """Argv that launches this build. Empty for remote sources."""

    env: Mapping[str, str] = field(default_factory=dict)
    """Non-secret environment for the launch. Credentials are merged in later."""

    notes: tuple[str, ...] = ()
    """Anything the operator should see in the update plan."""


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The result of probing one integration (arch §25)."""

    status: HealthStatus
    detail: str = ""
    latency_ms: float | None = None
    tool_count: int = 0
    protocol_version: str | None = None
    server_version: str | None = None
    checked_at: Any = field(default_factory=utcnow)

    @property
    def is_serving(self) -> bool:
        """Whether tools from this integration should be routed to."""
        return self.status.can_serve


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """Everything an adapter needs that it should not construct itself.

    Passing paths and timeouts in — rather than letting adapters read `Settings`
    — is what makes them testable against a temporary directory.
    """

    manifest: IntegrationManifest
    root: Path
    """`runtime/integrations/<id>/`, this integration's private directory."""

    cache_dir: Path
    """Shared scratch space for clones and downloads."""

    logs_dir: Path
    """Where a local server's stderr is captured for `mcp-hub logs`."""

    connect_timeout_seconds: float = 30.0
    request_timeout_seconds: float = 60.0
    allow_mutable_tags: bool = True
    """Whether an image may be pulled by tag rather than digest (arch §55)."""

    @property
    def versions_dir(self) -> Path:
        """Immutable per-version directories (arch §15)."""
        return self.root / "versions"

    @property
    def current_link(self) -> Path:
        """Pointer to the serving version. Swapping it is what `promote` does."""
        return self.root / "current"

    def version_dir(self, version: VersionRef) -> Path:
        """Where `version` lives once staged."""
        return self.versions_dir / _safe_component(version.identifier)

    def ensure_dirs(self) -> None:
        """Create this integration's directory tree."""
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


def _safe_component(value: str) -> str:
    """Make an identifier safe as a single path component.

    Version identifiers come from registries and image digests, so they can
    contain `/`, `:` and worse. Anything outside a conservative set is replaced
    rather than escaped, because the directory name only has to be stable and
    unique, not reversible.
    """
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value).strip("._-")
    return cleaned[:120] or "unknown"


class IntegrationAdapter(abc.ABC):
    """Base class for every source kind (arch §48).

    Subclasses implement the abstract primitives. The concrete methods below are
    arch §48's named interface, written once in terms of those primitives.
    """

    source_type: ClassVar[SourceType]
    """Which `source.type` this adapter handles. Used to register it."""

    def __init__(self, context: AdapterContext) -> None:
        self.context = context
        self.manifest = context.manifest

    @property
    def integration_id(self) -> str:
        """The integration this adapter instance is bound to."""
        return self.manifest.id

    @property
    def transport(self) -> TransportKind:
        """How the hub will speak MCP to this server."""
        return self.manifest.source.transport

    # ------------------------------------------------------------ primitives

    @abc.abstractmethod
    async def resolve_latest(self) -> VersionRef:
        """Determine the newest version satisfying the manifest's pin.

        For a branch this is the branch head; for a pinned commit, tag, version,
        or digest it is that pin, so a pinned integration reports "no update
        available" rather than drifting (arch §33).

        Raises:
            HubError: The source could not be reached or the pin does not exist.
        """

    @abc.abstractmethod
    async def current_version(self, lock: LockEntry | None) -> VersionRef | None:
        """The version currently serving, from the lock file, or `None`."""

    @abc.abstractmethod
    async def stage(self, version: VersionRef) -> StagedBuild:
        """Fetch, build, and validate `version` without disturbing the live one.

        Must be safe to call while the current version is serving, and must leave
        nothing behind that a later `discard` cannot remove.

        Raises:
            HubError: Fetching or building failed.
        """

    @abc.abstractmethod
    async def transport_factory(self, credentials: UpstreamCredentials) -> Transport:
        """Build a connected MCP transport for the serving version.

        Called per upstream session, with credentials already resolved for the
        calling principal. Adapters attach them to the channel their source
        expects — headers for remote, environment for a child process — and never
        look them up themselves.

        Raises:
            IntegrationUnavailable: Nothing is installed, or the server will not start.
        """

    # ------------------------------------------------------- default behaviour

    async def promote(self, staged: StagedBuild) -> None:
        """Make `staged` the version that serves traffic (arch §15).

        Repointing `current` is a single atomic rename, so a reader either sees
        the old version or the new one — never a partially updated tree. The old
        version directory is left in place, which is what makes rollback a
        pointer swap rather than a re-download.
        """
        import os

        link = self.context.current_link
        target = staged.path
        temporary = link.with_name(f".{link.name}.swap")
        temporary.unlink(missing_ok=True)
        os.symlink(target, temporary, target_is_directory=True)
        os.replace(temporary, link)
        log.info(
            "integration.promoted",
            integration=self.integration_id,
            version=staged.version.display,
            path=str(target),
        )

    async def discard(self, staged: StagedBuild) -> None:
        """Remove a staged build that will not be promoted.

        Never touches the currently promoted version, even if a caller passes it
        in by mistake — a failed update must not be able to delete what is
        serving.
        """
        import shutil

        if not staged.path.exists():
            return
        if self.context.current_link.exists() and self.context.current_link.resolve() == staged.path.resolve():
            log.warning(
                "integration.discard_skipped",
                integration=self.integration_id,
                reason="path is the currently promoted version",
            )
            return
        shutil.rmtree(staged.path, ignore_errors=True)
        log.info("integration.discarded", integration=self.integration_id, version=staged.version.display)

    async def install(self, version: VersionRef | None = None) -> VersionRef:
        """Stage and promote a version (arch §48).

        A convenience for a first install where there is nothing to roll back to.
        Updates go through `UpdateManager`, which adds backup, validation, and
        automatic rollback.
        """
        self.context.ensure_dirs()
        target = version or await self.resolve_latest()
        staged = await self.stage(target)
        await self.promote(staged)
        return target

    async def uninstall(self) -> None:
        """Remove every artifact this integration installed (arch §18, §48)."""
        import shutil

        self.context.current_link.unlink(missing_ok=True)
        if self.context.versions_dir.exists():
            shutil.rmtree(self.context.versions_dir, ignore_errors=True)
        log.info("integration.uninstalled", integration=self.integration_id)

    async def rollback(self, version: VersionRef) -> None:
        """Repoint `current` at an already-installed version (arch §19, §48).

        Raises:
            RollbackFailed: That version's directory is gone, so there is nothing
                to roll back to and an operator must reinstall.
        """
        from app.core.errors import RollbackFailed

        target = self.context.version_dir(version)
        if not target.exists():
            raise RollbackFailed(
                f"Version {version.display} of {self.integration_id!r} is no longer on disk. "
                "Reinstall it explicitly.",
                integration=self.integration_id,
                version=version.identifier,
            )
        await self.promote(StagedBuild(version=version, path=target))

    async def start(self) -> None:
        """Prepare the integration to serve (arch §48).

        Local sources start lazily on first use — the session pool launches the
        process — so the base implementation only verifies the artifact is there.
        """
        return None

    async def stop(self) -> None:
        """Stop serving (arch §48).

        Session teardown belongs to the pool, which owns the live sessions, so
        adapters override this only when they hold their own resources.
        """
        return None

    async def health_check(self, credentials: UpstreamCredentials) -> HealthReport:
        """Probe the upstream: connect, initialize, list tools (arch §25, §40).

        Implemented once here for every source kind, because "is it healthy" has
        the same meaning regardless of where the server came from: can a session
        be established and does it answer `tools/list` within budget.
        """
        import time

        from mcp.client.client import Client

        started = time.monotonic()
        try:
            transport = await self.transport_factory(credentials)
            async with Client(transport, read_timeout_seconds=self.context.request_timeout_seconds) as client:
                result = await client.list_tools()
                elapsed = (time.monotonic() - started) * 1000
                info = client.server_info
                return HealthReport(
                    status=HealthStatus.HEALTHY,
                    detail=f"{len(result.tools)} tools",
                    latency_ms=elapsed,
                    tool_count=len(result.tools),
                    protocol_version=client.protocol_version,
                    server_version=getattr(info, "version", None) if info else None,
                )
        except HubError as exc:
            return HealthReport(
                status=HealthStatus.UNAVAILABLE,
                detail=exc.message,
                latency_ms=(time.monotonic() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001 - a probe reports failure, it does not raise
            return HealthReport(
                status=HealthStatus.UNAVAILABLE,
                detail=describe_exception(exc),
                latency_ms=(time.monotonic() - started) * 1000,
            )

    async def validate_staged(
        self, staged: StagedBuild, credentials: UpstreamCredentials
    ) -> tuple[HealthReport, list[Any]]:
        """Smoke-test a staged build before it is promoted (arch §15).

        Launches the *staged* version in isolation — never the one currently
        serving — completes an MCP handshake, and lists its tools. That is the
        gate arch §15 puts between "built" and "promoted": a version that cannot
        initialise or enumerate its tools never reaches traffic.

        The base implementation is right for sources with no local artifact
        (remote, builtin), where staged and current are the same thing. Local
        adapters override it to launch from the staged directory.

        Returns:
            The probe result and the tools the staged version reported.
        """
        import time

        from mcp.client.client import Client

        started = time.monotonic()
        try:
            transport = await self.transport_factory(credentials)
            async with Client(transport, read_timeout_seconds=self.context.request_timeout_seconds) as client:
                result = await client.list_tools()
                info = client.server_info
                report = HealthReport(
                    status=HealthStatus.HEALTHY,
                    detail=f"staged build answered with {len(result.tools)} tools",
                    latency_ms=(time.monotonic() - started) * 1000,
                    tool_count=len(result.tools),
                    protocol_version=client.protocol_version,
                    server_version=getattr(info, "version", None) if info else None,
                )
                return report, list(result.tools)
        except Exception as exc:  # noqa: BLE001 - the caller decides whether this aborts the update
            return (
                HealthReport(
                    status=HealthStatus.UNAVAILABLE,
                    detail=describe_exception(exc),
                    latency_ms=(time.monotonic() - started) * 1000,
                ),
                [],
            )

    async def discover_tools(self, credentials: UpstreamCredentials) -> Sequence[Any]:
        """List the upstream's tools (arch §12, §48).

        Returns raw `mcp.types.Tool` objects; namespacing and classification are
        the gateway's job, not the adapter's.
        """
        from mcp.client.client import Client

        transport = await self.transport_factory(credentials)
        async with Client(transport, read_timeout_seconds=self.context.request_timeout_seconds) as client:
            result = await client.list_tools()
            return result.tools

    @asynccontextmanager
    async def connect(self, credentials: UpstreamCredentials) -> AsyncIterator[Any]:
        """Open a client session to the upstream (arch §48).

        A convenience for one-off calls. The gateway uses the session pool
        instead, which keeps sessions alive across calls.
        """
        from mcp.client.client import Client

        transport = await self.transport_factory(credentials)
        async with Client(transport, read_timeout_seconds=self.context.request_timeout_seconds) as client:
            yield client

    async def get_version(self, lock: LockEntry | None) -> str:
        """Human-readable current version (arch §48)."""
        version = await self.current_version(lock)
        return version.display if version else "not installed"

    async def get_latest_version(self) -> str:
        """Human-readable newest available version (arch §48)."""
        return (await self.resolve_latest()).display

    def build_lock_entry(self, version: VersionRef, *, staged: StagedBuild | None = None) -> LockEntry:
        """Render the lock-file entry for `version` (arch §62).

        Overridden by adapters that pin something other than a version string.
        """
        return LockEntry(
            source_type=self.source_type,
            resolved_version=version.identifier,
            installed_path=str(staged.path) if staged else None,
            updated_at=utcnow(),
        )
