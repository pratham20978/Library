# MCP Hub

**One MCP endpoint. Many governed integrations.**

An agent connects to a single URL — `http://localhost:8000/mcp` — and sees the
tools of every integration you have switched on, namespaced as
`<integration>.<tool>`: `jira.getJiraIssue`, `github.search_code`,
`figma.get_design_context`. Behind that endpoint the hub installs, updates,
sandboxes, authorizes, rate-limits, and audits each upstream MCP server.

**The hub does not reimplement anything.** Jira, GitHub, Figma, Notion, Brave
Search and the reference servers are the *official* MCP servers, pulled from
their own repositories, registries and remote endpoints. This repository is the
hub: the routing, lifecycle, policy and credential layer that sits in front of
them. Every integration in [config/manifests/](config/manifests/) is a pointer
to somebody else's server, not a copy of it.

```
  Claude · Cursor · VS Code · your own client
                  │
                  │  one endpoint, Streamable HTTP
                  ▼
  ┌──────────────────────────── MCP HUB ────────────────────────────┐
  │  /mcp        auth → policy → confirmation → routing → audit     │
  │  /api/...    install · update · rollback · health · secrets     │
  │                                                                 │
  │  tool registry (namespaced) · session pool · job queue          │
  └────────┬───────────────────────┬───────────────────────┬────────┘
           │                       │                       │
     remote HTTP              subprocess               container
           │                       │                       │
   Atlassian Rovo (jira)   fetch, time (python)     any manifest with
   Figma, Notion           filesystem, memory (npm)  runtime.isolation:
                           github, brave (git)        container
```

### What ships

Eleven manifests, all pointing at somebody else's server:

| Integration                                             | Source | Runs                                                            |
| ------------------------------------------------------- | ------ | --------------------------------------------------------------- |
| `jira` · `figma` · `notion`                     | remote | Nothing locally — the hub proxies the vendor's own MCP server. |
| `github` · `brave-search`                          | git    | Built from the vendor's repository, sandboxed.                  |
| `fetch` · `time` · `git`                        | python | Reference servers, pinned to exact releases.                    |
| `filesystem` · `memory` · `sequential-thinking` | npm    | Reference servers, pinned to exact releases.                    |

---

## Contents

1. [Architecture](#1-architecture)
2. [Installation](#2-installation)
3. [Development](#3-development)
4. [Docker](#4-docker)
5. [Configuration](#5-configuration)
6. [Authentication](#6-authentication)
7. [Adding integrations](#7-adding-integrations)
8. [Updating integrations](#8-updating-integrations)
9. [Removing integrations](#9-removing-integrations)
10. [Rollback](#10-rollback)
11. [Registry](#11-registry)
12. [Security](#12-security)
13. [MCP client configuration](#13-mcp-client-configuration)
14. [Troubleshooting](#14-troubleshooting)
15. [Production deployment](#15-production-deployment)

---

## 1. Architecture

### The one-endpoint rule

The hub exposes exactly one MCP endpoint over **Streamable HTTP**. There is no
per-integration URL, no SSE fallback, no second port. A client configures one
server and gains every integration; enabling a new upstream changes what that
client sees without the client changing anything.

Alongside it, on the same process and the same port, sits a REST management API
for operators. Both surfaces pass through one authentication middleware, so a
bearer token means the same thing to both.

| Path         | Purpose                                                                                 |
| ------------ | --------------------------------------------------------------------------------------- |
| `/mcp`     | The MCP endpoint. Agents connect here and nowhere else.                                 |
| `/api/...` | Management REST API (integrations, updates, jobs, audit, secrets, registry).            |
| `/health`  | Liveness — the process is up. Deliberately checks nothing else.                        |
| `/ready`   | Readiness — the database is reachable. Unhealthy integrations are reported, not fatal. |
| `/metrics` | Prometheus metrics.                                                                     |
| `/docs`    | OpenAPI documentation for the management API.                                           |

### Request path

A tool call travels through a fixed sequence, and every stage can stop it:

```
agent → /mcp → auth middleware → tool router → policy engine → rate limiter
      → confirmation (elicitation) → credential resolution → session pool
      → upstream MCP server → response → audit log → agent
```

* **Auth middleware** resolves the bearer token to a principal. No token means
  the anonymous principal — not an automatic rejection, because policy is a
  finer instrument than a blanket 401 and health probes need to work.
* **Tool router** maps `jira.getJiraIssue` to the `jira` integration and the
  upstream's own `getJiraIssue`. Namespacing is what makes two servers that both
  export `search` coexist.
* **Policy engine** evaluates, in order: `deny` → `allow` → risk thresholds →
  argument restrictions → rate limit. A non-empty `allow` list is a closed
  allowlist.
* **Confirmation** uses MCP elicitation to ask a human before anything
  destructive. A client that cannot be asked is **refused**, never silently
  allowed.
* **Credential resolution** injects the right token for *this* caller —
  per-user where the upstream enforces per-user permissions.
* **Error isolation** means one broken upstream degrades to `UNAVAILABLE` and
  the hub keeps serving everything else.

### Repository layout

```
app/
  server/       runtime composition root, MCP server, ASGI wiring
  gateway/      tool registry, discovery, routing, upstream session pool
  integrations/ manifests → installed servers: adapters, lifecycle, updater,
                backup, sandbox, network guard
  policy/       authorization, risk classification, confirmation, rate limits
  secrets/      credential storage and resolution (never readable back out)
  auth/         tokens, scopes, request-context middleware
  audit/        append-only audit trail
  api/          the REST management API
  cli/          the `mcp-hub` command line
  config/       settings, YAML models, config store
  database/     SQLAlchemy models, sessions, Alembic migrations
config/
  integrations.yaml     which integrations you want (desired state)
  policies.yaml         who may call what
  manifests/<id>.yaml   how to reach each upstream (one file per integration)
runtime/                mutable state: installed servers, caches, backups, logs
scripts/                thin wrappers over the CLI for operators
```

### The three-file contract

Configuration is deliberately split by *who owns it*:

| File                              | Answers                          | Written by                                           |
| --------------------------------- | -------------------------------- | ---------------------------------------------------- |
| `config/manifests/<id>.yaml`    | *How* do we reach this server? | You, or`registry install`                          |
| `config/integrations.yaml`      | *Which* ones do we want on?    | You, or`enable`/`disable`/`install`/`remove` |
| `config/integrations.lock.yaml` | *What* is actually installed?  | The update manager only — never by hand             |

Dropping a manifest into `config/manifests/` registers an integration; adding it
to `integrations.yaml` turns it on.

### Everything runs through one service

The MCP endpoint, the REST API and the CLI all build the same `HubRuntime` and
call the same `LifecycleService` and `UpdateManager`. There is no second
implementation of "enable an integration" for the command line to drift away
from. `mcp-hub enable github` and `POST /api/integrations/github/enable` are the
same code path.

---

## 2. Installation

### Requirements

|                                           |                                                                   |
| ----------------------------------------- | ----------------------------------------------------------------- |
| **Python**                          | 3.12 or newer (3.14 recommended)                                  |
| **[uv](https://docs.astral.sh/uv/)** | environment and lockfile manager                                  |
| **git**                             | needed only for git-sourced integrations                          |
| **node / npm**                      | needed only for npm-sourced integrations                          |
| **Docker**                          | needed only for container-isolated or docker-sourced integrations |
| **PostgreSQL + Redis**              | production only; development uses SQLite and in-process locks     |

`mcp-hub doctor` tells you which of these you actually need for *your* enabled
set, so a hub that only proxies remote services needs none of git, npm or
Docker.

### Install

```bash
git clone <this-repository> mcp-hub
cd mcp-hub

make install              # uv sync --all-extras
cp .env.example .env      # then edit; see §5
make migrate              # create the database schema
```

### First run

```bash
make dev                  # http://localhost:8000 — MCP at /mcp
```

Then, in another shell:

```bash
curl -s http://localhost:8000/health
# {"status":"ok","version":"0.1.0"}

mcp-hub list              # every configured integration and its state
mcp-hub doctor            # environment + configuration + per-integration checks
```

Out of the box, the three remote official services (Jira, Figma, Notion) are
enabled but report `AUTH_REQUIRED` until you store a credential; `fetch` and
`time` are enabled but report `UPDATE_REQUIRED` until installed; everything else
ships disabled. Nothing runs third-party code on your machine until you
explicitly install it.

```bash
mcp-hub install fetch time          # installs the pinned reference servers
mcp-hub secrets set ATLASSIAN_TOKEN # prompts, hidden; see §6
mcp-hub health                      # exits non-zero if anything enabled is down
```

---

## 3. Development

Everything CI runs is behind `make check`.

```bash
make help          # list every target
make dev           # run with auto-reload
make test          # pytest
make test-all      # pytest with coverage (HTML report in htmlcov/)
make lint          # ruff check + ruff format --check
make format        # apply formatting and safe fixes
make typecheck     # mypy --strict over app, tests and scripts
make check         # lint + typecheck + test
make migration m="add a column"   # autogenerate an Alembic revision
make migrate       # apply migrations
make clean         # remove caches; never touches runtime/ or .env
```

### Standards this codebase holds itself to

* **`mypy --strict`** over `app/`, `tests/` *and* `scripts/`. Test fixtures are
  Protocol-typed, because a fixture returning a bare callable is `Any` and would
  silently disable checking inside every test that uses it.
* **ruff** with bugbear, bandit, async-correctness and blind-except rules on. A
  bare `except Exception` has to justify itself in a comment.
* **Pydantic models with `extra="forbid"`** for every configuration file, so a
  typo in YAML is an error at load time and not a silently ignored key.
* **No business logic in the CLI or the API.** Both are thin adapters over the
  service layer.

### The test suite

```
tests/unit/          policy decisions, risk classification, error reporting
tests/integration/   the arch §40 contract, the update cycle, the CLI
tests/security/      credential isolation, the confirmation gate, SSRF, redaction
tests/e2e/           agent → hub → upstream, over real Streamable HTTP
tests/fixtures/      mock upstream MCP servers and a git repo holding a real one
```

Nothing important is stubbed. The end-to-end tests run real MCP servers over
real transports; the update tests clone a real git repository and launch a real
subprocess, because staging, promotion-by-rename and environment filtering are
exactly what a stub would skip.

Markers let you run a slice: `pytest -m security`, `-m integration`, `-m e2e`,
and `-m "not slow"`.

### Running the hub against a scratch directory

Every command accepts `--config-dir` and `--runtime-dir`, so you can experiment
without touching your working configuration:

```bash
mcp-hub --config-dir /tmp/hub-config --runtime-dir /tmp/hub-runtime list
```

Add `--json` to any command for machine-readable output, and `-v` / `-vv` to see
the hub's own logs.

### The MCP Inspector

```bash
make inspector
```

Connect it to `http://localhost:8000/mcp` over **Streamable HTTP**. If the hub
requires authentication, add an `Authorization: Bearer <token>` header from
`mcp-hub token issue`.

---

## 4. Docker

```bash
cp .env.example .env      # set MCP_HUB_AUTH_SECRET at minimum
make docker-up            # hub + postgres + redis
make docker-logs
make docker-down
```

`docker compose up` brings up three services: the hub, PostgreSQL 17 and Redis
7. The hub waits for both to report healthy, applies migrations on start
(`MCP_HUB_AUTO_MIGRATE=true`), and serves on `${MCP_HUB_PORT:-8000}`.

The image is multi-stage: dependencies resolve from `uv.lock` in a builder
stage, and the runtime stage carries only the virtualenv plus the tools
integrations need — `git`, `node`/`npx` and `uv`/`uvx` — so npm- and
python-sourced servers install inside the container. It runs as an unprivileged
user (uid 10001) with `no-new-privileges`.

`config/` and `runtime/` are bind-mounted, so the container edits the same files
you do. The Makefile exports `HOST_UID`/`HOST_GID` so those writes land with your
ownership rather than root's.

Two deliberate omissions, both documented in `docker-compose.yml`:

* **No `worker` service.** The job queue is in-process asyncio; a second
  container would start an idle queue and process nothing. A worker needs a
  Redis-backed queue first.
* **No Docker socket mount.** Mounting `/var/run/docker.sock` is equivalent to
  root on the host. Container-isolated integrations therefore need an explicit
  opt-in, not a default.

To use the MCP Inspector against the composed hub:

```bash
docker compose --profile tools up inspector
```

---

## 5. Configuration

Two layers, and they answer different questions.

### Environment — how this deployment is wired

`.env` (never committed) or real environment variables, all prefixed
`MCP_HUB_`. The names map one-to-one onto the fields of
[app/config/settings.py](app/config/settings.py), which is the authoritative
list. `.env.example` documents every one of them.

The ones that matter most:

```bash
MCP_HUB_ENV=development                     # development | staging | production | test
MCP_HUB_HOST=127.0.0.1
MCP_HUB_PORT=8000
MCP_HUB_MCP_PATH=/mcp                       # the single MCP endpoint

MCP_HUB_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/mcp_hub
MCP_HUB_REDIS_URL=redis://localhost:6379/0

MCP_HUB_AUTH_SECRET=<32+ random bytes>      # signs hub-issued tokens
MCP_HUB_AUTH_REQUIRED=true
MCP_HUB_SECRET_ENCRYPTION_KEY=<fernet key>  # encrypts stored credentials

MCP_HUB_TOOL_EXPOSURE_MODE=selective        # full | selective | discovery
```

Generate the two secrets with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"                       # AUTH_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # ENCRYPTION_KEY
```

`staging` and `production` refuse to start on development defaults: they require
an auth secret, PostgreSQL, Redis and authentication switched on. That check is
in the settings loader, so a misconfigured production hub fails at startup rather
than at the first request.

### YAML — what this hub offers and who may use it

**`config/integrations.yaml`** — desired state. Which integrations exist and
whether each is on:

```yaml
version: 1
tool_exposure_mode: selective
integrations:
  jira:
    enabled: true
  github:
    enabled: false
```

**`config/policies.yaml`** — authorization. Which tools each integration may
expose, which need a human, which are refused outright:

```yaml
version: 1
require_authentication: true

global_rate_limit:
  requests: 600
  window_seconds: 60
  scope: principal

default:
  confirm_risk_at_or_above: DESTRUCTIVE   # backstop for tools nobody reviewed yet
  rate_limit: { requests: 120, window_seconds: 60, scope: principal }

policies:
  jira:
    allow: [searchJiraIssuesUsingJql, getJiraIssue, createJiraIssue, editJiraIssue]
    require_confirmation: [createJiraIssue, editJiraIssue]
    deny: [deleteJiraIssue]
    rate_limit: { requests: 60, window_seconds: 60, scope: principal }
```

**`config/manifests/<id>.yaml`** — one file per integration, describing where the
upstream server comes from. See §7.

### Tool exposure modes

A hub with everything enabled can present several hundred tool schemas, which
degrades a model's tool selection long before it breaks any protocol limit.

| Mode                        | What an agent sees                                                                                          |
| --------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `full`                    | Every tool of every healthy integration.                                                                    |
| `selective` *(default)* | Only what the policy allowlist admits.                                                                      |
| `discovery`               | Just the`hub.*` tools; the agent searches, then calls `hub.activate_integration` to load what it needs. |

### The `hub.*` meta-tools

Always present, whatever the mode, so an agent can orient itself:

| Tool                         | Use                                                                  |
| ---------------------------- | -------------------------------------------------------------------- |
| `hub.list_integrations`    | What is available, with health and tool counts. Call this first.     |
| `hub.integration_status`   | Health, version, credentials and tool count for one integration.     |
| `hub.search_tools`         | Find the right tool without loading every schema.                    |
| `hub.describe_tool`        | One tool's full schema and risk classification.                      |
| `hub.health`               | Tell an outage apart from a missing tool.                            |
| `hub.refresh_tools`        | Re-read definitions from an upstream.                                |
| `hub.activate_integration` | Discovery mode only: load one integration's tools into this session. |

There is deliberately **no** `hub.execute_shell`, `hub.execute_python`, or
anything else that runs arbitrary code. The hub's own tool surface is read-only
apart from refresh and activation.

### Applying configuration changes

```bash
mcp-hub sync         # reload config, reconcile actual state, rediscover tools
mcp-hub reconcile    # drive actual state toward the configuration only
mcp-hub refresh      # re-read tool definitions from upstreams only
```

Or, against a running hub: `POST /api/admin/reload`.

---

## 6. Authentication

### Two sides, and they are different

**Inbound** — who is calling the hub. Bearer tokens the hub itself issues and
verifies, presented as `Authorization: Bearer <token>`. That header is the only
accepted channel: query parameters land in access logs, proxy logs and browser
history.

**Outbound** — how the hub authenticates to an upstream. Stored credentials,
encrypted at rest, injected at call time, never returned through the API and
never written to a log.

### Issuing a token

```bash
mcp-hub token issue alice@example.com \
  --scope tools:call --scope integrations:read \
  --ttl 3600
```

```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "subject": "alice@example.com",
  "scopes": ["tools:call", "integrations:read"],
  "expires_at": "2026-08-15T12:43:10+00:00"
}
```

The token is printed once and never stored. The **subject must be the caller's
real identity**, not a label — per-user credentials key off it, so a shared
subject collapses every caller into one identity and quietly widens everyone's
access.

### Scopes

| Scope                  | Grants                                                      |
| ---------------------- | ----------------------------------------------------------- |
| `tools:call`         | Invoke integration tools through`/mcp`.                   |
| `integrations:read`  | List integrations, health, tool metadata.                   |
| `tools:refresh`      | Trigger rediscovery (reconnects to upstreams, so not free). |
| `integrations:write` | Install, update, enable, disable, remove, roll back.        |
| `secrets:write`      | Store or delete credentials. Never grants reading one back. |
| `audit:read`         | Read the audit trail.                                       |
| `admin`              | Everything. For operators; never hand it to an agent.       |

`--scope` defaults to the agent set — `tools:call` and `integrations:read`: call
tools, read status, change nothing. Administrative operations are protected
separately from tool access precisely so a token good enough to read a Jira issue
cannot uninstall an integration.

### Storing upstream credentials

There is deliberately **no `--value` flag**. A credential passed as an argument
lands in shell history and in the process table, where any user on the host can
read it.

```bash
mcp-hub secrets set ATLASSIAN_TOKEN                       # hidden prompt
mcp-hub secrets set GITHUB_TOKEN --from-env GH_PAT        # from the environment
echo "$TOKEN" | mcp-hub secrets set BRAVE_API_KEY --stdin # from a pipe

mcp-hub secrets list                                      # names only, never values
mcp-hub secrets delete FIGMA_TOKEN
```

**Shared vs per-user.** A manifest declares which it is. `BRAVE_API_KEY` is
shared: one deployment-wide key, billed to you. `ATLASSIAN_TOKEN` is per-user,
because Atlassian's server enforces the calling user's own permissions and a
shared token would hand everyone the access of whoever holds it.

```bash
# Store a per-user credential on someone's behalf:
mcp-hub secrets set ATLASSIAN_TOKEN --principal alice@example.com

# Record sole ownership, so removing the integration cleans the credential up:
mcp-hub secrets set BRAVE_API_KEY --integration brave-search
```

### Turning authentication off

`MCP_HUB_AUTH_REQUIRED=false` (or `require_authentication: false` in
`policies.yaml`) resolves every caller to the anonymous principal, which disables
per-user credentials entirely. That is reasonable for a closed local development
loop and nowhere else — and `production` refuses to start with it off.

---

## 7. Adding integrations

Three routes, in increasing order of how much you have to write.

### a. Switch on something already shipped

Eleven integrations ship as manifests. `mcp-hub list` shows them all.

```bash
mcp-hub secrets set GITHUB_TOKEN
mcp-hub enable github
mcp-hub tools github        # confirm its tools arrived
```

### b. Install from the MCP Registry

See §11 — `mcp-hub registry search` then `mcp-hub registry install`. The hub
writes the manifest for you.

### c. Write a manifest by hand

Drop a file into `config/manifests/<id>.yaml`. That registers the integration;
`mcp-hub enable <id>` turns it on.

**A remote service** — the hub proxies it and installs nothing:

```yaml
id: jira
name: Jira / Atlassian
description: Jira, Confluence and Compass via Atlassian's official remote MCP server.
namespace: jira
homepage: https://github.com/atlassian/atlassian-mcp-server

source:
  type: remote
  endpoint: https://mcp.atlassian.com/v1/mcp
  transport: streamable-http
  verify_tls: true
  request_timeout_seconds: 90

trust: remote_official

auth:
  type: oauth2
  secret:
    name: ATLASSIAN_TOKEN
    per_user: true          # the upstream enforces per-user permissions
    required: false

risk_level: WRITE
tool_risk:
  getJiraIssue: READ
  createJiraIssue: WRITE
  deleteJiraIssue: DESTRUCTIVE

update_policy: manual
```

**A local server** — built and run here, sandboxed:

```yaml
id: my-server
name: My Server
namespace: my_server

source:
  type: npm                 # npm | python | git | docker | remote | builtin
  package: "@example/my-mcp-server"
  version: "1.4.2"          # pin it; omitted means "resolve latest at install"

trust: community            # community code needs an explicit confirmation to install

runtime:
  isolation: subprocess     # or: container
  network: outbound         # none | outbound | host
  memory_limit_mb: 512
  run_as_non_root: true
  allowed_env: [MY_SERVER_TOKEN]   # nothing else from the hub's environment leaks in

auth:
  type: bearer
  secret:
    name: MY_SERVER_TOKEN
    per_user: false

risk_level: READ
update_policy: manual
```

Then:

```bash
mcp-hub install my-server      # confirms first for community-tier code
mcp-hub enable my-server
mcp-hub tools my-server
```

### Manifest fields worth understanding

| Field                          | Why it matters                                                                                                   |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `namespace`                  | The tool prefix agents see. Defaults to`id` with `-` mapped to `_`.                                        |
| `trust`                      | `remote_official` / `local_official` / `community`. Community code needs an explicit install confirmation. |
| `risk_level` / `tool_risk` | Feeds the policy engine's risk thresholds. Mark deletions`DESTRUCTIVE` and they need a human by default.       |
| `runtime.allowed_env`        | An allowlist. The hub's own environment — including its secrets — is not inherited.                            |
| `runtime.network`            | `none` for a server that should never reach the internet.                                                      |
| `update_policy`              | `manual`, or automatic within a version constraint.                                                            |
| `auth.secret.per_user`       | Set it whenever the upstream enforces the calling user's permissions.                                            |

Add a matching rule to `config/policies.yaml` at the same time. Without one the
integration inherits `default`, and under `selective` exposure that means its
tools stay hidden.

---

## 8. Updating integrations

Nothing is updated in place. Each version stages into its own directory, proves
it can start and list tools **while the current one keeps serving**, and only
then is promoted by an atomic rename. A failure after promotion rolls back
automatically unless you tell it not to.

```bash
mcp-hub update jira                        # one
mcp-hub update jira figma github           # several
mcp-hub update --all                       # every enabled integration
mcp-hub update --all --exclude jira        # all but one
mcp-hub update --all --exclude jira,figma  # comma-separated
mcp-hub update --all --exclude jira figma  # or repeated — both spellings work
mcp-hub update --all --dry-run             # show the plan, change nothing
```

| Flag                         | Effect                                                                                |
| ---------------------------- | ------------------------------------------------------------------------------------- |
| `--dry-run`                | Print the plan and exit. Nothing is downloaded or promoted.                           |
| `--yes`                    | Execute without the confirmation prompt.                                              |
| `--force`                  | Update even when already at the resolved version.                                     |
| `--no-rollback-on-failure` | Leave a failed update in place for inspection. Default is to restore.                 |
| `--parallel`               | Update concurrently. Off by default — a serial run is easier to read when it breaks. |

Always start with `--dry-run`. It resolves versions and shows exactly what would
change:

```bash
mcp-hub update --all --dry-run
```

Updates are also available over the API — `POST /api/update`, `POST /api/update/all`, `POST /api/integrations/{name}/update` — which return a job id
you poll at `GET /api/jobs/{job_id}`.

After an update, verify:

```bash
mcp-hub health
mcp-hub tools jira        # tool count changed? the upstream changed its surface
```

If an upstream adds or renames tools, the tool digest in
`config/integrations.lock.yaml` changes and the hub logs it. Review your
`policies.yaml` allowlist when that happens — a new upstream tool is not exposed
until you allow it, which is the point.

---

## 9. Removing integrations

```bash
mcp-hub disable github          # stop routing; keep everything on disk
mcp-hub remove github           # remove entirely
mcp-hub remove --all --exclude jira
```

Removal happens in a fixed order: routing stops first, then sessions close, then
a configuration backup is taken, then artifacts and the integration's own
credentials are deleted.

**Credentials shared with another integration are never touched automatically.**
If two integrations use the same key, removing one leaves it in place for the
other. Delete it yourself with `mcp-hub secrets delete` once you are sure.

| Flag                | Effect                                                                     |
| ------------------- | -------------------------------------------------------------------------- |
| `--keep-secrets`  | Leave this integration's own credentials in place.                         |
| `--purge-backups` | Also delete its rollback points.**This makes removal irreversible.** |
| `--yes`           | Skip the confirmation.                                                     |

Without `--purge-backups`, rollback points survive, so a removal stays reversible
for as long as your retention window (`MCP_HUB_BACKUP_RETENTION`, default 10)
keeps them.

`disable` is the reversible option and usually the right first move: it stops the
integration serving without deleting anything.

---

## 10. Rollback

Updates never delete what they replace, so a rollback is an atomic rename back —
not a re-download, and it works with the network down.

```bash
mcp-hub rollback github --list          # what is available
mcp-hub rollback github                 # most recent rollback point
mcp-hub rollback github --version 0.9.1 # a specific one
```

The most recent versions are kept per integration
(`MCP_HUB_VERSION_RETENTION`, default 5) along with configuration backups
(`MCP_HUB_BACKUP_RETENTION`, default 10).

Over the API: `GET /api/integrations/{name}/rollback-points`, then
`POST /api/integrations/{name}/rollback`.

**Secrets are not restored from backup.** A rollback restores code and
configuration; credentials stay where they are, in the encrypted store. Restoring
a secret from a plaintext backup would mean the hub had written one in plaintext,
and it never does.

---

## 11. Registry

The hub can read the official MCP Registry to find servers it does not already
have a manifest for.

```bash
mcp-hub registry search jira            # search
mcp-hub registry search --limit 50
mcp-hub registry inspect io.github.example/my-server   # pre-install disclosure
mcp-hub registry versions io.github.example/my-server
mcp-hub registry install io.github.example/my-server
```

Reading the registry installs nothing. `registry inspect` shows what the server
would run — its source, its command, the environment it wants, its trust tier —
and `registry install` is the only step that acts.

Installation follows a fixed order: resolve the entry, show the disclosure, take
an explicit decision, write the manifest, then install. **A community server is
never installed without a confirmation**, because installing one means running
somebody else's code on your host. `--yes` accepts the disclosure for automation;
read it once by hand first.

Installed servers default to disabled (`--enable` to change that), so you get a
chance to write a policy rule before any agent can call anything.

The same surface is available over the API under `/api/registry/`.

---

## 12. Security

### What the hub guarantees

**Credentials are write-only.** They are encrypted at rest, injected at call
time, and there is no API, CLI command, MCP tool or log line that returns one.
`mcp-hub secrets list` shows names, never values. Audit records name the
credential used and never its contents.

**No arbitrary code execution surface.** The hub exposes no shell tool, no eval
tool, no "run this command" tool. What an agent can do through `hub.*` is list,
search, describe, check health, refresh and activate.

**Confirmation cannot be bypassed.** A tool requiring confirmation runs only
after a human accepts an MCP elicitation. A client that declares no elicitation
capability gets a **refusal**, not an implicit yes. This is the case worth
testing, and it is tested.

**Closed by default.** `selective` exposure plus a non-empty `allow` list means
an operator opens tools deliberately. A tool an upstream added yesterday is not
exposed today.

**Risk backstop.** Anything classified `DESTRUCTIVE` or `ADMIN` needs a human
whatever its name, so a tool nobody has reviewed cannot slip through on a policy
file written before it existed.

**Third-party code is sandboxed.** Local servers run as a non-root user with a
read-only root filesystem, CPU/memory/PID ceilings, a wall-clock cap, and an
**environment allowlist** — the hub's own environment, secrets included, is not
inherited.

**Outbound requests are guarded.** A model-supplied URL is an SSRF vector, so
private and link-local address ranges are blocked, redirects have a budget of
zero by default, and TLS verification is never disabled outside a test fixture.

**Everything is audited.** Every tool call, policy decision, confirmation,
install, update, removal and rollback lands in an append-only trail with the
principal, the integration, the tool, the decision and a request id. Arguments
are redacted before they are written.

### What you must do

* **Never commit `.env`.** It is gitignored; keep it that way.
* **Give agents agent scopes.** `tools:call` and `integrations:read`. Never
  `admin`.
* **Use real identities as token subjects.** Per-user credentials depend on it.
* **Set `per_user: true`** for any upstream that enforces the calling user's own
  permissions.
* **Pin versions.** Exact tags, commits or digests. `MCP_HUB_REQUIRE_SIGNED_ARTIFACTS`
  and digest pinning are enforced in production.
* **Read the disclosure** before installing community code.
* **Review the allowlist** after every update that changes an upstream's tool
  surface.

### Reporting

Security issues should go to a private channel, not a public issue. Include the
request id from the `x-request-id` response header if the report involves a live
request.

---

## 13. MCP client configuration

One endpoint for every client:

```
http://localhost:8000/mcp        transport: Streamable HTTP
```

If the hub requires authentication, issue a token first:

```bash
mcp-hub token issue alice@example.com --scope tools:call --scope integrations:read
```

### Claude Code

```bash
claude mcp add --transport http mcp-hub http://localhost:8000/mcp \
  --header "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..."
```

Then `/mcp` inside Claude Code lists the hub and its tools. Drop the `--header`
if the hub runs with `MCP_HUB_AUTH_REQUIRED=false`.

### Claude Desktop

Claude Desktop launches MCP servers as local processes, so a remote hub is
reached through the `mcp-remote` bridge. Edit `claude_desktop_config.json`:

* macOS — `~/Library/Application Support/Claude/claude_desktop_config.json`
* Windows — `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "mcp-hub": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "http://localhost:8000/mcp",
        "--header", "Authorization: Bearer ${MCP_HUB_TOKEN}"
      ],
      "env": {
        "MCP_HUB_TOKEN": "eyJhbGciOiJIUzI1NiIs..."
      }
    }
  }
}
```

Restart Claude Desktop after editing. Claude Desktop confirms destructive tool
calls itself; the hub's elicitation is a second, independent gate.

### Cursor

Project-level `.cursor/mcp.json` (or global `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "mcp-hub": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer eyJhbGciOiJIUzI1NiIs..."
      }
    }
  }
}
```

Cursor picks the transport up from the URL. Check **Settings → MCP** for a green
indicator and the tool list.

### VS Code

Workspace `.vscode/mcp.json`, with the token prompted for rather than committed:

```json
{
  "inputs": [
    {
      "id": "mcp-hub-token",
      "type": "promptString",
      "description": "MCP Hub bearer token",
      "password": true
    }
  ],
  "servers": {
    "mcp-hub": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer ${input:mcp-hub-token}"
      }
    }
  }
}
```

VS Code prompts on first connection and stores the value in its secret storage.
Commit this file; it contains no credential.

### A custom MCP client

Anything speaking MCP over Streamable HTTP works. With the official Python SDK:

```python
import asyncio

from mcp.client.client import Client


async def main() -> None:
    async with Client("http://localhost:8000/mcp") as client:
        listed = await client.list_tools()
        print([tool.name for tool in listed.tools])
        # ['hub.list_integrations', 'hub.integration_status', 'hub.search_tools',
        #  'hub.describe_tool', 'hub.health', 'hub.refresh_tools', ...]

        result = await client.call_tool("hub.list_integrations", {"enabled_only": True})
        for block in result.content:
            print(block)


asyncio.run(main())
```

With a token, and handling the confirmation prompt — this is the part a custom
client must get right, because a client that cannot be asked is refused:

```python
import asyncio

import mcp.types as types
from mcp.client.client import Client
from mcp.client.session import ClientRequestContext
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

TOKEN = "eyJhbGciOiJIUzI1NiIs..."


async def confirm(
    context: ClientRequestContext, params: types.ElicitRequestParams
) -> types.ElicitResult:
    """Called when the hub needs a human to approve a tool call."""
    print(params.message)
    answer = input("Proceed? [y/N] ").strip().lower()
    if answer != "y":
        return types.ElicitResult(action="decline")
    return types.ElicitResult(action="accept", content={"confirm": True})


async def main() -> None:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
    async with http:
        client = Client(
            streamable_http_client("http://localhost:8000/mcp", http_client=http),
            elicitation_callback=confirm,
        )
        async with client:
            result = await client.call_tool(
                "jira.searchJiraIssuesUsingJql",
                {"jql": "project = ENG AND status = 'In Progress'"},
            )
            for block in result.content:
                print(block)


asyncio.run(main())
```

Advertise elicitation support if your client will ever call a confirming tool.
Without it, those calls are refused by design — the hub will not treat silence as
consent.

### The `mcp-hub` skill

[.claude/skills/mcp-hub/](.claude/skills/mcp-hub/) is a Claude Skill that teaches
a consuming project how to use this hub correctly: the discovery loop, the
namespacing rules, every error code with its retry policy, the confirmation
exchange, and the operator commands to hand over rather than attempt. Copy it
into whichever project connects to the hub:

```bash
cp -r .claude/skills/mcp-hub  <other-project>/.claude/skills/   # one project
cp -r .claude/skills/mcp-hub  ~/.claude/skills/                 # every project
```

It is plain Markdown and needs nothing from this repository at runtime — only
the hub's URL and a token. Keep the copy in step with the hub's tool surface and
policy defaults.

---

## 14. Troubleshooting

**Start here.** `mcp-hub doctor` checks the environment, the configuration and
every integration in one pass, and exits non-zero if anything FAILs:

```
[OK]   Python               3.14.4 (need >= 3.12)
[OK]   MCP SDK              mcp installed
[OK]   docker               found — needed for container isolation and docker sources
[OK]   git                  found — needed for git sources
[OK]   npm                  found — needed for npm sources
[OK]   uv                   found — needed for python sources (falls back to pip)
[WARN] Redis                not configured — locks and rate limits are process-local
[WARN] Database             SQLite (development only)
[FAIL] Auth secret          MCP_HUB_AUTH_SECRET is not set
[OK]   Configuration        11 integrations resolved
[WARN] Lock file            not written yet
[OK]   github               DISABLED
[WARN] fetch                UPDATE_REQUIRED: Not installed. Run `mcp-hub install fetch`.
[WARN] jira                 AUTH_REQUIRED: No credential is configured — store one
                            with `mcp-hub secrets set ATLASSIAN_TOKEN`.
```

### Health statuses

| Status              | Meaning                                               | Do                                                   |
| ------------------- | ----------------------------------------------------- | ---------------------------------------------------- |
| `HEALTHY`         | Reachable, tools discovered.                          | —                                                   |
| `DEGRADED`        | Answering, but slowly or partially.                   | Check`mcp-hub logs <id>`.                          |
| `UNAVAILABLE`     | Not reachable.                                        | Check the upstream and the network.                  |
| `DISABLED`        | Switched off in`integrations.yaml`.                 | `mcp-hub enable <id>`.                             |
| `UPDATE_REQUIRED` | Not installed, or the installed version cannot start. | `mcp-hub install <id>` or `mcp-hub update <id>`. |
| `AUTH_REQUIRED`   | No usable credential.                                 | `mcp-hub secrets set <NAME>`.                      |

`mcp-hub health` exits non-zero when an enabled integration is `UNAVAILABLE`, so
it works as a deployment gate. Degraded and auth-required states are reported but
do not fail the command — the hub is still serving.

### Common problems

**The agent sees only `hub.*` tools.**
Expected in `discovery` mode. Otherwise: the integration is disabled, unhealthy,
or its tools are not in the `allow` list under `selective` exposure. Check in
that order:

```bash
mcp-hub list                 # enabled? healthy?
mcp-hub tools <id>           # discovered?
grep -A5 "<id>:" config/policies.yaml
```

**The agent sees no tools at all.**
The client may not be reaching the hub. Confirm the endpoint and the transport:

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8000/api/status
```

**A tool call returns "authentication required".**
No token, an expired token, or the wrong scope. Tokens default to a one-hour
lifetime. Issue a fresh one and check the scopes match what the call needs.

**A tool call returns "confirmation required" and never proceeds.**
Your client does not support elicitation. That is a refusal by design. Either use
a client that supports it, or remove the tool from `require_confirmation` —
knowing exactly what you are removing.

**An integration is `AUTH_REQUIRED` after storing a secret.**
For a `per_user: true` credential, it must be stored for *the calling subject*.
`mcp-hub secrets set ATLASSIAN_TOKEN --principal alice@example.com`, and make
sure Alice's token uses that same subject.

**An update failed.**
It rolled back automatically unless you passed `--no-rollback-on-failure`. Read
the job, then the integration's own log:

```bash
mcp-hub logs --action update --since 1h    # what the hub decided
mcp-hub logs <id> --process-log            # what the server itself printed
mcp-hub rollback <id> --list
```

**`npm`/`git`/`docker` not found.**
Only needed by the integrations that use those source types. `doctor` reports
`FAIL` when something *enabled* needs a missing binary and `WARN` when nothing
does.

**Rate limits firing unexpectedly.**
`scope: integration` counts across the whole hub, not per caller — correct for a
shared, metered key like Brave's. `scope: principal` counts per user. The global
limit applies in addition; whichever binds first wins.

**Two hubs fighting over the same state.**
Without Redis, locks and rate limits are process-local. Run one process, or
configure `MCP_HUB_REDIS_URL`.

### Reading the trail

```bash
mcp-hub logs                          # the audit trail: what was attempted and decided
mcp-hub logs -n 100
mcp-hub logs --user alice@example.com --since 2h
mcp-hub logs --action tool_call       # or: policy_violation, update, rollback, ...
mcp-hub logs <id> --process-log       # what a locally-run server printed to stderr
mcp-hub status                        # what the hub is doing right now
mcp-hub show <id>                     # source, lock, health, tools and policy for one
```

The audit trail and a server's process log answer different questions, which is
why they are different flags rather than one merged stream.

Every response carries an `x-request-id` header. Quote it when reporting a
problem — it ties the response to its audit record.

---

## 15. Production deployment

### Before you start

```
[ ] MCP_HUB_ENV=production
[ ] MCP_HUB_AUTH_SECRET set to 32+ random bytes, from a secret manager
[ ] MCP_HUB_SECRET_ENCRYPTION_KEY set, backed up, and rotatable
[ ] MCP_HUB_AUTH_REQUIRED=true
[ ] PostgreSQL — not SQLite
[ ] Redis — for distributed locks and rate limits
[ ] TLS terminated in front of the hub; MCP_HUB_PUBLIC_URL set to the external URL
[ ] Every integration pinned to an exact version, commit or digest
[ ] policies.yaml reviewed: allowlists closed, deletions denied or confirmed
[ ] Backups of the database and of config/
[ ] /health and /ready wired to your orchestrator
[ ] /metrics scraped
```

The settings loader enforces the credential and infrastructure items itself:
under `MCP_HUB_ENV=production` it refuses to start without an auth secret, a
PostgreSQL URL, a Redis URL, `MCP_HUB_AUTH_REQUIRED=true`, and — when the
encrypted-database secret provider is in use — an encryption key. That is
deliberate. A misconfigured production hub should fail loudly at startup, not
quietly at the first request. The remaining items are yours to check.

### Environment

```bash
MCP_HUB_ENV=production
MCP_HUB_HOST=0.0.0.0
MCP_HUB_PORT=8000
MCP_HUB_PUBLIC_URL=https://mcp.example.com

MCP_HUB_DATABASE_URL=postgresql+asyncpg://mcp_hub:...@postgres:5432/mcp_hub
MCP_HUB_REDIS_URL=redis://redis:6379/0

MCP_HUB_AUTH_SECRET=...
MCP_HUB_SECRET_ENCRYPTION_KEY=...
MCP_HUB_AUTH_REQUIRED=true

MCP_HUB_LOG_JSON=true
MCP_HUB_REQUIRE_SIGNED_ARTIFACTS=true
MCP_HUB_ALLOW_MUTABLE_IMAGE_TAGS=false
```

Inject secrets from a real secret manager. `.env` is for development.

### Migrations

Run them as a deliberate step, not a side effect of every container start:

```bash
alembic upgrade head
```

The compose file sets `MCP_HUB_AUTO_MIGRATE=true` for local convenience. In
production run migrations as a pre-deploy job and leave the flag off, so two
replicas starting at once cannot race.

### Scaling

The hub is stateless apart from its database and Redis, so replicas scale
horizontally behind a load balancer. Two constraints:

* **Redis is required with more than one replica.** Locks and rate limits are
  process-local without it, and two replicas would each allow the full quota.
* **The job queue is in-process.** Updates run on the replica that accepted
  them. Redis-backed locks keep two replicas from updating the same integration
  at once, and `mcp-hub` warns when it is running uncoordinated.

Sessions to upstream servers are pooled per replica, bounded by
`MCP_HUB_UPSTREAM_MAX_SESSIONS` and idled out after
`MCP_HUB_UPSTREAM_SESSION_IDLE_SECONDS`.

### Observability

* `/health` — liveness. Cheap; no dependency checks.
* `/ready` — readiness. Returns 503 when the database is unreachable, and
  reports how many integrations are serving without failing on the ones that
  are not. Use this as the load balancer's gate.
* `/metrics` — Prometheus: `mcp_hub_tool_calls_total`,
  `mcp_hub_tool_duration_seconds`, `mcp_hub_policy_decisions_total`,
  `mcp_hub_integration_healthy`, `mcp_hub_upstream_sessions`,
  `mcp_hub_tools_registered`, `mcp_hub_updates_total`,
  `mcp_hub_audit_events_dropped`.
* Logs — structured JSON with a request id, principal, integration and tool on
  every line. Never a credential.

Alert on: `UNAVAILABLE` integrations, policy denials spiking, confirmation
timeouts, update failures, and rate-limit rejections.

### Upgrading the hub itself

```bash
alembic upgrade head        # pre-deploy job
# roll out the new image
curl -sf https://mcp.example.com/ready
mcp-hub health              # every enabled integration still serving
```

Migrations are additive where possible, so a rolling deploy can run one version
of the schema against two versions of the code for the length of the rollout.

---

## Reference

|                                |                                                                                 |
| ------------------------------ | ------------------------------------------------------------------------------- |
| **Specification**        | [config/mcp/arch.md](config/mcp/arch.md) — the architecture this implements     |
| **Settings**             | [app/config/settings.py](app/config/settings.py) — every `MCP_HUB_*` variable |
| **Environment template** | [.env.example](.env.example)                                                     |
| **Manifests**            | [config/manifests/](config/manifests/)                                           |
| **API documentation**    | `http://localhost:8000/docs` when the hub is running                          |
| **MCP specification**    | https://modelcontextprotocol.io                                                 |

Licensed under Apache-2.0.
