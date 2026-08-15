"""Structured logging (arch §44).

One `configure_logging` call at startup wires structlog and the stdlib together
so that a library's `logging.getLogger(...).warning(...)` and the hub's own
`get_logger().warning(...)` come out in the same shape, through the same
processors, with the same redaction applied.

Two processors here are load-bearing rather than cosmetic:

* `_bind_request` stamps every line with the active request/principal, so no
  call site has to remember to pass them, and none can forget.
* `_redact_event` is the last line of defence for arch §21 — it walks the event
  dict and blanks anything that looks like a credential, catching values that
  reached a log without ever being wrapped in `Secret`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Final

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from app.core.context import current_request
from app.core.redaction import REDACTED, Secret, looks_sensitive

__all__ = ["configure_logging", "get_logger"]

_RESERVED: Final = frozenset({"event", "level", "logger", "timestamp", "exception", "exc_info", "stack"})
"""Keys the renderer owns; redaction leaves them alone so tracebacks survive."""

_configured = False


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Safe to call at import time."""
    return structlog.stdlib.get_logger(name)


def _bind_request(_logger: WrappedLogger, _method: str, event: EventDict) -> EventDict:
    """Stamp the active request context onto every record."""
    context = current_request()
    event.setdefault("request_id", context.request_id)
    if context.trace_id:
        event.setdefault("trace_id", context.trace_id)
    if context.principal.is_authenticated:
        event.setdefault("principal", context.principal.subject)
    event.setdefault("source", context.source)
    return event


def _redact_event(_logger: WrappedLogger, _method: str, event: EventDict) -> EventDict:
    """Blank credential-shaped values anywhere in the record.

    Runs last so it also covers keys bound far upstream by
    `structlog.contextvars` or added by another processor.
    """
    for key in list(event.keys()):
        if key in _RESERVED:
            continue
        value = event[key]
        if isinstance(value, Secret) or looks_sensitive(str(key)):
            event[key] = REDACTED
        elif isinstance(value, MutableMapping):
            event[key] = _redact_nested(value, depth=0)
    return event


def _redact_nested(value: Any, *, depth: int) -> Any:
    if depth > 6:
        return f"<{type(value).__name__}>"
    if isinstance(value, Secret):
        return REDACTED
    if isinstance(value, MutableMapping):
        return {
            k: (REDACTED if looks_sensitive(str(k)) else _redact_nested(v, depth=depth + 1)) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_nested(item, depth=depth + 1) for item in value]
    return value


def configure_logging(*, level: str = "INFO", json_output: bool = True, force: bool = False) -> None:
    """Configure structlog and the stdlib root logger.

    Idempotent by default: repeated calls (tests, a re-entered CLI) are ignored
    unless `force` is set, so a second call cannot quietly downgrade a
    deliberately configured level.

    Args:
        level: Root log level name.
        json_output: JSON lines for production; a coloured console renderer for
            local development.
        force: Reconfigure even if already configured.
    """
    global _configured
    if _configured and not force:
        return

    numeric = getattr(logging, level.upper(), logging.INFO)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _bind_request,
        _redact_event,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, mcp) through the same pipeline.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric)

    # These are chatty at INFO and say nothing the hub's own records do not.
    for noisy, noisy_level in (
        ("uvicorn.access", logging.WARNING),
        ("httpx", logging.WARNING),
        ("httpx2", logging.WARNING),
        ("sqlalchemy.engine", logging.WARNING),
        ("asyncio", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(noisy_level)

    _configured = True
