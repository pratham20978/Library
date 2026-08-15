"""Risk classification (arch §52).

Classification is what makes a policy file safe when it is out of date: a tool
nobody has reviewed still gets a risk level, and `confirm_risk_at_or_above`
catches it. So the tests that matter are the ones about *precedence* — whose
opinion wins when the manifest, the upstream's annotation, and the tool's own
name disagree.

The precedence is: manifest > annotation > name heuristic > integration default,
with one exception the tests pin down — a `readOnlyHint` on a tool called
`delete_repository` is not believed.
"""

from __future__ import annotations

import pytest

from app.config.models import IntegrationManifest
from app.core.domain import RiskLevel
from app.policy.classifier import classify_tool

pytestmark = pytest.mark.security


def manifest_for(
    *,
    tool_risk: dict[str, RiskLevel] | None = None,
    default_risk: RiskLevel = RiskLevel.READ,
) -> IntegrationManifest:
    return IntegrationManifest.model_validate(
        {
            "id": "jira",
            "source": {"type": "remote", "endpoint": "https://mcp.example.com/mcp"},
            "risk_level": default_risk,
            "tool_risk": {name: level.value for name, level in (tool_risk or {}).items()},
        }
    )


# ------------------------------------------------------------------------ precedence


def test_manifest_beats_everything() -> None:
    """An operator's explicit classification is not second-guessed."""
    verdict = classify_tool(
        "getJiraIssue",
        manifest=manifest_for(tool_risk={"getJiraIssue": RiskLevel.DESTRUCTIVE}),
        read_only_hint=True,
    )

    assert verdict.risk is RiskLevel.DESTRUCTIVE
    assert verdict.source == "manifest"
    assert verdict.is_explicit


def test_destructive_annotation_is_believed() -> None:
    """The tool's author saying it destroys something is taken at face value."""
    verdict = classify_tool("doTheThing", manifest=manifest_for(), destructive_hint=True)

    assert verdict.risk is RiskLevel.DESTRUCTIVE
    assert verdict.source == "annotation"


def test_read_only_annotation_is_not_believed_when_the_name_contradicts_it() -> None:
    """An upstream annotating `delete_repository` read-only is buggy or hostile.

    Either way the name wins, because believing it would route a deletion around
    every confirmation gate in the hub.
    """
    verdict = classify_tool("delete_repository", manifest=manifest_for(), read_only_hint=True)

    assert verdict.risk is RiskLevel.DESTRUCTIVE
    assert verdict.source == "heuristic"


def test_read_only_annotation_is_believed_when_nothing_contradicts_it() -> None:
    verdict = classify_tool("get_design_context", manifest=manifest_for(), read_only_hint=True)

    assert verdict.risk is RiskLevel.READ
    assert verdict.source == "annotation"


def test_unrecognisable_names_fall_back_to_the_integration_default() -> None:
    """Not to the safest reading — the manifest states what this integration is."""
    verdict = classify_tool("xyzzy", manifest=manifest_for(default_risk=RiskLevel.WRITE))

    assert verdict.risk is RiskLevel.WRITE
    assert verdict.source == "default"
    assert not verdict.is_explicit, "a fallback must be visibly a guess"


# ------------------------------------------------------------------------- heuristic


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("getJiraIssue", RiskLevel.READ),
        ("get_file_contents", RiskLevel.READ),
        ("searchJiraIssuesUsingJql", RiskLevel.READ),
        ("list_commits", RiskLevel.READ),
        ("createJiraIssue", RiskLevel.WRITE),
        ("update-page", RiskLevel.WRITE),
        ("add_issue_comment", RiskLevel.WRITE),
        ("transitionJiraIssue", RiskLevel.WRITE),
        ("deleteJiraIssue", RiskLevel.DESTRUCTIVE),
        ("delete_repository", RiskLevel.DESTRUCTIVE),
        ("force_push", RiskLevel.DESTRUCTIVE),
        ("merge_pull_request", RiskLevel.DESTRUCTIVE),
        ("grant_admin", RiskLevel.ADMIN),
        ("rotate_key", RiskLevel.ADMIN),
        ("transfer_ownership", RiskLevel.ADMIN),
    ],
)
def test_names_classify_as_expected(tool: str, expected: RiskLevel) -> None:
    """Both naming conventions upstreams actually use: camelCase and snake_case."""
    assert classify_tool(tool, manifest=manifest_for()).risk is expected


def test_the_worst_verb_in_a_name_wins() -> None:
    """`get_or_delete_thing` is a deletion, whatever it also does."""
    assert classify_tool("get_or_delete_thing", manifest=manifest_for()).risk is RiskLevel.DESTRUCTIVE


def test_description_promotes_an_otherwise_unrecognised_tool() -> None:
    """The tiebreaker for a tool whose name says nothing."""
    verdict = classify_tool(
        "xyzzy",
        manifest=manifest_for(),
        description="Permanently removes the record. This cannot be undone.",
    )

    assert verdict.risk is RiskLevel.DESTRUCTIVE
    assert verdict.source == "heuristic"


def test_description_does_not_override_a_clear_name() -> None:
    """A read tool that mentions deletion in passing stays a read."""
    verdict = classify_tool(
        "get_deletion_log",
        manifest=manifest_for(),
        description="Lists records that were permanently deleted.",
    )

    assert verdict.risk is RiskLevel.READ


def test_dotted_upstream_names_are_normalised() -> None:
    """Some upstreams namespace with dots; the verb still has to be found."""
    assert classify_tool("issues.delete", manifest=manifest_for()).risk is RiskLevel.DESTRUCTIVE
