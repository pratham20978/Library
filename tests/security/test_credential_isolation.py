"""Per-user credentials must not cross a user boundary (arch §20, §21).

This is the single most consequential invariant in the hub. Jira, GitHub and
Notion all enforce the *calling user's* own permissions, so if the hub sends
Alice's token for Bob's request, Bob silently gains Alice's access — and nothing
upstream can detect it, because from Atlassian's side it simply is Alice.

The mock upstream records the `Authorization` header it received, so these tests
assert on the credential that actually went out on the wire rather than on what
the resolver intended. No credential here is real (arch §42).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.auth.permissions import AGENT_SCOPES
from app.auth.tokens import TokenIssuer
from app.config.models import AuthSpec, SecretRef
from app.config.settings import Settings
from app.core.redaction import Secret, fingerprint
from app.database.session import Database
from app.secrets.manager import SecretManager
from app.secrets.providers import EncryptedDatabaseProvider
from tests.conftest import BuildRuntime, WriteConfig
from tests.fixtures.hub_server import agent_client, serve_hub
from tests.fixtures.mock_servers import build_mock_jira, serve_mcp

pytestmark = [pytest.mark.security, pytest.mark.e2e]

ALICE = "alice@example.com"
BOB = "bob@example.com"
ALICE_TOKEN = "atlassian-token-belonging-to-alice"
BOB_TOKEN = "atlassian-token-belonging-to-bob"

PER_USER_MANIFEST = {
    "name": "Mock Jira",
    "namespace": "jira",
    "trust": "remote_official",
    "auth": {
        "type": "bearer",
        "secret": {"name": "ATLASSIAN_TOKEN", "per_user": True, "required": False},
    },
}


async def test_each_caller_reaches_the_upstream_with_their_own_credential(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """The end-to-end claim, asserted on the wire."""
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={"jira": {**PER_USER_MANIFEST, "source": {"type": "remote", "endpoint": upstream.url}}},
            catalog={"jira": True},
            exposure_mode="full",
            require_authentication=True,
        )
        hub = await build_runtime(discover=False)

        try:
            await hub.secrets.store("ATLASSIAN_TOKEN", Secret(ALICE_TOKEN), principal=ALICE)
            await hub.secrets.store("ATLASSIAN_TOKEN", Secret(BOB_TOKEN), principal=BOB)
            await hub.discoverer.discover_all()

            issuer = _issuer(settings)
            async with serve_hub(hub) as hub_url:
                async with agent_client(hub_url, token=issuer(ALICE)) as alice:
                    result = await alice.call_tool("jira.getJiraIssue", {"key": "HUB-1"})
                    assert not result.is_error

                assert state.calls[-1].authorization == f"Bearer {ALICE_TOKEN}"

                async with agent_client(hub_url, token=issuer(BOB)) as bob:
                    result = await bob.call_tool("jira.getJiraIssue", {"key": "HUB-2"})
                    assert not result.is_error

                assert state.calls[-1].authorization == f"Bearer {BOB_TOKEN}", (
                    "Bob's request must not have carried Alice's credential"
                )

            sent = {call.authorization for call in state.calls}
            assert sent == {f"Bearer {ALICE_TOKEN}", f"Bearer {BOB_TOKEN}"}
        finally:
            await hub.stop()


async def test_a_caller_without_a_credential_does_not_borrow_another_users(
    settings: Settings, write_config: WriteConfig, build_runtime: BuildRuntime
) -> None:
    """The failure mode worth naming: falling back to *somebody's* token.

    A missing optional credential must produce an anonymous upstream call, never
    a lookup that happens to find the first stored value.
    """
    server, state = build_mock_jira()

    async with serve_mcp(server, state) as upstream:
        write_config(
            manifests={"jira": {**PER_USER_MANIFEST, "source": {"type": "remote", "endpoint": upstream.url}}},
            catalog={"jira": True},
            exposure_mode="full",
            require_authentication=True,
        )
        hub = await build_runtime(discover=False)

        try:
            await hub.secrets.store("ATLASSIAN_TOKEN", Secret(ALICE_TOKEN), principal=ALICE)
            await hub.discoverer.discover_all()

            issuer = _issuer(settings)
            async with serve_hub(hub) as hub_url, agent_client(hub_url, token=issuer(BOB)) as bob:
                await bob.call_tool("jira.getJiraIssue", {"key": "HUB-3"})

            assert state.calls, "the call still reaches the upstream; the upstream decides"
            assert ALICE_TOKEN not in (state.calls[-1].authorization or ""), (
                "Bob must never be served Alice's credential"
            )
        finally:
            await hub.stop()


# --------------------------------------------------------------------------- resolver


async def test_a_per_user_secret_is_invisible_to_another_principal(settings: Settings) -> None:
    """Storage-level isolation, without a hub in the way."""
    manager = await _standalone_manager(settings)
    await manager.store("ATLASSIAN_TOKEN", Secret(ALICE_TOKEN), principal=ALICE)

    for_alice = await manager.resolve(_ref(per_user=True), principal=ALICE)
    for_bob = await manager.resolve(_ref(per_user=True), principal=BOB)

    assert for_alice is not None
    assert for_alice.reveal() == ALICE_TOKEN
    assert for_bob is None, "a per-user lookup must not fall through to another user's value"


async def test_a_per_user_secret_is_invisible_to_an_anonymous_caller(settings: Settings) -> None:
    """Turning authentication off must not turn every credential into a shared one."""
    manager = await _standalone_manager(settings)
    await manager.store("ATLASSIAN_TOKEN", Secret(ALICE_TOKEN), principal=ALICE)

    assert await manager.resolve(_ref(per_user=True), principal=None) is None


async def test_a_shared_secret_serves_every_caller(settings: Settings) -> None:
    """The other half of the contract: `per_user: false` is deployment-wide.

    Brave's key is billed to the deployment and is meant to be shared; isolating
    it would break the integration rather than protect anyone.
    """
    manager = await _standalone_manager(settings)
    await manager.store("BRAVE_API_KEY", Secret("shared-brave-key"))

    ref = _ref(name="BRAVE_API_KEY", per_user=False)
    for principal in (ALICE, BOB, None):
        resolved = await manager.resolve(ref, principal=principal)
        assert resolved is not None
        assert resolved.reveal() == "shared-brave-key"


async def test_session_identities_differ_per_principal(settings: Settings) -> None:
    """The session-pool key must separate users, or one user's upstream session
    serves another's request — and MCP sessions carry upstream state."""
    manager = await _standalone_manager(settings)
    await manager.store("ATLASSIAN_TOKEN", Secret(ALICE_TOKEN), principal=ALICE)
    await manager.store("ATLASSIAN_TOKEN", Secret(BOB_TOKEN), principal=BOB)

    auth = _bearer()

    alice = await manager.resolve_auth(auth, principal=ALICE)
    bob = await manager.resolve_auth(auth, principal=BOB)

    assert alice.identity != bob.identity
    assert alice.principal == ALICE
    assert bob.principal == BOB


async def test_two_users_holding_the_same_token_still_get_separate_sessions(
    settings: Settings,
) -> None:
    """Sharing a session would leak the upstream's own per-session state, which
    is not something the credential check can catch."""
    manager = await _standalone_manager(settings)
    same = "a-token-both-of-them-somehow-have"
    await manager.store("ATLASSIAN_TOKEN", Secret(same), principal=ALICE)
    await manager.store("ATLASSIAN_TOKEN", Secret(same), principal=BOB)

    auth = _bearer()

    assert (await manager.resolve_auth(auth, principal=ALICE)).identity != (
        await manager.resolve_auth(auth, principal=BOB)
    ).identity


async def test_resolved_credentials_describe_themselves_without_disclosure(settings: Settings) -> None:
    """`redacted()` is what reaches the audit trail (arch §24)."""
    manager = await _standalone_manager(settings)
    await manager.store("ATLASSIAN_TOKEN", Secret(ALICE_TOKEN), principal=ALICE)

    auth = _bearer()
    resolved = await manager.resolve_auth(auth, principal=ALICE)
    described = str(resolved.redacted())

    assert ALICE_TOKEN not in described
    assert "Authorization" in described, "which channel carried it is useful and is not a secret"
    assert resolved.identity != ALICE_TOKEN
    assert resolved.identity == fingerprint(f"{ALICE}\x00{ALICE_TOKEN}")


async def test_deleting_a_users_credential_leaves_the_others_intact(settings: Settings) -> None:
    """Removal is per-user too, or offboarding one person breaks everyone."""
    manager = await _standalone_manager(settings)
    await manager.store("ATLASSIAN_TOKEN", Secret(ALICE_TOKEN), principal=ALICE)
    await manager.store("ATLASSIAN_TOKEN", Secret(BOB_TOKEN), principal=BOB)

    assert await manager.delete("ATLASSIAN_TOKEN", principal=ALICE)

    assert await manager.resolve(_ref(per_user=True), principal=ALICE) is None
    survivor = await manager.resolve(_ref(per_user=True), principal=BOB)
    assert survivor is not None
    assert survivor.reveal() == BOB_TOKEN


async def test_listing_credentials_never_returns_a_value(settings: Settings) -> None:
    """`mcp-hub secrets list` and `GET /api/admin/secrets` share this path."""
    manager = await _standalone_manager(settings)
    await manager.store("ATLASSIAN_TOKEN", Secret(ALICE_TOKEN), principal=ALICE)

    described = await manager.describe(principal=ALICE)

    assert ALICE_TOKEN not in str(described)
    assert any("ATLASSIAN_TOKEN" in names for names in described.values())


# --------------------------------------------------------------------------- helpers


def _ref(*, name: str = "ATLASSIAN_TOKEN", per_user: bool) -> SecretRef:
    return SecretRef(name=name, per_user=per_user, required=False)


def _bearer(*, name: str = "ATLASSIAN_TOKEN", per_user: bool = True) -> AuthSpec:
    return AuthSpec(type="bearer", secret=_ref(name=name, per_user=per_user))


async def _standalone_manager(settings: Settings) -> SecretManager:
    """A secret manager over the test's own database, without a whole hub."""
    database = Database(settings.database_url, is_production=False)
    await database.ensure_schema()
    provider = EncryptedDatabaseProvider(
        session_factory=database.session_factory,
        encryption_key=settings.secret_encryption_key.get_secret_value(),
    )
    return SecretManager([provider], cache_ttl_seconds=0.0)


def _issuer(settings: Settings) -> Callable[[str], str]:
    """A callable minting an agent token for a subject."""
    issuer = TokenIssuer(settings.auth_secret.get_secret_value())

    def issue(subject: str) -> str:
        return issuer.issue(subject, scopes=AGENT_SCOPES).token.reveal()

    return issue
