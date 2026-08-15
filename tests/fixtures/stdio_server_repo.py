"""A local git repository containing a real stdio MCP server, at two versions.

Arch §40 wants the update path tested as `install old → update → test → rollback
→ test`. Doing that honestly needs a source the hub can genuinely clone, build
and launch — not a stub — because the parts most likely to break are staging,
promotion by atomic rename, and the launcher's environment filtering.

A git repository on the local filesystem gives exactly that with no network:
`git fetch` against a path is the same code path as against a URL, and the
server the hub spawns is a real MCP server speaking the real stdio transport.

Each version exports a differently-named tool, so "which version is live" is
answerable from the tool registry rather than from a file on disk.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ServerRepo", "build_server_repo"]

_SERVER_TEMPLATE = '''\
"""A minimal MCP server over stdio. Version {version}."""

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOL = "ping_{version}"


async def on_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=TOOL,
                description="Reply with this server's version.",
                input_schema={{"type": "object", "properties": {{}}}},
                annotations=types.ToolAnnotations(read_only_hint=True),
            )
        ]
    )


async def on_call_tool(ctx, params):
    return types.CallToolResult(content=[types.TextContent(type="text", text="{version}")])


async def main():
    server = Server("demo", version="{version}", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


anyio.run(main)
'''


@dataclass(frozen=True)
class ServerRepo:
    """A git repository whose `main` branch holds v2, with v1 still reachable."""

    path: Path
    old_commit: str
    new_commit: str

    @property
    def url(self) -> str:
        """What a manifest's `source.repository` should be set to.

        `file://` rather than a bare path: the manifest validator refuses
        anything that is not `https://`, `git@`, or `file://`, and a bare path
        would be an unverifiable scheme by that rule.
        """
        return self.path.as_uri()


def build_server_repo(root: Path) -> ServerRepo:
    """Create the repository and return both commits.

    Raises:
        RuntimeError: git is not installed, so the caller should skip.
    """
    path = root / "demo-server-repo"
    path.mkdir(parents=True)

    _git(path, "init", "--quiet", "--initial-branch", "main")
    # Committer identity is required by git and must not depend on the developer's
    # own global configuration, which may be unset in CI.
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "MCP Hub Tests")

    (path / "server.py").write_text(_SERVER_TEMPLATE.format(version="v1"), encoding="utf-8")
    _git(path, "add", "server.py")
    _git(path, "commit", "--quiet", "-m", "v1")
    old = _git(path, "rev-parse", "HEAD").strip()

    (path / "server.py").write_text(_SERVER_TEMPLATE.format(version="v2"), encoding="utf-8")
    _git(path, "add", "server.py")
    _git(path, "commit", "--quiet", "-m", "v2")
    new = _git(path, "rev-parse", "HEAD").strip()

    # `git fetch <sha>` against a local repository is refused unless the server
    # side allows fetching arbitrary objects, which is exactly what the hub does
    # when it pins a commit.
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    return ServerRepo(path=path, old_commit=old, new_commit=new)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout
