"""Running build and resolution commands (arch §31).

Adapters shell out to `git`, `npm`, `pip`, and `docker` while installing. Those
invocations are the hub's own tooling rather than untrusted server code, but they
still act on attacker-influenceable inputs — a repository URL, a package name, an
image reference — so they get the same three guarantees everywhere:

* **No shell.** Every call passes an argv list to `exec` directly. There is no
  code path where a repository name can become shell syntax.
* **A deadline.** Every call is bounded, and a timeout kills the whole process
  group. A build that hangs must not hang the update that started it.
* **Bounded output.** stdout and stderr are captured with a size cap, so a
  runaway build cannot exhaust memory through its log.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import HubError
from app.core.logging import get_logger

__all__ = ["CommandFailed", "CommandResult", "require_binary", "run_command"]

log = get_logger(__name__)

_MAX_CAPTURE_BYTES = 1_048_576  # 1 MiB per stream


class CommandFailed(HubError):
    """A build or resolution command exited non-zero, or timed out."""

    code = "command_failed"
    http_status = 500


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of one command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def ok(self) -> bool:
        """Whether the command succeeded."""
        return self.returncode == 0

    def output(self) -> str:
        """Combined output, for error messages and log capture."""
        return "\n".join(part for part in (self.stdout.strip(), self.stderr.strip()) if part)


def require_binary(name: str, *, hint: str = "") -> str:
    """Return the absolute path to `name`, or explain what to install.

    Raises:
        CommandFailed: The binary is not on `PATH`.
    """
    path = shutil.which(name)
    if path is None:
        raise CommandFailed(
            f"Required tool {name!r} was not found on PATH." + (f" {hint}" if hint else ""),
            tool=name,
        )
    return path


async def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 300.0,
    check: bool = True,
    stdin: bytes | None = None,
) -> CommandResult:
    """Run `argv` to completion under a deadline.

    Args:
        argv: Command and arguments. Never a shell string.
        cwd: Working directory.
        env: Complete environment. `None` inherits the hub's, which is only
            appropriate for the hub's own tooling — never for server code.
        timeout: Wall-clock budget in seconds.
        check: Raise on a non-zero exit. Set false to inspect the result.
        stdin: Bytes to write to the process, if any.

    Returns:
        The captured result.

    Raises:
        CommandFailed: The command timed out, could not be spawned, or (with
            `check`) exited non-zero.
    """
    import time

    started = time.monotonic()
    printable = " ".join(argv[:6]) + (" …" if len(argv) > 6 else "")
    log.debug("process.start", command=printable, cwd=str(cwd) if cwd else None, timeout=timeout)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group, so a timeout kills the build's children too —
            # `npm install` spawning `node-gyp` should not survive its parent.
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise CommandFailed(f"Could not run {argv[0]!r}: {exc}", command=printable) from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(input=stdin), timeout=timeout)
    except TimeoutError:
        _terminate_group(process)
        raise CommandFailed(
            f"Command {printable!r} exceeded its {timeout:g}s budget and was terminated.",
            command=printable,
            timeout=timeout,
        ) from None
    except asyncio.CancelledError:
        _terminate_group(process)
        raise

    result = CommandResult(
        argv=tuple(argv),
        returncode=process.returncode or 0,
        stdout=_decode(stdout),
        stderr=_decode(stderr),
        duration_seconds=time.monotonic() - started,
    )
    log.debug("process.finished", command=printable, returncode=result.returncode, seconds=result.duration_seconds)

    if check and not result.ok:
        raise CommandFailed(
            f"Command {printable!r} exited {result.returncode}: {_tail(result.output())}",
            command=printable,
            returncode=result.returncode,
        )
    return result


def _terminate_group(process: asyncio.subprocess.Process) -> None:
    """Kill the process and everything it spawned."""
    if process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover - race with normal exit
        with __import__("contextlib").suppress(ProcessLookupError):
            process.kill()


def _decode(raw: bytes) -> str:
    """Decode captured output, truncating anything unreasonable."""
    if len(raw) > _MAX_CAPTURE_BYTES:
        raw = raw[:_MAX_CAPTURE_BYTES] + b"\n... (output truncated)"
    return raw.decode("utf-8", errors="replace")


def _tail(text: str, *, lines: int = 12) -> str:
    """The last few lines of output — where a build failure actually is."""
    collected = [line for line in text.splitlines() if line.strip()]
    return "\n".join(collected[-lines:]) if collected else "(no output)"
