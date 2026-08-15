#!/usr/bin/env python3
"""Probe integrations and report their status (arch §9, §25).

    python scripts/health.py
    python scripts/health.py jira figma
    python scripts/health.py --json

Built to be a deployment gate and a monitoring probe, so the exit code is the
contract: 0 when nothing is UNAVAILABLE, 1 when something is. DEGRADED and
AUTH_REQUIRED are reported without failing the command — the hub is still
serving, and one upstream needing a credential must not block a rollout
(arch §45).

`--json` prints the same payload the REST `/health/integrations` endpoint
returns, so a check written against one works against the other.

For a wider sweep that also validates configuration, secrets, connectivity and
version drift, use `mcp-hub doctor` (arch §36).

A wrapper around `mcp-hub health`, which calls `LifecycleService.health()` —
the same service the REST API calls (arch §72).
"""

from __future__ import annotations

from _bootstrap import delegate

if __name__ == "__main__":
    delegate("health")
