#!/usr/bin/env python3
"""Re-synchronise the tool registry with what the upstreams actually expose.

    python scripts/sync_registry.py
    python scripts/sync_registry.py --json

Three things happen, in order (arch §14, §51, §63):

1. `config/integrations.yaml` is re-read, so an operator who hand-edited it
   gets the change applied without restarting the hub.
2. Actual state is reconciled toward it — newly enabled integrations start,
   disabled ones stop routing. Each reconciles independently, so one failure is
   reported and the rest proceed.
3. Every enabled integration is re-queried and the tool registry (arch §51) is
   rebuilt from the answers, so tools an upstream added or withdrew are
   reflected in what the hub advertises.

This is the safe thing to run from cron, and the thing to run after editing
configuration by hand. It contacts upstreams but installs nothing and changes
no versions — use `scripts/update.py` for that.

Note this syncs the hub's *tool* registry. To search the public MCP Registry
at registry.modelcontextprotocol.io, use `mcp-hub registry search` (arch §7).

Exits 1 if any integration failed to reconcile.

A wrapper around `mcp-hub sync`, which calls `HubRuntime.reload_configuration()`
and `LifecycleService.reconcile()`/`.refresh()` — the same services the REST API
calls (arch §72).
"""

from __future__ import annotations

from _bootstrap import delegate

if __name__ == "__main__":
    delegate("sync")
