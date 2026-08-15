"""In-process MCP servers (arch §8).

A builtin integration is an `mcp.server.lowlevel.Server` living inside the hub,
reached over an in-memory transport with no socket, subprocess, or container in
between. That makes it the right shape for two things: functionality the hub
implements itself, and the mock upstreams the end-to-end tests point at (arch §42)
— a test can exercise the entire gateway path without binding a port.

The entrypoint is deliberately constrained to `app.*`. Arch §37 forbids the hub
from offering arbitrary code execution, and an unconstrained import path in a
YAML file is exactly that with extra steps: anyone who can write a manifest could
name any importable callable. The check lives in `BuiltinSource` so it fails at
configuration load, and is re-asserted here so it cannot be bypassed by
constructing a manifest programmatically.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, ClassVar

from app.config.models import BuiltinSource, LockEntry
from app.core.clock import utcnow
from app.core.domain import SourceType
from app.core.errors import IntegrationUnavailable, ValidationFailed
from app.core.logging import get_logger
from app.integrations.base import AdapterContext, IntegrationAdapter, StagedBuild, VersionRef

if TYPE_CHECKING:
    from mcp.client._transport import Transport

    from app.secrets.manager import UpstreamCredentials

__all__ = ["BuiltinAdapter"]

log = get_logger(__name__)

_ALLOWED_PREFIX = "app."


class BuiltinAdapter(IntegrationAdapter):
    """Serves an MCP server implemented inside the hub."""

    source_type: ClassVar[SourceType] = SourceType.BUILTIN

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        source = context.manifest.source
        if not isinstance(source, BuiltinSource):
            raise ValidationFailed(
                f"BuiltinAdapter cannot serve source type {source.type.value!r}.",
                integration=context.manifest.id,
            )
        self.source = source
        self._server: Any | None = None

    # ------------------------------------------------------------- versioning

    async def resolve_latest(self) -> VersionRef:
        """Builtins version with the hub itself."""
        from app import __version__

        return VersionRef(identifier=__version__, kind="version", metadata={"entrypoint": self.source.entrypoint})

    async def current_version(self, lock: LockEntry | None) -> VersionRef | None:
        from app import __version__

        recorded = lock.resolved_version if lock else None
        return VersionRef(identifier=recorded or __version__, kind="version")

    async def stage(self, version: VersionRef) -> StagedBuild:
        """Import the entrypoint to prove it works; nothing is written to disk.

        Raises:
            IntegrationUnavailable: The entrypoint cannot be imported or called.
        """
        self._load()
        return StagedBuild(version=version, path=self.context.root, notes=("builtin — shipped with the hub",))

    async def promote(self, staged: StagedBuild) -> None:
        """No-op: a builtin is whatever the running hub imports."""
        return None

    async def discard(self, staged: StagedBuild) -> None:
        """No-op: nothing was staged."""
        return None

    async def uninstall(self) -> None:
        """No-op: builtins ship with the hub and are removed by disabling them."""
        return None

    async def rollback(self, version: VersionRef) -> None:
        """A builtin's version is the hub's version.

        Raises:
            RollbackFailed: Always — roll back the hub deployment instead.
        """
        from app.core.errors import RollbackFailed

        raise RollbackFailed(
            f"{self.integration_id!r} is built into the hub, so it has no independent version "
            "to restore. Roll back the hub deployment itself.",
            integration=self.integration_id,
        )

    # ------------------------------------------------------------- connection

    async def transport_factory(self, credentials: UpstreamCredentials) -> Transport:
        """Return an in-memory transport bound to the builtin server.

        Credentials are not applied: an in-process server shares the hub's
        identity by construction, and there is no channel to attach them to.
        A builtin needing per-user behaviour should read the request context.
        """
        from mcp.client._memory import InMemoryTransport

        return InMemoryTransport(self._load())

    def _load(self) -> Any:
        """Import and call the entrypoint factory, once per adapter instance.

        Raises:
            IntegrationUnavailable: Import failed, the attribute is missing, or
                the factory did not return a server.
        """
        if self._server is not None:
            return self._server

        module_name, _, attribute = self.source.entrypoint.partition(":")
        if not module_name.startswith(_ALLOWED_PREFIX):
            raise ValidationFailed(
                f"Builtin entrypoint {self.source.entrypoint!r} must live under `{_ALLOWED_PREFIX}` "
                "— the hub does not import arbitrary modules named in configuration (arch §37).",
                integration=self.integration_id,
            )
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise IntegrationUnavailable(
                f"Could not import builtin module {module_name!r} for {self.integration_id!r}: {exc}",
                integration=self.integration_id,
            ) from exc

        factory = getattr(module, attribute, None)
        if factory is None:
            raise IntegrationUnavailable(
                f"Module {module_name!r} has no attribute {attribute!r}.",
                integration=self.integration_id,
            )
        server = factory() if callable(factory) else factory

        from mcp.server.lowlevel import Server

        if not isinstance(server, Server):
            raise IntegrationUnavailable(
                f"Builtin entrypoint {self.source.entrypoint!r} returned "
                f"{type(server).__name__}, expected an mcp.server.lowlevel.Server.",
                integration=self.integration_id,
            )
        self._server = server
        log.info("builtin.loaded", integration=self.integration_id, entrypoint=self.source.entrypoint)
        return server

    def build_lock_entry(self, version: VersionRef, *, staged: StagedBuild | None = None) -> LockEntry:
        return LockEntry(
            source_type=SourceType.BUILTIN,
            resolved_version=version.identifier,
            updated_at=utcnow(),
        )
