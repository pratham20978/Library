"""Remote MCP services (arch §8, §61).

Jira, Figma, and Notion publish MCP servers as hosted Streamable HTTP endpoints.
The hub proxies them; it does not clone, build, or run anything — arch §6.1 and
§6.2 say so explicitly, and arch §61 extends that to updates: the provider
controls their deployment, so a git-style update makes no sense.

"Updating" a remote integration therefore means re-checking it: connect,
initialize, note the protocol and server version, and rediscover its tools. If
the provider shipped a new tool this morning, a refresh is what makes it appear —
which is exactly why arch §12 forbids hard-coding tool lists.

Everything installed-shaped is a no-op here: there is no version directory to
stage, promote, or roll back. Saying that plainly beats pretending otherwise and
leaving an operator to wonder why `rollback figma` did nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import httpx2

from app.config.models import LockEntry, RemoteSource
from app.core.clock import utcnow
from app.core.domain import SourceType
from app.core.errors import IntegrationUnavailable, ValidationFailed
from app.core.logging import get_logger
from app.core.redaction import redact_url
from app.integrations.base import AdapterContext, IntegrationAdapter, StagedBuild, VersionRef
from app.integrations.netguard import guard_url

if TYPE_CHECKING:
    from mcp.client._transport import Transport

    from app.secrets.manager import UpstreamCredentials

__all__ = ["RemoteAdapter"]

log = get_logger(__name__)


class RemoteAdapter(IntegrationAdapter):
    """Proxies a provider-hosted MCP endpoint."""

    source_type: ClassVar[SourceType] = SourceType.REMOTE

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        source = context.manifest.source
        if not isinstance(source, RemoteSource):
            raise ValidationFailed(
                f"RemoteAdapter cannot serve source type {source.type.value!r}.",
                integration=context.manifest.id,
            )
        self.source = source

    @property
    def endpoint(self) -> str:
        """The upstream URL."""
        return str(self.source.endpoint)

    # ------------------------------------------------------------- versioning

    async def resolve_latest(self) -> VersionRef:
        """Probe the endpoint and report what the provider is currently running.

        There is no version to *choose* — this reports the protocol version the
        upstream negotiates, which is the only thing about a remote service the
        hub can pin (arch §33).

        Raises:
            IntegrationUnavailable: The endpoint is unreachable.
        """
        self._guard()
        from mcp.client.client import Client

        try:
            transport = await self._transport(headers={})
            async with Client(transport, read_timeout_seconds=self.context.connect_timeout_seconds) as client:
                protocol = client.protocol_version or "unknown"
                info = client.server_info
                return VersionRef(
                    identifier=protocol,
                    kind="protocol",
                    display=protocol,
                    metadata={
                        "endpoint": self.endpoint,
                        "server_name": getattr(info, "name", "") if info else "",
                        "server_version": getattr(info, "version", "") if info else "",
                    },
                )
        except Exception as exc:
            raise IntegrationUnavailable(
                f"Could not reach the remote MCP endpoint for {self.integration_id!r}: {exc}",
                integration=self.integration_id,
                endpoint=redact_url(self.endpoint),
            ) from exc

    async def current_version(self, lock: LockEntry | None) -> VersionRef | None:
        if lock is None:
            return None
        protocol = lock.protocol_version or "unknown"
        return VersionRef(identifier=protocol, kind="protocol", display=protocol)

    async def stage(self, version: VersionRef) -> StagedBuild:
        """No-op: there is nothing to download for a hosted service (arch §61)."""
        return StagedBuild(
            version=version,
            path=self.context.root,
            notes=("remote service — nothing installed locally",),
        )

    async def promote(self, staged: StagedBuild) -> None:
        """No-op: nothing local serves this integration."""
        return None

    async def discard(self, staged: StagedBuild) -> None:
        """No-op: nothing was staged."""
        return None

    async def uninstall(self) -> None:
        """No-op: removing the catalog entry is the whole of removal here."""
        return None

    async def rollback(self, version: VersionRef) -> None:
        """Remote services have no hub-side version to roll back to (arch §61).

        Raises:
            RollbackFailed: Always. The provider owns this deployment.
        """
        from app.core.errors import RollbackFailed

        raise RollbackFailed(
            f"{self.integration_id!r} is a provider-hosted remote service, so the hub has no "
            "previous version to restore. Disable the integration if it is misbehaving.",
            integration=self.integration_id,
        )

    # ------------------------------------------------------------- connection

    async def transport_factory(self, credentials: UpstreamCredentials) -> Transport:
        """Build a Streamable HTTP transport carrying this caller's credentials."""
        self._guard()
        return await self._transport(headers=dict(credentials.headers), query=dict(credentials.query))

    async def _transport(self, *, headers: dict[str, str], query: dict[str, str] | None = None) -> Transport:
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        url = self.endpoint
        if query:
            from urllib.parse import urlencode, urlparse, urlunparse

            parsed = urlparse(url)
            merged = f"{parsed.query}&{urlencode(query)}" if parsed.query else urlencode(query)
            url = urlunparse(parsed._replace(query=merged))

        http_client = create_mcp_http_client(
            headers={**self.source.headers, **headers},
            timeout=self._timeout(),
        )
        # Redirects are refused by default (arch §31): a 302 to an attacker-chosen
        # host would carry this caller's Authorization header with it.
        http_client.follow_redirects = self.source.max_redirects > 0
        http_client.max_redirects = self.source.max_redirects
        if not self.source.verify_tls:
            log.warning("remote.tls_verification_disabled", integration=self.integration_id)
        return streamable_http_client(url, http_client=http_client)

    def _timeout(self) -> httpx2.Timeout:
        return httpx2.Timeout(
            self.context.connect_timeout_seconds,
            read=self.source.request_timeout_seconds,
            write=self.context.connect_timeout_seconds,
            pool=self.context.connect_timeout_seconds,
        )

    def _guard(self) -> None:
        """Refuse the endpoint if it fails the SSRF and transport-security checks."""
        guard_url(
            self.endpoint,
            allowed_hosts=self.source.allowed_hosts,
            require_https=self.source.verify_tls,
            allow_loopback=True,
        )

    # ------------------------------------------------------------------ lock

    def build_lock_entry(self, version: VersionRef, *, staged: StagedBuild | None = None) -> LockEntry:
        """Record what a remote integration can meaningfully pin (arch §33)."""
        return LockEntry(
            source_type=SourceType.REMOTE,
            endpoint=self.endpoint,
            protocol_version=version.identifier if version.kind == "protocol" else None,
            server_version=version.metadata.get("server_version") or None,
            updated_at=utcnow(),
        )
