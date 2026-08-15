"""PyPI-packaged MCP servers (arch §8).

How the reference `git`, `fetch`, and `time` servers are consumed — arch §6.6
publishes them to PyPI as dated releases, pinned exactly.

Each version installs into its own `--target` directory rather than a virtual
environment. That keeps a version fully self-contained (rollback stays a pointer
swap) and makes the container case trivial: mount the directory and set
`PYTHONPATH`, with no interpreter state to reproduce.

`uv` is used when present because it is dramatically faster, falling back to
`pip`. Both are invoked with hash-agnostic exact version pins; what matters for
arch §55 is that the resolved version is recorded, and that is verified after
install rather than assumed.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import ClassVar

from app.config.models import PythonSource
from app.core.domain import SourceType
from app.core.errors import HubError, SupplyChainRejected, ValidationFailed
from app.core.logging import get_logger
from app.integrations.adapters.local import LocalAdapter
from app.integrations.base import AdapterContext, StagedBuild, VersionRef
from app.integrations.launcher import LaunchSpec
from app.integrations.process import require_binary, run_command

__all__ = ["PythonAdapter"]

log = get_logger(__name__)

_DEFAULT_INDEX = "https://pypi.org"
_INSTALL_TIMEOUT = 900.0


class PythonAdapter(LocalAdapter):
    """Installs and runs an MCP server distributed on PyPI."""

    source_type: ClassVar[SourceType] = SourceType.PYTHON

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        source = context.manifest.source
        if not isinstance(source, PythonSource):
            raise ValidationFailed(
                f"PythonAdapter cannot serve source type {source.type.value!r}.",
                integration=context.manifest.id,
            )
        self.source = source

    @property
    def index(self) -> str:
        """Package index base URL."""
        return str(self.source.index_url).rstrip("/") if self.source.index_url else _DEFAULT_INDEX

    # ------------------------------------------------------------- versioning

    async def resolve_latest(self) -> VersionRef:
        """Return the pinned version, or ask the index for the newest release.

        Raises:
            HubError: The index is unreachable or the distribution is unknown.
        """
        if self.source.version:
            return VersionRef(identifier=self.source.version, kind="version")

        import httpx2

        url = f"{self.index}/pypi/{self.source.package}/json"
        try:
            async with httpx2.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url, headers={"Accept": "application/json"})
        except httpx2.HTTPError as exc:
            raise HubError(
                f"Could not reach the package index for {self.source.package!r}: {exc}",
                integration=self.integration_id,
            ) from exc
        if response.status_code == 404:
            raise HubError(
                f"Distribution {self.source.package!r} was not found on {self.index}.",
                integration=self.integration_id,
            )
        if response.status_code >= 400:
            raise HubError(
                f"Package index returned {response.status_code} for {self.source.package!r}.",
                integration=self.integration_id,
            )
        version = response.json().get("info", {}).get("version")
        if not isinstance(version, str) or not version:
            raise HubError(
                f"Package index gave no version for {self.source.package!r}.",
                integration=self.integration_id,
            )
        return VersionRef(
            identifier=version, kind="version", metadata={"package": self.source.package, "index": self.index}
        )

    # ---------------------------------------------------------------- staging

    async def stage(self, version: VersionRef) -> StagedBuild:
        """Install `package==version` into this version's own target directory.

        Raises:
            HubError: The install failed.
        """
        self.context.ensure_dirs()
        target = self.context.version_dir(version)
        if (target / ".mcp-hub-install-complete").exists():
            log.info("python.stage_cached", integration=self.integration_id, version=version.display)
            return self._staged(version, target)

        staging = target.with_name(f".{target.name}.staging")
        shutil.rmtree(staging, ignore_errors=True)
        site = staging / "site-packages"
        site.mkdir(parents=True)

        requirement = f"{self.source.package}=={version.identifier}"
        try:
            await self._install(requirement, site)
            self._verify_installed(site, version)
            (staging / ".mcp-hub-install-complete").write_text(version.identifier, encoding="utf-8")
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
        log.info("python.staged", integration=self.integration_id, version=version.display, path=str(target))
        return self._staged(version, target)

    async def _install(self, requirement: str, site: Path) -> None:
        """Install into `site` with uv when available, else pip."""
        index_args: list[str] = []
        if self.source.index_url:
            index_args = ["--index-url", f"{self.index}/simple"]

        uv = shutil.which("uv")
        if uv:
            await run_command(
                [uv, "pip", "install", "--target", str(site), *index_args, requirement],
                timeout=_INSTALL_TIMEOUT,
            )
            return
        python = require_binary("python3", hint="Install Python to use python-sourced integrations.")
        await run_command(
            [python, "-m", "pip", "install", "--target", str(site), "--no-input", *index_args, requirement],
            timeout=_INSTALL_TIMEOUT,
        )

    def _verify_installed(self, site: Path, version: VersionRef) -> None:
        """Confirm the index installed the exact version requested.

        Raises:
            SupplyChainRejected: A different version arrived than was asked for.
        """
        normalised = self.source.package.replace("-", "_").lower()
        candidates = [path for path in site.glob("*.dist-info") if path.name.lower().startswith(f"{normalised}-")]
        if not candidates:
            raise HubError(
                f"Install reported success but no dist-info for {self.source.package!r} is present.",
                integration=self.integration_id,
            )
        installed = candidates[0].name.rsplit("-", 1)[0].split("-", 1)[-1]
        if installed != version.identifier:
            raise SupplyChainRejected(
                f"Requested {self.source.package}=={version.identifier} but "
                f"{installed!r} was installed. Refusing to promote it.",
                integration=self.integration_id,
                requested=version.identifier,
                installed=installed,
            )

    def _staged(self, version: VersionRef, path: Path) -> StagedBuild:
        spec = self.launch_spec(path)
        return StagedBuild(version=version, path=path, command=spec.command, env=dict(spec.env))

    # ------------------------------------------------------------------ launch

    def launch_spec(self, version_dir: Path) -> LaunchSpec:
        """Run the server as a module (or console script) off the staged tree."""
        contained = self.manifest.runtime.isolation == "container"
        base = Path("/srv") if contained else version_dir
        site = base / "site-packages"
        env = {
            "PYTHONPATH": str(site),
            "PYTHONUNBUFFERED": "1",  # stdio MCP needs unbuffered output
            "PYTHONDONTWRITEBYTECODE": "1",  # the version tree is mounted read-only
        }
        if self.source.binary:
            return LaunchSpec(
                command=(str(site / "bin" / self.source.binary), *self.source.args),
                cwd=version_dir,
                env=env,
            )
        module = self.source.module or self.source.package.replace("-", "_")
        return LaunchSpec(command=("python3", "-m", module, *self.source.args), cwd=version_dir, env=env)
