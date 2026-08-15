"""MCP Hub — one MCP endpoint, many governed integrations.

The hub is a proxy aggregator: an agent connects to a single Streamable HTTP
endpoint and sees every enabled integration's tools under a namespace, while the
hub handles authentication, authorization, policy, credentials, health, and the
lifecycle of the upstream servers behind it.

Layering, bottom up. Each layer may import from the ones above it in this list
and never the reverse:

    core        errors, domain vocabulary, redaction, request context, logging
    config      the YAML contract: catalog, manifests, lock file, policies
    database    persistent schema and sessions
    secrets     credential resolution (arch §20, §21)
    audit       the audit trail (arch §24)
    policy      risk classification, rate limits, decisions (arch §22, §23)
    integrations  adapters per source kind, sandboxing, lifecycle (arch §8, §48)
    registry    the official MCP Registry client (arch §7)
    gateway     tool registry, discovery, session pool, routing (arch §11-§13)
    server      the single MCP endpoint (arch §10)
    api / cli   REST and command-line surfaces, both over one service layer
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
