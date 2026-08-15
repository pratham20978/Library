"""The policy engine's decision order (arch §22, §23, §53).

The engine's guarantee is not "it usually refuses the right things" — it is that
the *order* of checks is fixed, so a later rule can never soften an earlier one.
These tests assert the order directly: each one arranges a rule where two checks
disagree, and pins which of them wins.

`rule` is the important word. A `PolicyRule` is what an operator writes in
`config/policies.yaml`, so every test here is a statement about what a line in
that file does.
"""

from __future__ import annotations

import pytest

from app.config.models import PolicyRule, RateLimitSpec, ToolPolicy
from app.core.context import ANONYMOUS, Principal
from app.core.domain import PolicyDecision, RiskLevel
from app.core.errors import RateLimited
from app.policy.engine import PolicyEngine, PolicyRequest
from app.policy.ratelimit import InMemoryRateLimiter

pytestmark = pytest.mark.security


def build_engine(*, require_authentication: bool = True, global_limit: RateLimitSpec | None = None) -> PolicyEngine:
    """An engine with a fresh limiter, so no test inherits another's quota."""
    return PolicyEngine(
        rate_limiter=InMemoryRateLimiter(),
        require_authentication=require_authentication,
        global_rate_limit=global_limit,
    )


def request_for(
    tool: str,
    *,
    principal: Principal | None = None,
    risk: RiskLevel = RiskLevel.READ,
    arguments: dict[str, object] | None = None,
    verbs: tuple[str, ...] = (),
) -> PolicyRequest:
    """A call to `jira.<tool>` by an ordinary authenticated caller."""
    return PolicyRequest(
        principal=principal or Principal(subject="alice", scopes=frozenset({"tools:call"})),
        integration_id="jira",
        namespace="jira",
        tool_name=tool,
        qualified_name=f"jira.{tool}",
        risk=risk,
        arguments=dict(arguments or {}),
        manifest_confirmation_verbs=verbs,
    )


# --------------------------------------------------------------------------- ordering


async def test_deny_beats_allow() -> None:
    """A tool on both lists is refused. `deny` is final (arch §22)."""
    rule = PolicyRule(allow=("deleteJiraIssue",), deny=("deleteJiraIssue",))

    outcome = await build_engine().evaluate(request_for("deleteJiraIssue"), rule)

    assert outcome.decision is PolicyDecision.DENY
    assert outcome.rule == "deny_list"


async def test_deny_beats_a_per_tool_allow_override() -> None:
    """A per-tool `allow` cannot re-open something the deny list closed."""
    rule = PolicyRule(
        deny=("deleteJiraIssue",),
        tools={"deleteJiraIssue": ToolPolicy(decision="allow")},
    )

    outcome = await build_engine().evaluate(request_for("deleteJiraIssue"), rule)

    assert outcome.decision is PolicyDecision.DENY


async def test_empty_allow_list_is_not_a_closed_allowlist() -> None:
    """Empty means "no allowlist configured", not "permit nothing".

    Conflating the two would open every tool the moment someone deleted the last
    line of an `allow:` block — a deletion that reads like a tightening.
    """
    outcome = await build_engine().evaluate(request_for("anythingAtAll"), PolicyRule())

    assert outcome.decision is PolicyDecision.ALLOW


async def test_non_empty_allow_list_is_closed() -> None:
    """Anything unnamed is refused once an allowlist exists."""
    rule = PolicyRule(allow=("getJiraIssue",))

    outcome = await build_engine().evaluate(request_for("createJiraIssue"), rule)

    assert outcome.decision is PolicyDecision.DENY
    assert outcome.rule == "allowlist"
    assert "config/policies.yaml" in outcome.reason, "the refusal must say how to fix it"


async def test_allow_list_accepts_glob_patterns() -> None:
    """Policy lists hold names, optionally with wildcards."""
    rule = PolicyRule(allow=("get*",))
    engine = build_engine()

    assert (await engine.evaluate(request_for("getJiraIssue"), rule)).decision is PolicyDecision.ALLOW
    assert (await engine.evaluate(request_for("createJiraIssue"), rule)).decision is PolicyDecision.DENY


# --------------------------------------------------------------------- authentication


async def test_anonymous_is_refused_when_authentication_is_required() -> None:
    outcome = await build_engine().evaluate(request_for("getJiraIssue", principal=ANONYMOUS), PolicyRule())

    assert outcome.decision is PolicyDecision.DENY
    assert outcome.rule == "authentication"


async def test_anonymous_is_permitted_when_authentication_is_not_required() -> None:
    """The closed development loop `require_authentication: false` describes."""
    engine = build_engine(require_authentication=False)

    outcome = await engine.evaluate(request_for("getJiraIssue", principal=ANONYMOUS), PolicyRule())

    assert outcome.decision is PolicyDecision.ALLOW


async def test_authentication_is_checked_before_the_allowlist() -> None:
    """An anonymous caller is refused for being anonymous, not for the tool.

    The distinction matters to whoever reads the audit trail: "log in" and "ask
    for this tool to be allowed" are different remedies.
    """
    rule = PolicyRule(allow=("getJiraIssue",))

    outcome = await build_engine().evaluate(request_for("createJiraIssue", principal=ANONYMOUS), rule)

    assert outcome.rule == "authentication"


async def test_restricted_principals_exclude_everyone_else() -> None:
    rule = PolicyRule(allowed_principals=("ops@example.com",))

    outcome = await build_engine().evaluate(request_for("getJiraIssue"), rule)

    assert outcome.decision is PolicyDecision.DENY
    assert outcome.rule == "allowed_principals"


async def test_scope_requirement_is_enforced() -> None:
    rule = PolicyRule(allowed_scopes=("integrations:write",))

    outcome = await build_engine().evaluate(request_for("createJiraIssue"), rule)

    assert outcome.decision is PolicyDecision.DENY
    assert outcome.rule == "scopes"


async def test_admin_scope_satisfies_any_scope_requirement() -> None:
    """`admin` is the wildcard — which is exactly why agents must not hold it."""
    rule = PolicyRule(allowed_scopes=("integrations:write",))
    operator = Principal(subject="ops", scopes=frozenset({"admin"}))

    outcome = await build_engine().evaluate(request_for("createJiraIssue", principal=operator), rule)

    assert outcome.decision is PolicyDecision.ALLOW


# ----------------------------------------------------------------------- confirmation


async def test_named_tool_requires_confirmation() -> None:
    rule = PolicyRule(require_confirmation=("createJiraIssue",))

    outcome = await build_engine().evaluate(request_for("createJiraIssue"), rule)

    assert outcome.decision is PolicyDecision.CONFIRM
    assert outcome.needs_confirmation
    assert outcome.allowed, "confirm is a gate, not a refusal"


async def test_risk_threshold_catches_a_tool_nobody_listed() -> None:
    """The backstop for tools an upstream added since the policy was written.

    This is the check that makes an out-of-date `policies.yaml` safe rather than
    silently permissive.
    """
    rule = PolicyRule(confirm_risk_at_or_above=RiskLevel.DESTRUCTIVE)

    outcome = await build_engine().evaluate(request_for("purgeEverything", risk=RiskLevel.DESTRUCTIVE), rule)

    assert outcome.decision is PolicyDecision.CONFIRM
    assert outcome.rule == "risk_threshold"


async def test_risk_threshold_leaves_lower_risk_alone() -> None:
    rule = PolicyRule(confirm_risk_at_or_above=RiskLevel.DESTRUCTIVE)

    outcome = await build_engine().evaluate(request_for("getJiraIssue", risk=RiskLevel.READ), rule)

    assert outcome.decision is PolicyDecision.ALLOW


async def test_admin_risk_is_above_the_destructive_threshold() -> None:
    """`ADMIN` outranks `DESTRUCTIVE`, so a threshold set at the latter catches it."""
    rule = PolicyRule(confirm_risk_at_or_above=RiskLevel.DESTRUCTIVE)

    outcome = await build_engine().evaluate(request_for("grantAdmin", risk=RiskLevel.ADMIN), rule)

    assert outcome.decision is PolicyDecision.CONFIRM


async def test_manifest_verbs_match_at_word_boundaries() -> None:
    """Manifest `confirmation_required` holds verbs, not names (arch §32).

    `create` must catch `createJiraIssue` without catching `getCreatorProfile`,
    which is the difference between a verb matcher and a substring search.
    """
    engine = build_engine()

    confirmed = await engine.evaluate(request_for("createJiraIssue", verbs=("create", "delete")), PolicyRule())
    untouched = await engine.evaluate(request_for("getCreatorProfile", verbs=("create", "delete")), PolicyRule())

    assert confirmed.decision is PolicyDecision.CONFIRM
    assert confirmed.rule == "manifest_verbs"
    assert untouched.decision is PolicyDecision.ALLOW


async def test_deny_wins_over_confirmation() -> None:
    """A tool that is both denied and confirmable is denied, not prompted for."""
    rule = PolicyRule(deny=("deleteJiraIssue",), require_confirmation=("deleteJiraIssue",))

    outcome = await build_engine().evaluate(request_for("deleteJiraIssue"), rule)

    assert outcome.decision is PolicyDecision.DENY


# ------------------------------------------------------------------------- arguments


async def test_argument_size_limit_is_enforced() -> None:
    rule = PolicyRule(tools={"fetch": ToolPolicy(max_argument_bytes=64)})

    outcome = await build_engine().evaluate(
        request_for("fetch", arguments={"url": "https://example.com/" + "x" * 200}), rule
    )

    assert outcome.decision is PolicyDecision.DENY
    assert outcome.rule == "argument_size"


async def test_allowed_arguments_compare_as_strings() -> None:
    """A YAML-authored `- "30"` must match a JSON-supplied `30`.

    Otherwise an operator's restriction is defeated by the agent's choice of
    literal type, which is not something they can see or control.
    """
    rule = PolicyRule(tools={"convert_time": ToolPolicy(allowed_arguments={"hours": ("30",)})})
    engine = build_engine()

    numeric = await engine.evaluate(request_for("convert_time", arguments={"hours": 30}), rule)
    other = await engine.evaluate(request_for("convert_time", arguments={"hours": 31}), rule)

    assert numeric.decision is PolicyDecision.ALLOW
    assert other.decision is PolicyDecision.DENY


async def test_denied_arguments_refuse_specific_values() -> None:
    rule = PolicyRule(tools={"git_push": ToolPolicy(denied_arguments={"branch": ("main",)})})
    engine = build_engine()

    blocked = await engine.evaluate(request_for("git_push", arguments={"branch": "main"}), rule)
    permitted = await engine.evaluate(request_for("git_push", arguments={"branch": "topic"}), rule)

    assert blocked.decision is PolicyDecision.DENY
    assert permitted.decision is PolicyDecision.ALLOW


async def test_argument_restrictions_ignore_absent_arguments() -> None:
    """A restriction on an argument the call did not send must not fire."""
    rule = PolicyRule(tools={"git_push": ToolPolicy(allowed_arguments={"branch": ("topic",)})})

    outcome = await build_engine().evaluate(request_for("git_push", arguments={}), rule)

    assert outcome.decision is PolicyDecision.ALLOW


# ----------------------------------------------------------------------- rate limits


async def test_rate_limit_refuses_once_the_bucket_is_empty() -> None:
    rule = PolicyRule(rate_limit=RateLimitSpec(requests=2, window_seconds=60))
    engine = build_engine()

    for _ in range(2):
        assert (await engine.evaluate(request_for("getJiraIssue"), rule)).decision is PolicyDecision.ALLOW

    with pytest.raises(RateLimited):
        await engine.evaluate(request_for("getJiraIssue"), rule)


async def test_principal_scoped_limits_do_not_bleed_between_callers() -> None:
    """Alice exhausting her quota must not refuse Bob."""
    rule = PolicyRule(rate_limit=RateLimitSpec(requests=1, window_seconds=60, scope="principal"))
    engine = build_engine()
    alice = Principal(subject="alice", scopes=frozenset({"tools:call"}))
    bob = Principal(subject="bob", scopes=frozenset({"tools:call"}))

    await engine.evaluate(request_for("getJiraIssue", principal=alice), rule)
    with pytest.raises(RateLimited):
        await engine.evaluate(request_for("getJiraIssue", principal=alice), rule)

    assert (await engine.evaluate(request_for("getJiraIssue", principal=bob), rule)).allowed


async def test_integration_scoped_limits_are_shared_across_callers() -> None:
    """Correct for a metered key the whole deployment shares, like Brave's."""
    rule = PolicyRule(rate_limit=RateLimitSpec(requests=1, window_seconds=60, scope="integration"))
    engine = build_engine()

    await engine.evaluate(
        request_for("getJiraIssue", principal=Principal(subject="alice", scopes=frozenset({"tools:call"}))), rule
    )

    with pytest.raises(RateLimited):
        await engine.evaluate(
            request_for("getJiraIssue", principal=Principal(subject="bob", scopes=frozenset({"tools:call"}))),
            rule,
        )


async def test_preview_does_not_spend_quota() -> None:
    """Building the exposed tool list must not consume a caller's budget (arch §13).

    Otherwise listing tools would ration calling them, and an agent that read the
    catalogue twice would find itself unable to work.
    """
    rule = PolicyRule(rate_limit=RateLimitSpec(requests=1, window_seconds=60))
    engine = build_engine()

    for _ in range(10):
        assert engine.preview(request_for("getJiraIssue"), rule).decision is PolicyDecision.ALLOW

    assert (await engine.evaluate(request_for("getJiraIssue"), rule)).allowed, (
        "preview must have left the bucket full"
    )


async def test_denied_calls_never_reach_the_rate_limiter() -> None:
    """A deny short-circuits, so a refused call cannot exhaust a real caller's quota.

    Without this, anyone able to name a denied tool could rate-limit a victim by
    spending their bucket on calls that were never going to run.
    """
    rule = PolicyRule(
        deny=("deleteJiraIssue",),
        rate_limit=RateLimitSpec(requests=1, window_seconds=60),
    )
    engine = build_engine()

    for _ in range(5):
        assert (await engine.evaluate(request_for("deleteJiraIssue"), rule)).decision is PolicyDecision.DENY

    assert (await engine.evaluate(request_for("getJiraIssue"), rule)).allowed


async def test_global_limit_applies_on_top_of_the_integration_limit() -> None:
    """Whichever binds first wins; the global ceiling is not escapable per-rule."""
    engine = build_engine(global_limit=RateLimitSpec(requests=1, window_seconds=60, scope="global"))
    rule = PolicyRule(rate_limit=RateLimitSpec(requests=1000, window_seconds=60))

    await engine.evaluate(request_for("getJiraIssue"), rule)

    with pytest.raises(RateLimited):
        await engine.evaluate(request_for("getJiraIssue"), rule)
