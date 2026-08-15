# Errors, health, and retry policy

Every deliberate failure carries a stable machine-readable `code`. Those codes
are part of the hub's public contract — they appear in REST bodies, MCP tool
errors and audit records. **Branch on the code, not on the message text.**

## Error codes

| Code | HTTP | What happened | What to do | Retry? |
|---|---|---|---|---|
| `not_authenticated` | 401 | No token, malformed token, or expired (default TTL 1h). | Reissue a token; send it as `Authorization: Bearer`. | After fixing |
| `not_authorized` | 403 | Token lacks the required scope. The message names the scope. | Reissue with that scope. Agents get `tools:call` + `integrations:read`. | After fixing |
| `policy_violation` | 403 | The policy engine denied it: deny list, closed allowlist, risk floor, restricted principal, or an argument restriction. The message names the rule. | Report it with the rule name. Only an operator can change policy. | **No** |
| `confirmation_required` | 403 | The tool needs a human and your client cannot be asked (no elicitation capability). | Use a client that supports elicitation. See [confirmation.md](confirmation.md). | **No** |
| `rate_limited` | 429 | A configured limit is exhausted. Message carries a retry hint; REST sets `Retry-After`. | Wait the hint, then retry once. | Yes, after the hint |
| `tool_not_found` | 404 | No such tool. Almost always an unqualified or mistyped name. | `hub.search_tools`, then the exact qualified name. | **No** |
| `integration_not_found` | 404 | No integration registered under that id. | `hub.list_integrations` with `enabled_only: false`. | **No** |
| `integration_disabled` | 409 | Exists, switched off in `integrations.yaml`. | Operator: `mcp-hub enable <id>`. | **No** |
| `integration_unavailable` | 503 | Enabled but its upstream cannot serve. Graceful degradation — the rest of the hub is fine. | Use other integrations; report which one is down. | Once, then stop |
| `integration_locked` | 409 | Another lifecycle operation holds that integration's lock. | Wait for the update/install to finish. | Yes, after a pause |
| `upstream_error` | 502 | The upstream server itself errored. | Read the message; it is the upstream's complaint, not the hub's. | Depends on the tool's risk |
| `secret_not_found` | 424 | A credential the call needs is absent from every provider. | Operator: `mcp-hub secrets set <NAME>`; per-user ones need `--principal`. | **No** |
| `validation_failed` | 422 | Arguments failed schema or semantic validation. | Rebuild arguments from `hub.describe_tool`. | After fixing |
| `invalid_configuration` | 400 | Config the hub refuses to run with. | Operator problem. | **No** |
| `supply_chain_rejected` | 400 | An artifact failed signature or pinning checks. | Operator problem; do not try to bypass. | **No** |
| `update_failed` / `rollback_failed` | 500 | A lifecycle operation failed. | Operator: read the job, then `mcp-hub logs`. | **No** |
| `internal_error` | 500 | A bug. The hub does not echo unknown exception text. | Quote the `x-request-id` from the response headers. | Once |

Never retry a deterministic refusal in a loop. `policy_violation`,
`not_authorized`, `confirmation_required` and `tool_not_found` return exactly
the same answer the second time, and each attempt lands in the audit trail.

## Health statuses

| Status | Meaning | Whose problem |
|---|---|---|
| `HEALTHY` | Initialised, tools listed, serving. | — |
| `DEGRADED` | Reachable but failing checks, or slow past its budget. | Report; retry sparingly |
| `UNAVAILABLE` | Enabled but unreachable; its tools stop being routed. | Operator / upstream outage |
| `DISABLED` | Switched off by configuration. Not an error. | Operator: `mcp-hub enable <id>` |
| `UPDATE_REQUIRED` | Not installed, or the installed version cannot start. | Operator: `mcp-hub install/update <id>` |
| `AUTH_REQUIRED` | No usable credential. | Operator: `mcp-hub secrets set <NAME>` |

`mcp-hub health` exits non-zero only when an *enabled* integration is
`UNAVAILABLE`, which makes it usable as a deployment gate. `DEGRADED` and
`AUTH_REQUIRED` are reported without failing — the hub is still serving.

## Risk levels

`hub.describe_tool` returns one of these. A manifest's explicit classification
beats the name heuristic.

| Risk | Meaning | Expect |
|---|---|---|
| `READ` | Observes state. Safe to retry. | No confirmation |
| `WRITE` | Creates or modifies state. Not necessarily reversible. | Confirmation if policy says so |
| `DESTRUCTIVE` | Removes state. Assume it cannot be undone. | Confirmation by default |
| `ADMIN` | Changes permissions, billing, or upstream configuration. | Confirmation, often denied outright |

## Reporting a failure well

Include, in this order: the qualified tool name, the error `code`, the hub's
message verbatim, and the `x-request-id` response header. That id ties the
response to its audit record, which is how an operator finds the decision that
produced it:

```bash
mcp-hub logs --action tool_call --since 1h
mcp-hub logs --action policy_violation
mcp-hub logs <id> --process-log     # what the upstream server printed
```

Paraphrasing the message loses the rule name, which is usually the only part
that tells the operator what to change.
