"""Confirmation cannot be bypassed (arch §53).

A tool marked `require_confirmation` runs only after a human accepts an MCP
elicitation. The case that matters is the one a model could otherwise exploit:
a client that declares *no* elicitation capability. If "I can't ask" degraded to
"go ahead", then bypassing every confirmation in the hub would be a matter of
connecting with a simpler client — which the model, not the operator, chooses.

Each test asserts on the upstream's own record of what it was asked to do, so a
refusal that still forwarded the call would fail here rather than pass quietly.
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.core.domain import AuditAction, RiskLevel
from tests.conftest import BuildRuntime, WriteConfig
from tests.fixtures.hub_server import agent_client, serve_hub
from tests.fixtures.mock_servers import build_mock_jira, serve_mcp

pytestmark = [pytest.mark.security, pytest.mark.e2e]

MANIFEST = {
    "name": "Mock Jira",
    "namespace": "jira",
    "trust": "remote_official",
}
POLICY = {
    "jira": {
        "allow": ["getJiraIssue", "createJiraIssue", "deleteJiraIssue"],
        "require_confirmation": ["createJiraIssue"],
    }
}


async def test_accepting_the_prompt_lets_the_call_through(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """The baseline. Without this, "everything is refused" would also pass."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={"jira": {**MANIFEST, "source": {"type": "remote", "endpoint": upstream.url}}},
            catalog={"jira": True},
            policies=POLICY,
            exposure_mode="full",
        )
        hub = await build_runtime()

        try:
            async with serve_hub(hub) as url, agent_client(url, confirm=True) as agent:
                result = await agent.call_tool("jira.createJiraIssue", {"summary": "approved"})

            assert not result.is_error
            assert [call.tool for call in state.calls] == ["createJiraIssue"]
        finally:
            await hub.stop()


async def test_declining_the_prompt_stops_the_call(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={"jira": {**MANIFEST, "source": {"type": "remote", "endpoint": upstream.url}}},
            catalog={"jira": True},
            policies=POLICY,
            exposure_mode="full",
        )
        hub = await build_runtime()

        try:
            async with serve_hub(hub) as url, agent_client(url, confirm=False) as agent:
                result = await agent.call_tool("jira.createJiraIssue", {"summary": "refused"})

            assert result.is_error
            assert not state.calls, "a declined call must never reach the upstream"
        finally:
            await hub.stop()


async def test_cancelling_the_prompt_stops_the_call(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """`cancel` is not `accept`. A dismissed dialog is not consent."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={"jira": {**MANIFEST, "source": {"type": "remote", "endpoint": upstream.url}}},
            catalog={"jira": True},
            policies=POLICY,
            exposure_mode="full",
        )
        hub = await build_runtime()

        try:
            async with serve_hub(hub) as url, agent_client(url, confirm=None) as agent:
                result = await agent.call_tool("jira.createJiraIssue", {"summary": "cancelled"})

            assert result.is_error
            assert not state.calls
        finally:
            await hub.stop()


async def test_a_client_that_cannot_be_asked_is_refused(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """The bypass arch §53 exists to close.

    The client here declares no elicitation capability at all, which is exactly
    what a model would reach for if silence were treated as consent.
    """
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={"jira": {**MANIFEST, "source": {"type": "remote", "endpoint": upstream.url}}},
            catalog={"jira": True},
            policies=POLICY,
            exposure_mode="full",
        )
        hub = await build_runtime()

        try:
            async with serve_hub(hub) as url, agent_client(url, supports_elicitation=False) as agent:
                result = await agent.call_tool("jira.createJiraIssue", {"summary": "no elicitation here"})

            assert result.is_error
            assert not state.calls, "the call must not have been forwarded"
            assert "confirm" in result.content[0].text.lower()  # type: ignore[union-attr]
        finally:
            await hub.stop()


async def test_a_client_that_cannot_be_asked_may_still_call_ordinary_tools(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """The refusal is scoped to tools that need a human, not to the client.

    A simple client is not a second-class caller; it just cannot approve things.
    """
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={"jira": {**MANIFEST, "source": {"type": "remote", "endpoint": upstream.url}}},
            catalog={"jira": True},
            policies=POLICY,
            exposure_mode="full",
        )
        hub = await build_runtime()

        try:
            async with serve_hub(hub) as url, agent_client(url, supports_elicitation=False) as agent:
                result = await agent.call_tool("jira.getJiraIssue", {"key": "HUB-1"})

            assert not result.is_error
            assert [call.tool for call in state.calls] == ["getJiraIssue"]
        finally:
            await hub.stop()


async def test_risk_threshold_confirms_a_tool_no_policy_line_mentions(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """The backstop, exercised end to end.

    `deleteJiraIssue` appears in no `require_confirmation` list here. It is
    caught because the manifest classifies it `DESTRUCTIVE` and the default rule
    confirms at that level — which is what protects a policy file written before
    the upstream shipped the tool.
    """
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={
                "jira": {
                    **MANIFEST,
                    "source": {"type": "remote", "endpoint": upstream.url},
                    "tool_risk": {"deleteJiraIssue": RiskLevel.DESTRUCTIVE.value},
                }
            },
            catalog={"jira": True},
            policies={
                "jira": {
                    "allow": ["deleteJiraIssue"],
                    "confirm_risk_at_or_above": RiskLevel.DESTRUCTIVE.value,
                }
            },
            exposure_mode="full",
        )
        hub = await build_runtime()

        try:
            async with serve_hub(hub) as url, agent_client(url, supports_elicitation=False) as agent:
                result = await agent.call_tool("jira.deleteJiraIssue", {"key": "HUB-1"})

            assert result.is_error
            assert not state.calls
        finally:
            await hub.stop()


async def test_a_refused_confirmation_is_recorded(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """Arch §24: the decision, not just the outcome, has to be auditable.

    An operator investigating "why did nothing happen" needs to see that a human
    was asked and said no — that is a different event from a policy denial.
    """
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={"jira": {**MANIFEST, "source": {"type": "remote", "endpoint": upstream.url}}},
            catalog={"jira": True},
            policies=POLICY,
            exposure_mode="full",
        )
        hub = await build_runtime()

        try:
            async with serve_hub(hub) as url, agent_client(url, confirm=False) as agent:
                await agent.call_tool("jira.createJiraIssue", {"summary": "refused"})

            await hub.audit.flush()
            actions = {
                record.action for record in await hub.audit.query(limit=50) if record.tool == "jira.createJiraIssue"
            }

            assert AuditAction.CONFIRMATION_REQUESTED.value in actions
            assert AuditAction.CONFIRMATION_DENIED.value in actions
        finally:
            await hub.stop()
