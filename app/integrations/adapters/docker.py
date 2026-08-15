"""Container-image MCP servers (arch §8, §55).

The strongest isolation the hub offers, and the one arch §31 prefers for
third-party code: the server ships as an image and runs with no filesystem,
network, or privilege it was not explicitly granted.

The digest is the whole point of this adapter. A tag is mutable — `:latest`
means something different tomorrow — so `resolve_latest` turns a tag into the
`sha256:` digest it currently points at, and that digest is what gets recorded,
pulled, and run. Arch §55 refuses mutable image tags in production outright, and
`allow_mutable_tags` enforces that here rather than trusting a deployment to
have configured it.

"Staging" for an image is a pull plus a digest verification. There is no version
directory: the local image store holds every pulled digest, so rolling back is
running the previous digest again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from app.config.models import DockerSource
from app.core.domain import SourceType
from app.core.errors import HubError, SupplyChainRejected, ValidationFailed
from app.core.logging import get_logger
from app.integrations.adapters.local import LocalAdapter
from app.integrations.base import AdapterContext, StagedBuild, VersionRef
from app.integrations.launcher import LaunchSpec
from app.integrations.process import require_binary, run_command

__all__ = ["DockerAdapter"]

log = get_logger(__name__)

_PULL_TIMEOUT = 1800.0


class DockerAdapter(LocalAdapter):
    """Pulls and runs an MCP server distributed as a container image."""

    source_type: ClassVar[SourceType] = SourceType.DOCKER

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        source = context.manifest.source
        if not isinstance(source, DockerSource):
            raise ValidationFailed(
                f"DockerAdapter cannot serve source type {source.type.value!r}.",
                integration=context.manifest.id,
            )
        self.source = source

    # ------------------------------------------------------------- versioning

    async def resolve_latest(self) -> VersionRef:
        """Resolve the manifest's image reference to an exact digest (arch §55).

        Raises:
            SupplyChainRejected: The image is tag-only in a deployment that
                requires digests.
            HubError: The registry could not be queried.
        """
        if self.source.digest:
            return self._version(self.source.digest)

        if not self.context.allow_mutable_tags:
            raise SupplyChainRejected(
                f"Integration {self.integration_id!r} references the mutable tag "
                f"{self.source.image}:{self.source.tag}. Production requires a `digest:` pin "
                "so the image cannot change underneath a deployment (arch §55).",
                integration=self.integration_id,
                image=self.source.image,
                tag=self.source.tag,
            )

        docker = require_binary("docker", hint="Install a container runtime to use docker-sourced integrations.")
        reference = f"{self.source.image}:{self.source.tag}"
        result = await run_command(
            [docker, "manifest", "inspect", "--verbose", reference], timeout=120.0, check=False
        )
        if not result.ok:
            raise HubError(
                f"Could not inspect image {reference!r}: {result.output()}",
                integration=self.integration_id,
                image=reference,
            )
        digest = _extract_digest(result.stdout)
        if not digest:
            raise HubError(
                f"Registry returned no digest for {reference!r}.",
                integration=self.integration_id,
                image=reference,
            )
        return self._version(digest)

    def _version(self, digest: str) -> VersionRef:
        return VersionRef(
            identifier=digest,
            kind="digest",
            display=digest[7:19] if digest.startswith("sha256:") else digest[:12],
            metadata={"image": self.source.image, "tag": self.source.tag},
        )

    # ---------------------------------------------------------------- staging

    async def stage(self, version: VersionRef) -> StagedBuild:
        """Pull the image by digest and record which digest is active.

        Raises:
            HubError: The pull failed.
        """
        self.context.ensure_dirs()
        docker = require_binary("docker")
        reference = f"{self.source.image}@{version.identifier}"

        await run_command([docker, "pull", "--quiet", reference], timeout=_PULL_TIMEOUT)
        await self._verify_present(docker, reference, version)

        # No source tree to stage, but a version directory still gives promote a
        # target to point `current` at, so rollback works the same way as for
        # every other source kind.
        target = self.context.version_dir(version)
        target.mkdir(parents=True, exist_ok=True)
        (target / "image").write_text(reference, encoding="utf-8")
        (target / ".mcp-hub-install-complete").write_text(version.identifier, encoding="utf-8")

        log.info("docker.staged", integration=self.integration_id, image=reference)
        spec = self.launch_spec(target)
        return StagedBuild(
            version=version,
            path=target,
            command=spec.command,
            env=dict(spec.env),
            notes=(f"pulled {reference}",),
        )

    async def _verify_present(self, docker: str, reference: str, version: VersionRef) -> None:
        """Confirm the local image store really holds the requested digest.

        Raises:
            SupplyChainRejected: The pulled image does not carry the digest asked
                for, which means the registry served something else.
        """
        result = await run_command(
            [docker, "image", "inspect", reference, "--format", "{{json .RepoDigests}}"],
            timeout=60.0,
            check=False,
        )
        if not result.ok:
            raise HubError(
                f"Image {reference!r} is not present after pull: {result.output()}",
                integration=self.integration_id,
            )
        if version.identifier not in result.stdout:
            raise SupplyChainRejected(
                f"Pulled image for {self.integration_id!r} does not carry digest "
                f"{version.identifier}. Refusing to run it.",
                integration=self.integration_id,
                expected=version.identifier,
            )

    # ------------------------------------------------------------------ launch

    def launch_spec(self, version_dir: Path) -> LaunchSpec:
        """Run the pulled image, pinned by digest."""
        image_file = version_dir / "image"
        reference = image_file.read_text(encoding="utf-8").strip() if image_file.exists() else self.source.reference
        return LaunchSpec(command=tuple(self.source.args), image=reference, cwd=version_dir)

    async def uninstall(self) -> None:
        """Remove version pointers, leaving the image in the local store.

        Deleting the image is deliberately not done: another integration or
        another deployment on the same host may be using it, and re-pulling is
        cheap while an unexpected deletion is not.
        """
        await super().uninstall()
        log.info(
            "docker.uninstalled",
            integration=self.integration_id,
            detail="Image left in the local store; remove it with `docker image rm` if unused.",
        )


def _extract_digest(payload: str) -> str | None:
    """Pull the manifest digest out of `docker manifest inspect --verbose`.

    The output shape varies: a single-platform image yields one object, a
    multi-platform image yields a list. Both carry `Descriptor.digest`, which is
    the digest that identifies the whole manifest.
    """
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    entries = parsed if isinstance(parsed, list) else [parsed]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        descriptor = entry.get("Descriptor")
        if isinstance(descriptor, dict):
            digest = descriptor.get("digest")
            if isinstance(digest, str) and digest.startswith("sha256:"):
                return digest
        digest = entry.get("digest")
        if isinstance(digest, str) and digest.startswith("sha256:"):
            return digest
    return None
