# Operator actions

Everything here needs `integrations:write`, `secrets:write` or `admin` — scopes
an agent token should not hold. **The right move is to hand the operator the
exact command, not to acquire the scope.** Run these yourself only when you are
working in the hub repository as the operator and have been asked to.

## Diagnose first

```bash
mcp-hub doctor              # environment, config and every integration in one pass
mcp-hub list                # what exists, enabled, healthy
mcp-hub health              # non-zero exit if an enabled integration is UNAVAILABLE
mcp-hub show <id>           # source, lock, health, tools and policy for one
mcp-hub tools <id>          # what it actually discovered
mcp-hub status              # what the hub is doing right now
mcp-hub logs                # audit trail: attempted and decided
mcp-hub logs <id> --process-log   # what the upstream server itself printed
```

The audit trail and a server's process log answer different questions, which is
why they are separate flags rather than one stream.

## Switch an integration on

```bash
mcp-hub secrets set GITHUB_TOKEN     # if it needs a credential
mcp-hub enable github
mcp-hub tools github                 # confirm its tools arrived
```

Then, from the agent session, `hub.refresh_tools` or reconnect — an already
connected client is holding a stale tool list.

## Credentials

There is deliberately **no `--value` flag**: an argument lands in shell history
and the process table, readable by any user on the host.

```bash
mcp-hub secrets set ATLASSIAN_TOKEN                        # hidden prompt
mcp-hub secrets set GITHUB_TOKEN --from-env GH_PAT         # from the environment
echo "$TOKEN" | mcp-hub secrets set BRAVE_API_KEY --stdin  # from a pipe

mcp-hub secrets list                                       # names only, never values
mcp-hub secrets delete FIGMA_TOKEN
```

**Shared vs per-user.** The manifest declares which. A shared key
(`BRAVE_API_KEY`) is one deployment-wide credential billed to the operator. A
per-user credential (`ATLASSIAN_TOKEN`) exists because the upstream enforces the
calling user's own permissions, and a shared one would hand everybody the access
of whoever stored it.

```bash
mcp-hub secrets set ATLASSIAN_TOKEN --principal alice@example.com
mcp-hub secrets set BRAVE_API_KEY --integration brave-search   # sole ownership: removal cleans it up
```

This is the usual cause of "still `AUTH_REQUIRED` after I stored the secret":
a per-user credential must be stored for *the subject the caller's token uses*.

Stored credentials are encrypted at rest, injected at call time, never returned
through the API, and never logged. `secrets:write` does not grant reading one
back — nothing does.

## Policy

`config/policies.yaml` decides who may call what. Evaluation order is
`deny` → `allow` → risk thresholds → argument restrictions → rate limit, and a
non-empty `allow` list is a **closed allowlist**.

```yaml
policies:
  jira:
    allow: [searchJiraIssuesUsingJql, getJiraIssue, createJiraIssue, editJiraIssue]
    require_confirmation: [createJiraIssue, editJiraIssue]
    deny: [deleteJiraIssue]
    rate_limit: { requests: 60, window_seconds: 60, scope: principal }
```

`scope: principal` counts per caller; `scope: integration` counts across the
whole hub — correct for a shared metered key. The global limit applies in
addition; whichever binds first wins.

A new integration with no rule inherits `default`, and under `selective`
exposure that means **its tools stay hidden**. Add the policy rule at the same
time as the manifest.

## Apply configuration changes

```bash
mcp-hub sync         # reload config, reconcile actual state, rediscover tools
mcp-hub reconcile    # drive actual state toward configuration only
mcp-hub refresh      # re-read tool definitions from upstreams only
```

Against a running hub: `POST /api/admin/reload`.

## Add an upstream

Three routes, in increasing order of effort. **None of them is "write a client
for the vendor's API"** — the hub points at somebody else's MCP server.

1. **Already shipped** — `mcp-hub list` shows every manifest that ships with the
   hub; `mcp-hub enable <id>` turns one on.
2. **From the MCP Registry** — `mcp-hub registry search <term>`, then
   `mcp-hub registry install <name>`. The hub writes the manifest.
3. **By hand** — drop `config/manifests/<id>.yaml`, which registers the
   integration; `mcp-hub enable <id>` turns it on.

Manifest fields that matter most:

| Field | Why |
|---|---|
| `namespace` | The tool prefix agents see. Defaults to `id` with `-` mapped to `_`. |
| `source.type` | `remote` proxies and installs nothing; `npm`/`python`/`git`/`docker` build and run locally. |
| `source.version` / commit / digest | Pin it. Omitted means "resolve latest at install". |
| `trust` | `remote_official` / `local_official` / `community`. Community code needs an explicit install confirmation. |
| `risk_level` / `tool_risk` | Feeds the risk thresholds. Mark deletions `DESTRUCTIVE` and they need a human by default. |
| `runtime.isolation` | `subprocess` or `container`. |
| `runtime.network` | `none` for a server that should never reach the internet. |
| `runtime.allowed_env` | An allowlist — the hub's own environment and secrets are not inherited. |
| `auth.secret.per_user` | Set whenever the upstream enforces the calling user's permissions. |
| `update_policy` | `manual`, or automatic within a version constraint. |

## Update and roll back

Nothing updates in place: a version stages into its own directory, proves it can
start and list tools while the current one keeps serving, and is promoted by an
atomic rename. Failures roll back automatically unless told not to.

```bash
mcp-hub update --all --dry-run        # always start here
mcp-hub update jira
mcp-hub update --all --exclude jira
mcp-hub rollback <id> --list
mcp-hub rollback <id>
```

Useful flags: `--dry-run` (plan only), `--yes` (skip the prompt), `--force`
(update even at the resolved version), `--no-rollback-on-failure` (leave the
failure in place for inspection), `--parallel` (off by default — a serial run is
easier to read when it breaks).

Over the API: `POST /api/update`, `POST /api/update/all`,
`POST /api/integrations/{name}/update` return a job id to poll at
`GET /api/jobs/{job_id}`.

## The three-file contract

| File | Answers | Written by |
|---|---|---|
| `config/manifests/<id>.yaml` | *How* do we reach this server? | You, or `registry install` |
| `config/integrations.yaml` | *Which* ones do we want on? | You, or `enable`/`disable`/`install`/`remove` |
| `config/integrations.lock.yaml` | *What* is actually installed? | **The update manager only — never by hand** |

Hand-editing the lock file makes the hub's record of reality disagree with
reality, which is exactly the failure the file exists to prevent.

## REST equivalents

The CLI, the REST API and the MCP endpoint build the same runtime and call the
same services — `mcp-hub enable github` and
`POST /api/integrations/github/enable` are one code path, so there is no drift
to reason about.

| Action | Endpoint | Scope |
|---|---|---|
| List / describe integrations | `GET /api/integrations`, `GET /api/integrations/{name}` | `integrations:read` |
| Enable / disable | `POST /api/integrations/{name}/enable` \| `/disable` | `integrations:write` |
| Install / remove | `POST /api/integrations/{name}/install`, `DELETE /api/integrations/{name}` | `integrations:write` |
| Update / rollback | `POST /api/integrations/{name}/update` \| `/rollback`, `GET /api/integrations/{name}/rollback-points` | `integrations:write` |
| Rediscover | `POST /api/integrations/{name}/refresh`, `POST /api/refresh` | `integrations:write` |
| Jobs | `GET /api/jobs`, `GET /api/jobs/{job_id}` | `integrations:read` |
| Audit | `GET /api/audit` | `audit:read` |
| Secrets | `PUT`/`DELETE /api/admin/secrets/{name}`, `GET /api/admin/secrets` | `secrets:write` |
| Config / reload / doctor | `GET /api/admin/config`, `POST /api/admin/reload`, `GET /api/admin/doctor` | `admin` |
| Registry | `GET /api/registry/search`, `POST /api/registry/install` | `integrations:read` / `write` |

## Production reminders

`MCP_HUB_ENV=production` refuses to start without an auth secret, PostgreSQL,
Redis and `MCP_HUB_AUTH_REQUIRED=true` — a misconfigured hub should fail at
startup, not at the first request. With more than one replica Redis is
mandatory: without it locks and rate limits are process-local, and each replica
would grant the full quota.
