"""MCP Registry data model (arch §7).

Models for the official registry at `registry.modelcontextprotocol.io`. They are
deliberately permissive — `extra="ignore"` — because the registry is an external
service that will add fields, and a hub that fails to parse a search result
because of a new key is worse than one that ignores it.

The important method here is `to_manifest`. A registry entry is *someone else's*
description of a server; turning it into a hub manifest is where the hub decides
how much to trust it. The answer is: not much. Everything arrives as
`COMMUNITY` trust with the restrictive sandbox default, whatever the registry
claims about the publisher, and the install flow (arch §54, §56) puts a human in
front of that before any code runs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.domain import SourceType, TrustTier
from app.core.logging import get_logger

__all__ = ["RegistryPackage", "RegistryRemote", "RegistryServer", "SearchResults"]

log = get_logger(__name__)


class _Lenient(BaseModel):
    """Base for registry models: tolerate unknown fields the registry adds."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)


class RegistryPackage(_Lenient):
    """A runnable package a registry entry points at."""

    registry_type: str = Field(default="", alias="registryType", description="`npm`, `pypi`, `oci`, …")
    identifier: str = Field(default="", description="Package name or image reference.")
    version: str = Field(default="", description="Published version.")
    runtime_hint: str = Field(default="", alias="runtimeHint", description="Suggested runtime, e.g. `npx`.")
    transport_type: str = Field(default="", alias="transport", description="`stdio` or `streamable-http`.")
    environment_variables: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="environmentVariables",
        description="Variables the server asks for. Shown to the operator before install (arch §54).",
    )

    def to_source_type(self) -> SourceType | None:
        """Map the registry's package kind onto a hub source type."""
        match self.registry_type.lower():
            case "npm":
                return SourceType.NPM
            case "pypi" | "pip" | "python":
                return SourceType.PYTHON
            case "oci" | "docker":
                return SourceType.DOCKER
            case _:
                return None

    @property
    def requested_env(self) -> list[str]:
        """Names of environment variables this package wants (arch §54)."""
        names: list[str] = []
        for entry in self.environment_variables:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name:
                names.append(name)
        return names


class RegistryRemote(_Lenient):
    """A hosted endpoint a registry entry points at."""

    type: str = Field(default="", description="`streamable-http` or `sse`.")
    url: str = Field(default="", description="Endpoint URL.")
    headers: list[dict[str, Any]] = Field(default_factory=list, description="Headers the service expects.")


class RegistryServer(_Lenient):
    """One server as the registry describes it."""

    name: str = Field(default="", description="Canonical name, e.g. `io.github.owner/server`.")
    description: str = Field(default="", description="What the server does.")
    version: str = Field(default="", description="Latest published version.")
    status: str = Field(default="active", description="`active` or `deprecated`.")
    repository: dict[str, Any] = Field(default_factory=dict, description="Source repository metadata.")
    packages: list[RegistryPackage] = Field(default_factory=list, description="Installable packages.")
    remotes: list[RegistryRemote] = Field(default_factory=list, description="Hosted endpoints.")
    website_url: str = Field(default="", alias="websiteUrl", description="Homepage.")
    published_at: datetime | None = Field(default=None, alias="publishedAt", description="Publication time.")
    meta: dict[str, Any] = Field(default_factory=dict, alias="_meta", description="Registry-internal metadata.")

    @property
    def short_name(self) -> str:
        """The last path segment, which is what an operator types."""
        return self.name.rsplit("/", 1)[-1] if self.name else ""

    @property
    def repository_url(self) -> str:
        """Source repository URL, when the entry carries one."""
        url = self.repository.get("url")
        return str(url) if isinstance(url, str) else ""

    @property
    def is_deprecated(self) -> bool:
        """Whether the registry marks this server as deprecated."""
        return self.status.lower() == "deprecated"

    def preferred_installation(self) -> tuple[Literal["remote", "package"], Any] | None:
        """Pick how to install this server.

        A hosted endpoint is preferred over a package: nothing is downloaded and
        no third-party code runs on the hub's host, which is the same reasoning
        arch §6.1/§6.5 apply to Jira and Notion.
        """
        for remote in self.remotes:
            if remote.url and remote.type in ("streamable-http", "", "http"):
                return "remote", remote
        for package in self.packages:
            if package.to_source_type() is not None:
                return "package", package
        return None

    def to_manifest_payload(self, integration_id: str) -> dict[str, Any]:
        """Render as an `IntegrationManifest` payload (arch §56).

        Raises:
            ValidationFailed: The entry describes nothing the hub can run.
        """
        from app.core.errors import ValidationFailed

        chosen = self.preferred_installation()
        if chosen is None:
            raise ValidationFailed(
                f"Registry entry {self.name!r} has no installable package or usable remote endpoint.",
                server=self.name,
            )

        kind, item = chosen
        if kind == "remote":
            assert isinstance(item, RegistryRemote)
            source: dict[str, Any] = {"type": "remote", "endpoint": item.url}
            # A vendor-hosted endpoint still runs no code here, so it is the
            # lower-risk tier even though the entry itself is untrusted.
            trust = TrustTier.REMOTE_OFFICIAL
        else:
            assert isinstance(item, RegistryPackage)
            source_type = item.to_source_type()
            assert source_type is not None
            source = _package_source(source_type, item)
            # Third-party code that will execute on this host. Always COMMUNITY,
            # whatever the registry says — arch §46 requires explicit trust.
            trust = TrustTier.COMMUNITY

        return {
            "id": integration_id,
            "name": self.short_name or integration_id,
            "description": self.description[:500],
            "namespace": integration_id.replace("-", "_"),
            "source": source,
            "trust": trust.value,
            "homepage": self.website_url or self.repository_url or None,
            # The restrictive default sandbox. An operator widens it deliberately
            # by editing the written manifest.
            "runtime": {
                "isolation": "container",
                "read_only_root": True,
                "network": "outbound",
                "run_as_non_root": True,
                "allowed_env": ["PATH", "HOME", *_requested_env(item)],
            },
            "risk_level": "WRITE",
        }


def _package_source(source_type: SourceType, package: RegistryPackage) -> dict[str, Any]:
    """Build the manifest `source` block for a registry package."""
    match source_type:
        case SourceType.NPM:
            return {
                "type": "npm",
                "package": package.identifier,
                **({"version": package.version} if package.version else {}),
            }
        case SourceType.PYTHON:
            return {
                "type": "python",
                "package": package.identifier,
                "module": package.identifier.replace("-", "_"),
                **({"version": package.version} if package.version else {}),
            }
        case SourceType.DOCKER:
            image, _, tag = package.identifier.partition(":")
            return {"type": "docker", "image": image, "tag": tag or package.version or "latest"}
        case _:  # pragma: no cover - guarded by to_source_type
            raise ValueError(f"Unsupported registry package type {source_type!r}.")


def _requested_env(item: Any) -> list[str]:
    """Environment variables a package asked for, for the sandbox allowlist."""
    return item.requested_env if isinstance(item, RegistryPackage) else []


class SearchResults(_Lenient):
    """A page of registry search results."""

    servers: list[RegistryServer] = Field(default_factory=list, description="Matching servers.")
    next_cursor: str | None = Field(default=None, alias="nextCursor", description="Cursor for the next page.")
    total: int | None = Field(default=None, description="Total matches, when the registry reports one.")

    def __len__(self) -> int:
        return len(self.servers)
