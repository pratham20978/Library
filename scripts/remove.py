#!/usr/bin/env python3
"""Remove integrations (arch §58).

    python scripts/remove.py jira
    python scripts/remove.py jira figma
    python scripts/remove.py --all --exclude github

Removal asks for confirmation unless `--yes` is passed (arch §58). Routing
stops first, then sessions close, then a configuration backup is taken, then
artifacts and this integration's own credentials are deleted — credentials
shared with another integration are never touched (arch §18).

Rollback points survive unless `--purge-backups` is given, so a removal stays
reversible for a while. `--keep-secrets` leaves the credentials in place.

A wrapper around `mcp-hub remove`, which calls `LifecycleService.remove()` —
the same service the REST API calls (arch §72).
"""

from __future__ import annotations

from _bootstrap import delegate

if __name__ == "__main__":
    delegate("remove")
