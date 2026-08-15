#!/usr/bin/env python3
"""Update integrations (arch §57).

    python scripts/update.py jira
    python scripts/update.py github figma
    python scripts/update.py --all
    python scripts/update.py --all --exclude jira
    python scripts/update.py --all --exclude jira figma

Every version stages into its own directory and must start and list its tools
before anything is promoted, so a bad release never takes the current one down
(arch §15). A failure after promotion rolls back automatically.

This is a wrapper around `mcp-hub update`, which calls `UpdateManager.update()`
— the same service the REST API calls (arch §57: "Do not duplicate update
logic"). Every CLI flag works here, including `--dry-run`, `--yes`, `--force`,
`--parallel` and `--json`; `--help` lists them.

Exits 0 when every update succeeded, 1 when any failed, 130 if the
confirmation was declined.
"""

from __future__ import annotations

from _bootstrap import delegate

if __name__ == "__main__":
    delegate("update")
