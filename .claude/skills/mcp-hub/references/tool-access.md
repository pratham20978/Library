# Finding and calling tools

## Namespacing

Every integration tool is exposed as `<namespace>.<tool>`, separator a literal
`.`:

```
jira.getJiraIssue          github.search_code        figma.get_design_context
fetch.fetch                time.get_current_time     filesystem.read_file
```

Those are the *shape* of a name, not a catalogue: the real list is whatever the
pinned upstream version exposed at discovery time, which is why you read it from
the hub rather than from this file.

Namespacing is what lets two upstreams that both export `search` coexist. The
tool part is the upstream's own name, verbatim — casing, underscores and camel
case follow whoever wrote that server, not a hub convention. Never normalise it,
never guess it from the vendor's documentation; read it from `hub.search_tools`
or `hub.describe_tool`.

`hub.*` is reserved for the hub's own meta-tools and is never an integration.

### The namespace is not always the integration id

A manifest sets `namespace`, defaulting to the id with `-` mapped to `_`. So the
id you pass to `hub.integration_status`, `hub.search_tools {integration: ...}`
and `hub.activate_integration` is **not** always the prefix in a tool name:

| Integration id | Tool prefix |
|---|---|
| `brave-search` | `brave_search.` |
| `sequential-thinking` | `sequential_thinking.` |
| `jira`, `github`, `figma`, `fetch`, `time`, `git`, `memory`, `filesystem`, `notion` | unchanged |

`hub.search_tools` results carry both — `integration` is the id, `qualified_name`
is what you call. Use each where it belongs; swapping them yields
`integration_not_found` or `tool_not_found` for something that plainly exists.

## Exposure modes

Set hub-wide (`tool_exposure_mode` in `config/integrations.yaml`, or
`MCP_HUB_TOOL_EXPOSURE_MODE`). It decides what your session lists:

| Mode | What you see | Implication |
|---|---|---|
| `full` | Every tool of every healthy integration. | Hundreds of schemas; model tool selection degrades before any protocol limit is hit. |
| `selective` *(default)* | Only what the policy allowlist admits. | A tool can exist, be healthy, and still not be listed. |
| `discovery` | Only the `hub.*` tools. | You must call `hub.activate_integration` before that integration's tools appear. |

Seeing only `hub.*` tools is *expected* in discovery mode. In any other mode it
means nothing is enabled, healthy, or allowed — check in that order.

## The `hub.*` tools

Present in every mode, so a session can always orient itself.

| Tool | Arguments | Scope needed | Notes |
|---|---|---|---|
| `hub.list_integrations` | `enabled_only` (bool, default **true**) | — | Call first. Pass `false` to see disabled ones too. |
| `hub.integration_status` | `integration` (required) | — | Health, version, credential state, tool count for one. |
| `hub.search_tools` | `query` (required), `integration`, `limit` (max 50) | — | Name matches rank above description matches. |
| `hub.describe_tool` | `tool` (qualified name, required) | — | Full input schema plus risk classification. |
| `hub.health` | none | — | Every integration's health at once. |
| `hub.refresh_tools` | `integration` (omit for all) | `tools:refresh` | Reconnects upstreams; changes the session tool list. |
| `hub.activate_integration` | `integration` (required) | — | Discovery mode only; absent otherwise. |

There is deliberately no `hub.execute_shell`, no `hub.execute_python`, and no
tool that runs arbitrary code. Apart from refresh and activation, the hub's own
surface is read-only.

## The loop that works

```
hub.list_integrations            → which ids exist, and are they HEALTHY?
hub.search_tools {query: "..."}  → the qualified name, ranked
hub.describe_tool {tool: "..."}  → schema + risk, before you build arguments
<integration>.<tool> {...}       → the call
```

Skipping straight to a remembered tool name is the most common cause of
`tool_not_found`. Skipping `describe_tool` is the most common cause of an
argument-validation failure on a `WRITE` tool that then needs a confirmation
round trip to retry.

## Why a tool can be invisible

`hub.search_tools` only returns tools that are **enabled and policy-allowed**.
`hub.describe_tool` resolves anything in the registry. That asymmetry is a
diagnostic:

| search | describe | Meaning |
|---|---|---|
| miss | hit | Policy is hiding it — allowlist, deny list, or risk floor. |
| miss | miss | Not registered: integration disabled, unhealthy, or never installed. |
| hit | hit | Available; any failure now is at call time. |

Then confirm with `hub.integration_status` / `hub.health`:

* `DISABLED` — operator runs `mcp-hub enable <id>`.
* `AUTH_REQUIRED` — operator stores the credential; for a per-user credential it
  must be stored for *your* subject.
* `UPDATE_REQUIRED` — operator runs `mcp-hub install <id>` or `update <id>`.
* `UNAVAILABLE` — a real outage of that upstream. Everything else still works.
* `DEGRADED` — serving, but slow or partial. Retry sparingly; report it.

## Argument discipline

* Build arguments from the schema `hub.describe_tool` returns, not from an
  upstream's public docs, which may describe a different version than the pinned
  one the hub installed.
* Never pass an API key, token or password as an argument. Credentials are
  injected by the hub at call time and are never part of a tool's schema. A tool
  that appears to want one is the wrong tool.
* Argument values are redacted before they appear in confirmation prompts, logs
  and audit records — but do not rely on that to launder something you should
  not have sent.
* Some policies restrict *arguments*, not just tools (an allowed tool with a
  denied argument shape). That surfaces as `policy_violation` naming the rule.

## After the tool list changes

Enabling, updating, or activating an integration changes what your session can
call. The hub emits `tools/list_changed`; clients that ignore it keep a stale
list. If a tool you were told exists is missing, call `hub.refresh_tools`
(with `tools:refresh`) or reconnect before reporting it as absent.
