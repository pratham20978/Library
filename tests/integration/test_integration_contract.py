"""The contract every integration must satisfy (arch §40).

Arch §40 lists seven checks per integration — connection, tool discovery,
namespace, authentication, policy, timeout, failure handling — plus the three
protocol steps `initialize`, `tools/list` and a health check.

Writing eleven near-identical copies of those would test the copies rather than
the hub. Instead the checks are written once and parametrised over the *shapes*
the hub actually ships, because the shape is what changes the code path:

    remote + per-user credential   jira, notion
    remote + no credential         figma's read-only surface
    remote + shared credential     brave-search's metered key

Each shape runs against a real MCP server over a real transport. Local
subprocess sources are covered by `test_update_rollback.py`, which needs a real
artifact on disk for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.config.settings import Settings
from app.core.domain import HealthStatus
from app.core.redaction import Secret
from tests.conftest import BuildRuntime, WriteConfig
from tests.fixtures.hub_server import agent_client, serve_hub
from tests.fixtures.mock_servers import build_failing_server, build_mock_jira, build_slow_server, serve_mcp

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Shape:
    """One integration shape and the credential arrangement it implies."""

    id: str
    namespace: str
    secret_name: str | None
    per_user: bool

    @property
    def auth(self) -> dict[str, Any]:
        if self.secret_name is None:
            return {"type": "none"}
        return {
            "type": "bearer",
            "secret": {"name": self.secret_name, "per_user": self.per_user, "required": False},
        }


SHAPES = [
    Shape(id="jira", namespace="jira", secret_name="ATLASSIAN_TOKEN", per_user=True),
    Shape(id="figma", namespace="figma", secret_name=None, per_user=False),
    Shape(id="brave-search", namespace="brave_search", secret_name="BRAVE_API_KEY", per_user=False),
]
SHAPE_IDS = [shape.id for shape in SHAPES]

PRINCIPAL = "alice@example.com"
CREDENTIAL = "not-a-real-credential"


def manifest_for(shape: Shape, endpoint: str, **overrides: Any) -> dict[str, Any]:
    source: dict[str, Any] = {"type": "remote", "endpoint": endpoint}
    source.update(overrides.pop("source", {}))
    return {
        "name": shape.id,
        "namespace": shape.namespace,
        "trust": "remote_official",
        "source": source,
        "auth": shape.auth,
        **overrides,
    }


@pytest.fixture(params=SHAPES, ids=SHAPE_IDS)
def shape(request: pytest.FixtureRequest) -> Shape:
    return request.param  # type: ignore[no-any-return]


# --------------------------------------------------------- connection & discovery


async def test_connection(
    shape: Shape, settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """`initialize` succeeds and the health probe says so (arch §25, §40)."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={shape.id: manifest_for(shape, upstream.url)},
            catalog={shape.id: True},
            exposure_mode="full",
        )
        hub = await build_runtime(discover=False)

        try:
            await _store_credential(hub, shape)
            report = await hub.manager.check_health(shape.id)

            assert report.status is HealthStatus.HEALTHY, report.detail
            assert report.is_serving
        finally:
            await hub.stop()


async def test_tool_discovery(
    shape: Shape, settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """`tools/list` reaches the registry, with schemas intact."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={shape.id: manifest_for(shape, upstream.url)},
            catalog={shape.id: True},
            exposure_mode="full",
        )
        hub = await build_runtime(discover=False)

        try:
            await _store_credential(hub, shape)
            result = await hub.discoverer.discover(shape.id)

            assert result.ok, result.detail
            assert result.tools, "an integration with no tools is not usable"

            described = hub.registry.get(f"{shape.namespace}.getJiraIssue")
            assert described is not None
            assert described.tool.input_schema["properties"]["key"]["type"] == "string", (
                "the upstream's schema must survive discovery unmodified"
            )
        finally:
            await hub.stop()


async def test_namespace(
    shape: Shape, settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """Every tool is exposed qualified, and never under its bare upstream name."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={shape.id: manifest_for(shape, upstream.url)},
            catalog={shape.id: True},
            exposure_mode="full",
        )
        hub = await build_runtime(discover=False)

        try:
            await _store_credential(hub, shape)
            await hub.discoverer.discover(shape.id)

            names = {item.qualified_name for item in hub.registry.all()}

            assert names, "discovery produced nothing to check"
            assert all(name.startswith(f"{shape.namespace}.") for name in names), names
            assert "getJiraIssue" not in names, "the bare upstream name must never be exposed"
            assert "-" not in shape.namespace, "a namespace must be a valid identifier prefix"
        finally:
            await hub.stop()


# ------------------------------------------------------------------- authentication


async def test_authentication(
    shape: Shape, settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """The configured credential — and only that — reaches the upstream."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={shape.id: manifest_for(shape, upstream.url)},
            catalog={shape.id: True},
            exposure_mode="full",
        )
        hub = await build_runtime(discover=False)

        try:
            await _store_credential(hub, shape)
            await hub.discoverer.discover(shape.id)

            async with serve_hub(hub) as url, agent_client(url) as agent:
                result = await agent.call_tool(f"{shape.namespace}.getJiraIssue", {"key": "HUB-1"})

            assert not result.is_error
            sent = state.calls[-1].authorization
            if shape.secret_name is None:
                assert sent is None, "an integration with no credential must send none"
            elif shape.per_user:
                # The agent client here is unauthenticated, so there is no
                # principal to resolve a per-user credential for. Borrowing
                # somebody else's would be the bug worth catching.
                assert sent is None or CREDENTIAL not in sent
            else:
                assert sent == f"Bearer {CREDENTIAL}", "a shared credential serves every caller"
        finally:
            await hub.stop()


async def test_a_required_credential_that_is_missing_reports_auth_required(
    shape: Shape, settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """Arch §25: the health status names the remedy rather than saying "down"."""
    if shape.secret_name is None:
        pytest.skip("this shape needs no credential")

    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        manifest = manifest_for(shape, upstream.url)
        manifest["auth"] = {
            "type": "bearer",
            "secret": {"name": shape.secret_name, "per_user": shape.per_user, "required": True},
        }
        write_config(manifests={shape.id: manifest}, catalog={shape.id: True}, exposure_mode="full")
        hub = await build_runtime(discover=False)

        try:
            report = await hub.manager.check_health(shape.id)

            assert report.status is HealthStatus.AUTH_REQUIRED
            assert shape.secret_name in report.detail, "the operator must be told which credential"
        finally:
            await hub.stop()


# -------------------------------------------------------------------------- policy


async def test_policy(
    shape: Shape, settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """The allowlist is enforced at the endpoint, not merely recorded."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={shape.id: manifest_for(shape, upstream.url)},
            catalog={shape.id: True},
            policies={shape.id: {"allow": ["getJiraIssue"], "deny": ["deleteJiraIssue"]}},
            exposure_mode="selective",
        )
        hub = await build_runtime(discover=False)

        try:
            await _store_credential(hub, shape)
            await hub.discoverer.discover(shape.id)

            async with serve_hub(hub) as url, agent_client(url) as agent:
                exposed = {tool.name for tool in (await agent.list_tools()).tools}
                assert f"{shape.namespace}.getJiraIssue" in exposed
                assert f"{shape.namespace}.deleteJiraIssue" not in exposed, (
                    "selective exposure must hide what policy would refuse"
                )

                refused = await agent.call_tool(f"{shape.namespace}.deleteJiraIssue", {"key": "HUB-1"})

            assert refused.is_error
            assert not any(call.tool == "deleteJiraIssue" for call in state.calls), (
                "a denied call must not reach the upstream"
            )
        finally:
            await hub.stop()


# ------------------------------------------------------------------------- timeout


@pytest.mark.slow
async def test_timeout(
    shape: Shape, settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """A hung upstream produces an error, not a hung hub (arch §45)."""
    server, _ = build_slow_server(delay_seconds=30.0)

    async with serve_mcp(server) as upstream:
        write_config(
            manifests={shape.id: manifest_for(shape, upstream.url, source={"request_timeout_seconds": 2.0})},
            catalog={shape.id: True},
            exposure_mode="full",
        )
        hub = await build_runtime(discover=False)

        try:
            await _store_credential(hub, shape)
            await hub.discoverer.discover(shape.id)

            async with serve_hub(hub) as url, agent_client(url) as agent:
                result = await agent.call_tool(f"{shape.namespace}.slow_operation", {})

            assert result.is_error, "the hub must give up rather than wait indefinitely"
        finally:
            await hub.stop()


# ----------------------------------------------------------------- failure handling


async def test_failure_handling(
    shape: Shape, settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """An upstream that refuses `tools/list` degrades alone and stays diagnosable."""
    async with serve_mcp(build_failing_server()) as upstream:
        write_config(
            manifests={shape.id: manifest_for(shape, upstream.url)},
            catalog={shape.id: True},
            exposure_mode="full",
        )
        hub = await build_runtime(discover=False)

        try:
            await _store_credential(hub, shape)
            result = await hub.discoverer.discover(shape.id)

            assert not result.ok
            assert not result.tools
            # The SDK reports transport failures as exception groups; a health
            # detail saying "unhandled errors in a TaskGroup" would tell an
            # operator nothing at all.
            assert "TaskGroup" not in result.detail, result.detail
            assert result.detail, "a failure must explain itself"
        finally:
            await hub.stop()


async def test_an_upstream_error_result_is_returned_not_raised(
    shape: Shape, settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """An upstream's own error is data the agent can act on, not a hub failure."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={shape.id: manifest_for(shape, upstream.url)},
            catalog={shape.id: True},
            exposure_mode="full",
        )
        hub = await build_runtime(discover=False)

        try:
            await _store_credential(hub, shape)
            await hub.discoverer.discover(shape.id)
            state.fail_next = True

            async with serve_hub(hub) as url, agent_client(url) as agent:
                result = await agent.call_tool(f"{shape.namespace}.getJiraIssue", {"key": "HUB-1"})
                assert result.is_error

                # And the integration is still usable immediately afterwards.
                recovered = await agent.call_tool(f"{shape.namespace}.getJiraIssue", {"key": "HUB-2"})

            assert not recovered.is_error, "one bad call must not poison the session"
        finally:
            await hub.stop()


# --------------------------------------------------------------------------- helpers


async def _store_credential(hub: Any, shape: Shape) -> None:
    """Store this shape's credential, if it has one."""
    if shape.secret_name is None:
        return
    await hub.secrets.store(
        shape.secret_name,
        Secret(CREDENTIAL),
        principal=PRINCIPAL if shape.per_user else None,
    )
