"""Shared behaviour for locally-run MCP servers.

Git, npm, Python, and Docker sources differ entirely in how they *acquire* a
server and not at all in how they *talk* to one: every local server is a child
process speaking MCP over stdin/stdout, launched inside the sandbox its manifest
describes. That connection path is written once here.

The order of operations in `transport_factory` is the security-relevant part:
resolve credentials, filter the environment down to the manifest's allowlist,
build the sandboxed argv, and only then spawn. A subclass supplies the server's
own command and nothing else, so no adapter can accidentally hand a child
process the hub's environment.
"""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from app.config.models import LockEntry
from app.core.clock import utcnow
from app.core.domain import HealthStatus
from app.core.errors import IntegrationUnavailable, describe_exception
from app.core.logging import get_logger
from app.integrations.base import HealthReport, IntegrationAdapter, StagedBuild, VersionRef
from app.integrations.launcher import LaunchSpec, build_launch_argv, filter_environment

if TYPE_CHECKING:
    from typing import Any

    from mcp.client._transport import Transport

    from app.secrets.manager import UpstreamCredentials

__all__ = ["LocalAdapter"]

log = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]


class LocalAdapter(IntegrationAdapter):
    """Base for adapters whose server runs as a sandboxed child process."""

    @abstractmethod
    def launch_spec(self, version_dir: Path) -> LaunchSpec:
        """How the installed server starts, before sandboxing.

        Args:
            version_dir: The promoted version's directory.

        Returns:
            The server's own command, working directory, and non-secret env.
        """

    # ------------------------------------------------------------- connection

    async def transport_factory(self, credentials: UpstreamCredentials) -> Transport:
        """Spawn the promoted server under its sandbox and return a stdio transport.

        Raises:
            IntegrationUnavailable: Nothing is installed for this integration.
        """
        return self._transport_for(self.resolve_current_dir(), credentials)

    async def validate_staged(
        self, staged: StagedBuild, credentials: UpstreamCredentials
    ) -> tuple[HealthReport, list[Any]]:
        """Smoke-test the staged build by launching *it*, not the live one (arch §15).

        This is the isolated test instance arch §15 requires: the running version
        keeps serving throughout, and a staged build that cannot start is
        discarded without traffic ever reaching it.
        """
        import time

        from mcp.client.client import Client

        started = time.monotonic()
        try:
            transport = self._transport_for(staged.path, credentials)
            async with Client(transport, read_timeout_seconds=self.context.request_timeout_seconds) as client:
                result = await client.list_tools()
                info = client.server_info
                return (
                    HealthReport(
                        status=HealthStatus.HEALTHY,
                        detail=f"staged build answered with {len(result.tools)} tools",
                        latency_ms=(time.monotonic() - started) * 1000,
                        tool_count=len(result.tools),
                        protocol_version=client.protocol_version,
                        server_version=getattr(info, "version", None) if info else None,
                    ),
                    list(result.tools),
                )
        except Exception as exc:  # noqa: BLE001 - reported to the caller, which aborts the update
            log.warning(
                "local.staged_validation_failed",
                integration=self.integration_id,
                version=staged.version.display,
                error=describe_exception(exc),
            )
            return (
                HealthReport(
                    status=HealthStatus.UNAVAILABLE,
                    detail=describe_exception(exc),
                    latency_ms=(time.monotonic() - started) * 1000,
                ),
                [],
            )

    def _transport_for(self, version_dir: Path, credentials: UpstreamCredentials) -> Transport:
        """Build a sandboxed stdio transport for a specific version directory."""
        from mcp.client.stdio import StdioServerParameters, stdio_client

        spec = self.launch_spec(version_dir)

        env = filter_environment(
            self.manifest.runtime,
            adapter_env=spec.env,
            credential_env=credentials.env,
        )
        argv, process_env, cwd = build_launch_argv(
            spec,
            self.manifest.runtime,
            trust=self.manifest.trust,
            integration_id=self.integration_id,
            env=env,
            repo_root=_REPO_ROOT,
        )
        log.debug(
            "local.launch",
            integration=self.integration_id,
            isolation=self.manifest.runtime.isolation,
            argv0=argv[0],
            env_names=sorted(env),  # names only: values may be credentials
        )

        parameters = StdioServerParameters(
            command=argv[0],
            args=list(argv[1:]),
            env=process_env,
            cwd=str(cwd) if cwd else None,
        )
        # The server's stderr goes to a per-integration log so `mcp-hub logs <id>`
        # can show why a server failed to start, rather than losing it to DEVNULL.
        return stdio_client(parameters, errlog=self._open_error_log())

    def resolve_current_dir(self) -> Path:
        """The promoted version's directory.

        Raises:
            IntegrationUnavailable: Nothing has been installed and promoted.
        """
        link = self.context.current_link
        if not link.exists():
            raise IntegrationUnavailable(
                f"Integration {self.integration_id!r} is not installed. "
                f"Run `mcp-hub install {self.integration_id}` first.",
                integration=self.integration_id,
            )
        return link.resolve()

    def _open_error_log(self) -> TextIO:
        """Append-only stderr sink for this integration's server process.

        The handle is owned by the stdio transport, which closes it when the
        session ends — hence no context manager here.
        """
        self.context.logs_dir.mkdir(parents=True, exist_ok=True)
        path = self.context.logs_dir / f"{self.integration_id}.log"
        return path.open("a", encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------ lock

    async def current_version(self, lock: LockEntry | None) -> VersionRef | None:
        if lock is None:
            return None
        identifier = lock.resolved_commit or lock.resolved_version or lock.resolved_digest
        if not identifier:
            return None
        kind = "commit" if lock.resolved_commit else ("digest" if lock.resolved_digest else "version")
        return VersionRef(identifier=identifier, kind=kind)

    def build_lock_entry(self, version: VersionRef, *, staged: StagedBuild | None = None) -> LockEntry:
        """Record the exact artifact installed (arch §33, §55)."""
        fields: dict[str, object] = {
            "source_type": self.source_type,
            "installed_path": str(staged.path) if staged else None,
            "updated_at": utcnow(),
        }
        match version.kind:
            case "commit":
                fields["resolved_commit"] = version.identifier
                fields["repository"] = version.metadata.get("repository")
                fields["branch"] = version.metadata.get("branch")
            case "digest":
                fields["resolved_digest"] = version.identifier
                fields["resolved_version"] = version.metadata.get("tag")
            case _:
                fields["resolved_version"] = version.identifier
        return LockEntry.model_validate({k: v for k, v in fields.items() if v is not None})
