"""Launching local MCP servers under a sandbox (arch §31).

Arch §31 is unambiguous: every third-party MCP server is untrusted code. The
ecosystem has already shipped malicious packages, so a server the hub downloads
gets the narrowest environment that still lets it work.

Both isolation modes end at the same place — an argv the MCP stdio transport
spawns — which is why this lives in one module instead of being reimplemented in
four adapters:

* **container** (the default, and the only sensible choice for community code)
  wraps the server's own command in `docker run` with a read-only root, dropped
  capabilities, a non-root user, CPU/memory/PID ceilings, and no network unless
  the manifest asked for it.
* **subprocess** runs it directly. It is available for trusted, vendor-published
  servers on hosts with no container runtime, and it is genuinely weaker: the
  hub can restrict the environment and the working directory, but not what the
  process may open. The manifest has to opt into it.

The environment allowlist is the part that matters most in both modes. A server
process starts with *nothing* from the hub's own environment except the
variables its manifest names — otherwise a filesystem server inherits
`MCP_HUB_AUTH_SECRET`, `DATABASE_URL`, and every other integration's credential
simply by being launched.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.config.models import RuntimeSpec
from app.core.domain import TrustTier
from app.core.errors import HubError, ValidationFailed
from app.core.logging import get_logger

__all__ = ["LaunchSpec", "build_launch_argv", "container_runtime_available", "filter_environment"]

log = get_logger(__name__)

_BASE_ENV_KEYS: Final = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR")
"""Variables a process needs to function at all. Still subject to the allowlist."""

_NEVER_FORWARD: Final = frozenset(
    {
        # The hub's own configuration. A child server has no business reading any
        # of it, and several of these are credentials outright.
        "MCP_HUB_AUTH_SECRET",
        "MCP_HUB_SECRET_ENCRYPTION_KEY",
        "MCP_HUB_VAULT_TOKEN",
        "MCP_HUB_DATABASE_URL",
        "MCP_HUB_REDIS_URL",
        "MCP_HUB_ADMIN_TOKENS",
    }
)
"""Belt-and-braces: refused even if a manifest lists them in `allowed_env`."""


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """A server's own idea of how it starts, before sandboxing is applied."""

    command: tuple[str, ...]
    """Argv as the server would be run directly, e.g. `("node", "dist/index.js")`."""

    cwd: Path | None = None
    """Working directory — the promoted version directory for local sources."""

    env: Mapping[str, str] = field(default_factory=dict)
    """Non-secret environment the adapter computed (module paths, config flags)."""

    image: str | None = None
    """Container image, when the source is a container in the first place."""

    def __post_init__(self) -> None:
        if not self.command and not self.image:
            raise ValidationFailed("A launch spec needs either a command or an image.")


def container_runtime_available(binary: str = "docker") -> bool:
    """Whether a container runtime is on `PATH`."""
    return shutil.which(binary) is not None


def filter_environment(
    runtime: RuntimeSpec,
    *,
    adapter_env: Mapping[str, str],
    credential_env: Mapping[str, str],
    inherit: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the child process environment (arch §31).

    Three sources, in increasing precedence: variables inherited from the hub
    (only those the manifest allowlists), the adapter's own non-secret settings,
    and resolved credentials. Credentials win because a manifest must not be able
    to shadow the token the hub just resolved for this caller.

    Nothing from the hub's environment survives unless `runtime.allowed_env`
    names it — an empty allowlist means an empty inherited environment, which is
    the safe default rather than an oversight.

    Args:
        runtime: The manifest's sandbox profile, holding the allowlist.
        adapter_env: Non-secret variables the adapter computed.
        credential_env: Resolved credential variables for this call.
        inherit: Source environment, defaulting to the hub's own.

    Returns:
        The exact environment the child will see.
    """
    source = dict(os.environ if inherit is None else inherit)
    allowed = {name.upper() for name in runtime.allowed_env}

    env: dict[str, str] = {}
    for name in (*_BASE_ENV_KEYS, *sorted(allowed)):
        if name in _NEVER_FORWARD or name.upper() not in allowed:
            continue
        value = source.get(name)
        if value is not None:
            env[name] = value

    # PATH is required for a process to find its own interpreter. Supply a
    # conservative one when the manifest did not allowlist the hub's.
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")

    for name, value in adapter_env.items():
        if name in _NEVER_FORWARD:
            continue
        env[name] = value

    for name, value in credential_env.items():
        if name in _NEVER_FORWARD:
            log.error("launcher.credential_name_refused", name=name)
            continue
        env[name] = value

    return env


def build_launch_argv(
    spec: LaunchSpec,
    runtime: RuntimeSpec,
    *,
    trust: TrustTier,
    integration_id: str,
    env: Mapping[str, str],
    repo_root: Path,
) -> tuple[tuple[str, ...], dict[str, str], Path | None]:
    """Turn a launch spec into the argv, environment, and cwd to spawn.

    Args:
        spec: How the server runs unsandboxed.
        runtime: The manifest's sandbox profile.
        trust: Provenance tier, which decides whether `subprocess` is permitted.
        integration_id: Used to name the container.
        env: The already-filtered environment from `filter_environment`.
        repo_root: Base for resolving relative mount paths.

    Returns:
        `(argv, env, cwd)`. In container mode the environment is passed *into*
        the container via `-e` flags and the returned env is minimal, so the
        values never appear in the hub process's own child environment.

    Raises:
        HubError: Container isolation was requested but no runtime is installed.
        ValidationFailed: Untrusted code asked for subprocess isolation.
    """
    if runtime.isolation == "subprocess":
        if trust is TrustTier.COMMUNITY:
            raise ValidationFailed(
                f"Integration {integration_id!r} is community-tier and may not run without "
                "container isolation. Set `runtime.isolation: container` — arch §31 requires "
                "untrusted servers to be contained.",
                integration=integration_id,
            )
        log.warning(
            "launcher.subprocess_isolation",
            integration=integration_id,
            trust=trust.value,
            detail="Running without a container; filesystem and network are not restricted.",
        )
        return tuple(spec.command), dict(env), spec.cwd

    if not container_runtime_available():
        raise HubError(
            f"Integration {integration_id!r} requires container isolation but no `docker` "
            "binary was found. Install a container runtime, or set "
            "`runtime.isolation: subprocess` for this trusted integration.",
            integration=integration_id,
        )

    argv = _docker_argv(spec, runtime, integration_id=integration_id, env=env, repo_root=repo_root)
    # The docker client itself only needs enough environment to find the daemon.
    client_env = {key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "DOCKER_HOST")}
    return argv, client_env, None


def _docker_argv(
    spec: LaunchSpec,
    runtime: RuntimeSpec,
    *,
    integration_id: str,
    env: Mapping[str, str],
    repo_root: Path,
) -> tuple[str, ...]:
    """Assemble the `docker run` command implementing the sandbox profile."""
    argv: list[str] = [
        "docker",
        "run",
        "--rm",
        "--interactive",  # the MCP stdio transport needs stdin held open
        "--attach",
        "stdin",
        "--attach",
        "stdout",
        "--attach",
        "stderr",
        "--label",
        f"mcp-hub.integration={integration_id}",
        # Nothing inside may gain privileges, whatever the image's setuid bits say.
        "--security-opt",
        "no-new-privileges",
        "--cap-drop",
        "ALL",
        "--pids-limit",
        str(runtime.pids_limit),
        "--memory",
        f"{runtime.memory_limit_mb}m",
        "--memory-swap",
        f"{runtime.memory_limit_mb}m",  # no swap: the memory cap is real
        "--cpus",
        f"{runtime.cpu_limit:g}",
    ]

    if runtime.read_only_root:
        argv += ["--read-only"]
        # A read-only root breaks anything that writes a temp file, so give it a
        # small in-memory /tmp rather than making the whole filesystem writable.
        # This path is inside the container, not on the host.
        argv += ["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"]  # noqa: S108

    if runtime.run_as_non_root:
        argv += ["--user", "65534:65534"]  # nobody:nogroup

    match runtime.network:
        case "none":
            argv += ["--network", "none"]
        case "host":
            argv += ["--network", "host"]
        case _:
            argv += ["--network", "bridge"]

    for mount in runtime.mounts:
        argv += ["--volume", _resolve_mount(mount, repo_root=repo_root, integration_id=integration_id)]

    for path in runtime.writable_paths:
        argv += ["--tmpfs", f"{path}:rw,nosuid,size=64m"]

    for name, value in env.items():
        # Passed as `-e NAME=value`; the value never enters the hub's own child
        # environment, only the container's.
        argv += ["--env", f"{name}={value}"]

    if spec.image:
        argv.append(spec.image)
        argv.extend(spec.command)
    else:
        # A source with no image of its own runs inside a base image with its
        # version directory mounted read-only at /srv.
        assert spec.cwd is not None, "non-image launches must have a version directory"
        argv += ["--volume", f"{spec.cwd}:/srv:ro", "--workdir", "/srv"]
        argv.append(_base_image_for(spec))
        argv.extend(spec.command)

    return tuple(argv)


def _resolve_mount(mount: str, *, repo_root: Path, integration_id: str) -> str:
    """Turn a manifest `host:container` pair into an absolute, read-only mount.

    Relative host paths resolve against the repository root so a manifest stays
    portable. Mounts are read-only unless the manifest explicitly appends `:rw`,
    because a writable host mount is the single easiest way for an untrusted
    server to escape everything else configured here.
    """
    parts = mount.split(":")
    if len(parts) < 2:
        raise ValidationFailed(
            f"Mount {mount!r} for {integration_id!r} must be 'host:container' (optionally ':rw').",
            integration=integration_id,
        )
    host, container = parts[0], parts[1]
    mode = parts[2] if len(parts) > 2 else "ro"
    if mode not in ("ro", "rw"):
        raise ValidationFailed(f"Mount mode {mode!r} must be 'ro' or 'rw'.", integration=integration_id)

    host_path = Path(host)
    if not host_path.is_absolute():
        host_path = (repo_root / host_path).resolve()
    host_path.mkdir(parents=True, exist_ok=True)
    if mode == "rw":
        log.warning("launcher.writable_mount", integration=integration_id, path=str(host_path))
    return f"{host_path}:{container}:{mode}"


def _base_image_for(spec: LaunchSpec) -> str:
    """Pick a minimal base image for a source that ships no image of its own.

    Chosen from the interpreter the command actually invokes so a Node server
    does not get a Python image. Both are pinned to a specific minor version —
    a floating `:latest` base would silently change what untrusted code runs on.
    """
    executable = Path(spec.command[0]).name if spec.command else ""
    if executable in ("node", "npx", "npm"):
        return "node:22-alpine"
    if executable in ("python", "python3", "uv", "uvx"):
        return "python:3.12-slim"
    return "debian:bookworm-slim"
