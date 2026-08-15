#!/usr/bin/env python3
"""Install integrations from the catalog (arch §9, §14).

    python scripts/install.py jira
    python scripts/install.py github figma
    python scripts/install.py filesystem --no-enable
    python scripts/install.py git --version v1.2.0

Community-tier sources run third-party code on this host, so they ask for an
explicit confirmation before anything is fetched (arch §7, §54); `--yes`
answers it for unattended runs. Official remote services install nothing
locally and ask nothing.

To install a server the catalog does not know about yet, search the public MCP
Registry instead: `mcp-hub registry search <query>` then `mcp-hub registry
install <server-name>` (arch §7, §56).

A wrapper around `mcp-hub install`, which calls `LifecycleService.install()` —
the same service the REST API calls (arch §72).
"""

from __future__ import annotations

from _bootstrap import delegate

if __name__ == "__main__":
    delegate("install")
