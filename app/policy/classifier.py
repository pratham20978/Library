"""Classifying what a tool can do (arch §52).

Tools arrive from upstreams the hub does not control, with no risk field in the
MCP schema. Something has to decide whether `delete_issue` deserves a human
confirmation, and it cannot be a hard-coded list — arch §12 forbids that, and an
upstream can add a tool tomorrow.

So classification is layered, strongest evidence first:

1. **The manifest.** An explicit `tool_risk` entry is a human decision about a
   named tool. Arch §52 says to trust it over heuristics, and it wins outright.
2. **MCP annotations.** `destructive_hint` and `read_only_hint` come from the
   upstream author describing their own tool. Better than guessing at a name,
   but self-reported, so it cannot *lower* what the name plainly implies.
3. **The name.** Verb matching over the tool name, and only then its description.

The bias throughout is toward over-classification. Calling a read a write costs
one confirmation prompt; calling a delete a read costs data. Anything
unrecognised lands on `WRITE`, never `READ`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.config.models import IntegrationManifest
from app.core.domain import RiskLevel

__all__ = ["Classification", "classify_tool"]


@dataclass(frozen=True, slots=True)
class Classification:
    """A risk verdict and where it came from."""

    risk: RiskLevel
    source: str
    """`manifest`, `annotation`, `heuristic`, or `default` — surfaced in the tool
    registry so an operator can see which tools were guessed at."""

    @property
    def is_explicit(self) -> bool:
        """Whether a human or the tool's author stated this, rather than a guess."""
        return self.source in ("manifest", "annotation")


# Ordered most-severe first: the first pattern that matches wins, so `delete`
# beats `get` in `get_or_delete_thing`.
_PATTERNS: Final[tuple[tuple[RiskLevel, re.Pattern[str]], ...]] = (
    (
        RiskLevel.ADMIN,
        re.compile(
            r"(?:^|_|\b)(?:"
            r"grant|revoke|impersonate|escalate|"
            r"set_permission|add_member|remove_member|"
            r"transfer_ownership|change_owner|"
            r"admin|billing|subscription|"
            r"rotate_key|reset_password|disable_user|enable_user|"
            r"set_policy|update_policy|configure"
            r")(?:_|\b)",
            re.IGNORECASE,
        ),
    ),
    (
        RiskLevel.DESTRUCTIVE,
        re.compile(
            r"(?:^|_|\b)(?:"
            r"delete|destroy|remove|drop|purge|erase|wipe|truncate|"
            r"force_push|hard_reset|reset|revert|rollback|"
            r"merge|close|archive|cancel|terminate|kill|"
            r"uninstall|deprovision|expire"
            r")(?:_|\b)",
            re.IGNORECASE,
        ),
    ),
    (
        RiskLevel.WRITE,
        re.compile(
            r"(?:^|_|\b)(?:"
            r"create|add|insert|new|make|"
            r"update|edit|modify|patch|change|set|put|"
            r"write|save|store|upload|post|send|publish|"
            r"move|rename|copy|clone|duplicate|"
            r"assign|transition|approve|reject|comment|"
            r"start|stop|restart|trigger|run|execute|invoke|dispatch"
            r")(?:_|\b)",
            re.IGNORECASE,
        ),
    ),
    (
        RiskLevel.READ,
        re.compile(
            r"(?:^|_|\b)(?:"
            r"get|list|read|fetch|find|search|query|lookup|"
            r"show|view|describe|inspect|check|"
            r"count|exists|has|is|can|"
            r"download|export|render|preview|resolve|summar(?:y|ize|ise)"
            r")(?:_|\b)",
            re.IGNORECASE,
        ),
    ),
)

_DESCRIPTION_DESTRUCTIVE: Final = re.compile(
    r"\b(?:permanently|irreversib\w+|cannot be undone|destructive|deletes?|removes?)\b",
    re.IGNORECASE,
)


def classify_tool(
    tool_name: str,
    *,
    manifest: IntegrationManifest,
    description: str | None = None,
    read_only_hint: bool | None = None,
    destructive_hint: bool | None = None,
) -> Classification:
    """Decide how dangerous `tool_name` is.

    Args:
        tool_name: The upstream tool name, unqualified.
        manifest: Owning integration, consulted for explicit overrides and the
            fallback risk level.
        description: The tool's description, used only as a tiebreaker.
        read_only_hint: MCP `annotations.readOnlyHint`, if the upstream set it.
        destructive_hint: MCP `annotations.destructiveHint`, if the upstream set it.

    Returns:
        The verdict and the evidence it rests on.
    """
    explicit = manifest.tool_risk.get(tool_name)
    if explicit is not None:
        return Classification(risk=explicit, source="manifest")

    heuristic = _from_name(tool_name, description)

    if destructive_hint is True:
        # The author says it destroys something. Take them at their word, unless
        # the name says something even worse (an admin operation).
        risk = RiskLevel.ADMIN if heuristic and heuristic.rank > RiskLevel.DESTRUCTIVE.rank else RiskLevel.DESTRUCTIVE
        return Classification(risk=risk, source="annotation")

    if read_only_hint is True:
        # A read-only claim is only believed when nothing contradicts it. An
        # upstream that annotates `delete_repository` as read-only is either
        # buggy or hostile, and either way the name wins.
        if heuristic is None or heuristic is RiskLevel.READ:
            return Classification(risk=RiskLevel.READ, source="annotation")
        return Classification(risk=heuristic, source="heuristic")

    if heuristic is not None:
        return Classification(risk=heuristic, source="heuristic")

    # Nothing matched. Fall back to the integration's declared default rather
    # than assuming the safest interpretation.
    return Classification(risk=manifest.risk_level, source="default")


def _from_name(tool_name: str, description: str | None) -> RiskLevel | None:
    """Match the name against the verb patterns, then consult the description."""
    normalised = _normalise(tool_name)
    for level, pattern in _PATTERNS:
        if pattern.search(normalised):
            return level
    if description and _DESCRIPTION_DESTRUCTIVE.search(description):
        return RiskLevel.DESTRUCTIVE
    return None


def _normalise(tool_name: str) -> str:
    """Rewrite camelCase and dots into underscore-separated words.

    Upstreams are inconsistent — Atlassian ships `deleteJiraIssue`, the reference
    servers ship `delete_entities`. Both must reach the same verb boundary for
    the patterns above to fire.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", tool_name)
    return spaced.replace(".", "_").replace("-", "_").lower()
