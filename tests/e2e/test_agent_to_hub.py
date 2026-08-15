"""The end-to-end flow arch §41 specifies.

    Agent  ->  MCP Hub  ->  mock Jira MCP server

Everything here runs over real Streamable HTTP against real MCP servers. Arch §41
lists eleven things this must verify, and `test_arch_41_end_to_end` checks them
in order, in one test, because they describe a single continuous story: tools
appear, are callable, are audited, are governed, and come and go with the
integration.
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.core.domain import AuditAction
from tests.conftest import BuildRuntime, WriteConfig
from tests.fixtures.hub_server import agent_client, serve_hub
from tests.fixtures.mock_servers import build_mock_jira, build_mock_search, serve_mcp

pytestmark = pytest.mark.e2e


async def test_arch_41_end_to_end(settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime) -> None:
    """All eleven checks arch §41 requires, in sequence."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={
                "jira": {
                    "name": "Mock Jira",
                    "namespace": "jira",
                    "source": {"type": "remote", "endpoint": upstream.url},
                    "trust": "remote_official",
                    "confirmation_required": ["delete"],
                }
            },
            catalog={"jira": True},
            policies={
                "jira": {
                    "allow": [
                        "searchJiraIssuesUsingJql",
                        "getJiraIssue",
                        "createJiraIssue",
                        "deleteJiraIssue",
                    ],
                    "require_confirmation": ["createJiraIssue"],
                }
            },
            exposure_mode="selective",
        )
        hub = await build_runtime()

        try:
            async with serve_hub(hub) as hub_url:
                # 1. The agent connects to exactly one endpoint.
                async with agent_client(hub_url) as agent:
                    listed = await agent.list_tools()
                    names = {tool.name for tool in listed.tools}

                    # 2 & 3. Jira's tools were discovered and appear under `jira.*`.
                    assert "jira.searchJiraIssuesUsingJql" in names
                    assert "jira.getJiraIssue" in names
                    assert all(name.startswith(("jira.", "hub.")) for name in names), (
                        f"unexpected namespace in {names}"
                    )

                    # 4 & 5. A call is forwarded upstream and its response returned.
                    result = await agent.call_tool("jira.searchJiraIssuesUsingJql", {"jql": "project = HUB"})
                    assert not result.is_error
                    assert "HUB-1" in result.content[0].text  # type: ignore[union-attr]
                    assert state.calls[-1].tool == "searchJiraIssuesUsingJql", (
                        "the upstream must receive the unqualified name"
                    )

                    # 6. An audit event was created.
                    await hub.audit.flush()
                    records = await hub.audit.query(action=AuditAction.TOOL_CALL, limit=10)
                    assert any(
                        record.tool == "jira.searchJiraIssuesUsingJql" and record.status == "success"
                        for record in records
                    )

                    # 7. Policy is enforced: an unlisted tool is refused, and a
                    # tool requiring confirmation goes through the elicitation.
                    denied = await agent.call_tool("jira.getJiraProjects", {})
                    assert denied.is_error

                    confirmed = await agent.call_tool("jira.createJiraIssue", {"summary": "from a test"})
                    assert not confirmed.is_error
                    assert "HUB-99" in confirmed.content[0].text  # type: ignore[union-attr]

                # 8 & 9. Disabling the integration withdraws its tools.
                await hub.lifecycle.disable("jira")
                async with agent_client(hub_url) as agent:
                    after_disable = {tool.name for tool in (await agent.list_tools()).tools}
                    assert not any(name.startswith("jira.") for name in after_disable)

                    unavailable = await agent.call_tool("jira.getJiraIssue", {"key": "HUB-1"})
                    assert unavailable.is_error

                # 10 & 11. Re-enabling brings them back.
                await hub.lifecycle.enable("jira")
                async with agent_client(hub_url) as agent:
                    after_enable = {tool.name for tool in (await agent.list_tools()).tools}
                    assert "jira.getJiraIssue" in after_enable

                    restored = await agent.call_tool("jira.getJiraIssue", {"key": "HUB-1"})
                    assert not restored.is_error
        finally:
            await hub.stop()


async def test_namespacing_prevents_collisions(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """Two upstreams both offering `search` stay distinguishable (arch §11)."""
    jira_server, jira_state = build_mock_jira()
    search_server, search_state = build_mock_search()

    async with serve_mcp(jira_server, jira_state) as jira, serve_mcp(search_server, search_state) as search:
        write_config(
            manifests={
                "jira": {
                    "namespace": "jira",
                    "source": {"type": "remote", "endpoint": jira.url},
                    "trust": "remote_official",
                },
                "brave-search": {
                    "namespace": "brave_search",
                    "source": {"type": "remote", "endpoint": search.url},
                    "trust": "remote_official",
                },
            },
            catalog={"jira": True, "brave-search": True},
            exposure_mode="full",
        )
        hub = await build_runtime()

        try:
            names = {item.qualified_name for item in hub.registry.all()}
            assert "brave_search.search" in names
            assert "jira.searchJiraIssuesUsingJql" in names
            # The bare upstream name is never exposed — only the qualified form.
            assert "search" not in names

            async with serve_hub(hub) as hub_url, agent_client(hub_url) as agent:
                result = await agent.call_tool("brave_search.search", {"q": "mcp hub"})
                assert not result.is_error
                assert search_state.calls[-1].tool == "search"
                assert not jira_state.calls, "the call must not have reached the wrong upstream"
        finally:
            await hub.stop()


async def test_one_broken_upstream_does_not_break_the_hub(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """Arch §45: a dead integration degrades alone, the rest keep serving."""
    jira_server, jira_state = build_mock_jira()

    async with serve_mcp(jira_server, jira_state) as jira:
        write_config(
            manifests={
                "jira": {
                    "namespace": "jira",
                    "source": {"type": "remote", "endpoint": jira.url},
                    "trust": "remote_official",
                },
                "ghost": {
                    "namespace": "ghost",
                    # Nothing is listening here.
                    "source": {"type": "remote", "endpoint": "http://127.0.0.1:9/mcp"},
                    "trust": "remote_official",
                },
            },
            catalog={"jira": True, "ghost": True},
            exposure_mode="full",
        )
        hub = await build_runtime()

        try:
            health = await hub.lifecycle.health()
            assert health["jira"]["status"] == "HEALTHY"
            assert health["ghost"]["status"] == "UNAVAILABLE"

            async with serve_hub(hub) as hub_url, agent_client(hub_url) as agent:
                names = {tool.name for tool in (await agent.list_tools()).tools}
                assert "jira.getJiraIssue" in names, "a healthy integration must still be usable"

                working = await agent.call_tool("jira.getJiraIssue", {"key": "HUB-7"})
                assert not working.is_error

                broken = await agent.call_tool("ghost.anything", {})
                assert broken.is_error
                assert "ghost.anything" in broken.content[0].text  # type: ignore[union-attr]
        finally:
            await hub.stop()


async def test_upstream_added_tool_appears_after_refresh(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """Arch §12: a tool the upstream adds shows up on refresh, with no code change."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={
                "jira": {
                    "namespace": "jira",
                    "source": {"type": "remote", "endpoint": upstream.url},
                    "trust": "remote_official",
                }
            },
            catalog={"jira": True},
            exposure_mode="full",
        )
        hub = await build_runtime()

        try:
            assert "jira.getJiraProjects" not in {item.qualified_name for item in hub.registry.all()}

            state.extra_tool_visible = True
            result = await hub.discoverer.refresh("jira")

            assert result.ok
            assert "jira.getJiraProjects" in {item.qualified_name for item in result.tools}
        finally:
            await hub.stop()
