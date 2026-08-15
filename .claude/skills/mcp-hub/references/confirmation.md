# Human confirmation

Some tools cannot run until a human approves them. This is a feature of the hub,
not a malfunction, and it is the gate most often broken by a client that was
built without it.

## Why it is two rounds

Streamable HTTP gives a server no live back-channel during a tool call, so the
hub cannot "ask the user" mid-handler. Confirmation is an exchange:

```
round 1   client → tools/call
          hub    → InputRequiredResult { ElicitRequest, requestState }
round 2   client → tools/call  with input_responses + requestState
          hub    → the real result, or a denial
```

The hub stores nothing between the rounds. `requestState` carries the pending
decision, sealed with authenticated encryption bound to the method, the tool, a
digest of the arguments, the calling principal, and a TTL. It therefore cannot
be forged, replayed against different arguments, handed to another user, or
reused after it expires — and because the state travels with the request rather
than living in the hub, confirmations survive restarts and work across replicas.

Practical consequences:

* **Echo `requestState` back unchanged.** Do not parse, trim, or rebuild it.
* **Do not change the arguments between rounds.** The digest binds them; an
  altered call is rejected rather than silently approved.
* **Do not cache a `requestState` for later use.** It is single-purpose and
  time-limited.

Clients built on the official SDK get all of this from an
`elicitation_callback`; only hand-rolled HTTP clients need to handle the two
rounds themselves.

## Two rules the hub enforces

1. **A missing capability is a refusal, not an approval.** A client that
   declares no elicitation capability at `initialize` cannot be asked, so the
   call is denied with `confirmation_required`. Silence is never consent.
2. **The verdict is read from the response, not assumed.** `decline`, `cancel`,
   a missing answer, and `confirm: false` all deny. Only an explicit accept
   proceeds.

## What the prompt contains

The elicitation names the tool, its risk classification, which policy rule
demanded confirmation, and a redacted preview of the arguments — a human cannot
meaningfully approve "a destructive action" without knowing what it acts on:

```
Confirm jira.deleteJiraIssue
Risk: DESTRUCTIVE
Reason: require_confirmation
{
  "issueIdOrKey": "ENG-1421"
}
```

Argument values pass through redaction before rendering, since the preview
reaches a UI. Long previews are truncated.

## What to do when a call needs confirmation

* **Pass it through to the human.** Show the tool, the risk, and the arguments.
  Do not summarise away the specifics — the arguments are the point.
* **A decline is a final answer.** Do not re-issue the same call hoping for a
  different verdict.
* **Never route around the gate.** Finding a lower-risk tool that causes the
  same effect, splitting a destructive call into unconfirmed pieces, or asking
  the operator to remove the tool from `require_confirmation` mid-task all
  defeat a control that exists precisely for this moment. If confirmation is
  genuinely wrong for a tool, say so and leave the policy change to the
  operator, deliberately and outside the task.
* **Client cannot be asked?** Say so plainly: the call is refused by design,
  and the fix is a client that supports elicitation — not a policy edit.

## Confirmation vs. your own client's prompt

Claude Desktop, Claude Code and Cursor may confirm tool calls themselves. That
is an independent gate: seeing two prompts for one destructive call is correct.
Approving in the client does not pre-approve the hub's elicitation, and the hub
never treats a client-side approval as its own.

## Where confirmation comes from

`config/policies.yaml`, per integration:

```yaml
policies:
  jira:
    allow: [searchJiraIssuesUsingJql, getJiraIssue, createJiraIssue, editJiraIssue]
    require_confirmation: [createJiraIssue, editJiraIssue]
    deny: [deleteJiraIssue]

default:
  confirm_risk_at_or_above: DESTRUCTIVE   # backstop for tools nobody reviewed
```

`deny` is refusal — no prompt, no path through. `require_confirmation` is the
prompt. `confirm_risk_at_or_above` catches anything a reviewer never listed, so
a newly discovered destructive tool is gated before anyone notices it exists.
