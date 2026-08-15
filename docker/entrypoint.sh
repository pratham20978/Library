#!/bin/sh
# Container entrypoint (arch §43).
#
# Deliberately small. It does one optional thing — apply migrations — and then
# gets out of the way, so `docker run mcp-hub <anything>` still works:
#
#   docker compose exec mcp-hub mcp-hub list
#   docker compose exec mcp-hub mcp-hub update jira --dry-run
#   docker compose run --rm mcp-hub alembic upgrade head
#
# `set -e` matters: if migrations fail the container must not start serving
# against a schema it does not understand.
set -eu

# Off by default. Compose turns it on for local work, where "up and running"
# beats ceremony. In production the schema is a deploy step someone reviews and
# can roll back, not a side effect of a pod restarting (arch §27).
if [ "${MCP_HUB_AUTO_MIGRATE:-false}" = "true" ]; then
    echo "entrypoint: applying database migrations" >&2
    alembic upgrade head
fi

exec "$@"
