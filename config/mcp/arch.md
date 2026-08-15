
# MCP Hub / MCP Gateway

Build a production-ready, self-hosted **Universal MCP Hub** in Python.

The goal is to provide a single MCP endpoint to an AI agent while internally managing multiple MCP integrations such as Jira, Figma, GitHub, web search, Notion, filesystem, Git, fetch, and additional MCP servers discovered from the MCP Registry.

The AI agent must connect to exactly ONE MCP endpoint:

```
https://<host>/mcp
```

The hub internally manages all configured MCP servers.

---

# 1. Core Concept

Architecture:

```
                AI AGENT
                   |
                   | MCP / Streamable HTTP
                   v
          +--------------------+
          |      MCP HUB       |
          |                    |
          | Authentication     |
          | Authorization      |
          | Tool Router        |
          | Server Registry     |
          | Lifecycle Manager  |
          | Policy Engine      |
          | Audit Logger       |
          +---------+----------+
                    |
    +---------------+----------------+
    |               |                |
    v               v                v
  Jira            Figma          Web Search
  MCP              MCP              MCP
    |               |                |
    v               v                v
 Jira API        Figma API       Brave API

    +---------------+----------------+
    |               |                |
    v               v                v
 GitHub          Notion          Filesystem
   MCP             MCP               MCP
```

The hub is an MCP Proxy Aggregator + Tool Gateway + Integration Manager.

Do NOT reimplement every third-party API if an official or high-quality MCP server already exists.

Prefer:

1. Official remote MCP server
2. Official MCP repository
3. Official/reference MCP server
4. Trusted community MCP server
5. Direct API adapter only when no suitable MCP server exists

---

# 2. Primary Requirements

The system MUST provide:

* One MCP endpoint
* Multiple internal MCP integrations
* Dynamic integration registration
* Enable/disable integrations
* Install integrations
* Remove integrations
* Update one integration
* Update selected integrations
* Update all integrations
* Update all except specified integrations
* Rollback an integration
* Pin versions/commits
* Health checks
* Tool discovery
* Tool namespacing
* Authentication
* Authorization
* Per-user credentials
* Secret management
* Audit logs
* Rate limiting
* Tool policies
* Human confirmation for destructive operations
* Automatic backup before updates
* Automatic rollback on failed update
* Integration tests
* MCP Inspector support
* Docker deployment
* CLI management
* REST management API
* Web dashboard/API-ready architecture

---

# 3. Recommended Technology

Backend:

* Python 3.12+
* FastAPI
* FastMCP
* Pydantic v2
* SQLAlchemy
* PostgreSQL
* Redis
* httpx
* asyncio
* structlog
* Alembic
* pytest
* pytest-asyncio
* Docker
* Docker Compose

Package management:

* uv

Use Streamable HTTP as the primary remote MCP transport.

Use stdio internally where an integration is a local process.

Use subprocess isolation or containers for third-party local MCP servers.

---

# 4. MCP Core Dependencies

Use the official MCP Python SDK:

Repository:

https://github.com/modelcontextprotocol/python-sdk

Branch:

main

The current SDK repository documents v2 as the current stable line, with `v1.x` maintained separately. Use the current v2 line rather than writing against the old v1 API.

Also evaluate FastMCP for server/client composition:

Repository:

https://github.com/jlowin/fastmcp

Branch:

main

FastMCP supports MCP server composition/proxying and is suitable for the gateway layer.

Use the official MCP Inspector for development/testing:

Repository:

https://github.com/modelcontextprotocol/inspector

Branch:

main

The Inspector provides web, CLI and TUI testing capabilities.

---

# 5. Integration Catalog

Create:

```
config/integrations.yaml
```

and:

```
config/integrations.lock.yaml
```

The YAML catalog describes desired integrations.

The lock file records the exact resolved version/commit.

Example:

```
integrations:

  jira:
    enabled: true
    source_type: remote
    transport: streamable-http
    endpoint: https://mcp.atlassian.com/v1/mcp

  figma:
    enabled: true
    source_type: remote
    transport: streamable-http
    endpoint: https://mcp.figma.com/mcp

  github:
    enabled: true
    source_type: git
    repository: https://github.com/github/github-mcp-server.git
    branch: main

  brave-search:
    enabled: true
    source_type: git
    repository: https://github.com/brave/brave-search-mcp-server.git
    branch: main

  notion:
    enabled: true
    source_type: remote
    transport: streamable-http
    endpoint: https://mcp.notion.com/mcp

  filesystem:
    enabled: false
    source_type: package
```

The lock file MUST contain resolved versions/commits.

Example:

```
integrations:

  github:
    repository: https://github.com/github/github-mcp-server.git
    branch: main
    resolved_commit: <sha>
    updated_at: <timestamp>
```

This gives us reproducible deployments.

---

# 6. Initial Official Integrations

Implement these first.

## 6.1 Jira / Atlassian

Official repository:

https://github.com/atlassian/atlassian-mcp-server

Branch:

main

Atlassian's Rovo MCP Server provides access to Jira, Confluence and Compass and is a remote MCP service.

Endpoint:

```
https://mcp.atlassian.com/v1/mcp
```

Do NOT clone/build the Atlassian server by default.

Treat it as a managed remote integration.

Authentication must support OAuth 2.1 and API-token/headless authentication where appropriate.

Atlassian states that its MCP server respects the user's existing Atlassian permissions.

Integration name:

```
jira
```

Tools should be exposed under:

```
jira.*
```

Examples:

```
jira.search_issues
jira.get_issue
jira.create_issue
jira.update_issue
jira.add_comment
```

Do not hard-code the tool list.

Discover tools dynamically from the upstream MCP server.

If upstream adds a tool, the hub should automatically expose it after refresh.

---

# 6.2 Figma

Official Figma MCP documentation/repository:

https://github.com/figma/mcp-server-guide

Branch:

main

Official remote endpoint:

```
https://mcp.figma.com/mcp
```

Figma currently publishes its MCP server as a remote Streamable HTTP service rather than a normal open-source server that should be cloned into the project.

Integration:

```
figma.*
```

Authentication:

OAuth / Figma-supported authentication.

Do not attempt to fork or rebuild the Figma MCP implementation.

The hub should proxy it.

---

# 6.3 GitHub

Official repository:

https://github.com/github/github-mcp-server

Branch:

main

Integration:

```
github.*
```

Use the official GitHub MCP server.

The repository describes itself as GitHub's official MCP Server.

Support:

* repositories
* issues
* pull requests
* branches
* commits
* files
* code search
* workflows
* releases
* discussions
* users
* organizations

Do not manually enumerate tools.

Perform MCP tool discovery.

---

# 6.4 Brave Search

Official repository:

https://github.com/brave/brave-search-mcp-server

Branch:

main

Integration:

```
brave_search.*
```

The official server supports:

* web search
* news search
* image search
* video search
* local/business search
* place search
* LLM context
* AI summarization

The repository currently supports both stdio and HTTP transports.

Use:

```
BRAVE_API_KEY
```

from the secret manager.

Never put API keys in YAML committed to Git.

---

# 6.5 Notion

Official repository:

https://github.com/makenotion/notion-mcp-server

Branch:

main

However, Notion now recommends its remote MCP service rather than the open-source local repository.

Official remote endpoint:

```
https://mcp.notion.com/mcp
```

Use remote MCP by default.

The local repository should only be supported as an optional self-hosted adapter.

Notion explicitly says the open-source repository is no longer actively maintained and recommends the remote MCP server.

Integration:

```
notion.*
```

---

# 6.6 Official MCP reference servers

Repository:

https://github.com/modelcontextprotocol/servers

Branch:

main

Use reference implementations where appropriate:

* filesystem
* git
* fetch
* time
* memory
* sequential-thinking
* everything

Important:

Do not treat this repository as a collection of production integrations.

The MCP project explicitly describes these as reference implementations and warns that they are not necessarily production-ready.

Pin releases/commits.

The repository currently publishes dated releases such as 2026.7.10.

---

# 7. MCP Registry Integration

Integrate with the official MCP Registry:

https://github.com/modelcontextprotocol/registry

Registry:

```
https://registry.modelcontextprotocol.io
```

The registry acts as an app-store-like directory for MCP servers.

Create a registry client:

```
app/registry/client.py
```

Capabilities:

```
search_servers()
get_server()
get_versions()
resolve_server()
validate_server()
```

CLI:

```
mcp-hub registry search jira
mcp-hub registry search figma
mcp-hub registry search database
```

Allow users to install an MCP server from the registry.

Example:

```
mcp-hub install <registry-server-name>
```

But require explicit confirmation before installing an unknown/community server.

---

# 8. Integration Types

The hub MUST support these source types.

## remote

Example:

```
https://mcp.figma.com/mcp
```

No local installation.

---

## git

Example:

```
https://github.com/github/github-mcp-server.git
```

Clone into:

```
runtime/integrations/github/
```

Build and run according to its detected project configuration.

---

## npm

Example:

```
@some-org/some-mcp-server
```

Use an isolated Node environment/container.

---

## python

Example:

```
some-mcp-package
```

Use isolated Python environment/container.

---

## docker

Example:

```
ghcr.io/example/mcp-server:latest
```

Run as a controlled container.

---

## builtin

Implemented directly in the hub.

---

# 9. Directory Structure

Create:

```
mcp-hub/

├── app/
│   ├── main.py
│   │
│   ├── server/
│   │   ├── mcp_server.py
│   │   ├── router.py
│   │   ├── session.py
│   │   └── middleware.py
│   │
│   ├── gateway/
│   │   ├── gateway.py
│   │   ├── proxy.py
│   │   ├── tool_router.py
│   │   ├── tool_registry.py
│   │   └── discovery.py
│   │
│   ├── integrations/
│   │   ├── base.py
│   │   ├── manager.py
│   │   ├── installer.py
│   │   ├── updater.py
│   │   ├── remover.py
│   │   ├── rollback.py
│   │   ├── health.py
│   │   └── adapters/
│   │       ├── remote.py
│   │       ├── git.py
│   │       ├── npm.py
│   │       ├── python.py
│   │       └── docker.py
│   │
│   ├── registry/
│   │   ├── client.py
│   │   ├── models.py
│   │   └── resolver.py
│   │
│   ├── auth/
│   │   ├── oauth.py
│   │   ├── jwt.py
│   │   ├── middleware.py
│   │   └── permissions.py
│   │
│   ├── secrets/
│   │   ├── manager.py
│   │   └── providers.py
│   │
│   ├── policy/
│   │   ├── engine.py
│   │   ├── models.py
│   │   └── confirmation.py
│   │
│   ├── audit/
│   │   ├── logger.py
│   │   └── models.py
│   │
│   ├── database/
│   │   ├── models.py
│   │   ├── session.py
│   │   └── migrations/
│   │
│   ├── api/
│   │   ├── integrations.py
│   │   ├── registry.py
│   │   ├── health.py
│   │   └── admin.py
│   │
│   └── cli/
│       └── commands.py
│
├── config/
│   ├── integrations.yaml
│   ├── integrations.lock.yaml
│   ├── policies.yaml
│   └── settings.yaml
│
├── runtime/
│   ├── integrations/
│   ├── cache/
│   ├── backups/
│   └── logs/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── security/
│   └── e2e/
│
├── scripts/
│   ├── install.py
│   ├── update.py
│   ├── remove.py
│   ├── rollback.py
│   ├── health.py
│   └── sync_registry.py
│
├── docker/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── .env.example
├── README.md
└── Makefile
```

---

# 10. Single MCP Endpoint

Expose:

```
/mcp
```

Transport:

```
Streamable HTTP
```

Example:

```
https://localhost:8000/mcp
```

The agent must NOT need to know individual upstream servers.

The agent sees:

```
jira.*
figma.*
github.*
brave_search.*
notion.*
```

---

# 11. Namespacing

Every integration must use a namespace.

Examples:

```
jira.search_issues
jira.create_issue

github.search_code
github.create_issue

figma.get_design_context

brave_search.web_search

notion.search
```

Prevent name collisions.

If upstream exposes:

```
search
```

then the hub exposes:

```
jira.search
```

or:

```
github.search
```

depending on integration.

---

# 12. Dynamic Tool Discovery

Do not hard-code third-party tool definitions.

At startup:

1. Load enabled integrations.
2. Connect to each integration.
3. Perform MCP initialization.
4. Call tools/list.
5. Cache tool metadata.
6. Register tools in the gateway.
7. Apply namespace.
8. Apply policies.
9. Expose the resulting tools.

When an integration changes:

```
refresh_tools(integration)
```

must rediscover its tools.

Provide:

```
mcp-hub tools list
mcp-hub tools list jira
mcp-hub tools refresh jira
```

---

# 13. Tool Count / Context Management

Do NOT blindly expose hundreds of tools to the model if avoidable.

Implement three modes:

```
FULL
SELECTIVE
DISCOVERY
```

FULL:

Expose all tools.

SELECTIVE:

Expose only tools enabled by policy.

DISCOVERY:

Expose a small number of discovery/router tools and dynamically activate the appropriate integration.

Default:

```
SELECTIVE
```

Allow configuration:

```
tool_exposure_mode: selective
```

The architecture must make it possible to avoid overwhelming an LLM with hundreds of tool schemas.

---

# 14. Lifecycle Manager

Create:

```
mcp-hub
```

CLI.

Commands:

```
mcp-hub list
mcp-hub status
mcp-hub health
mcp-hub tools
mcp-hub logs
```

Install:

```
mcp-hub install jira
```

Update:

```
mcp-hub update jira
```

Update multiple:

```
mcp-hub update jira figma github
```

Update all:

```
mcp-hub update --all
```

Update all except:

```
mcp-hub update --all --exclude jira
```

Multiple exclusions:

```
mcp-hub update --all --exclude jira,figma
```

Remove:

```
mcp-hub remove jira
```

Remove multiple:

```
mcp-hub remove jira figma
```

Enable:

```
mcp-hub enable jira
```

Disable:

```
mcp-hub disable jira
```

Rollback:

```
mcp-hub rollback jira
```

Rollback to specific version:

```
mcp-hub rollback jira --version <version>
```

Refresh:

```
mcp-hub refresh
```

Sync:

```
mcp-hub sync
```

---

# 15. Update Algorithm

This is critical.

Never perform:

```
git pull
restart
```

without validation.

The update process MUST be:

```
discover
    |
    v
determine latest version/commit
    |
    v
acquire lock
    |
    v
create backup
    |
    v
create staging directory
    |
    v
download/clone new version
    |
    v
resolve dependencies
    |
    v
build
    |
    v
static validation
    |
    v
security scan
    |
    v
launch isolated test instance
    |
    v
MCP initialize
    |
    v
tools/list
    |
    v
smoke tests
    |
    v
compare tools
    |
    v
promote
    |
    v
health check
    |
    +---- failure ----> automatic rollback
    |
    v
update lock file
    |
    v
audit log
```

Never update a running integration in-place.

Use:

```
runtime/integrations/<name>/versions/<commit>/
```

and:

```
runtime/integrations/<name>/current
```

as a symlink or pointer.

This makes rollback cheap.

---

# 16. Update All

Command:

```
mcp-hub update --all
```

Algorithm:

1. Enumerate all enabled integrations.
2. Check available versions.
3. Create an update plan.
4. Display plan.
5. Ask for confirmation unless --yes is provided.
6. Update integrations one by one.
7. Run health checks after every integration.
8. If one fails, rollback only that integration.
9. Continue or abort according to policy.
10. Generate summary.

Example:

```
Update plan:

jira
  current: abc123
  latest:  def456

github
  current: 123abc
  latest: 456def

figma
  remote
  no local update required

Proceed? [y/N]
```

---

# 17. Update All Except

Command:

```
mcp-hub update --all --exclude jira
```

or:

```
mcp-hub update --all --exclude jira,figma
```

The exclusion list must be parsed before generating the update plan.

Excluded integrations must not be modified.

---

# 18. Remove

Command:

```
mcp-hub remove jira
```

Before removal:

1. Confirm.
2. Disable routing.
3. Disconnect sessions.
4. Backup configuration.
5. Remove runtime artifacts.
6. Remove secrets belonging only to the integration.
7. Update integrations.yaml.
8. Update lock file.
9. Remove database records if configured.
10. Write audit event.

Never delete shared credentials automatically.

---

# 19. Rollback

Every update must create a rollback point.

Store:

```
runtime/backups/<integration>/<timestamp>/
```

Rollback must restore:

* code/version
* lock file
* configuration
* tool metadata
* runtime configuration

Never restore secrets from plaintext backup.

---

# 20. Per-user Authentication

The hub must support multiple users.

Do NOT use one global Jira token for every user unless explicitly configured as a service-account mode.

Preferred model:

```
User
  |
  +-- Jira OAuth token
  +-- Figma OAuth token
  +-- GitHub token
  +-- Notion token
  +-- Brave API key
```

When the agent invokes:

```
jira.search_issues
```

the hub identifies the authenticated user and forwards that user's credentials.

Credentials must never be exposed to the model.

---

# 21. Secret Storage

Implement an abstraction:

```
SecretProvider
```

Providers:

```
environment
encrypted_database
vault
```

Default development provider:

```
environment
```

Production recommendation:

```
encrypted database or external secret manager.
```

Never:

* log tokens
* return tokens through MCP
* store secrets in YAML
* commit .env
* include credentials in audit logs

---

# 22. Authorization

Create:

```
config/policies.yaml
```

Example:

```
policies:

  jira:
    allow:
      - search_issues
      - get_issue
      - get_project

    require_confirmation:
      - create_issue
      - update_issue
      - delete_issue

  github:
    allow:
      - search_code
      - get_file

    require_confirmation:
      - create_pull_request
      - merge_pull_request
      - delete_repository
```

Destructive operations must require confirmation.

---

# 23. Tool Policy Engine

Before every tool call:

```
Agent
  |
  v
Authentication
  |
  v
Authorization
  |
  v
Policy Engine
  |
  +---- DENY
  |
  +---- CONFIRM
  |
  v
Rate Limiter
  |
  v
Upstream MCP
  |
  v
Audit Log
```

The policy engine must support:

* allow
* deny
* confirmation
* rate limits
* argument restrictions
* user restrictions
* integration restrictions

---

# 24. Audit Logging

Record every tool invocation.

Example:

```
{
  "timestamp": "...",
  "user_id": "...",
  "integration": "jira",
  "tool": "create_issue",
  "status": "success",
  "duration_ms": 523,
  "request_id": "..."
}
```

Do not store secrets.

For sensitive arguments, store redacted metadata.

Audit:

* install
* update
* remove
* rollback
* enable
* disable
* tool calls
* authentication events
* authorization failures
* policy violations
* upstream errors

---

# 25. Health System

Every integration gets:

```
health_check()
```

Statuses:

```
HEALTHY
DEGRADED
UNAVAILABLE
DISABLED
UPDATE_REQUIRED
AUTH_REQUIRED
```

CLI:

```
mcp-hub health
```

Example:

```
Jira             HEALTHY
Figma            HEALTHY
GitHub           HEALTHY
Brave Search     AUTH_REQUIRED
Notion           HEALTHY
```

---

# 26. Dashboard API

Create REST endpoints:

```
GET  /api/integrations
GET  /api/integrations/{name}
POST /api/integrations/{name}/enable
POST /api/integrations/{name}/disable
POST /api/integrations/{name}/update
POST /api/integrations/{name}/rollback
DELETE /api/integrations/{name}

POST /api/update
POST /api/update/all

GET /api/health
GET /api/tools
GET /api/audit
```

Registry:

```
GET /api/registry/search?q=jira
```

---

# 27. Database

Use PostgreSQL.

Tables:

```
users
integrations
integration_versions
integration_credentials
integration_health
tool_registry
policies
audit_logs
update_jobs
update_history
rollback_points
```

Use Alembic migrations.

---

# 28. Redis

Use Redis for:

* distributed locks
* session state where needed
* rate limiting
* short-lived tool metadata cache
* update job queues

Never store long-lived secrets in Redis.

---

# 29. Worker Architecture

Do not perform long update operations inside HTTP request handlers.

Use:

```
API
  |
  v
Job Queue
  |
  v
Worker
  |
  v
Integration Manager
```

Jobs:

```
install
update
remove
rollback
health_check
tool_refresh
```

Provide job status:

```
GET /api/jobs/{job_id}
```

---

# 30. Concurrency

Prevent two operations on the same integration.

Example:

```
update jira
update jira
```

The second request must receive:

```
Integration jira is currently locked by update job <id>
```

Use Redis distributed locks.

Different integrations may be updated concurrently only when explicitly enabled.

Default:

```
sequential
```

Optional:

```
parallel
```

---

# 31. Security

Treat every third-party MCP server as untrusted code.

This is extremely important.

The MCP ecosystem has already experienced malicious MCP packages, so do not blindly execute arbitrary community servers with host-level permissions.

Implement:

* sandboxing
* container isolation
* read-only filesystem by default
* network restrictions
* non-root containers
* CPU limits
* memory limits
* execution timeout
* subprocess timeout
* allowed environment variables
* secret allowlist
* package provenance
* Git commit tracking
* image digest tracking
* dependency scanning

For local MCP servers, prefer containers.

For remote MCP servers, enforce:

* HTTPS
* TLS verification
* allowed-host list
* request timeout
* redirect restrictions
* SSRF protection

---

# 32. Integration Manifest

Every integration should have a manifest:

```
integrations/jira/manifest.yaml
```

Example:

```
id: jira
name: Jira / Atlassian
source:
  type: remote
  endpoint: https://mcp.atlassian.com/v1/mcp

transport: streamable-http

authentication:
  type: oauth2

namespace: jira

capabilities:
  - issue-management
  - project-management
  - search

risk:
  level: high

confirmation_required:
  - create
  - update
  - delete
```

For Git repositories:

```
id: github
source:
  type: git
  repository: https://github.com/github/github-mcp-server.git
  branch: main

runtime:
  type: container
```

---

# 33. Version Strategy

Never rely solely on:

```
latest
```

For reproducible deployments:

```
desired_branch: main
```

and:

```
resolved_commit: <SHA>
```

The update manager can discover the latest main commit, test it, and update the lock file.

For releases:

```
desired_version: 1.2.3
```

For Docker:

```
image: ghcr.io/example/server
digest: sha256:...
```

For npm:

```
package: ...
version: ...
```

For PyPI:

```
package: ...
version: ...
```

For remote services:

```
endpoint: ...
protocol_version: ...
```

---

# 34. Update Policy

Support:

```
update_policy: manual

update_policy: scheduled

update_policy: automatic
```

Default:

```
manual
```

Scheduled updates:

```
daily
weekly
monthly
```

Automatic updates must never immediately replace production integrations.

Use:

```
download
validate
test
stage
promote
```

---

# 35. CLI Examples

The final CLI must support:

```
mcp-hub list

mcp-hub status

mcp-hub install github

mcp-hub install jira

mcp-hub enable jira

mcp-hub disable jira

mcp-hub update jira

mcp-hub update jira github

mcp-hub update --all

mcp-hub update --all --exclude jira

mcp-hub update --all --exclude jira,figma

mcp-hub remove jira

mcp-hub rollback jira

mcp-hub health

mcp-hub tools

mcp-hub tools jira

mcp-hub refresh

mcp-hub registry search jira

mcp-hub registry search "web search"

mcp-hub logs jira

mcp-hub doctor
```

---

# 36. Doctor Command

Implement:

```
mcp-hub doctor
```

It should check:

* Python version
* Docker
* PostgreSQL
* Redis
* MCP SDK
* configuration
* lock file
* credentials
* network connectivity
* integration health
* MCP protocol initialization
* tool discovery
* filesystem permissions
* container runtime
* registry connectivity

Output:

```
[OK] Python
[OK] PostgreSQL
[OK] Redis
[OK] MCP SDK
[OK] Jira
[OK] GitHub
[WARN] Figma authentication
[OK] Brave Search
[FAIL] Notion
```

---

# 37. MCP Tool Interface

The hub itself may expose administrative tools.

Examples:

```
hub.list_integrations
hub.integration_status
hub.search_integrations
hub.refresh_tools
hub.health
```

Do NOT expose unrestricted:

```
hub.execute_shell
```

or:

```
hub.execute_python
```

Never provide an arbitrary code execution tool.

---

# 38. Configuration API

Support:

```
config/integrations.yaml
```

Example:

```
integrations:

  jira:
    enabled: true

  figma:
    enabled: true

  github:
    enabled: true

  brave-search:
    enabled: true

  notion:
    enabled: false
```

The application should be able to update this safely.

Never edit YAML using unsafe string replacement.

Use Pydantic models.

---

# 39. Backup Strategy

Before:

* update
* remove
* configuration migration

create:

```
runtime/backups/
```

Backups should include:

* configuration
* lock file
* integration metadata
* version metadata

Do not backup raw credentials.

Use encryption for sensitive configuration.

---

# 40. Testing

Every integration must pass:

```
initialize
tools/list
health check
```

For each integration:

```
test_connection()
test_tool_discovery()
test_namespace()
test_authentication()
test_policy()
test_timeout()
test_failure_handling()
```

Update tests:

```
install old
update
test
rollback
test
```

---

# 41. End-to-End Test

Build an E2E test:

```
Agent
  |
  v
MCP Hub
  |
  v
mock Jira MCP server
```

The test should verify:

1. Agent connects to one endpoint.
2. Jira tools are discovered.
3. Jira tools appear under jira.*.
4. Tool call is forwarded.
5. Response is returned.
6. Audit event is created.
7. Policy is enforced.
8. Integration can be disabled.
9. Tools disappear.
10. Integration can be enabled again.
11. Tools return.

---

# 42. Mock MCP Servers

Create test-only mock servers:

```
tests/fixtures/mock_jira
tests/fixtures/mock_figma
tests/fixtures/mock_search
```

Do not use real credentials in tests.

---

# 43. Docker Architecture

Create:

```
docker-compose.yml
```

Services:

```
mcp-hub
postgres
redis
```

Optional:

```
worker
inspector
```

Production architecture:

```
            Load Balancer
                  |
                  v
             MCP Hub API
              /       \
             /         \
         Redis       PostgreSQL
            |
          Worker
            |
   +--------+---------+
   |        |         |
  Jira    GitHub    Search
```

Third-party local MCP servers should run in isolated containers.

---

# 44. Observability

Implement:

* structured logs
* request IDs
* trace IDs
* latency
* error rates
* tool invocation metrics
* integration health
* update metrics

Expose:

```
/health
/ready
/metrics
```

Use Prometheus-compatible metrics.

---

# 45. Error Handling

If upstream Jira is unavailable:

Do not crash the hub.

Return:

```
integration_unavailable
```

while keeping other integrations available.

Example:

```
Jira: unavailable
Figma: healthy
GitHub: healthy
Search: healthy
```

The MCP Hub must remain operational.

---

# 46. Remote vs Local Integration Rule

For every integration, classify it as:

```
REMOTE_OFFICIAL
LOCAL_OFFICIAL
COMMUNITY
BUILTIN
```

Remote official servers should not be unnecessarily cloned.

Local official servers should be isolated.

Community servers must require explicit trust/installation.

---

# 47. Future Integrations

The architecture must allow adding:

* Slack
* Linear
* Asana
* Google Drive
* Google Calendar
* Microsoft 365
* Sentry
* Datadog
* PostgreSQL
* MySQL
* MongoDB
* Redis
* AWS
* Azure
* Kubernetes
* Docker
* Playwright
* Browser automation
* Stripe
* Salesforce
* HubSpot
* Notion
* Confluence
* Bitbucket
* GitLab
* Cloudflare

without modifying the core gateway.

Every integration should be a plugin/manifest.

---

# 48. Plugin Interface

Create:

```
IntegrationAdapter
```

Methods:

```
install()
uninstall()
update()
rollback()
start()
stop()
health_check()
discover_tools()
connect()
disconnect()
get_version()
get_latest_version()
```

Implement:

```
RemoteAdapter
GitAdapter
NpmAdapter
PythonAdapter
DockerAdapter
```

---

# 49. Important Design Rule

Do NOT make the core gateway dependent on Jira-specific code.

Bad:

```
if integration == "jira":
    ...
```

Good:

```
adapter = adapter_registry.get(source_type)
```

The core system should know about:

```
MCP server
```

not:

```
Jira
```

---

# 50. Dynamic Registry

Maintain:

```
IntegrationRegistry
```

Example:

```
registry.register("jira", JiraManifest)
registry.register("figma", FigmaManifest)
registry.register("github", GitHubManifest)
```

But manifests should be loadable dynamically.

Adding:

```
integrations/new-service/manifest.yaml
```

should be enough to register a new integration.

---

# 51. Tool Registry

Database/cache:

```
tool_registry
```

Fields:

```
integration_id
tool_name
qualified_name
description
input_schema
risk_level
enabled
discovered_at
```

Example:

```
integration: jira
tool_name: search_issues
qualified_name: jira.search_issues
```

---

# 52. Tool Risk Classification

Automatically classify tools:

```
READ
WRITE
DESTRUCTIVE
ADMIN
```

Examples:

```
jira.search_issues -> READ

jira.create_issue -> WRITE

jira.update_issue -> WRITE

jira.delete_issue -> DESTRUCTIVE

github.delete_repository -> DESTRUCTIVE
```

If the manifest explicitly declares a risk, trust the manifest over heuristics.

---

# 53. Human Confirmation

For destructive operations:

```
Agent
  |
  v
MCP Hub
  |
  v
"This action will delete Jira issue XYZ.
 Confirm?"
  |
  v
User
  |
  v
Execute
```

Never allow a model to bypass confirmation.

---

# 54. Installation Security

Before installing a GitHub/community MCP server:

Show:

```
repository
owner
branch
commit
license
runtime
requested environment variables
requested network access
requested filesystem access
requested permissions
```

Example:

```
Installing:
  example/mcp-server

Runtime:
  Docker

Network:
  outbound internet

Filesystem:
  read-only /workspace

Secrets:
  API_KEY

Continue? [y/N]
```

---

# 55. Supply Chain Security

For every installed integration record:

```
repository URL
owner
branch
commit SHA
package version
Docker digest
installation timestamp
```

Reject:

* untrusted shell scripts
* unsigned/unverified artifacts when policy requires signatures
* mutable image tags in production
* unknown redirect hosts
* arbitrary postinstall scripts unless explicitly approved

Run dependency/security scans where available.

---

# 56. Registry Installation Flow

Command:

```
mcp-hub registry search jira
```

Then:

```
mcp-hub registry install <server-id>
```

Flow:

```
registry
   |
   v
metadata
   |
   v
security inspection
   |
   v
user confirmation
   |
   v
staging installation
   |
   v
test
   |
   v
enable
```

---

# 57. Update Script

Create:

```
scripts/update.py
```

It must support:

```
python scripts/update.py jira

python scripts/update.py --all

python scripts/update.py --all --exclude jira

python scripts/update.py --all --exclude jira figma

python scripts/update.py github figma
```

The CLI command should call the same service layer as the REST API.

Do not duplicate update logic.

The real implementation should be:

```
UpdateManager
```

CLI/API both call:

```
UpdateManager.update()
```

---

# 58. Remove Script

Create:

```
scripts/remove.py
```

Support:

```
python scripts/remove.py jira

python scripts/remove.py jira figma

python scripts/remove.py --all --exclude github
```

Require confirmation unless:

```
--yes
```

---

# 59. Update Modes

Support:

```
--dry-run
```

Example:

```
mcp-hub update --all --dry-run
```

Output:

```
Jira       update available
GitHub     update available
Figma      remote
Notion     no action
Brave      update available
```

No changes should be made.

Also support:

```
--yes

--force

--rollback-on-failure
```

Default:

```
rollback-on-failure = true
```

---

# 60. Update Strategy

Default:

```
rolling-per-integration
```

Do not take the whole MCP Hub offline because one integration is updating.

If GitHub update fails:

```
GitHub -> rollback
```

while:

```
Jira -> healthy
Figma -> healthy
Search -> healthy
```

---

# 61. Remote Integration Updates

For:

```
Figma
Jira
Notion
```

where the provider controls the remote MCP service:

DO NOT attempt Git-based updates.

Instead:

```
check_remote_health()
check_protocol()
refresh_tools()
```

Record:

```
provider_version if exposed
```

or:

```
protocol metadata
```

Remote provider updates are outside the hub's deployment lifecycle.

---

# 62. Configuration Locking

Use:

```
integrations.yaml
```

for desired state.

Use:

```
integrations.lock.yaml
```

for resolved state.

Example:

```
desired:
  branch: main

resolved:
  commit: abc123
```

This is similar to dependency lock files.

---

# 63. Desired State Reconciliation

Implement:

```
Reconciler
```

It compares:

```
desired configuration
```

against:

```
actual runtime state
```

Example:

```
desired:
  github: enabled

actual:
  github: disabled
```

Reconciler:

```
enable github
```

Another example:

```
desired:
  jira: disabled

actual:
  jira: enabled
```

Reconciler:

```
disable jira
```

Command:

```
mcp-hub reconcile
```

---

# 64. Admin API

Protect administrative endpoints separately from MCP tool access.

Admin endpoints:

```
/api/admin/install
/api/admin/update
/api/admin/remove
/api/admin/rollback
/api/admin/config
/api/admin/audit
```

Do not expose these operations to ordinary agents unless explicitly enabled.

---

# 65. Environment Variables

Create:

```
.env.example
```

Example:

```
MCP_HUB_ENV=development

DATABASE_URL=postgresql://...
REDIS_URL=redis://...

MCP_HUB_HOST=0.0.0.0
MCP_HUB_PORT=8000

MCP_AUTH_SECRET=

BRAVE_API_KEY=

LOG_LEVEL=INFO
```

Never commit actual secrets.

---

# 66. Development Commands

Provide:

```
make install
make dev
make test
make lint
make format
make typecheck
make inspector
make docker-up
make docker-down
make migrate
make seed
make health
```

---

# 67. MCP Inspector

The project must be testable with:

```
npx @modelcontextprotocol/inspector
```

The official Inspector supports connecting to MCP servers over transports including stdio, SSE and Streamable HTTP.

Document:

```
http://localhost:8000/mcp
```

as the development MCP endpoint.

---

# 68. README

Generate a complete README containing:

* architecture
* installation
* development
* Docker
* configuration
* authentication
* adding integrations
* updating integrations
* removing integrations
* rollback
* registry
* security
* MCP client configuration
* troubleshooting
* production deployment

Include examples for:

```
Claude
Cursor
VS Code
custom MCP clients
```

---

# 69. Definition of Done

The implementation is NOT complete until all of the following work.

## Basic

```
[ ] One MCP endpoint
[ ] Python implementation
[ ] Streamable HTTP
[ ] Authentication
[ ] PostgreSQL
[ ] Redis
[ ] Docker
```

## Integrations

```
[ ] Jira
[ ] Figma
[ ] GitHub
[ ] Brave Search
[ ] Notion
[ ] Filesystem
[ ] Git
[ ] Fetch
```

## Gateway

```
[ ] Dynamic tool discovery
[ ] Namespacing
[ ] Tool registry
[ ] Routing
[ ] Error isolation
```

## Lifecycle

```
[ ] install
[ ] update
[ ] update selected
[ ] update all
[ ] update all except
[ ] remove
[ ] disable
[ ] enable
[ ] rollback
[ ] dry-run
[ ] health
[ ] doctor
```

## Security

```
[ ] OAuth
[ ] secret storage
[ ] authorization
[ ] policy engine
[ ] confirmations
[ ] audit logs
[ ] sandboxing
[ ] rate limiting
[ ] supply-chain checks
```

## Reliability

```
[ ] health checks
[ ] automatic rollback
[ ] distributed locks
[ ] update jobs
[ ] retries
[ ] timeouts
[ ] graceful degradation
```

## Testing

```
[ ] unit tests
[ ] integration tests
[ ] security tests
[ ] update tests
[ ] rollback tests
[ ] E2E MCP tests
```

---

# 70. Critical Implementation Instruction

Do not create a fake implementation with placeholder methods such as:

```
pass

TODO

return None
```

for core functionality.

Implement the system incrementally but fully.

First implement:

1. MCP Gateway
2. Integration registry
3. Remote adapter
4. Jira
5. Figma
6. GitHub
7. Brave Search
8. lifecycle manager
9. update/rollback
10. authentication
11. policy engine
12. audit
13. Docker
14. tests

Then add:

15. Notion
16. MCP Registry
17. additional plugin integrations

If an upstream integration is remote-only, implement the remote proxy rather than trying to clone its implementation.

---

# 71. Final Expected Architecture

The finished system should look like:

```
                     AI AGENT
                        |
                        |
                 ONE MCP ENDPOINT
                        |
                        v
              +-------------------+
              |     MCP HUB       |
              +-------------------+
              | Auth              |
              | Policy            |
              | Tool Router       |
              | Tool Registry     |
              | Audit             |
              | Lifecycle Manager |
              +---------+---------+
                        |
         +--------------+--------------+
         |              |              |
         v              v              v
      REMOTE          LOCAL          CONTAINER
      MCP             MCP              MCP
         |              |              |
      Jira           GitHub          Search
      Figma          Filesystem      Other
      Notion         Git             Community

                |
                v
          PostgreSQL
                +
              Redis

                |
                v
          Registry Manager

                |
                v
          Update Manager
                |
      +---------+---------+
      |         |         |
   Install    Update    Rollback
```

The user/agent should never need to configure every individual MCP server.

The hub is the single control plane.

---

# 72. Most Important CLI Requirement

The final implementation MUST make these commands work:

```
mcp-hub update jira

mcp-hub update jira figma github

mcp-hub update --all

mcp-hub update --all --exclude jira

mcp-hub update --all --exclude jira figma

mcp-hub remove jira

mcp-hub remove jira figma

mcp-hub rollback jira

mcp-hub disable jira

mcp-hub enable jira

mcp-hub health

mcp-hub doctor

mcp-hub tools

mcp-hub registry search jira

mcp-hub reconcile
```

Every command must operate against the same underlying integration-management service used by the REST API.

Do not create separate business logic for CLI and API.

---

# 73. Development Priority

Implement in this order:

PHASE 1

* project scaffolding
* MCP Gateway
* Streamable HTTP
* integration registry
* remote MCP proxy
* tool discovery
* namespace router

PHASE 2

* Jira
* Figma
* GitHub
* Brave Search

PHASE 3

* lifecycle manager
* install
* update
* remove
* rollback
* health
* doctor

PHASE 4

* authentication
* OAuth
* secrets
* authorization
* policy engine
* audit

PHASE 5

* PostgreSQL
* Redis
* workers
* distributed locking
* job system

PHASE 6

* Docker
* security sandbox
* supply-chain validation

PHASE 7

* Notion
* MCP Registry
* dynamic plugin installation

PHASE 8

* dashboard/API
* observability
* metrics
* production hardening

Do not stop at a mock architecture. Build the actual working implementation.
