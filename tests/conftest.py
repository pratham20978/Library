"""Shared test fixtures.

Every test gets an isolated hub: its own config directory, its own SQLite
database, its own runtime directory. Nothing touches the repository's real
`config/` or `runtime/`, so a test cannot corrupt a developer's working state and
tests cannot interfere with each other when run in parallel.

The hub under test is wired exactly as production wires it — same
`HubRuntime.create`, same components. Only the *inputs* differ: mock upstreams
instead of Atlassian, a temporary directory instead of `/`, and an in-memory
secret provider. Testing the real composition is the point; a hand-assembled
object graph would drift from the one that actually ships.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Protocol

import pytest

from app.config.models import Catalog, CatalogEntry, IntegrationManifest, PolicyDocument, PolicyRule
from app.config.settings import Settings, load_settings
from app.config.store import ConfigStore, write_model
from app.core.context import Principal, RequestContext, bind_request
from app.core.logging import configure_logging
from app.server.runtime import HubRuntime


class WriteConfig(Protocol):
    """The `write_config` fixture's signature, so tests can be typed.

    A fixture that returns a bare callable is `Any` to a type checker, which
    quietly disables checking inside every test that uses it. Naming the shape
    here keeps the suite under the same strictness as the code it guards.
    """

    def __call__(
        self,
        *,
        manifests: dict[str, dict[str, Any]] | None = None,
        catalog: dict[str, bool | dict[str, Any]] | None = None,
        policies: dict[str, Any] | None = None,
        exposure_mode: str = "full",
        require_authentication: bool = False,
    ) -> None: ...


class BuildRuntime(Protocol):
    """The `build_runtime` fixture's signature."""

    async def __call__(self, *, discover: bool = True) -> HubRuntime: ...


@pytest.fixture(scope="session", autouse=True)
def _quiet_logging() -> None:
    """Human-readable logs at WARNING, so a failing test is readable."""
    configure_logging(level="WARNING", json_output=False, force=True)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip `MCP_HUB_*` from the environment.

    Without this a developer's exported `MCP_HUB_DATABASE_URL` would silently
    redirect the whole suite at their real database.
    """
    for name in list(os.environ):
        if name.startswith("MCP_HUB_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """An empty configuration directory with a manifests subdirectory."""
    directory = tmp_path / "config"
    (directory / "manifests").mkdir(parents=True)
    return directory


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Path:
    """An empty runtime directory."""
    directory = tmp_path / "runtime"
    for name in ("integrations", "cache", "backups", "logs"):
        (directory / name).mkdir(parents=True)
    return directory


@pytest.fixture
def settings(tmp_path: Path, config_dir: Path, runtime_dir: Path) -> Settings:
    """Settings pointing entirely inside the test's temporary directory.

    The encrypted-database secret provider is configured rather than the
    environment-only default, because it is the only one that supports per-user
    credentials — and per-user isolation is the property most worth testing. The
    key is generated per test, so nothing written by one test is readable by
    another.
    """
    from cryptography.fernet import Fernet

    return load_settings(
        env="test",
        config_dir=config_dir,
        runtime_dir=runtime_dir,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'hub.db'}",
        redis_url=None,
        # 32+ bytes, or PyJWT warns that the HMAC key is short for SHA-256.
        auth_secret="test-secret-not-a-real-one-0123456789abcdef",
        auth_required=False,
        log_level="WARNING",
        secret_providers=("encrypted_database",),
        secret_encryption_key=Fernet.generate_key().decode(),
        upstream_connect_timeout_seconds=10.0,
        upstream_request_timeout_seconds=10.0,
    )


@pytest.fixture
def store(settings: Settings) -> ConfigStore:
    """A config store over the temporary configuration directory."""
    return ConfigStore.from_settings(settings)


@pytest.fixture
def write_config(store: ConfigStore) -> WriteConfig:
    """Write manifests, a catalog, and policies into the test's config directory.

    Returns a callable so a test can describe exactly the deployment it needs
    rather than mutating a shared default.
    """

    def _write(
        *,
        manifests: dict[str, dict[str, Any]] | None = None,
        catalog: dict[str, bool | dict[str, Any]] | None = None,
        policies: dict[str, Any] | None = None,
        exposure_mode: str = "full",
        require_authentication: bool = False,
    ) -> None:
        for integration_id, payload in (manifests or {}).items():
            manifest = IntegrationManifest.model_validate({"id": integration_id, **payload})
            store.write_manifest(manifest)

        entries: dict[str, CatalogEntry] = {}
        for integration_id, value in (catalog or {}).items():
            entries[integration_id] = CatalogEntry.model_validate(
                {"enabled": value} if isinstance(value, bool) else value
            )
        write_model(
            store.catalog_path,
            Catalog(version=1, tool_exposure_mode=exposure_mode, integrations=entries),  # type: ignore[arg-type]
        )

        rules = {key: PolicyRule.model_validate(value) for key, value in (policies or {}).items()}
        write_model(
            store.policies_path,
            PolicyDocument(
                version=1,
                policies=rules,
                require_authentication=require_authentication,
                default=PolicyRule(),
            ),
        )

    return _write


@pytest.fixture
async def runtime(settings: Settings, write_config: WriteConfig) -> AsyncIterator[HubRuntime]:
    """A started hub runtime with no integrations configured.

    Tests that need integrations write their configuration first and then use
    `built_runtime`, so this stays the empty baseline.
    """
    write_config()
    hub = await HubRuntime.create(settings)
    await hub.start(discover=False)
    try:
        yield hub
    finally:
        await hub.stop()


@pytest.fixture
def build_runtime(settings: Settings) -> BuildRuntime:
    """Build and start a runtime *after* a test has written its configuration."""

    async def _build(*, discover: bool = True) -> HubRuntime:
        hub = await HubRuntime.create(settings)
        await hub.start(discover=discover)
        return hub

    return _build


@pytest.fixture
def alice() -> Principal:
    """An ordinary authenticated caller."""
    return Principal(
        subject="alice",
        display_name="Alice",
        scopes=frozenset({"tools:call", "integrations:read", "tools:refresh"}),
    )


@pytest.fixture
def bob() -> Principal:
    """A second caller, for credential-isolation tests."""
    return Principal(subject="bob", display_name="Bob", scopes=frozenset({"tools:call"}))


@pytest.fixture
def operator() -> Principal:
    """An admin caller."""
    return Principal(subject="ops", display_name="Operator", scopes=frozenset({"admin"}))


@pytest.fixture
def as_alice(alice: Principal) -> Iterator[RequestContext]:
    """Bind Alice as the active request principal for the test's duration."""
    context = RequestContext(request_id="req_test", principal=alice, source="test")
    with bind_request(context) as bound:
        yield bound
