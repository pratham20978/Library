"""Outbound request guard — the SSRF surface (arch §31).

A model can choose a URL. `fetch` exists precisely so it can. That makes every
outbound request a place where an agent could aim the hub at something only the
hub can reach: a cloud metadata endpoint, a database on the private network, a
service bound to loopback.

Every case here uses an address literal rather than a hostname, so the suite
asserts the guard's logic and never depends on DNS or the network.
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationFailed
from app.integrations.netguard import assert_public_host, guard_url, is_loopback_host

pytestmark = pytest.mark.security


# ------------------------------------------------------------------- blocked ranges


@pytest.mark.parametrize(
    ("address", "why"),
    [
        ("10.0.0.1", "RFC1918 private"),
        ("172.16.0.1", "RFC1918 private"),
        ("192.168.1.1", "RFC1918 private"),
        ("169.254.169.254", "cloud instance metadata"),
        ("169.254.170.2", "ECS task metadata"),
        ("127.0.0.1", "loopback by address"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("240.0.0.1", "reserved"),
        ("[::1]", "IPv6 loopback"),
        ("[fe80::1]", "IPv6 link-local"),
        ("[fc00::1]", "IPv6 unique-local"),
        ("[::ffff:169.254.169.254]", "IPv4-mapped metadata endpoint"),
    ],
)
def test_private_and_reserved_targets_are_refused(address: str, why: str) -> None:
    """The whole point: an agent-chosen URL cannot reach internal infrastructure."""
    with pytest.raises(ValidationFailed) as caught:
        guard_url(f"https://{address}/latest/meta-data/", allow_loopback=False)

    assert "not publicly routable" in str(caught.value) or "loopback" in str(caught.value), why


def test_metadata_endpoint_is_refused_even_over_plain_http() -> None:
    """The classic SSRF payload, in the form it is actually written."""
    with pytest.raises(ValidationFailed):
        guard_url("http://169.254.169.254/latest/meta-data/iam/security-credentials/")


def test_assert_public_host_refuses_a_private_literal() -> None:
    with pytest.raises(ValidationFailed) as caught:
        assert_public_host("10.1.2.3")

    assert "10.1.2.3" in str(caught.value)
    assert "arch §31" in str(caught.value), "the refusal must point at the rule it enforces"


# ------------------------------------------------------------------------- schemes


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://example.com/", "ftp://example.com/x"])
def test_non_http_schemes_are_refused(url: str) -> None:
    """`file://` is the other half of the SSRF classic."""
    with pytest.raises(ValidationFailed) as caught:
        guard_url(url)

    assert "scheme" in str(caught.value)


def test_plaintext_http_is_refused_for_remote_hosts() -> None:
    """Arch §31: a remote MCP endpoint carries a credential, so it needs TLS."""
    with pytest.raises(ValidationFailed) as caught:
        guard_url("http://mcp.example.com/mcp", check_dns=False)

    assert "HTTPS" in str(caught.value)


def test_a_url_without_a_host_is_refused() -> None:
    with pytest.raises(ValidationFailed):
        guard_url("https:///mcp")


# ------------------------------------------------------------------------- loopback


def test_loopback_is_permitted_only_when_asked_for() -> None:
    """Tests and local development need it; a model-supplied URL must not have it."""
    assert guard_url("http://localhost:8080/mcp", allow_loopback=True)

    with pytest.raises(ValidationFailed):
        guard_url("http://localhost:8080/mcp", allow_loopback=False)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "host.docker.internal", "LOCALHOST"])
def test_loopback_names_are_recognised(host: str) -> None:
    """`host.docker.internal` is loopback from inside a container, and is the one
    people forget."""
    assert is_loopback_host(host)


def test_ordinary_hosts_are_not_loopback() -> None:
    assert not is_loopback_host("mcp.atlassian.com")


# -------------------------------------------------------------------- host allowlist


def test_allowlist_admits_only_the_named_host() -> None:
    """A manifest's `allowed_hosts` pins an integration to its own vendor."""
    assert guard_url("https://mcp.atlassian.com/v1/mcp", allowed_hosts=["mcp.atlassian.com"], check_dns=False)

    with pytest.raises(ValidationFailed) as caught:
        guard_url("https://evil.example.com/v1/mcp", allowed_hosts=["mcp.atlassian.com"], check_dns=False)

    assert "not in the allowed-host list" in str(caught.value)


def test_allowlist_suffix_entries_match_subdomains_only() -> None:
    """`.atlassian.com` admits `api.atlassian.com`, not `atlassian.com.evil.net`."""
    assert guard_url("https://api.atlassian.com/mcp", allowed_hosts=[".atlassian.com"], check_dns=False)

    with pytest.raises(ValidationFailed):
        guard_url("https://atlassian.com.evil.net/mcp", allowed_hosts=[".atlassian.com"], check_dns=False)


def test_allowlist_is_case_insensitive() -> None:
    """Hostnames are, so the allowlist must be, or the check is bypassable by shift key."""
    assert guard_url("https://MCP.Atlassian.COM/mcp", allowed_hosts=["mcp.atlassian.com"], check_dns=False)


def test_no_allowlist_means_no_host_restriction() -> None:
    """Absent is not empty: an integration without `allowed_hosts` is only
    restricted by the private-range check, not pinned to nothing."""
    assert guard_url("https://mcp.example.com/mcp", check_dns=False)
