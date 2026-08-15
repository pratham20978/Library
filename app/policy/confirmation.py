"""Human confirmation for dangerous tools (arch §53).

MCP's modern Streamable HTTP era has no live back-channel during a tool call, so
a server cannot simply "ask the user" mid-handler. Confirmation is a two-round
exchange instead:

    round 1   hub returns InputRequiredResult{ElicitRequest, requestState}
    round 2   client re-calls tools/call with input_responses + requestState

The hub stores nothing between the rounds. `requestState` carries the pending
decision, and the SDK's `RequestStateBoundary` middleware seals it with
authenticated encryption bound to the method, the tool, a digest of the
arguments, the calling principal, and a TTL. A client cannot forge one, replay
one against different arguments, or hand one to another user — and because the
state travels with the request rather than living in the hub, confirmations keep
working across replicas and restarts.

Two rules this module exists to enforce:

* **A missing capability is a refusal, not an approval.** A client that declares
  no elicitation capability cannot be asked, so the call is denied. That is the
  bypass arch §53 is written to prevent.
* **The verdict is read from the response, not assumed.** `decline`, `cancel`, a
  missing answer, or `confirm: false` all deny. Only an explicit accept proceeds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

import mcp.types as types

from app.core.domain import RiskLevel
from app.core.logging import get_logger

__all__ = [
    "CONFIRMATION_KEY",
    "ConfirmationVerdict",
    "build_confirmation_request",
    "client_supports_confirmation",
    "read_confirmation",
]

log = get_logger(__name__)

CONFIRMATION_KEY: Final = "mcp_hub_confirmation"
"""Key under which the elicitation is sent and its answer comes back."""

_STATE_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class ConfirmationVerdict:
    """What the human decided."""

    accepted: bool
    reason: str
    """Operator- and agent-facing explanation of the outcome."""

    responded: bool = True
    """False when no answer was present — the first round of the exchange."""


def client_supports_confirmation(session: Any) -> bool:
    """Whether this client can be shown a confirmation prompt.

    Args:
        session: The MCP `ServerSession` for the calling connection.

    Returns:
        True when the client declared the elicitation capability at initialize.
    """
    try:
        return bool(
            session.check_client_capability(types.ClientCapabilities(elicitation=types.ElicitationCapability()))
        )
    except Exception as exc:  # noqa: BLE001 - an unusable session cannot be asked
        log.warning("confirmation.capability_check_failed", error=str(exc))
        return False


def build_confirmation_request(
    *,
    qualified_name: str,
    risk: RiskLevel,
    reason: str,
    arguments: dict[str, Any] | None = None,
    integration: str,
) -> types.InputRequiredResult:
    """Build the round-one result that asks a human to approve the call.

    The prompt names the tool, its risk classification, why confirmation was
    required, and a preview of the arguments — a human cannot meaningfully
    approve "a destructive action" without knowing what it acts on. Argument
    values are redacted before they are rendered, since they reach a UI.

    Args:
        qualified_name: The tool as the agent named it, e.g. `jira.delete_issue`.
        risk: How the tool was classified (arch §52).
        reason: Which policy rule demanded confirmation.
        arguments: The call's arguments, rendered as a redacted preview.
        integration: Owning integration id, recorded in the sealed state.

    Returns:
        An `InputRequiredResult` the MCP runtime turns into an elicitation.
    """
    from app.core.redaction import redact_arguments

    preview = redact_arguments(arguments)
    lines = [
        f"Confirm {qualified_name}",
        f"Risk: {risk.value}",
        f"Reason: {reason}",
    ]
    if preview:
        rendered = json.dumps(preview, indent=2, sort_keys=True, default=str)
        if len(rendered) > 1200:
            rendered = rendered[:1200] + "\n… (truncated)"
        lines.append(f"Arguments:\n{rendered}")
    lines.append("Approve this call?")

    return types.InputRequiredResult(
        input_requests={
            CONFIRMATION_KEY: types.ElicitRequest(
                params=types.ElicitRequestFormParams(
                    message="\n\n".join(lines),
                    requested_schema={
                        "type": "object",
                        "properties": {
                            "confirm": {
                                "type": "boolean",
                                "title": "Approve",
                                "description": f"Run {qualified_name}. Declining cancels the call.",
                            }
                        },
                        "required": ["confirm"],
                    },
                )
            )
        },
        request_state=json.dumps(
            {"v": _STATE_VERSION, "tool": qualified_name, "integration": integration, "risk": risk.value},
            separators=(",", ":"),
        ),
    )


def read_confirmation(params: types.CallToolRequestParams, *, qualified_name: str) -> ConfirmationVerdict:
    """Interpret the round-two answer, if there is one.

    The SDK has already verified and unsealed `request_state` by the time this
    runs — a tampered or expired token is rejected at the middleware boundary and
    never reaches here. The tool-name check below is defence in depth against a
    future refactor that weakens that binding, not a substitute for it.

    Args:
        params: The incoming `tools/call` parameters.
        qualified_name: The tool being called this round.

    Returns:
        The verdict. `responded=False` means this is round one and a prompt
        should be issued.
    """
    answer = (params.input_responses or {}).get(CONFIRMATION_KEY)
    if answer is None:
        return ConfirmationVerdict(accepted=False, reason="No confirmation answer present.", responded=False)

    if params.request_state:
        try:
            state = json.loads(params.request_state)
        except (TypeError, ValueError):
            return ConfirmationVerdict(accepted=False, reason="Confirmation state was unreadable.")
        if state.get("tool") != qualified_name:
            log.warning(
                "confirmation.tool_mismatch",
                expected=qualified_name,
                found=state.get("tool"),
            )
            return ConfirmationVerdict(
                accepted=False, reason="Confirmation was issued for a different tool and cannot be reused."
            )

    if not isinstance(answer, types.ElicitResult):
        return ConfirmationVerdict(accepted=False, reason="Confirmation answer had an unexpected shape.")

    if answer.action == "decline":
        return ConfirmationVerdict(accepted=False, reason="The user declined this action.")
    if answer.action == "cancel":
        return ConfirmationVerdict(accepted=False, reason="The user cancelled this action.")

    content = answer.content or {}
    if content.get("confirm") is True:
        return ConfirmationVerdict(accepted=True, reason="The user approved this action.")
    return ConfirmationVerdict(accepted=False, reason="The user did not approve this action.")
