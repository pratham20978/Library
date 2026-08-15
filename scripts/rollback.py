#!/usr/bin/env python3
"""Restore an integration's previous version (arch §9, §19).

    python scripts/rollback.py jira
    python scripts/rollback.py jira --list
    python scripts/rollback.py jira --version 1.4.2

With no `--version` this restores the most recent rollback point. `--list`
shows what is available and changes nothing.

Updates never delete what they replace, so this is an atomic rename back to a
directory that is still on disk rather than a re-download (arch §19). Secrets
are not part of a rollback point: a restored version re-reads whatever is in
the secret store today, because plaintext credentials are never written to a
backup in the first place.

Exits 1 if the restored version comes back unable to serve — that is the case
that needs a human, and it should fail a deployment.

A wrapper around `mcp-hub rollback`, which calls `LifecycleService.rollback()`
— the same service the REST API calls (arch §72).
"""

from __future__ import annotations

from _bootstrap import delegate

if __name__ == "__main__":
    delegate("rollback")
