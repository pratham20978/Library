"""Resolving credentials for an upstream call (arch §20, §21).

`SecretManager` is the only component that reads credential material. It owns
three rules the rest of the hub relies on and never re-implements:

1. **Per-user means per-user.** A `SecretRef` marked `per_user` resolves under
   the calling principal and *never* falls back to a deployment-wide value. The
   fallback is the whole vulnerability — it silently promotes every caller to
   whoever owns the shared token (arch §20).
2. **Credentials never reach the model.** Resolution returns an
   `UpstreamCredentials` carrying `Secret` values plus a non-reversible
   fingerprint. The gateway uses the fingerprint as a session-pool key and logs
   that; the material itself only ever moves into an HTTP header or a child
   process environment.
3. **Providers are consulted in configured order.** First hit wins, so a
   deployment can layer Vault over the environment without code changes.

The cache is short-lived and in-memory only. It exists so a burst of tool calls
does not become a burst of Vault requests; it is never written to disk and is
dropped whenever a secret is written or deleted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.config.models import AuthSpec, SecretRef
from app.core.clock import Clock, SystemClock
from app.core.errors import SecretNotFound
from app.core.logging import get_logger
from app.core.redaction import Secret, fingerprint
from app.secrets.providers import SecretKey, SecretProvider, SecretWriteUnsupported

__all__ = ["SecretManager", "UpstreamCredentials"]

log = get_logger(__name__)

_ANONYMOUS_FINGERPRINT = "anonymous"


@dataclass(frozen=True, slots=True)
class UpstreamCredentials:
    """Everything needed to authenticate one upstream call, already resolved.

    `identity` is the session-pool key. Two calls may share an upstream MCP
    session only when their identities match, which is what stops one user's
    session from serving another user's request.
    """

    headers: Mapping[str, str] = field(default_factory=dict)
    """HTTP headers to attach. Values contain credential material."""

    env: Mapping[str, str] = field(default_factory=dict)
    """Environment variables to inject into a local server process."""

    query: Mapping[str, str] = field(default_factory=dict)
    """Query parameters carrying a credential, for upstreams that want one there."""

    identity: str = _ANONYMOUS_FINGERPRINT
    """Non-reversible fingerprint of the credential set. Safe to log."""

    principal: str | None = None
    """Subject these credentials belong to, or `None` when deployment-wide."""

    @property
    def is_anonymous(self) -> bool:
        """Whether no credential was resolved at all."""
        return not self.headers and not self.env and not self.query

    def redacted(self) -> dict[str, object]:
        """Audit-safe description: which channels carry a credential, not what."""
        return {
            "header_names": sorted(self.headers),
            "env_names": sorted(self.env),
            "query_names": sorted(self.query),
            "identity": self.identity,
            "principal": self.principal,
        }


@dataclass(slots=True)
class _CacheEntry:
    secret: Secret
    expires_at: float


class SecretManager:
    """Resolves `SecretRef`s and `AuthSpec`s through an ordered provider chain."""

    def __init__(
        self,
        providers: Sequence[SecretProvider],
        *,
        clock: Clock | None = None,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        if not providers:
            raise ValueError("SecretManager requires at least one provider.")
        self._providers = tuple(providers)
        self._clock = clock or SystemClock()
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    @property
    def provider_names(self) -> tuple[str, ...]:
        """Configured providers, in consultation order."""
        return tuple(provider.name for provider in self._providers)

    @property
    def supports_per_user(self) -> bool:
        """Whether any provider can hold per-principal credentials (arch §20)."""
        return any(provider.supports_per_user for provider in self._providers)

    # ------------------------------------------------------------------ reads

    async def resolve(self, ref: SecretRef, *, principal: str | None = None) -> Secret | None:
        """Resolve one reference.

        Args:
            ref: What to look up, including whether it is per-user.
            principal: The calling subject. Required when `ref.per_user`.

        Returns:
            The secret, or `None` when it cannot be resolved and `ref.required`
            is false.

        Raises:
            SecretNotFound: The secret is required and no provider holds it, or a
                *required* per-user secret was requested without a principal, or
                no configured provider can store per-user values at all.

        An unresolvable per-user reference always yields nothing rather than a
        shared value — that is arch §20's invariant, and it holds whether this
        returns `None` or raises. Which of the two happens is a separate
        question, answered by `ref.required`: a required credential must stop the
        call, while an optional one (an OAuth flow may supply the identity by
        another route) must not turn an anonymous health probe into an error.
        """
        if ref.per_user and not principal:
            if not ref.required:
                return None
            raise SecretNotFound(
                f"Secret {ref.name!r} is per-user but this call has no authenticated principal. "
                "Per-user credentials are never satisfied by a shared value (arch §20).",
                secret=ref.name,
            )
        if ref.per_user and not self.supports_per_user:
            if not ref.required:
                log.warning(
                    "secret.per_user_unsupported",
                    secret=ref.name,
                    providers=list(self.provider_names),
                    detail="Optional per-user secret skipped; no provider can store per-principal values.",
                )
                return None
            raise SecretNotFound(
                f"Secret {ref.name!r} is per-user, but no configured provider "
                f"({', '.join(self.provider_names)}) can store per-principal values. "
                "Enable the encrypted_database or vault provider.",
                secret=ref.name,
                providers=list(self.provider_names),
            )

        key = SecretKey(name=ref.name, principal=principal if ref.per_user else None)
        secret = await self._lookup(key, forced_provider=ref.provider)
        if secret is not None:
            return secret
        if ref.required:
            raise SecretNotFound(
                f"Required secret {ref.name!r} was not found in any configured provider "
                f"({', '.join(self.provider_names)}).",
                secret=ref.name,
                per_user=ref.per_user,
                providers=list(self.provider_names),
            )
        return None

    async def _lookup(self, key: SecretKey, *, forced_provider: str | None) -> Secret | None:
        """Consult providers in order, with a short in-memory cache."""
        cache_key = f"{forced_provider or '*'}|{key.storage_key()}"
        now = self._clock.monotonic()
        async with self._lock:
            entry = self._cache.get(cache_key)
            if entry is not None and entry.expires_at > now:
                return entry.secret

        for provider in self._providers:
            if forced_provider and provider.name != forced_provider:
                continue
            if key.is_per_user and not provider.supports_per_user:
                continue
            secret = await provider.get(key)
            if secret is not None and not secret.is_empty:
                async with self._lock:
                    self._cache[cache_key] = _CacheEntry(
                        secret=secret, expires_at=self._clock.monotonic() + self._cache_ttl
                    )
                log.debug("secret.resolved", secret=key.name, provider=provider.name, per_user=key.is_per_user)
                return secret
        return None

    async def resolve_auth(
        self,
        auth: AuthSpec,
        *,
        principal: str | None = None,
        extra: Sequence[SecretRef] = (),
    ) -> UpstreamCredentials:
        """Build the credential set for one upstream (arch §20).

        Args:
            auth: The integration's authentication contract.
            principal: Calling subject, used for per-user references.
            extra: Additional secrets the manifest declares beyond `auth`.

        Returns:
            Headers, environment variables, and query parameters to apply, plus a
            fingerprint identifying the set.

        Raises:
            SecretNotFound: A required credential is missing.
        """
        headers: dict[str, str] = {}
        env: dict[str, str] = {}
        query: dict[str, str] = {}
        material: list[str] = []

        if auth.type != "none" and auth.secret is not None:
            secret = await self.resolve(auth.secret, principal=principal)
            if secret is not None:
                material.append(secret.reveal())
                self._apply(auth, secret, headers=headers, env=env, query=query)

        for ref in extra:
            secret = await self.resolve(ref, principal=principal)
            if secret is None:
                continue
            material.append(secret.reveal())
            # Additional secrets reach local servers as environment variables;
            # there is no header convention for them.
            env[ref.name] = secret.reveal()

        identity = self._identity(material, principal=principal, per_user=auth.is_per_user)
        return UpstreamCredentials(
            headers=headers,
            env=env,
            query=query,
            identity=identity,
            principal=principal if auth.is_per_user else None,
        )

    @staticmethod
    def _apply(
        auth: AuthSpec,
        secret: Secret,
        *,
        headers: dict[str, str],
        env: dict[str, str],
        query: dict[str, str],
    ) -> None:
        """Place one resolved credential on the channel the upstream expects."""
        value = secret.reveal()
        if auth.env_var:
            env[auth.env_var] = value
        if auth.query_parameter:
            query[auth.query_parameter] = value
        if not auth.env_var and not auth.query_parameter:
            headers[auth.header] = auth.value_template.format(secret=value)

    @staticmethod
    def _identity(material: Sequence[str], *, principal: str | None, per_user: bool) -> str:
        """Fingerprint a credential set for use as a session-pool key.

        The principal is mixed in for per-user credentials so that two users who
        happen to hold the *same* token still get separate sessions — sharing one
        would make an upstream's own per-session state cross a user boundary.
        """
        if not material:
            return _ANONYMOUS_FINGERPRINT
        seed = "\x00".join(sorted(material))
        if per_user and principal:
            seed = f"{principal}\x00{seed}"
        return fingerprint(seed)

    # ----------------------------------------------------------------- writes

    async def store(
        self,
        name: str,
        value: Secret,
        *,
        principal: str | None = None,
        integration: str | None = None,
        provider: str | None = None,
    ) -> str:
        """Write a secret to the first provider that accepts it.

        Returns:
            The name of the provider that stored it.

        Raises:
            SecretNotFound: No configured provider can store this secret.
        """
        key = SecretKey(name=name, principal=principal, integration=integration)
        errors: list[str] = []
        for candidate in self._providers:
            if provider and candidate.name != provider:
                continue
            if key.is_per_user and not candidate.supports_per_user:
                continue
            try:
                await candidate.set(key, value)
            except SecretWriteUnsupported as exc:
                errors.append(f"{candidate.name}: {exc.message}")
                continue
            await self._invalidate(key)
            return candidate.name
        raise SecretNotFound(
            f"No configured provider could store secret {name!r}. " + " ".join(errors),
            secret=name,
            providers=list(self.provider_names),
        )

    async def delete(self, name: str, *, principal: str | None = None) -> bool:
        """Remove a secret from every provider that holds it."""
        key = SecretKey(name=name, principal=principal)
        removed = False
        for provider in self._providers:
            if key.is_per_user and not provider.supports_per_user:
                continue
            try:
                removed |= await provider.delete(key)
            except SecretWriteUnsupported:
                continue
        await self._invalidate(key)
        return removed

    async def delete_for_integration(self, integration: str) -> int:
        """Remove secrets owned solely by `integration` (arch §18).

        Shared credentials have no owning integration recorded and are therefore
        never touched — arch §18 is explicit that they must not be deleted
        automatically.
        """
        total = 0
        for provider in self._providers:
            scoped_delete = getattr(provider, "delete_for_integration", None)
            if scoped_delete is not None:
                total += await scoped_delete(integration)
        await self.clear_cache()
        return total

    async def _invalidate(self, key: SecretKey) -> None:
        """Drop cached copies of one secret after a write."""
        suffix = f"|{key.storage_key()}"
        async with self._lock:
            for cache_key in [k for k in self._cache if k.endswith(suffix)]:
                del self._cache[cache_key]

    async def clear_cache(self) -> None:
        """Drop every cached secret. Called on rotation and at shutdown."""
        async with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------ diagnostics

    async def describe(self, *, principal: str | None = None) -> dict[str, list[str]]:
        """Which names each provider holds, for `mcp-hub doctor`. Never values."""
        result: dict[str, list[str]] = {}
        for provider in self._providers:
            try:
                result[provider.name] = await provider.list_names(principal=principal)
            except Exception as exc:  # noqa: BLE001 - diagnostics must not fail the caller
                log.warning("secret.list_failed", provider=provider.name, error=str(exc))
                result[provider.name] = []
        return result

    async def check_requirements(self, refs: Sequence[SecretRef], *, principal: str | None = None) -> list[str]:
        """Return the names of required secrets that are missing.

        Used by health checks to report `AUTH_REQUIRED` before a call fails
        (arch §25), rather than discovering it mid-request.
        """
        missing: list[str] = []
        for ref in refs:
            if not ref.required:
                continue
            try:
                if await self.resolve(ref, principal=principal) is None:
                    missing.append(ref.name)
            except SecretNotFound:
                missing.append(ref.name)
        return missing


def build_secret_manager(settings: object, *, session_factory: object | None = None) -> SecretManager:
    """Construct the manager described by `Settings`.

    Providers that cannot be 
    (no encryption key, no Vault URL) are
    skipped with a warning rather than aborting startup, so a misconfigured
    optional provider degrades the hub instead of stopping it. The environment
    provider is always available as a floor.
    """
    from app.config.settings import Settings

    assert isinstance(settings, Settings)
    providers: list[SecretProvider] = []
    for name in settings.secret_providers:
        if name == "environment":
            from app.secrets.providers import EnvironmentProvider

            providers.append(EnvironmentProvider())
        elif name == "encrypted_database":
            key = settings.secret_encryption_key.get_secret_value()
            if not key or session_factory is None:
                log.warning("secret.provider_skipped", provider=name, reason="no encryption key or database")
                continue
            from sqlalchemy.ext.asyncio import async_sessionmaker

            from app.secrets.providers import EncryptedDatabaseProvider

            assert isinstance(session_factory, async_sessionmaker)
            providers.append(EncryptedDatabaseProvider(session_factory=session_factory, encryption_key=key))
        elif name == "vault":
            token = settings.vault_token.get_secret_value()
            if not settings.vault_url or not token:
                log.warning("secret.provider_skipped", provider=name, reason="no url or token")
                continue
            from app.secrets.providers import VaultProvider

            providers.append(VaultProvider(url=settings.vault_url, token=token))
    if not providers:
        from app.secrets.providers import EnvironmentProvider

        providers.append(EnvironmentProvider())
    return SecretManager(providers)
