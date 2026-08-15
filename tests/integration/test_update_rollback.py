"""The update cycle arch §40 specifies: install old → update → test → rollback → test.

Nothing here is stubbed. The hub clones a real git repository, launches a real
MCP server as a real subprocess, and speaks the real stdio transport to it. That
matters because the parts of the update path most likely to break are the parts a
stub would skip: staging into a version directory, validating the staged build
while the old one is still serving, promotion by atomic rename, and the rollback
that renames back.

"Which version is live" is answered from the tool registry — v1 exports
`ping_v1`, v2 exports `ping_v2` — so the assertions are about what an agent would
actually see rather than about a file on disk.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from app.config.settings import Settings
from app.core.domain import AuditAction
from app.integrations.base import VersionRef
from tests.conftest import BuildRuntime, WriteConfig
from tests.fixtures.stdio_server_repo import ServerRepo, build_server_repo

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def server_repo(tmp_path: Path) -> ServerRepo:
    """A two-commit repository holding a working stdio MCP server."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    return build_server_repo(tmp_path)


def manifest_for(repo: ServerRepo, **source_overrides: Any) -> dict[str, Any]:
    """A git-sourced integration tracking `main`.

    `command` names this interpreter explicitly rather than relying on `python3`
    resolving to the virtualenv's — the launcher strips the environment down to
    the allowlist, so an inherited `VIRTUAL_ENV` is exactly the thing that is
    not there.
    """
    source: dict[str, Any] = {
        "type": "git",
        "repository": repo.url,
        "branch": "main",
        "command": [sys.executable, "server.py"],
        "transport": "stdio",
        "startup_timeout_seconds": 60,
    }
    source.update(source_overrides)
    return {
        "name": "Demo Server",
        "namespace": "demo",
        "trust": "local_official",
        "source": source,
        "runtime": {
            "isolation": "subprocess",
            "network": "none",
            # PATH so the interpreter can find its own libraries; nothing else.
            "allowed_env": ["PATH", "HOME", "LANG"],
        },
    }


async def test_install_update_rollback_cycle(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime, server_repo: ServerRepo
) -> None:
    """The whole arch §40 sequence, in order, on one integration."""
    write_config(
        manifests={"demo": manifest_for(server_repo)},
        catalog={"demo": True},
        exposure_mode="full",
    )
    hub = await build_runtime(discover=False)

    try:
        # --- install old -------------------------------------------------
        await hub.lifecycle.install(
            "demo",
            enable=True,
            version=VersionRef(identifier=server_repo.old_commit, kind="commit"),
        )

        assert _tools(hub) == {"demo.ping_v1"}
        assert _locked_commit(hub) == server_repo.old_commit

        # --- update ------------------------------------------------------
        plan, outcomes = await hub.updater.update(names=["demo"])

        assert [item.integration_id for item in plan.actionable] == ["demo"]
        assert outcomes and outcomes[0].succeeded, outcomes[0].detail if outcomes else "no outcome"

        # --- test --------------------------------------------------------
        await hub.discoverer.refresh("demo")
        assert _tools(hub) == {"demo.ping_v2"}, "the new version's tools must be live"
        assert _locked_commit(hub) == server_repo.new_commit

        # --- rollback ----------------------------------------------------
        result = await hub.lifecycle.rollback("demo")
        assert result["from"] == server_repo.new_commit[:12]
        assert result["to"] == server_repo.old_commit[:12]

        # --- test --------------------------------------------------------
        await hub.discoverer.refresh("demo")
        assert _tools(hub) == {"demo.ping_v1"}, "rolling back must restore the old tool surface"
        assert _locked_commit(hub) == server_repo.old_commit
    finally:
        await hub.stop()


async def test_the_replaced_version_is_kept_on_disk(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime, server_repo: ServerRepo
) -> None:
    """Rollback is a rename, not a re-download — so it works with the network down.

    This is why an update stages into its own directory instead of overwriting.
    """
    write_config(manifests={"demo": manifest_for(server_repo)}, catalog={"demo": True}, exposure_mode="full")
    hub = await build_runtime(discover=False)

    try:
        await hub.lifecycle.install(
            "demo", enable=True, version=VersionRef(identifier=server_repo.old_commit, kind="commit")
        )
        await hub.updater.update(names=["demo"])

        versions = settings.runtime_dir / "integrations" / "demo" / "versions"
        installed = {path.name for path in versions.iterdir() if path.is_dir() and not path.name.startswith(".")}

        # Version directories are named after the commit, possibly abbreviated.
        kept = {name for name in installed if server_repo.old_commit.startswith(name[:12])}

        assert kept, f"the replaced version must survive on disk; found {installed}"
        assert len(installed) >= 2, "both versions are kept, which is what makes rollback a rename"
    finally:
        await hub.stop()


async def test_a_dry_run_changes_nothing(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime, server_repo: ServerRepo
) -> None:
    """Arch §59: the plan is shown, and the deployment is untouched."""
    write_config(manifests={"demo": manifest_for(server_repo)}, catalog={"demo": True}, exposure_mode="full")
    hub = await build_runtime(discover=False)

    try:
        await hub.lifecycle.install(
            "demo", enable=True, version=VersionRef(identifier=server_repo.old_commit, kind="commit")
        )

        plan, outcomes = await hub.updater.update(names=["demo"], dry_run=True)

        assert plan.actionable, "there is an update available to describe"
        assert not outcomes, "a dry run executes nothing"
        assert _locked_commit(hub) == server_repo.old_commit
        assert _tools(hub) == {"demo.ping_v1"}
    finally:
        await hub.stop()


async def test_a_pinned_integration_reports_no_update(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime, server_repo: ServerRepo
) -> None:
    """Arch §33: pinning to a commit means the branch moving does not matter."""
    manifest = manifest_for(server_repo, branch=None, commit=server_repo.old_commit)
    write_config(manifests={"demo": manifest}, catalog={"demo": True}, exposure_mode="full")
    hub = await build_runtime(discover=False)

    try:
        await hub.lifecycle.install("demo", enable=True)
        plan = await hub.updater.plan(names=["demo"])

        assert not plan.actionable, "a pinned integration must never drift"
    finally:
        await hub.stop()


async def test_excluded_integrations_are_not_contacted(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime, server_repo: ServerRepo
) -> None:
    """Arch §17: `--exclude` is applied before versions are resolved.

    The excluded integration here points at a repository that does not exist, so
    a plan that tried to resolve it would fail rather than skip.
    """
    write_config(
        manifests={
            "demo": manifest_for(server_repo),
            "ghost": {
                "namespace": "ghost",
                "trust": "local_official",
                "source": {
                    "type": "git",
                    "repository": "file:///nonexistent/repository.git",
                    "branch": "main",
                    "command": ["true"],
                },
            },
        },
        catalog={"demo": True, "ghost": True},
        exposure_mode="full",
    )
    hub = await build_runtime(discover=False)

    try:
        await hub.lifecycle.install(
            "demo", enable=True, version=VersionRef(identifier=server_repo.old_commit, kind="commit")
        )

        plan = await hub.updater.plan(all_integrations=True, exclude=["ghost"])

        assert {item.integration_id for item in plan.actionable} == {"demo"}
    finally:
        await hub.stop()


async def test_the_cycle_is_audited(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime, server_repo: ServerRepo
) -> None:
    """Arch §24: install, update and rollback all leave a record."""
    write_config(manifests={"demo": manifest_for(server_repo)}, catalog={"demo": True}, exposure_mode="full")
    hub = await build_runtime(discover=False)

    try:
        await hub.lifecycle.install(
            "demo", enable=True, version=VersionRef(identifier=server_repo.old_commit, kind="commit")
        )
        await hub.updater.update(names=["demo"])
        await hub.lifecycle.rollback("demo")

        await hub.audit.flush()
        actions = {record.action for record in await hub.audit.query(limit=100)}

        assert AuditAction.INSTALL.value in actions
        assert AuditAction.UPDATE.value in actions
        assert AuditAction.ROLLBACK.value in actions
    finally:
        await hub.stop()


# --------------------------------------------------------------------------- helpers


def _tools(hub: object) -> set[str]:
    """The qualified names currently in the registry."""
    return {item.qualified_name for item in hub.registry.all()}  # type: ignore[attr-defined]


def _locked_commit(hub: object) -> str:
    """The commit the lock file records as installed."""
    from app.config.store import ConfigStore

    store: ConfigStore = hub.store  # type: ignore[attr-defined]
    entry = store.load_lock().integrations["demo"]
    assert entry.resolved_commit is not None
    return entry.resolved_commit
