---
name: mcp-hub
description: Use an MCP Hub — one Streamable HTTP endpoint fronting many governed MCP integrations, with `<integration>.<tool>` names, `hub.*` discovery tools, policy, human confirmation and audit. Use when wiring a project to the hub, finding and calling hub-routed tools, interpreting tool_not_found / policy_violation / confirmation_required / rate_limited / integration_unavailable, or telling an operator exactly what to run.
compatibility: Any MCP client that speaks Streamable HTTP. Tools that require confirmation additionally need client-side elicitation support; without it those calls are refused by design.
---

# MCP Hub

## What this is

An MCP Hub is **one endpoint** — `http://<host>:8000/mcp`, transport Streamable
HTTP — that exposes the tools of every integration an operator has switched on.
Tool names are namespaced `<namespace>.<tool>`, the namespace usually being the
integration's id: `jira.getJiraIssue`, `github.search_code`,
`figma.get_design_context`.

Three facts that decide almost every question about it:

* **The hub proxies other people's servers.** Jira, GitHub, Figma, Notion,
  Brave, and the MCP reference servers are the *official* upstream servers. The
  hub adds routing, lifecycle, policy, credentials and audit. It never
  reimplements an upstream, and neither should you.
* **Every call passes gates.** auth → policy → rate limit → confirmation →
  credential injection → upstream → audit. A refusal is usually configuration,
  not a bug, and each gate has a distinct error code.
* **What you can see is not everything that exists.** Default exposure is
  `selective`: only tools the policy allowlist admits are listed. `discovery`
  mode lists nothing until you activate an integration.

## Workflow

Run this in order. Skipping step 1 is the single most common failure.

1. **Orient** — `hub.list_integrations` (`enabled_only` defaults to `true`;
   pass `false` to see what exists but is off). Gives ids, health, tool counts.
2. **Find** — `hub.search_tools` with a `query`; optionally `integration`,
   `limit` (max 50). Do not guess a tool name from memory of the upstream's
   docs; casing and naming differ per server.
3. **Inspect** — `hub.describe_tool` with the qualified name for the full input
   schema and risk classification (`READ` / `WRITE` / `DESTRUCTIVE` / `ADMIN`).
   Anything above `READ` may trigger a confirmation prompt.
4. **Call** — the qualified name, never the bare upstream name. Pass arguments
   that match the schema exactly.
5. **On failure** — read the error `code` first, then act per
   [references/errors.md](references/errors.md). Do not retry blindly.

`hub.health` tells an outage apart from a missing tool. `hub.refresh_tools`
re-reads definitions from an upstream (needs the `tools:refresh` scope).
`hub.activate_integration` exists only in `discovery` mode.

## Rules

These are the mistakes that actually get made. Each line is the rule, then why.

1. **Connect to `/mcp` and nowhere else.** No per-integration URL, no SSE
   fallback, no second port. Do not also register `jira`/`github`/`figma` as
   separate MCP servers in the same client — that duplicates the tool surface
   and bypasses the hub's policy and audit for half the calls.
2. **Never reimplement an upstream.** If a tool is missing, the integration is
   off, unhealthy, uncredentialed, or policy-hidden. Writing a direct API client
   "just for this call" defeats the credential and audit layer. Fix the hub.
3. **Always use the qualified name.** `getJiraIssue` → `tool_not_found`;
   `jira.getJiraIssue` works. The separator is a literal `.`.
4. **The tool prefix is the manifest's `namespace`, not always the integration
   id.** `brave-search` serves tools under `brave_search.`,
   `sequential-thinking` under `sequential_thinking.`. Pass the *id* to
   `hub.integration_status` and `hub.activate_integration`; call the
   *qualified name* search returned.
5. **An empty search result is not proof of absence.** `hub.search_tools` hides
   tools that policy disallows. `hub.describe_tool` still resolves them — so a
   tool that describes but cannot be found by search is a *policy* answer.
6. **Check health before concluding anything is broken.** `DISABLED`,
   `AUTH_REQUIRED` and `UPDATE_REQUIRED` are operator actions, not outages.
   `UNAVAILABLE` is the only genuine outage state.
7. **`integration_unavailable` degrades one integration, not the hub.** Keep
   using everything else; say which one is down.
8. **Confirmation prompts are expected, not errors.** A `DESTRUCTIVE` tool asks
   a human first. Your client must support elicitation and answer the two-round
   exchange; a client that cannot be asked gets `confirmation_required` and the
   call is refused. Never work around it by picking a non-confirming tool that
   does the same damage, and never edit `require_confirmation` yourself.
9. **Send the token in `Authorization: Bearer <token>`.** Never a query
   parameter — those land in access logs, proxy logs and browser history.
10. **Tokens expire (default 1 hour).** A session that worked and now returns
    `not_authenticated` usually needs a fresh token, not a fresh diagnosis.
11. **The token subject must be the caller's real identity.** Per-user
    credentials key off it. A shared subject collapses everyone into one
    identity, silently widening access and producing confusing `AUTH_REQUIRED`.
12. **Agent tokens get `tools:call` + `integrations:read`.** Add
    `tools:refresh` only if you call `hub.refresh_tools`. Never `admin`, never
    `integrations:write` or `secrets:write` for an agent.
13. **Never put a credential in a tool argument.** The hub injects credentials
    at call time. If a tool seems to want an API key, you have the wrong tool.
14. **Back off on `rate_limited`.** The message carries a retry hint and the
    REST surface sets `Retry-After`. Retrying immediately just burns the quota
    of whoever shares that limit.
15. **Do not retry `policy_violation`, `not_authorized`, or `tool_not_found`.**
    They are deterministic. Report them with the exact code and message.
16. **There is no `hub.execute_shell` or `hub.execute_python`.** The hub's own
    tool surface is read-only apart from refresh and activation. Stop looking.
17. **Configuration is the operator's, not yours.** `config/integrations.yaml`
    (which are on), `config/manifests/<id>.yaml` (how to reach each), and
    `config/integrations.lock.yaml` (what is installed — written only by the
    update manager, never by hand). Surface the exact command; do not run
    installs or secret writes on your own initiative.
18. **After enabling or updating an integration, the tool list changes.** Call
    `hub.refresh_tools` or reconnect before assuming the new tools are absent.

## Triage

| Symptom | First check | Reference |
|---|---|---|
| Only `hub.*` tools visible | Exposure mode; `discovery` needs `hub.activate_integration` | [tool-access.md](references/tool-access.md) |
| Tool missing from search | Health, then the policy allowlist | [tool-access.md](references/tool-access.md) |
| `tool_not_found` | Qualified name and exact casing | [tool-access.md](references/tool-access.md) |
| `not_authenticated` / `not_authorized` | Token present, unexpired, right scopes | [connecting.md](references/connecting.md) |
| `confirmation_required` | Client elicitation support | [confirmation.md](references/confirmation.md) |
| `policy_violation` | Deny list, allowlist, risk floor | [errors.md](references/errors.md) |
| `rate_limited` | Retry hint; per-principal vs per-integration scope | [errors.md](references/errors.md) |
| `integration_unavailable` | `hub.health`, then the operator | [errors.md](references/errors.md) |
| `AUTH_REQUIRED` after a secret was stored | Per-user credential stored for the *calling* subject | [operating.md](references/operating.md) |
| No tools at all | Client is not reaching the hub | [connecting.md](references/connecting.md) |

## References

* [references/connecting.md](references/connecting.md) — endpoint, tokens,
  scopes, and working client config for Claude Code, Claude Desktop, Cursor,
  VS Code and a custom Python client.
* [references/tool-access.md](references/tool-access.md) — namespacing, the
  three exposure modes, the `hub.*` contract with scopes and arguments, and why
  a tool can be invisible.
* [references/errors.md](references/errors.md) — every error code with cause,
  fix and retry policy; health statuses; risk levels.
* [references/confirmation.md](references/confirmation.md) — the two-round
  elicitation exchange, what a client must implement, what denies a call.
* [references/operating.md](references/operating.md) — the operator commands to
  hand over: enable, credentials, policy, update, rollback, adding an upstream.

## Installing this skill in another project

Copy the directory into the consuming project (or a user-level skills
directory), then restart the client so it is picked up:

```bash
cp -r <mcp-hub-repo>/.claude/skills/mcp-hub  <other-project>/.claude/skills/
# or, for every project on this machine:
cp -r <mcp-hub-repo>/.claude/skills/mcp-hub  ~/.claude/skills/
```

It is plain Markdown with no dependency on this repository at runtime — the
consuming project only needs the hub's URL and a token. Keep the copy in sync
when the hub's tool surface or policy defaults change.
