"""Redaction for anything that might carry a secret to a log, audit row, or wire.

Arch §21 and §24 are absolute: tokens never reach logs, audit records, or the
model. Two independent mechanisms enforce that, because either alone leaks.

`Secret` makes a value *unprintable* — the wrapper survives `repr`, f-strings,
`json.dumps` via structlog, and traceback rendering without disclosing what it
holds. Reading it is an explicit, greppable `.reveal()` call.

`redact_arguments` handles the values the hub never wrapped because they came
from an agent: tool arguments are attacker-influenced and may contain anything.
It is name-driven and deliberately over-eager — a redacted non-secret costs an
operator one debugging step; a logged secret costs a rotation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

__all__ = [
    "REDACTED",
    "Secret",
    "fingerprint",
    "looks_sensitive",
    "redact_arguments",
    "redact_mapping",
    "redact_url",
]

REDACTED: Final = "[REDACTED]"
"""The single placeholder. Constant so audit consumers can match on it."""

_MAX_DEPTH: Final = 8
"""Deeper structures are summarised rather than walked; bounds pathological input."""

_MAX_STRING: Final = 512
"""Retained strings longer than this are truncated, so one argument cannot flood a log."""

_SENSITIVE_NAME: Final = re.compile(
    r"(?:^|[_\-.])(?:"
    r"secret|token|password|passwd|pwd|credential|creds?|"
    r"api[_\-.]?key|access[_\-.]?key|private[_\-.]?key|secret[_\-.]?key|"
    r"auth|authorization|bearer|session|cookie|signature|salt|nonce|otp|pin"
    r")(?:$|[_\-.])",
    re.IGNORECASE,
)
"""Matches a *whole word* inside a key name, so `token` and `api_key` hit while
`tokenizer_config` and `keyboard` do not."""

_BARE_SENSITIVE: Final = frozenset(
    {
        "secret",
        "token",
        "password",
        "passwd",
        "pwd",
        "credential",
        "credentials",
        "cred",
        "creds",
        "apikey",
        "accesskey",
        "privatekey",
        "secretkey",
        "auth",
        "authorization",
        "bearer",
        "session",
        "cookie",
        "signature",
        "salt",
        "nonce",
        "otp",
        "pin",
        "key",
    }
)
"""Names that are sensitive standing alone. `key` is here but not in the regex:
bare `key` is a credential, while `sort_key` and `cache_key` are not."""


class Secret:
    """A string that refuses to render itself.

    Wrap every credential the moment it leaves its provider and unwrap it only
    at the point of use. The wrapper defeats accidental disclosure through
    `repr`, `str`, formatting, and logging — the paths that actually leak in
    practice.

    Equality is constant-time and only defined against another `Secret`, so a
    comparison cannot be used as an oracle and a stray `==` against a plain
    string is always `False` rather than a timing side channel.

        >>> s = Secret("hunter2")
        >>> f"{s}"
        '[REDACTED]'
        >>> s.reveal()
        'hunter2'
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the underlying value. Every call site should be auditable."""
        return self._value

    def fingerprint(self) -> str:
        """A stable, non-reversible tag for this value.

        Safe to log and to use as a cache/pool key: it identifies *which*
        credential is in play without disclosing it.
        """
        return fingerprint(self._value)

    @property
    def is_empty(self) -> bool:
        """Whether the wrapped value is blank — an unset secret in disguise."""
        return not self._value.strip()

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        from hmac import compare_digest

        return compare_digest(self._value, other._value)

    def __hash__(self) -> int:
        return hash(self.fingerprint())


def fingerprint(value: str, *, length: int = 12) -> str:
    """Return a short, non-reversible tag identifying `value`.

    Used as a session-pool key so two principals with different credentials can
    never share an upstream session, and so logs can say *which* credential was
    used without saying what it is.
    """
    from hashlib import blake2b

    return blake2b(value.encode("utf-8"), digest_size=16).hexdigest()[:length]


def looks_sensitive(name: str) -> bool:
    """Whether a field named `name` should be redacted on sight."""
    lowered = name.strip().lower()
    if lowered.replace("_", "").replace("-", "").replace(".", "") in _BARE_SENSITIVE:
        return True
    return _SENSITIVE_NAME.search(lowered) is not None


def redact_url(url: str) -> str:
    """Strip userinfo and query values from a URL so it is safe to log.

    `https://user:pw@host/path?token=abc` becomes
    `https://host/path?token=[REDACTED]`. Query *keys* are kept because they
    are useful and are not secrets; their values are dropped unless the key is
    plainly benign.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    query = urlencode(
        [(k, v if not looks_sensitive(k) else REDACTED) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def redact_arguments(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Render tool-call arguments as audit-safe metadata (arch §24).

    Sensitive-looking keys collapse to `[REDACTED]`. Everything else is kept
    but bounded: long strings are truncated and deep structures summarised, so
    an audit row stays a row. `Secret` values are always redacted regardless of
    their key.
    """
    if not arguments:
        return {}
    return {key: _redact_value(key, value, depth=0) for key, value in arguments.items()}


def redact_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Redact an arbitrary mapping — headers, env, manifest fragments."""
    return redact_arguments(mapping)


def _redact_value(key: str, value: Any, *, depth: int) -> Any:
    if isinstance(value, Secret):
        return REDACTED
    if looks_sensitive(key):
        return REDACTED
    if depth >= _MAX_DEPTH:
        return f"<{type(value).__name__}>"
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + f"…(+{len(value) - _MAX_STRING})"
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _redact_value(str(k), v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, str | bytes):  # bytes: length only, contents unknown
        return f"<bytes len={len(value)}>"
    if isinstance(value, Sequence | set | frozenset):
        items = list(value)
        head = [_redact_value(key, item, depth=depth + 1) for item in items[:20]]
        if len(items) > 20:
            head.append(f"…(+{len(items) - 20} more)")
        return head
    if isinstance(value, Iterable):
        return f"<{type(value).__name__}>"
    return f"<{type(value).__name__}>"
