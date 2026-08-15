"""npm-packaged MCP servers (arch §8).

How the reference filesystem, memory, and sequential-thinking servers are
consumed — arch §6.6 publishes them to npm as dated releases, and pins them.

Each version installs into its own directory with its own `node_modules`, which
is what makes rollback a pointer swap: two versions of the same package coexist
without either seeing the other's dependency tree.

Version resolution goes through the registry's HTTP API rather than `npm view`,
so it does not depend on a working npm CLI and, more importantly, so the exact
version string the hub records is the one the registry reported — arch §55 wants
that written down, not inferred.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import ClassVar

from app.config.models import NpmSource
from app.core.domain import SourceType
from app.core.errors import HubError, SupplyChainRejected, ValidationFailed
from app.core.logging import get_logger
from app.integrations.adapters.local import LocalAdapter
from app.integrations.base import AdapterContext, StagedBuild, VersionRef
from app.integrations.launcher import LaunchSpec
from app.integrations.process import require_binary, run_command

__all__ = ["NpmAdapter"]

log = get_logger(__name__)

_DEFAULT_REGISTRY = "https://registry.npmjs.org"
_INSTALL_TIMEOUT = 900.0


class NpmAdapter(LocalAdapter):
    """Installs and runs an MCP server distributed on npm."""

    source_type: ClassVar[SourceType] = SourceType.NPM

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        source = context.manifest.source
        if not isinstance(source, NpmSource):
            raise ValidationFailed(
                f"NpmAdapter cannot serve source type {source.type.value!r}.",
                integration=context.manifest.id,
            )
        self.source = source

    @property
    def registry(self) -> str:
        """Registry base URL."""
        return str(self.source.registry).rstrip("/") if self.source.registry else _DEFAULT_REGISTRY

    # ------------------------------------------------------------- versioning

    async def resolve_latest(self) -> VersionRef:
        """Return the pinned version, or ask the registry for `latest`.

        Raises:
            HubError: The registry is unreachable or the package is unknown.
        """
        if self.source.version:
            return VersionRef(identifier=self.source.version, kind="version")

        import httpx2

        url = f"{self.registry}/{self.source.package}/latest"
        try:
            async with httpx2.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url, headers={"Accept": "application/json"})
        except httpx2.HTTPError as exc:
            raise HubError(
                f"Could not reach the npm registry for {self.source.package!r}: {exc}",
                integration=self.integration_id,
            ) from exc
        if response.status_code == 404:
            raise HubError(
                f"Package {self.source.package!r} was not found in {self.registry}.",
                integration=self.integration_id,
            )
        if response.status_code >= 400:
            raise HubError(
                f"npm registry returned {response.status_code} for {self.source.package!r}.",
                integration=self.integration_id,
            )
        version = response.json().get("version")
        if not isinstance(version, str) or not version:
            raise HubError(
                f"npm registry gave no version for {self.source.package!r}.",
                integration=self.integration_id,
            )
        return VersionRef(
            identifier=version, kind="version", metadata={"package": self.source.package, "registry": self.registry}
        )

    # ---------------------------------------------------------------- staging

    async def stage(self, version: VersionRef) -> StagedBuild:
        """Install `package@version` into its own directory.

        Runs with `--ignore-scripts`: an npm lifecycle script executes arbitrary
        code on the *host*, before any container isolation applies, which arch §55
        calls out as something to refuse unless explicitly approved.

        Raises:
            HubError: The install failed.
        """
        self.context.ensure_dirs()
        target = self.context.version_dir(version)
        marker = target / ".mcp-hub-install-complete"
        if marker.exists():
            log.info("npm.stage_cached", integration=self.integration_id, version=version.display)
            return self._staged(version, target)

        npm = require_binary("npm", hint="Install Node.js to use npm-sourced integrations.")
        staging = target.with_name(f".{target.name}.staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)

        try:
            # A private package.json keeps npm from treating the directory as
            # publishable and from walking up to a parent project's config.
            (staging / "package.json").write_text(
                json.dumps({"name": f"mcp-hub-{self.integration_id}", "private": True, "version": "0.0.0"}),
                encoding="utf-8",
            )
            await run_command(
                [
                    npm,
                    "install",
                    f"{self.source.package}@{version.identifier}",
                    "--prefix",
                    str(staging),
                    "--registry",
                    self.registry,
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--omit=dev",
                ],
                cwd=staging,
                timeout=_INSTALL_TIMEOUT,
            )
            self._verify_installed(staging, version)
            marker_staging = staging / ".mcp-hub-install-complete"
            marker_staging.write_text(version.identifier, encoding="utf-8")
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
        log.info("npm.staged", integration=self.integration_id, version=version.display, path=str(target))
        return self._staged(version, target)

    def _verify_installed(self, root: Path, version: VersionRef) -> None:
        """Confirm the registry actually gave us the version we asked for.

        Raises:
            SupplyChainRejected: The installed version differs from the requested
                one — a registry that substitutes versions is not one to trust.
        """
        manifest_path = root / "node_modules" / self.source.package / "package.json"
        if not manifest_path.exists():
            raise HubError(
                f"npm reported success but {self.source.package!r} is not present under node_modules.",
                integration=self.integration_id,
            )
        try:
            installed = json.loads(manifest_path.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError) as exc:
            raise HubError(
                f"Installed package manifest is unreadable: {exc}", integration=self.integration_id
            ) from exc
        if installed != version.identifier:
            raise SupplyChainRejected(
                f"Requested {self.source.package}@{version.identifier} but the registry installed "
                f"{installed!r}. Refusing to promote a version that was not asked for.",
                integration=self.integration_id,
                requested=version.identifier,
                installed=installed,
            )

    def _staged(self, version: VersionRef, path: Path) -> StagedBuild:
        spec = self.launch_spec(path)
        return StagedBuild(version=version, path=path, command=spec.command, env=dict(spec.env))

    # ------------------------------------------------------------------ launch

    def launch_spec(self, version_dir: Path) -> LaunchSpec:
        """Run the package's bin entry directly through Node.

        Invoking the resolved JavaScript file rather than `npx` keeps a launch
        from ever touching the network — `npx` will happily fetch a package it
        cannot find locally, which would defeat the pinning above.
        """
        contained = self.manifest.runtime.isolation == "container"
        base = Path("/srv") if contained else version_dir
        package_root = version_dir / "node_modules" / self.source.package
        relative = self._entry_point(package_root)
        entry = base / "node_modules" / self.source.package / relative
        return LaunchSpec(
            command=("node", str(entry), *self.source.args),
            cwd=version_dir,
            env={"NODE_ENV": "production"},
        )

    def _entry_point(self, package_root: Path) -> str:
        """Find the package's executable script, relative to its own root."""
        try:
            payload = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "index.js"

        binary = payload.get("bin")
        if isinstance(binary, str):
            return binary.lstrip("./")
        if isinstance(binary, dict) and binary:
            wanted = self.source.binary
            if wanted and wanted in binary:
                return str(binary[wanted]).lstrip("./")
            return str(next(iter(binary.values()))).lstrip("./")
        main = payload.get("main")
        return str(main).lstrip("./") if isinstance(main, str) else "index.js"
