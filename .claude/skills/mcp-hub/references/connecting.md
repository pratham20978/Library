# Connecting

## The endpoint

```
http://localhost:8000/mcp        transport: Streamable HTTP
```

One endpoint per hub. Enabling a new integration changes what a connected client
sees without the client changing anything, which is the whole point — do not add
per-integration servers alongside it.

Other paths on the same port, for operators rather than agents:

| Path | Auth | Purpose |
|---|---|---|
| `/health` | none | Liveness. Checks nothing else on purpose. |
| `/ready` | none | Readiness — 503 when the database is unreachable. |
| `/api/status` | `integrations:read` | What the hub is serving right now. |
| `/api/tools` | `integrations:read` | Registered tools. |
| `/api/health` | `integrations:read` | Per-integration health. |
| `/api/...` | scope per route | Management REST API. |
| `/metrics` | none | Prometheus. |
| `/docs` | none | OpenAPI for the management API. |

## Token

If the hub requires authentication (it does everywhere except a local dev loop,
and `production` refuses to start without it), issue a token first:

```bash
mcp-hub token issue alice@example.com \
  --scope tools:call --scope integrations:read \
  --ttl 3600
```

The token is printed once and never stored — the hub keeps no copy to hand back.
Default TTL is one hour; a session that worked an hour ago and now fails with
`not_authenticated` needs a new token.

**The subject must be the caller's real identity**, not a team label. Per-user
upstream credentials are resolved by subject, so `--subject agent` for five
people gives all five whoever's credential was stored last.

### Scopes

| Scope | Grants | Give to an agent? |
|---|---|---|
| `tools:call` | Invoke integration tools through `/mcp`. | Yes |
| `integrations:read` | List integrations, health, tool metadata. | Yes |
| `tools:refresh` | Trigger rediscovery — reconnects upstreams, not free. | Only if it calls `hub.refresh_tools` |
| `integrations:write` | Install, update, enable, disable, remove, roll back. | No |
| `secrets:write` | Store or delete credentials (never read one back). | No |
| `audit:read` | Read the audit trail. | No |
| `admin` | Everything. | Never |

`--scope` defaults to the agent set: `tools:call` and `integrations:read`. That
separation exists so a token good enough to read a Jira issue cannot uninstall
an integration.

The header is the only accepted channel:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
```

Not a query parameter — those are logged by every proxy in the path.

## Client configuration

### Claude Code

```bash
claude mcp add --transport http mcp-hub http://localhost:8000/mcp \
  --header "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..."
```

`/mcp` inside Claude Code then lists the hub and its tools. Drop `--header` only
when the hub runs with `MCP_HUB_AUTH_REQUIRED=false`.

### Claude Desktop

Desktop launches MCP servers as local processes, so a remote hub is reached
through the `mcp-remote` bridge. Edit `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

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
      "env": { "MCP_HUB_TOKEN": "eyJhbGciOiJIUzI1NiIs..." }
    }
  }
}
```

Restart Desktop after editing. Desktop confirms destructive calls itself; the
hub's confirmation is a second, independent gate — expect both.

### Cursor

`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` globally:

```json
{
  "mcpServers": {
    "mcp-hub": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer eyJhbGciOiJIUzI1NiIs..." }
    }
  }
}
```

Transport is inferred from the URL. Settings → MCP shows a green indicator and
the tool list when it worked.

### VS Code

`.vscode/mcp.json`, with the token prompted rather than committed:

```json
{
  "inputs": [
    { "id": "mcp-hub-token", "type": "promptString",
      "description": "MCP Hub bearer token", "password": true }
  ],
  "servers": {
    "mcp-hub": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer ${input:mcp-hub-token}" }
    }
  }
}
```

Committing this file is safe: it holds no credential.

### A custom client

Anything speaking MCP over Streamable HTTP works. Minimal, unauthenticated:

```python
import asyncio

from mcp.client.client import Client


async def main() -> None:
    async with Client("http://localhost:8000/mcp") as client:
        listed = await client.list_tools()
        print([tool.name for tool in listed.tools])

        result = await client.call_tool("hub.list_integrations", {"enabled_only": True})
        for block in result.content:
            print(block)


asyncio.run(main())
```

With a token, and answering confirmations — the part custom clients get wrong,
because a client that cannot be asked is refused:

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

Declare elicitation support if the client will ever call a confirming tool. See
[confirmation.md](confirmation.md) for the wire-level exchange.

## Verifying a connection

```bash
curl -s http://localhost:8000/health        # {"status":"ok",...} — process is up
curl -s http://localhost:8000/ready         # database reachable
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/status
```

Then from the client: `hub.list_integrations`. If that answers, routing, auth
and policy are all working — anything still failing is about one integration,
not the connection.

## Connection failures

| Symptom | Cause | Fix |
|---|---|---|
| No tools at all | Client never reached the hub | `curl /health`; confirm URL, port and transport |
| Immediate 401 / `not_authenticated` | No token, expired token, token in the wrong place | Reissue; put it in `Authorization: Bearer` |
| `not_authorized` on a call that used to work | Token lacks the scope | Reissue with the scope the call needs |
| Works via curl, not from the client | Client sending no header, or using SSE | Use Streamable HTTP; check the header block |
| Auth suddenly unnecessary | `MCP_HUB_AUTH_REQUIRED=false` | Fine locally; per-user credentials are disabled in that mode |
