"""The MCP Registry client (arch §7).

Talks to the official registry at `registry.modelcontextprotocol.io`, which acts
as an app-store-like directory of MCP servers. The capabilities arch §7 names
are all here: `search_servers`, `get_server`, `get_versions`, `resolve_server`,
`validate_server`.

Two things this client is careful about.

**The registry is untrusted input.** Every response is parsed into a permissive
model and every URL it hands back is re-checked before the hub would fetch it. A
registry entry naming `http://169.254.169.254/` must not become a request the
hub makes on someone's behalf.

**Finding is not installing.** `resolve_server` produces a manifest *proposal*
and `validate_server` produces the disclosure arch §54 requires — repository,
owner, licence, runtime, requested environment, requested network and filesystem
access. Nothing is written and nothing is executed. The install flow (arch §56)
puts a human between this and any code running.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2

from app.config.models import IntegrationManifest
from app.core.clock import Clock, SystemClock
from app.core.domain import SourceType, TrustTier
from app.core.errors import HubError, ValidationFailed
from app.core.ids import is_valid_namespace
from app.core.logging import get_logger
from app.integrations.netguard import guard_url
from app.registry.models import RegistryServer, SearchResults

__all__ = ["InstallDisclosure", "RegistryClient"]

log = get_logger(__name__)

_USER_AGENT = "mcp-hub/0.1 (+https://github.com/modelcontextprotocol)"


@dataclass(frozen=True, slots=True)
class InstallDisclosure:
    """Everything an operator must see before installing (arch §54).

    Arch §54 lists exactly this: repository, owner, branch/commit, licence,
    runtime, requested environment variables, requested network access, requested
    filesystem access, requested permissions. Rendering it is the *only* thing
    standing between a registry entry and third-party code on the host.
    """

    server_name: str
    integration_id: str
    description: str
    repository: str
    owner: str
    version: str
    license: str
    source_type: SourceType
    trust: TrustTier
    runtime: str
    network: str
    filesystem: str
    requested_env: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    manifest: IntegrationManifest | None = None

    @property
    def requires_confirmation(self) -> bool:
        """Whether this install needs an explicit human decision (arch §7, §56)."""
        return self.trust.requires_explicit_trust

    def render(self) -> str:
        """The operator-facing disclosure, in arch §54's shape."""
        lines = [
            "Installing:",
            f"  {self.server_name}",
            "",
            f"Integration id:  {self.integration_id}",
            f"Repository:      {self.repository or 'not published'}",
            f"Owner:           {self.owner or 'unknown'}",
            f"Version:         {self.version or 'unpinned'}",
            f"Licence:         {self.license or 'unknown'}",
            f"Trust tier:      {self.trust.value}",
            "",
            f"Runtime:         {self.runtime}",
            f"Network:         {self.network}",
            f"Filesystem:      {self.filesystem}",
            f"Secrets:         {', '.join(self.requested_env) if self.requested_env else 'none requested'}",
        ]
        if self.warnings:
            lines.extend(["", "Warnings:"])
            lines.extend(f"  ! {warning}" for warning in self.warnings)
        return "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        """JSON form for the REST API."""
        return {
            "server": self.server_name,
            "integration_id": self.integration_id,
            "description": self.description,
            "repository": self.repository,
            "owner": self.owner,
            "version": self.version,
            "license": self.license,
            "source_type": self.source_type.value,
            "trust": self.trust.value,
            "runtime": self.runtime,
            "network": self.network,
            "filesystem": self.filesystem,
            "requested_env": list(self.requested_env),
            "warnings": list(self.warnings),
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class RegistryClient:
    """Searches and resolves servers from the official MCP Registry."""

    def __init__(
        self,
        *,
        base_url: str = "https://registry.modelcontextprotocol.io",
        timeout_seconds: float = 15.0,
        cache_ttl_seconds: float = 900.0,
        clock: Clock | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._cache_ttl = cache_ttl_seconds
        self._clock = clock or SystemClock()
        self._cache: dict[str, _CacheEntry] = {}

    # ------------------------------------------------------------------ search

    async def search_servers(self, query: str = "", *, limit: int = 25, cursor: str | None = None) -> SearchResults:
        """Search the registry (arch §7).

        Raises:
            HubError: The registry is unreachable or returned an error.
        """
        params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if query:
            params["search"] = query
        if cursor:
            params["cursor"] = cursor

        payload = await self._get("/v0/servers", params=params, cache_key=f"search:{query}:{limit}:{cursor}")
        return SearchResults.model_validate(payload)

    async def get_server(self, name: str) -> RegistryServer:
        """Fetch one server by its canonical name (arch §7).

        Raises:
            HubError: Not found, or the registry is unreachable.
        """
        from urllib.parse import quote

        payload = await self._get(f"/v0/servers/{quote(name, safe='')}", cache_key=f"server:{name}")
        # The registry returns either the server itself or a wrapper around it.
        if isinstance(payload, dict) and "server" in payload:
            payload = payload["server"]
        return RegistryServer.model_validate(payload)

    async def get_versions(self, name: str) -> list[str]:
        """List published versions of a server (arch §7)."""
        from urllib.parse import quote

        try:
            payload = await self._get(f"/v0/servers/{quote(name, safe='')}/versions", cache_key=f"versions:{name}")
        except HubError:
            # Not every registry deployment exposes the versions collection;
            # falling back to the entry's own version beats failing the command.
            server = await self.get_server(name)
            return [server.version] if server.version else []
        servers = payload.get("servers", payload) if isinstance(payload, dict) else payload
        versions: list[str] = []
        for entry in servers if isinstance(servers, list) else []:
            version = entry.get("version") if isinstance(entry, dict) else None
            if isinstance(version, str) and version:
                versions.append(version)
        return versions

    # ---------------------------------------------------------------- resolve

    async def resolve_server(self, name: str, *, integration_id: str | None = None) -> IntegrationManifest:
        """Turn a registry entry into a hub manifest proposal (arch §7, §56).

        Nothing is written to disk and nothing is installed. The result is a
        candidate for an operator to inspect.

        Raises:
            ValidationFailed: The entry describes nothing installable, or the
                derived integration id is not usable.
            HubError: The registry is unreachable.
        """
        server = await self.get_server(name)
        candidate_id = integration_id or _derive_integration_id(server)
        payload = server.to_manifest_payload(candidate_id)
        try:
            return IntegrationManifest.model_validate(payload)
        except Exception as exc:
            raise ValidationFailed(
                f"Registry entry {name!r} could not be turned into a valid manifest: {exc}",
                server=name,
            ) from exc

    async def validate_server(self, name: str, *, integration_id: str | None = None) -> InstallDisclosure:
        """Build the pre-install disclosure arch §54 requires.

        Raises:
            ValidationFailed: The entry is not installable.
            HubError: The registry is unreachable.
        """
        server = await self.get_server(name)
        candidate_id = integration_id or _derive_integration_id(server)
        manifest = await self.resolve_server(name, integration_id=candidate_id)

        warnings: list[str] = []
        if server.is_deprecated:
            warnings.append("The registry marks this server as DEPRECATED.")
        if not server.repository_url:
            warnings.append("No source repository is published — the code cannot be reviewed before install.")
        if manifest.trust is TrustTier.COMMUNITY:
            warnings.append("Third-party code will run on this host. It is sandboxed, but treat it as untrusted.")
        chosen = server.preferred_installation()
        if chosen and chosen[0] == "package" and not getattr(chosen[1], "version", ""):
            warnings.append("No version is pinned; the newest published version would be installed.")

        requested_env = tuple(name for name in manifest.runtime.allowed_env if name not in ("PATH", "HOME"))
        if requested_env:
            warnings.append(f"The server asks for environment variable(s): {', '.join(requested_env)}.")

        runtime = manifest.runtime
        return InstallDisclosure(
            server_name=server.name,
            integration_id=candidate_id,
            description=server.description,
            repository=server.repository_url,
            owner=_owner_of(server),
            version=server.version,
            license=str(server.meta.get("license") or server.repository.get("license") or ""),
            source_type=manifest.source.type,
            trust=manifest.trust,
            runtime=(
                "container (read-only root, non-root user)"
                if runtime.isolation == "container"
                else "subprocess (NOT contained)"
            ),
            network={"none": "none", "outbound": "outbound internet", "host": "HOST NETWORK"}.get(
                runtime.network, runtime.network
            ),
            filesystem="read-only" if runtime.read_only_root else "writable root",
            requested_env=requested_env,
            warnings=tuple(warnings),
            manifest=manifest,
        )

    # ------------------------------------------------------------------ http

    async def _get(self, path: str, *, params: dict[str, Any] | None = None, cache_key: str | None = None) -> Any:
        """Perform a cached GET against the registry.

        Raises:
            HubError: Unreachable, or a non-success status.
        """
        if cache_key is not None:
            cached = self._cache.get(cache_key)
            if cached is not None and cached.expires_at > self._clock.monotonic():
                return cached.value

        url = f"{self._base_url}{path}"
        guard_url(url, require_https=True, allow_loopback=True)

        try:
            async with httpx2.AsyncClient(
                timeout=self._timeout, follow_redirects=False, headers={"User-Agent": _USER_AGENT}
            ) as client:
                response = await client.get(url, params=params)
        except httpx2.HTTPError as exc:
            raise HubError(
                f"Could not reach the MCP Registry at {self._base_url}: {exc}",
                registry=self._base_url,
            ) from exc

        if response.status_code == 404:
            raise HubError(f"The registry has no entry at {path}.", path=path, status=404)
        if response.status_code >= 400:
            raise HubError(
                f"The MCP Registry returned {response.status_code} for {path}.",
                path=path,
                status=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise HubError("The MCP Registry returned a response that is not JSON.", path=path) from exc

        if cache_key is not None:
            self._cache[cache_key] = _CacheEntry(value=payload, expires_at=self._clock.monotonic() + self._cache_ttl)
        return payload

    def clear_cache(self) -> None:
        """Drop cached registry responses."""
        self._cache.clear()


def _derive_integration_id(server: RegistryServer) -> str:
    """Pick a hub integration id for a registry entry.

    Raises:
        ValidationFailed: No usable id can be derived from the name.
    """
    import re

    candidate = re.sub(r"[^a-z0-9-]+", "-", server.short_name.lower()).strip("-")
    candidate = re.sub(r"-{2,}", "-", candidate)[:64].strip("-")
    # Registry names are frequently `mcp-server-foo` or `foo-mcp-server`; the
    # redundant part makes for a worse namespace than the thing it integrates.
    for noise in ("mcp-server-", "-mcp-server", "server-mcp-", "-mcp", "mcp-"):
        if candidate.startswith(noise) or candidate.endswith(noise):
            candidate = candidate.replace(noise, "", 1).strip("-")
    if not candidate or not is_valid_namespace(candidate.replace("-", "_")):
        raise ValidationFailed(
            f"Could not derive a usable integration id from registry name {server.name!r}. Pass one explicitly.",
            server=server.name,
        )
    return candidate


def _owner_of(server: RegistryServer) -> str:
    """Best-effort publisher identity, for the disclosure."""
    url = server.repository_url
    if url:
        parts = [part for part in url.rstrip("/").split("/") if part]
        if len(parts) >= 2:
            return parts[-2]
    if "/" in server.name:
        return server.name.split("/", 1)[0]
    return ""
