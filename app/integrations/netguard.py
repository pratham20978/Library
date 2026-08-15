"""Outbound request guarding (arch §31).

The hub makes HTTP requests to addresses that ultimately come from
configuration, a registry response, or a redirect — none of which it fully
controls. Left unguarded that is a server-side request forgery primitive
pointing straight at whatever the hub can reach and an agent cannot: the
container network, the cloud metadata endpoint, an internal admin API.

Two checks, applied before a connection is made:

* **Scheme and host.** HTTPS only, and the host must be one the caller expects.
  Loopback is permitted so tests and local development work without disabling
  the guard wholesale.
* **Resolved address.** The hostname is resolved and *every* returned address is
  checked against private, loopback, link-local, and reserved ranges. Checking
  every address matters — a hostname that resolves to one public and one private
  address would otherwise pass on the public one and connect to the private one.

This closes the DNS-name path, not the DNS-rebinding one: an attacker who
controls a name can answer differently between this check and the connection.
Pinning the checked address is possible only by taking over connection
establishment; against a *configured* endpoint list the remaining window is not
worth that complexity, and the allowed-host list is the real control.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from app.core.errors import ValidationFailed
from app.core.logging import get_logger
from app.core.redaction import redact_url

__all__ = ["assert_public_host", "guard_url", "is_loopback_host"]

log = get_logger(__name__)

# Names that mean "this machine". These are refused as request targets, not
# bound to — S104 is about binding, which nothing here does.
_LOOPBACK_NAMES = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}  # noqa: S104
)


def is_loopback_host(host: str) -> bool:
    """Whether `host` names the local machine."""
    return host.lower() in _LOOPBACK_NAMES


def guard_url(
    url: str,
    *,
    allowed_hosts: Sequence[str] = (),
    require_https: bool = True,
    allow_loopback: bool = True,
    check_dns: bool = True,
) -> str:
    """Validate an outbound URL, returning it unchanged if acceptable.

    Args:
        url: The URL about to be requested.
        allowed_hosts: Hosts permitted in addition to the URL's own. An entry
            starting with `.` matches any subdomain of that suffix.
        require_https: Refuse plaintext HTTP for non-loopback hosts.
        allow_loopback: Permit `localhost` and friends. Tests need this.
        check_dns: Resolve and reject private/reserved addresses.

    Returns:
        The URL, unchanged.

    Raises:
        ValidationFailed: The URL is malformed, uses a refused scheme, names a
            host outside the allowlist, or resolves into a private range.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ValidationFailed(f"Malformed URL: {exc}") from exc

    if parts.scheme not in ("http", "https"):
        raise ValidationFailed(
            f"Refusing scheme {parts.scheme!r}; only http and https are allowed.",
            url=redact_url(url),
        )

    host = parts.hostname
    if not host:
        raise ValidationFailed("URL has no host.", url=redact_url(url))

    loopback = is_loopback_host(host)
    if loopback:
        if not allow_loopback:
            raise ValidationFailed("Refusing a loopback address for this request.", host=host)
        return url

    if require_https and parts.scheme != "https":
        raise ValidationFailed("Remote MCP endpoints must use HTTPS (arch §31).", url=redact_url(url), host=host)

    if allowed_hosts and not _host_allowed(host, allowed_hosts):
        raise ValidationFailed(
            f"Host {host!r} is not in the allowed-host list for this integration.",
            host=host,
            allowed=list(allowed_hosts),
        )

    if check_dns:
        assert_public_host(host)
    return url


def _host_allowed(host: str, allowed: Iterable[str]) -> bool:
    """Match a host against the allowlist, honouring `.suffix` wildcards."""
    lowered = host.lower()
    for entry in allowed:
        candidate = entry.lower().strip()
        if not candidate:
            continue
        if candidate.startswith("."):
            if lowered == candidate[1:] or lowered.endswith(candidate):
                return True
        elif lowered == candidate:
            return True
    return False


def assert_public_host(host: str) -> None:
    """Resolve `host` and refuse any private or reserved address.

    Raises:
        ValidationFailed: Resolution failed, or an address is not publicly
            routable.
    """
    try:
        resolved = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValidationFailed(f"Could not resolve host {host!r}: {exc}", host=host) from exc

    addresses = {info[4][0] for info in resolved}
    if not addresses:
        raise ValidationFailed(f"Host {host!r} resolved to no addresses.", host=host)

    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:  # pragma: no cover - getaddrinfo returns valid literals
            raise ValidationFailed(f"Host {host!r} resolved to an unparsable address.", host=host) from None
        if _is_blocked(address):
            log.warning("netguard.blocked", host=host, address=raw)
            raise ValidationFailed(
                f"Host {host!r} resolves to {raw}, which is not publicly routable. "
                "Refusing the request to prevent access to internal services (arch §31).",
                host=host,
                address=raw,
            )


def _is_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address points somewhere the hub must not be steered."""
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return True
    # 169.254.169.254 is already link-local, but IPv6 metadata endpoints and
    # NAT64-mapped v4 are not, so map v6 back and re-check.
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped or (
            ipaddress.IPv4Address(int(address) & 0xFFFFFFFF) if address.sixtofour else None
        )
        if mapped is not None and _is_blocked(mapped):
            return True
    return False
