"""Secret providers (arch §21).

A provider answers one question: *what is the value of this named secret, for
this principal?* Nothing more — no policy, no fallback ordering, no caching.
Those belong to `SecretManager`, which is the only thing that talks to more than
one provider.

Three ship here. `environment` is the development default and holds only
deployment-wide values. `encrypted_database` is the production default: values
are sealed with authenticated encryption before they touch a row, so a database
dump is not a credential dump. `vault` defers to an external manager.

Every provider returns `Secret`, never a bare `str`, so a value cannot reach a
log through an f-string on the way back up.
"""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from app.core.errors import HubError
from app.core.logging import get_logger
from app.core.redaction import Secret

if TYPE_CHECKING:
    from sqlalchemy import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "EncryptedDatabaseProvider",
    "EnvironmentProvider",
    "SecretKey",
    "SecretProvider",
    "SecretWriteUnsupported",
    "VaultProvider",
]

log = get_logger(__name__)


class SecretWriteUnsupported(HubError):
    """This provider is read-only — the value must be managed where it lives."""

    code = "secret_write_unsupported"
    http_status = 400


@dataclass(frozen=True, slots=True)
class SecretKey:
    """What is being looked up, and on whose behalf.

    `principal` is the whole of arch §20. When set, the lookup is for that
    user's own credential and a deployment-wide value must never satisfy it —
    the manager enforces that, and providers simply return nothing rather than
    reaching for a shared value.
    """

    name: str
    """Logical secret name, e.g. `GITHUB_TOKEN`."""

    principal: str | None = None
    """Subject of the owning user, or `None` for a deployment-wide secret."""

    integration: str | None = None
    """Owning integration, when the secret belongs to exactly one (arch §18)."""

    @property
    def is_per_user(self) -> bool:
        """Whether this lookup is scoped to an individual caller."""
        return self.principal is not None

    def storage_key(self) -> str:
        """Stable string form used as a row key or vault path segment."""
        return f"user:{self.principal}:{self.name}" if self.principal else f"shared:{self.name}"

    def __str__(self) -> str:
        # Deliberately excludes nothing sensitive: names and subjects are not secrets.
        return self.storage_key()


@runtime_checkable
class SecretProvider(Protocol):
    """A source of credential material."""

    name: str
    """Identifier matching the `secret_providers` setting."""

    @property
    def supports_per_user(self) -> bool:
        """Whether this provider can store distinct values per principal."""
        ...

    async def get(self, key: SecretKey) -> Secret | None:
        """Return the value, or `None` if this provider does not hold it."""
        ...

    async def set(self, key: SecretKey, value: Secret) -> None:
        """Store a value.

        Raises:
            SecretWriteUnsupported: This provider is read-only.
        """
        ...

    async def delete(self, key: SecretKey) -> bool:
        """Remove a value. Returns whether anything was removed."""
        ...

    async def list_names(self, *, principal: str | None = None) -> list[str]:
        """Names this provider holds, for `mcp-hub doctor`. Never values."""
        ...


# --------------------------------------------------------------------------- environment


class EnvironmentProvider:
    """Reads deployment-wide secrets from the process environment.

    The development default (arch §21). Deliberately cannot do per-user
    credentials: one process environment cannot hold a token per user, and
    pretending otherwise would hand user A's token to user B.

    Two spellings are accepted — the bare name (`BRAVE_API_KEY`, which is what
    upstream servers document) and a prefixed form (`MCP_HUB_SECRET_BRAVE_API_KEY`)
    for deployments that want the hub's variables namespaced.
    """

    name = "environment"

    def __init__(self, *, prefix: str = "MCP_HUB_SECRET_") -> None:
        self._prefix = prefix

    @property
    def supports_per_user(self) -> bool:
        return False

    async def get(self, key: SecretKey) -> Secret | None:
        if key.is_per_user:
            return None
        for candidate in (f"{self._prefix}{key.name}", key.name):
            raw = os.environ.get(candidate)
            if raw:
                return Secret(raw)
        return None

    async def set(self, key: SecretKey, value: Secret) -> None:
        raise SecretWriteUnsupported(
            f"The environment provider is read-only; set {key.name} in the process "
            "environment or .env, or enable the encrypted_database provider.",
            secret=key.name,
        )

    async def delete(self, key: SecretKey) -> bool:
        raise SecretWriteUnsupported(
            f"The environment provider is read-only; unset {key.name} in the environment.",
            secret=key.name,
        )

    async def list_names(self, *, principal: str | None = None) -> list[str]:
        if principal:
            return []
        return sorted(name.removeprefix(self._prefix) for name in os.environ if name.startswith(self._prefix))


# --------------------------------------------------------------------------- encrypted database


class EncryptedDatabaseProvider:
    """Stores secrets encrypted at rest in PostgreSQL (arch §21).

    Values are sealed with Fernet (AES-128-CBC plus an HMAC tag) before they are
    written, so a row is useless without `MCP_HUB_SECRET_ENCRYPTION_KEY`. The key
    is never persisted alongside the data — that is the entire point.

    This is the only provider that supports per-user credentials, which makes it
    the production choice for arch §20.
    """

    name = "encrypted_database"

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], encryption_key: str) -> None:
        if not encryption_key:
            raise ValueError("EncryptedDatabaseProvider requires a non-empty encryption key.")
        from cryptography.fernet import Fernet

        self._sessions = session_factory
        self._fernet = Fernet(self._derive_key(encryption_key))

    @staticmethod
    def _derive_key(material: str) -> bytes:
        """Accept either a real Fernet key or arbitrary material.

        Operators paste whatever they have. A valid 32-byte urlsafe-base64 key is
        used directly; anything else is stretched through HKDF so a short or
        low-entropy string still yields a full-width key rather than a crash.
        """
        import base64
        import binascii

        raw = material.encode("utf-8")
        with suppress(binascii.Error, ValueError):
            # A decode failure just means "not a Fernet key"; fall through to HKDF.
            if len(base64.urlsafe_b64decode(raw)) == 32:
                return raw
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"mcp-hub-secrets").derive(raw)
        return base64.urlsafe_b64encode(derived)

    @property
    def supports_per_user(self) -> bool:
        return True

    async def get(self, key: SecretKey) -> Secret | None:
        from sqlalchemy import select

        from app.database.models import StoredSecret

        async with self._sessions() as session:
            row = await session.scalar(select(StoredSecret).where(StoredSecret.key == key.storage_key()))
            if row is None:
                return None
            try:
                plaintext = self._fernet.decrypt(row.ciphertext.encode("utf-8")).decode("utf-8")
            except Exception:  # noqa: BLE001 - a bad token means the key rotated or the row was tampered with
                log.error("secret.decrypt_failed", secret=key.name, scope=key.storage_key())
                return None
            return Secret(plaintext)

    async def set(self, key: SecretKey, value: Secret) -> None:
        from sqlalchemy import select

        from app.core.clock import utcnow
        from app.database.models import StoredSecret

        ciphertext = self._fernet.encrypt(value.reveal().encode("utf-8")).decode("utf-8")
        async with self._sessions() as session, session.begin():
            row = await session.scalar(select(StoredSecret).where(StoredSecret.key == key.storage_key()))
            if row is None:
                session.add(
                    StoredSecret(
                        key=key.storage_key(),
                        name=key.name,
                        principal=key.principal,
                        integration=key.integration,
                        ciphertext=ciphertext,
                    )
                )
            else:
                row.ciphertext = ciphertext
                row.integration = key.integration or row.integration
                row.updated_at = utcnow()
        log.info("secret.stored", secret=key.name, per_user=key.is_per_user, integration=key.integration)

    async def delete(self, key: SecretKey) -> bool:
        from sqlalchemy import delete

        from app.database.models import StoredSecret

        async with self._sessions() as session, session.begin():
            statement = delete(StoredSecret).where(StoredSecret.key == key.storage_key())
            result = cast("CursorResult[Any]", await session.execute(statement))
            removed = bool(result.rowcount)
        if removed:
            log.info("secret.deleted", secret=key.name, per_user=key.is_per_user)
        return removed

    async def delete_for_integration(self, integration: str) -> int:
        """Remove every secret owned solely by `integration` (arch §18).

        Only rows explicitly attributed to this integration are touched, which is
        how the removal path honours "never delete shared credentials
        automatically" — a shared secret has no owning integration recorded.
        """
        from sqlalchemy import delete

        from app.database.models import StoredSecret

        async with self._sessions() as session, session.begin():
            statement = delete(StoredSecret).where(StoredSecret.integration == integration)
            result = cast("CursorResult[Any]", await session.execute(statement))
            return int(result.rowcount or 0)

    async def list_names(self, *, principal: str | None = None) -> list[str]:
        from sqlalchemy import select

        from app.database.models import StoredSecret

        async with self._sessions() as session:
            statement = select(StoredSecret.name).where(StoredSecret.principal == principal)
            rows = await session.scalars(statement)
            return sorted(set(rows))


# --------------------------------------------------------------------------- vault


class VaultProvider:
    """Reads from an external secret manager over HTTP (arch §21).

    Speaks HashiCorp Vault's KV v2 API, which several managers emulate. Secrets
    are fetched on demand and never written to the hub's own storage; the
    manager's short-lived cache is the only copy that lingers, in memory.
    """

    name = "vault"

    def __init__(
        self,
        *,
        url: str,
        token: str,
        mount: str = "secret",
        base_path: str = "mcp-hub",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not url or not token:
            raise ValueError("VaultProvider requires both a URL and a token.")
        self._url = url.rstrip("/")
        self._token = token
        self._mount = mount.strip("/")
        self._base_path = base_path.strip("/")
        self._timeout = timeout_seconds

    @property
    def supports_per_user(self) -> bool:
        return True

    def _path(self, key: SecretKey) -> str:
        scope = f"users/{key.principal}" if key.principal else "shared"
        return f"{self._url}/v1/{self._mount}/data/{self._base_path}/{scope}/{key.name}"

    async def get(self, key: SecretKey) -> Secret | None:
        import httpx2

        try:
            async with httpx2.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self._path(key), headers={"X-Vault-Token": self._token})
        except httpx2.HTTPError as exc:
            log.warning("secret.vault_unreachable", secret=key.name, error=str(exc))
            return None
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            log.warning("secret.vault_error", secret=key.name, status=response.status_code)
            return None
        payload = response.json()
        value = payload.get("data", {}).get("data", {}).get("value")
        return Secret(value) if isinstance(value, str) and value else None

    async def set(self, key: SecretKey, value: Secret) -> None:
        import httpx2

        async with httpx2.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._path(key),
                headers={"X-Vault-Token": self._token},
                json={"data": {"value": value.reveal()}},
            )
        if response.status_code >= 400:
            raise HubError(
                f"Vault rejected the write for {key.name} with status {response.status_code}.",
                secret=key.name,
                status=response.status_code,
            )
        log.info("secret.stored", secret=key.name, provider="vault", per_user=key.is_per_user)

    async def delete(self, key: SecretKey) -> bool:
        import httpx2

        path = self._path(key).replace("/data/", "/metadata/", 1)
        async with httpx2.AsyncClient(timeout=self._timeout) as client:
            response = await client.delete(path, headers={"X-Vault-Token": self._token})
        return response.status_code < 300

    async def list_names(self, *, principal: str | None = None) -> list[str]:
        import httpx2

        scope = f"users/{principal}" if principal else "shared"
        path = f"{self._url}/v1/{self._mount}/metadata/{self._base_path}/{scope}"
        try:
            async with httpx2.AsyncClient(timeout=self._timeout) as client:
                response = await client.request("LIST", path, headers={"X-Vault-Token": self._token})
        except httpx2.HTTPError:
            return []
        if response.status_code >= 400:
            return []
        keys = response.json().get("data", {}).get("keys", [])
        return sorted(str(item) for item in keys)
