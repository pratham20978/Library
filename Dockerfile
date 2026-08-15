# MCP Hub image (arch §43).
#
# Multi-stage: dependencies resolve from `uv.lock` in a builder that never ships,
# and the runtime carries only the virtualenv plus the toolchains that local
# integrations actually need.
#
# Toolchains, and why they are here:
#   git   — git-sourced integrations are cloned and built (arch §8)
#   node  — npm-sourced servers, and git-sourced servers that are Node projects
#   uv    — python-sourced servers install into their own isolated environment
#
# Deliberately absent: the Docker CLI. Docker-sourced integrations need a
# container runtime, and giving this container one means mounting the host's
# socket — root on the host, from inside the hub. If you need them, build with
# `--build-arg WITH_DOCKER_CLI=1` and read the note in docker-compose.yml first.
#
#   docker build -t mcp-hub .
#   docker run --rm -p 8000:8000 --env-file .env mcp-hub

# Pinned deliberately. Bumping these is a reviewed change, not a rebuild
# side effect (arch §55). PYTHON_VERSION tracks `.python-version`.
ARG PYTHON_VERSION=3.14
ARG NODE_VERSION=22
ARG UV_VERSION=0.11.26

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM node:${NODE_VERSION}-bookworm-slim AS node


# --------------------------------------------------------------------- builder

FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/venv

WORKDIR /src

# Dependencies first, from the lockfile, in their own layer: application edits
# do not invalidate it, and `--locked` fails rather than silently resolving
# something new (arch §55).
COPY pyproject.toml uv.lock README.md ./
RUN uv venv "${VIRTUAL_ENV}" \
 && uv sync --locked --no-dev --no-install-project

# Then the application itself.
COPY app ./app
RUN uv sync --locked --no-dev


# --------------------------------------------------------------------- runtime

FROM python:${PYTHON_VERSION}-slim AS runtime

ARG WITH_DOCKER_CLI=0

LABEL org.opencontainers.image.title="MCP Hub" \
      org.opencontainers.image.description="One MCP endpoint fronting many governed integrations." \
      org.opencontainers.image.source="https://github.com/modelcontextprotocol" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        tini \
 && if [ "${WITH_DOCKER_CLI}" = "1" ]; then apt-get install -y --no-install-recommends docker.io; fi \
 && rm -rf /var/lib/apt/lists/*

# Node without the full node image: the binary and npm's own package, linked in.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY --from=uv /uv /uvx /usr/local/bin/

# Nothing here runs as root: an integration is third-party code, and this
# process launches it (arch §31, §54).
RUN groupadd --system --gid 10001 mcphub \
 && useradd --system --uid 10001 --gid mcphub --create-home --shell /usr/sbin/nologin mcphub

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HUB_CONFIG_DIR=/app/config \
    MCP_HUB_RUNTIME_DIR=/app/runtime \
    MCP_HUB_HOST=0.0.0.0 \
    MCP_HUB_PORT=8000 \
    npm_config_cache=/app/runtime/cache/npm \
    UV_CACHE_DIR=/app/runtime/cache/uv \
    HOME=/home/mcphub

COPY --from=builder --chown=mcphub:mcphub /opt/venv /opt/venv

WORKDIR /app
COPY --chown=mcphub:mcphub app ./app
COPY --chown=mcphub:mcphub config ./config
COPY --chown=mcphub:mcphub scripts ./scripts
COPY --chown=mcphub:mcphub alembic.ini pyproject.toml README.md ./
COPY --chown=mcphub:mcphub docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /app/runtime/integrations /app/runtime/backups /app/runtime/cache /app/runtime/logs \
 && chown -R mcphub:mcphub /app/runtime

USER mcphub

EXPOSE 8000

# Liveness only — `/health` answers while integrations are still starting, which
# is the point: one unreachable upstream must not restart the hub (arch §45).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${MCP_HUB_PORT}/health" || exit 1

# tini reaps the MCP server subprocesses this container spawns; without a real
# init they would accumulate as zombies (arch §29).
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["mcp-hub", "serve"]
