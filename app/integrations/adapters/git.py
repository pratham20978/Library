"""Git-sourced MCP servers (arch §8, §33).

Clone a repository, pin the exact commit, build it, and run it sandboxed. This
is how the hub consumes `github/github-mcp-server` and
`brave/brave-search-mcp-server` — arch §6.3 and §6.4 name both.

The reproducibility story is arch §33's: the manifest tracks a *branch*, and the
lock file records the *commit* that branch pointed at when the integration was
installed. `resolve_latest` asks the remote what the branch head is now, so an
update is an explicit, reviewable move from one recorded commit to another rather
than a `git pull` that quietly changes what runs.

Builds are inferred from the project layout, not configured per integration:
`go.mod` means Go, `package.json` means Node, `pyproject.toml` means Python. A
manifest can override with an explicit `build_command` and `command` when
inference is wrong, which is the escape hatch rather than the norm.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import ClassVar

from app.config.models import GitSource
from app.core.domain import SourceType
from app.core.errors import HubError, ValidationFailed
from app.core.logging import get_logger
from app.integrations.adapters.local import LocalAdapter
from app.integrations.base import AdapterContext, StagedBuild, VersionRef
from app.integrations.launcher import LaunchSpec
from app.integrations.process import require_binary, run_command

__all__ = ["GitAdapter"]

log = get_logger(__name__)

_CLONE_TIMEOUT = 600.0
_BUILD_TIMEOUT = 1800.0


class GitAdapter(LocalAdapter):
    """Clones, builds, and runs a server from a Git repository."""

    source_type: ClassVar[SourceType] = SourceType.GIT

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        source = context.manifest.source
        if not isinstance(source, GitSource):
            raise ValidationFailed(
                f"GitAdapter cannot serve source type {source.type.value!r}.",
                integration=context.manifest.id,
            )
        self.source = source

    # ------------------------------------------------------------- versioning

    async def resolve_latest(self) -> VersionRef:
        """Ask the remote what commit the manifest's ref currently points at.

        A manifest pinned to an explicit commit resolves to that commit, so a
        pinned integration never reports an update as available (arch §33).

        Raises:
            HubError: The repository is unreachable or the ref does not exist.
        """
        if self.source.commit:
            return self._version(self.source.commit)

        git = require_binary("git", hint="Install git to use git-sourced integrations.")
        ref = self.source.tag or self.source.branch or "HEAD"
        result = await run_command(
            [git, "ls-remote", "--exit-code", self.source.repository, ref],
            timeout=60.0,
            check=False,
        )
        if not result.ok or not result.stdout.strip():
            raise HubError(
                f"Could not resolve ref {ref!r} in {self.source.repository}: {result.output() or 'no matching ref'}",
                integration=self.integration_id,
                repository=self.source.repository,
                ref=ref,
            )
        # `ls-remote` prints "<sha>\t<ref>"; an annotated tag also prints a
        # "^{}" line for the commit it dereferences to, which is the one to use.
        commit = ""
        for line in result.stdout.splitlines():
            sha, _, name = line.partition("\t")
            if name.strip().endswith("^{}"):
                commit = sha.strip()
                break
            if not commit:
                commit = sha.strip()
        if len(commit) < 7:
            raise HubError(
                f"Unexpected ls-remote output while resolving {ref!r}.",
                integration=self.integration_id,
                repository=self.source.repository,
            )
        return self._version(commit)

    def _version(self, commit: str) -> VersionRef:
        return VersionRef(
            identifier=commit,
            kind="commit",
            display=commit[:12],
            metadata={
                "repository": self.source.repository,
                "branch": self.source.branch or "",
                "tag": self.source.tag or "",
            },
        )

    # ---------------------------------------------------------------- staging

    async def stage(self, version: VersionRef) -> StagedBuild:
        """Clone at `version` into its own directory and build it.

        The clone lands in a temporary sibling and is renamed into place only
        after the build succeeds, so a half-built tree is never mistaken for an
        installed version — including if the process dies mid-build.

        Raises:
            HubError: Cloning or building failed.
        """
        self.context.ensure_dirs()
        git = require_binary("git")
        target = self.context.version_dir(version)
        if (target / ".mcp-hub-build-complete").exists():
            log.info("git.stage_cached", integration=self.integration_id, version=version.display)
            return self._staged(version, target)

        staging = target.with_name(f".{target.name}.staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)

        try:
            await run_command([git, "init", "--quiet"], cwd=staging, timeout=60.0)
            await run_command([git, "remote", "add", "origin", self.source.repository], cwd=staging, timeout=60.0)
            # Fetch exactly one commit rather than the whole history: faster, and
            # it makes the pin explicit at the protocol level.
            await run_command(
                [git, "fetch", "--depth", "1", "--quiet", "origin", version.identifier],
                cwd=staging,
                timeout=_CLONE_TIMEOUT,
            )
            await run_command([git, "checkout", "--quiet", "FETCH_HEAD"], cwd=staging, timeout=120.0)

            root = staging / self.source.subdirectory if self.source.subdirectory else staging
            if not root.exists():
                raise HubError(
                    f"Subdirectory {self.source.subdirectory!r} does not exist in "
                    f"{self.source.repository} at {version.display}.",
                    integration=self.integration_id,
                )
            notes = await self._build(root)
            (staging / ".mcp-hub-build-complete").write_text(version.identifier, encoding="utf-8")
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
        log.info("git.staged", integration=self.integration_id, version=version.display, path=str(target))
        return self._staged(version, target, notes=notes)

    def _staged(self, version: VersionRef, path: Path, *, notes: tuple[str, ...] = ()) -> StagedBuild:
        spec = self.launch_spec(path)
        return StagedBuild(version=version, path=path, command=spec.command, env=dict(spec.env), notes=notes)

    async def _build(self, root: Path) -> tuple[str, ...]:
        """Build the checked-out tree, inferring the toolchain when not told."""
        if self.source.build_command:
            await run_command(list(self.source.build_command), cwd=root, timeout=_BUILD_TIMEOUT)
            return (f"built with configured command: {' '.join(self.source.build_command)}",)

        if (root / "go.mod").exists():
            go = require_binary("go", hint="Install Go to build Go-based MCP servers.")
            # Most Go MCP servers put their entry point under ./cmd; try that
            # first without failing the build, then fall back to the module root.
            await run_command(
                [go, "build", "-o", "mcp-server", "./cmd/..."],
                cwd=root,
                timeout=_BUILD_TIMEOUT,
                check=False,
            )
            if not (root / "mcp-server").exists():
                await run_command([go, "build", "-o", "mcp-server", "."], cwd=root, timeout=_BUILD_TIMEOUT)
            return ("built with `go build`",)

        if (root / "package.json").exists():
            npm = require_binary("npm", hint="Install Node.js to build Node-based MCP servers.")
            lockfile = (root / "package-lock.json").exists()
            await run_command(
                [npm, "ci" if lockfile else "install", "--no-audit", "--no-fund"],
                cwd=root,
                timeout=_BUILD_TIMEOUT,
            )
            if _has_npm_script(root, "build"):
                await run_command([npm, "run", "build"], cwd=root, timeout=_BUILD_TIMEOUT)
                return ("built with `npm ci && npm run build`",)
            return ("installed with `npm ci`",)

        if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
            uv = shutil.which("uv")
            if uv:
                await run_command(
                    [uv, "pip", "install", "--target", str(root / ".deps"), "."],
                    cwd=root,
                    timeout=_BUILD_TIMEOUT,
                )
            else:
                python = require_binary("python3")
                await run_command(
                    [python, "-m", "pip", "install", "--target", str(root / ".deps"), "."],
                    cwd=root,
                    timeout=_BUILD_TIMEOUT,
                )
            return ("installed Python dependencies into .deps",)

        return ("no build step detected — running the repository as-is",)

    # ------------------------------------------------------------------ launch

    def launch_spec(self, version_dir: Path) -> LaunchSpec:
        """Infer how to start the built server.

        Raises:
            HubError: The layout is unrecognised and the manifest gave no command.
        """
        root = version_dir / self.source.subdirectory if self.source.subdirectory else version_dir

        if self.source.command:
            return LaunchSpec(command=tuple(self.source.command), cwd=root)

        binary = root / "mcp-server"
        if binary.exists():
            return LaunchSpec(command=("./mcp-server",), cwd=root)

        for candidate in ("dist/index.js", "build/index.js", "index.js", "src/index.js"):
            if (root / candidate).exists():
                return LaunchSpec(command=("node", candidate), cwd=root)

        deps = root / ".deps"
        if deps.exists():
            module = self.manifest.id.replace("-", "_")
            return LaunchSpec(
                command=("python3", "-m", module),
                cwd=root,
                env={"PYTHONPATH": str(deps) if self.manifest.runtime.isolation == "subprocess" else "/srv/.deps"},
            )

        raise HubError(
            f"Could not infer how to start {self.integration_id!r} from its repository layout. "
            "Set `source.command` in the manifest.",
            integration=self.integration_id,
            path=str(root),
        )


def _has_npm_script(root: Path, name: str) -> bool:
    """Whether `package.json` defines the named script."""
    import json

    try:
        payload = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and name in (payload.get("scripts") or {})
