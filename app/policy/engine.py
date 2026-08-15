"""The policy engine (arch §22, §23).

Every tool call passes through `evaluate` before anything reaches an upstream.
The pipeline is fixed and ordered, and the order is the contract:

    authentication -> principal -> scopes -> deny -> risk floor -> allow
      -> per-tool override -> argument restrictions -> confirmation -> rate limit

Three invariants hold throughout:

* **Deny is final.** Nothing downstream can turn a `DENY` into anything else.
  A deny short-circuits, so an expensive check never runs for a refused call.
* **Confirm is not a soft allow.** `CONFIRM` means a human must accept before
  the call proceeds. If the client cannot be asked, the caller gets a refusal —
  arch §53 says a model must never be able to bypass confirmation, and
  "the client didn't support it" is exactly the bypass that would matter.
* **An empty `allow` list is not an empty allowlist.** Empty means "no allowlist
  configured, fall through"; non-empty means closed. Conflating the two would
  silently open every tool the moment someone deleted a line from a policy file.

Two matchers, deliberately different. Policy lists (`allow`, `deny`,
`require_confirmation`) hold tool *names*, optionally with glob wildcards —
arch §22's examples are exact names, and an operator writing `create_issue`
means that tool. Manifest `confirmation_required` holds *verbs* (`create`,
`delete`) matched at word boundaries, because arch §32's example is a list of
verbs meant to cover a family of tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, assert_never

from app.config.models import PolicyRule, RateLimitSpec, ToolPolicy
from app.core.context import Principal
from app.core.domain import PolicyDecision, RiskLevel
from app.core.errors import RateLimited
from app.core.logging import get_logger
from app.policy.classifier import _normalise
from app.policy.ratelimit import RateLimiter, RateLimitResult

__all__ = ["PolicyEngine", "PolicyOutcome", "PolicyRequest"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    """Everything the engine needs to judge one tool call."""

    principal: Principal
    integration_id: str
    namespace: str
    tool_name: str
    """Unqualified upstream name, which is what policy lists are written against."""

    qualified_name: str
    risk: RiskLevel
    arguments: dict[str, Any] = field(default_factory=dict)
    manifest_confirmation_verbs: tuple[str, ...] = ()
    """Verb tokens from the manifest's `confirmation_required` (arch §32)."""


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    """The engine's verdict, with the reason that produced it."""

    decision: PolicyDecision
    reason: str
    """Operator-facing explanation. Safe to return to the caller."""

    rule: str = ""
    """Which check fired, e.g. `deny_list`, `risk_floor`, `allowlist`."""

    rate_limit: RateLimitResult | None = None
    """Result of the limit check, when one ran."""

    @property
    def allowed(self) -> bool:
        """Whether the call may proceed, possibly after a confirmation."""
        return self.decision is not PolicyDecision.DENY

    @property
    def needs_confirmation(self) -> bool:
        """Whether a human must accept before the call runs."""
        return self.decision is PolicyDecision.CONFIRM


class PolicyEngine:
    """Evaluates policy for tool calls."""

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter,
        require_authentication: bool = True,
        global_rate_limit: RateLimitSpec | None = None,
    ) -> None:
        self._limiter = rate_limiter
        self._require_authentication = require_authentication
        self._global_rate_limit = global_rate_limit

    # ------------------------------------------------------------- evaluation

    def preview(self, request: PolicyRequest, rule: PolicyRule) -> PolicyOutcome:
        """Decide without consuming quota.

        Used when building the exposed tool list (arch §13): `SELECTIVE` mode has
        to know which tools policy would permit, and answering that question must
        not spend a caller's rate-limit budget on tools they never called.
        """
        for check in (
            self._check_authentication,
            self._check_principal,
            self._check_scopes,
            self._check_deny_list,
            self._check_risk_floor,
            self._check_allow_list,
            self._check_tool_override,
            self._check_arguments,
        ):
            outcome = check(request, rule)
            if outcome is not None and outcome.decision is PolicyDecision.DENY:
                return outcome
        return self._confirmation_decision(request, rule)

    async def evaluate(self, request: PolicyRequest, rule: PolicyRule) -> PolicyOutcome:
        """Decide whether `request` may proceed, and charge it against its limits.

        Rate limits are checked last so a call that would be denied anyway does
        not consume quota — a probing agent should not be able to exhaust a
        legitimate user's budget by calling tools it has no access to.

        Raises:
            RateLimited: A configured limit is exhausted. Raised rather than
                returned because it is a transient condition with a retry hint,
                not a policy decision about this caller's permissions.
        """
        decision = self.preview(request, rule)
        if decision.decision is PolicyDecision.DENY:
            return decision
        await self._enforce_rate_limits(request, rule)
        return decision

    # --------------------------------------------------------------- gates

    def _check_authentication(self, request: PolicyRequest, _rule: PolicyRule) -> PolicyOutcome | None:
        if self._require_authentication and not request.principal.is_authenticated:
            return PolicyOutcome(
                decision=PolicyDecision.DENY,
                reason="This hub requires an authenticated caller for tool invocation.",
                rule="authentication",
            )
        return None

    def _check_principal(self, request: PolicyRequest, rule: PolicyRule) -> PolicyOutcome | None:
        if rule.allowed_principals and request.principal.subject not in rule.allowed_principals:
            return PolicyOutcome(
                decision=PolicyDecision.DENY,
                reason=f"Integration {request.integration_id!r} is restricted to specific principals.",
                rule="allowed_principals",
            )
        return None

    def _check_scopes(self, request: PolicyRequest, rule: PolicyRule) -> PolicyOutcome | None:
        tool_rule = self._tool_rule(rule, request.tool_name)
        required = tool_rule.allowed_scopes if tool_rule and tool_rule.allowed_scopes else rule.allowed_scopes
        if required and not any(request.principal.has_scope(scope) for scope in required):
            return PolicyOutcome(
                decision=PolicyDecision.DENY,
                reason=f"Calling {request.qualified_name} requires one of these scopes: {', '.join(required)}.",
                rule="scopes",
            )
        return None

    def _check_deny_list(self, request: PolicyRequest, rule: PolicyRule) -> PolicyOutcome | None:
        if _matches_any(request.tool_name, rule.deny):
            return PolicyOutcome(
                decision=PolicyDecision.DENY,
                reason=f"{request.qualified_name} is explicitly denied by policy.",
                rule="deny_list",
            )
        return None

    def _check_risk_floor(self, request: PolicyRequest, rule: PolicyRule) -> PolicyOutcome | None:
        floor = rule.deny_risk_at_or_above
        if floor is not None and request.risk.rank >= floor.rank:
            return PolicyOutcome(
                decision=PolicyDecision.DENY,
                reason=(
                    f"{request.qualified_name} is classified {request.risk.value}, at or above "
                    f"the {floor.value} threshold this integration refuses."
                ),
                rule="risk_floor",
            )
        return None

    def _check_allow_list(self, request: PolicyRequest, rule: PolicyRule) -> PolicyOutcome | None:
        if rule.allow and not _matches_any(request.tool_name, rule.allow):
            return PolicyOutcome(
                decision=PolicyDecision.DENY,
                reason=(
                    f"{request.qualified_name} is not on the allowlist for "
                    f"{request.integration_id!r}. Add it to config/policies.yaml to permit it."
                ),
                rule="allowlist",
            )
        return None

    def _check_tool_override(self, request: PolicyRequest, rule: PolicyRule) -> PolicyOutcome | None:
        tool_rule = self._tool_rule(rule, request.tool_name)
        if tool_rule and tool_rule.decision == "deny":
            return PolicyOutcome(
                decision=PolicyDecision.DENY,
                reason=f"{request.qualified_name} is denied by its per-tool policy.",
                rule="tool_override",
            )
        return None

    def _check_arguments(self, request: PolicyRequest, rule: PolicyRule) -> PolicyOutcome | None:
        """Argument restrictions (arch §23).

        Compared as strings so a YAML-authored allowlist matches a JSON-supplied
        number — an operator writing `- "30"` should not be defeated by the agent
        sending `30`.
        """
        tool_rule = self._tool_rule(rule, request.tool_name)
        if tool_rule is None:
            return None

        if tool_rule.max_argument_bytes is not None:
            import json

            size = len(json.dumps(request.arguments, default=str).encode("utf-8"))
            if size > tool_rule.max_argument_bytes:
                return PolicyOutcome(
                    decision=PolicyDecision.DENY,
                    reason=(
                        f"Arguments for {request.qualified_name} are {size} bytes, over the "
                        f"{tool_rule.max_argument_bytes}-byte limit."
                    ),
                    rule="argument_size",
                )

        for name, permitted in tool_rule.allowed_arguments.items():
            if name not in request.arguments:
                continue
            if str(request.arguments[name]) not in permitted:
                return PolicyOutcome(
                    decision=PolicyDecision.DENY,
                    reason=f"Argument {name!r} of {request.qualified_name} has a value policy does not permit.",
                    rule="allowed_arguments",
                )

        for name, forbidden in tool_rule.denied_arguments.items():
            if name in request.arguments and str(request.arguments[name]) in forbidden:
                return PolicyOutcome(
                    decision=PolicyDecision.DENY,
                    reason=f"Argument {name!r} of {request.qualified_name} has a value policy forbids.",
                    rule="denied_arguments",
                )
        return None

    # -------------------------------------------------------- confirmation

    def _confirmation_decision(self, request: PolicyRequest, rule: PolicyRule) -> PolicyOutcome:
        """Decide between `ALLOW` and `CONFIRM` (arch §53).

        Four independent triggers; any one of them is enough. They are checked
        most-specific first only so the reason returned to the operator names the
        thing they actually configured.
        """
        tool_rule = self._tool_rule(rule, request.tool_name)
        if tool_rule and tool_rule.decision == "confirm":
            return PolicyOutcome(
                decision=PolicyDecision.CONFIRM,
                reason=f"{request.qualified_name} requires confirmation by its per-tool policy.",
                rule="tool_override",
            )

        if _matches_any(request.tool_name, rule.require_confirmation):
            return PolicyOutcome(
                decision=PolicyDecision.CONFIRM,
                reason=f"{request.qualified_name} requires confirmation by policy.",
                rule="require_confirmation",
            )

        threshold = rule.confirm_risk_at_or_above
        if threshold is not None and request.risk.rank >= threshold.rank:
            return PolicyOutcome(
                decision=PolicyDecision.CONFIRM,
                reason=(
                    f"{request.qualified_name} is classified {request.risk.value}, at or above "
                    f"the {threshold.value} confirmation threshold."
                ),
                rule="risk_threshold",
            )

        if _matches_verb(request.tool_name, request.manifest_confirmation_verbs):
            return PolicyOutcome(
                decision=PolicyDecision.CONFIRM,
                reason=f"{request.qualified_name} matches a confirmation verb in its integration manifest.",
                rule="manifest_verbs",
            )

        if tool_rule and tool_rule.decision == "allow":
            return PolicyOutcome(
                decision=PolicyDecision.ALLOW,
                reason="Permitted by per-tool policy.",
                rule="tool_override",
            )

        return PolicyOutcome(decision=PolicyDecision.ALLOW, reason="Permitted by policy.", rule="default")

    # --------------------------------------------------------- rate limits

    async def _enforce_rate_limits(self, request: PolicyRequest, rule: PolicyRule) -> None:
        """Apply every applicable limit, narrowest first.

        Raises:
            RateLimited: One of the buckets is empty.
        """
        subject = request.principal.subject or "anonymous"
        tool_rule = self._tool_rule(rule, request.tool_name)

        candidates: list[tuple[str, RateLimitSpec]] = []
        if tool_rule and tool_rule.rate_limit:
            candidates.append((f"tool:{request.qualified_name}", tool_rule.rate_limit))
        if rule.rate_limit:
            candidates.append((f"integration:{request.integration_id}", rule.rate_limit))
        if self._global_rate_limit:
            candidates.append(("global", self._global_rate_limit))

        for label, spec in candidates:
            key = _limit_key(label, spec, subject=subject, integration=request.integration_id)
            result = await self._limiter.check(key, spec)
            if not result.allowed:
                log.warning(
                    "policy.rate_limited",
                    scope=label,
                    principal=subject,
                    integration=request.integration_id,
                    tool=request.qualified_name,
                    retry_after=result.retry_after_seconds,
                )
                raise RateLimited(
                    f"Rate limit exceeded for {label} "
                    f"({spec.requests} requests per {spec.window_seconds:g}s). "
                    f"Retry in {result.retry_after_seconds:.1f}s.",
                    retry_after_seconds=result.retry_after_seconds,
                    scope=label,
                    limit=spec.capacity,
                )

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _tool_rule(rule: PolicyRule, tool_name: str) -> ToolPolicy | None:
        """Find the per-tool policy for `tool_name`, allowing glob keys."""
        direct = rule.tools.get(tool_name)
        if direct is not None:
            return direct
        for pattern, tool_rule in rule.tools.items():
            if _is_pattern(pattern) and fnmatchcase(tool_name, pattern):
                return tool_rule
        return None


def _limit_key(label: str, spec: RateLimitSpec, *, subject: str, integration: str) -> str:
    """Build the bucket key for one limit, honouring its scope."""
    match spec.scope:
        case "principal":
            return f"{label}|principal:{subject}"
        case "integration":
            return f"{label}|integration:{integration}"
        case "global":
            return f"{label}|global"
    assert_never(spec.scope)


def _is_pattern(value: str) -> bool:
    """Whether a policy entry is a glob rather than a literal name."""
    return any(char in value for char in "*?[")


def _matches_any(tool_name: str, patterns: tuple[str, ...]) -> bool:
    """Match a tool name against policy entries: exact, or glob when wildcarded."""
    for pattern in patterns:
        if _is_pattern(pattern):
            if fnmatchcase(tool_name, pattern):
                return True
        elif pattern == tool_name:
            return True
    return False


def _matches_verb(tool_name: str, verbs: tuple[str, ...]) -> bool:
    """Match manifest confirmation verbs at word boundaries (arch §32).

    `create` matches `create_issue` and `createJiraIssue`, but not `recreated`,
    so a verb list stays a verb list rather than a substring search.
    """
    if not verbs:
        return False
    tokens = set(_normalise(tool_name).split("_"))
    for verb in verbs:
        normalised = _normalise(verb)
        if normalised in tokens:
            return True
        # Multi-word entries such as `create_pull_request` are full names.
        if "_" in normalised and normalised in _normalise(tool_name):
            return True
    return False
