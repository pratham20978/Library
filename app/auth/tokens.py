"""Verifying and issuing hub credentials (arch §20, §64).

The hub accepts two kinds of bearer token, and the distinction is deliberate.

**Hub-issued JWTs** are the normal path: signed HS256 with `MCP_HUB_AUTH_SECRET`,
carrying a subject and scopes, and expiring. They identify a person, which is
what makes per-user credentials meaningful.

**Static admin tokens** are a bootstrap affordance, for the first operator who
has to configure the hub before any user exists. They never expire and cannot be
revoked individually, so they are compared in constant time, resolve to a
service-account principal, and are meant to be replaced by real tokens.

Verification is written to fail closed at every branch: a malformed token, an
unknown algorithm, a missing subject, and an expired signature all return `None`
rather than a partially-trusted result. `verify_token` implements the SDK's
`TokenVerifier` protocol, so the same logic backs both the MCP endpoint and the
REST API — there is no second, subtly different auth path to get wrong.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken

from app.core.clock import Clock, SystemClock
from app.core.context import Principal
from app.core.errors import NotAuthenticated
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.redaction import Secret

__all__ = ["ADMIN_CLIENT_ID", "HubTokenVerifier", "TokenIssuer", "principal_from_access_token"]

log = get_logger(__name__)

_ALGORITHM = "HS256"
ADMIN_CLIENT_ID = "mcp-hub-admin"
"""Client id recorded for a static admin token."""

_ISSUER = "mcp-hub"


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A freshly minted token and when it stops working."""

    token: Secret
    expires_at: int
    subject: str
    scopes: tuple[str, ...]


class TokenIssuer:
    """Mints hub JWTs.

    Raises:
        NotAuthenticated: Constructed without a signing secret. Issuing an
            unsigned token would produce a credential anyone could forge, so it
            is refused rather than degraded.
    """

    def __init__(self, secret: str, *, ttl_seconds: int = 3600, clock: Clock | None = None) -> None:
        if not secret:
            raise NotAuthenticated("Cannot issue tokens without MCP_HUB_AUTH_SECRET. Set it to a long random string.")
        self._secret = secret
        self._ttl = ttl_seconds
        self._clock = clock or SystemClock()

    def issue(
        self,
        subject: str,
        *,
        scopes: tuple[str, ...] = (),
        display_name: str | None = None,
        ttl_seconds: int | None = None,
        audience: str | None = None,
    ) -> IssuedToken:
        """Mint a token for `subject`.

        Args:
            subject: Stable user identity. This is what per-user credentials key
                off, so it must be the real identity and not a display name.
            scopes: Granted scopes.
            display_name: Human-readable label, carried for logs only.
            ttl_seconds: Lifetime override.
            audience: Resource this token is valid for, when the deployment
                distinguishes several.

        Returns:
            The token, wrapped so it cannot be logged by accident.
        """
        now = int(self._clock.now().timestamp())
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl
        claims: dict[str, Any] = {
            "iss": _ISSUER,
            "sub": subject,
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
            "jti": new_id(),
            "scope": " ".join(scopes),
        }
        if display_name:
            claims["name"] = display_name
        if audience:
            claims["aud"] = audience
        encoded = jwt.encode(claims, self._secret, algorithm=_ALGORITHM)
        log.info("auth.token_issued", subject=subject, scopes=list(scopes), ttl_seconds=ttl)
        return IssuedToken(token=Secret(encoded), expires_at=claims["exp"], subject=subject, scopes=tuple(scopes))


class HubTokenVerifier:
    """Verifies bearer tokens for both the MCP endpoint and the REST API.

    Implements the SDK's `TokenVerifier` protocol, so the SDK's bearer-auth
    middleware can use it directly and the request-state boundary can bind
    pending confirmations to the verified principal.
    """

    def __init__(
        self,
        *,
        secret: str,
        admin_tokens: tuple[str, ...] = (),
        audience: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._secret = secret
        self._admin_tokens = tuple(token for token in admin_tokens if token)
        self._audience = audience
        self._clock = clock or SystemClock()

    @property
    def can_verify(self) -> bool:
        """Whether any credential could possibly be accepted."""
        return bool(self._secret or self._admin_tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a bearer token (SDK `TokenVerifier` protocol).

        Returns `None` for anything not positively verified. Never raises: the
        SDK middleware treats `None` as unauthenticated, and an exception here
        would become a 500 that tells an attacker their token was interesting.
        """
        if not token:
            return None

        if self._match_admin_token(token):
            log.info("auth.admin_token_accepted")
            return AccessToken(
                token=token,
                client_id=ADMIN_CLIENT_ID,
                scopes=["admin"],
                subject=ADMIN_CLIENT_ID,
                expires_at=None,
                claims={"service_account": True},
            )

        if not self._secret:
            return None

        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],  # never trust the token's own `alg`
                audience=self._audience,
                issuer=_ISSUER,
                options={
                    "require": ["exp", "sub", "iat"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": self._audience is not None,
                },
            )
        except jwt.ExpiredSignatureError:
            log.info("auth.token_expired")
            return None
        except jwt.InvalidTokenError as exc:
            log.info("auth.token_invalid", reason=type(exc).__name__)
            return None

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            return None

        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or subject),
            scopes=_parse_scopes(claims),
            subject=subject,
            expires_at=int(claims["exp"]),
            claims={key: value for key, value in claims.items() if key != "scope"},
        )

    def _match_admin_token(self, candidate: str) -> bool:
        """Constant-time comparison against every configured admin token.

        Every token is compared even after a match, so the number of comparisons
        does not reveal which one matched.
        """
        matched = False
        for known in self._admin_tokens:
            matched |= hmac.compare_digest(candidate, known)
        return matched


def _parse_scopes(claims: dict[str, Any]) -> list[str]:
    """Read scopes from either `scope` (space-delimited) or `scopes` (list)."""
    raw = claims.get("scope")
    if isinstance(raw, str):
        return [item for item in raw.split() if item]
    listed = claims.get("scopes")
    if isinstance(listed, list):
        return [str(item) for item in listed if item]
    return []


def principal_from_access_token(token: AccessToken | None) -> Principal:
    """Convert a verified token into the hub's `Principal`.

    Returns `ANONYMOUS` for `None`, so callers never have to branch on absence.
    """
    from app.core.context import ANONYMOUS

    if token is None or not token.subject:
        return ANONYMOUS
    claims = token.claims or {}
    return Principal(
        subject=token.subject,
        display_name=str(claims.get("name")) if claims.get("name") else None,
        scopes=frozenset(token.scopes),
        is_service_account=bool(claims.get("service_account")),
    )
