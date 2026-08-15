"""Process settings, from environment and `.env` (arch §65).

Everything that varies by deployment lives here; everything that varies by
*integration* lives in the YAML catalog. The split matters: settings are
environment-scoped and may hold secrets, so they are never written back to disk.

`Settings` is constructed once at startup and passed down explicitly. It is
frozen, so no request handler can reconfigure the process it is running in.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.domain import ExposureMode

__all__ = ["Settings", "load_settings"]

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Deployment configuration, read from `MCP_HUB_*` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="MCP_HUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        validate_default=True,
    )

    # -- environment ---------------------------------------------------------

    env: Literal["development", "staging", "production", "test"] = Field(
        default="development", description="Deployment environment. Production tightens several defaults."
    )
    log_level: str = Field(default="INFO", description="Root log level.")
    log_json: bool | None = Field(default=None, description="JSON log lines. Defaults to on outside development.")

    # -- network -------------------------------------------------------------

    host: str = Field(default="127.0.0.1", description="Bind address. Compose overrides to 0.0.0.0.")
    port: int = Field(default=8000, ge=1, le=65535, description="Bind port.")
    mcp_path: str = Field(default="/mcp", description="The single MCP endpoint path (arch §10).")
    public_url: str | None = Field(default=None, description="Externally reachable base URL, used in OAuth metadata.")
    cors_origins: tuple[str, ...] = Field(default=(), description="Allowed browser origins for the REST API.")

    # -- storage -------------------------------------------------------------

    database_url: str = Field(
        default="sqlite+aiosqlite:///./runtime/mcp_hub.db",
        description="SQLAlchemy async URL. PostgreSQL in production; SQLite for local work.",
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis URL for locks, rate limits and caches. When unset the hub "
        "uses in-process equivalents, which are correct only for a single worker.",
    )
    database_echo: bool = Field(default=False, description="Log every SQL statement. Debugging only.")

    # -- security ------------------------------------------------------------

    auth_secret: SecretStr = Field(
        default=SecretStr(""),
        description="HMAC key for hub-issued tokens and sealed request state. Required outside development.",
    )
    auth_required: bool = Field(default=True, description="Refuse unauthenticated MCP and API traffic.")
    admin_tokens: tuple[SecretStr, ...] = Field(
        default=(), description="Static tokens granting the `admin` scope. For bootstrap only."
    )
    token_ttl_seconds: int = Field(default=3600, ge=60, le=86400, description="Lifetime of hub-issued tokens.")
    secret_providers: tuple[str, ...] = Field(
        default=("environment",),
        description="Secret providers, consulted in order (arch §21).",
    )
    secret_encryption_key: SecretStr = Field(
        default=SecretStr(""),
        description="Fernet key for the encrypted-database provider. Required when it is enabled.",
    )
    vault_url: str | None = Field(default=None, description="External secret manager endpoint.")
    vault_token: SecretStr = Field(default=SecretStr(""), description="External secret manager credential.")

    # -- gateway -------------------------------------------------------------

    tool_exposure_mode: ExposureMode | None = Field(
        default=None,
        description="Overrides the catalog's exposure mode when set (arch §13).",
    )
    upstream_connect_timeout_seconds: float = Field(
        default=30.0, gt=0, le=300, description="Budget for establishing an upstream MCP session."
    )
    upstream_request_timeout_seconds: float = Field(
        default=60.0, gt=0, le=3600, description="Default per-call budget when a manifest sets none."
    )
    upstream_session_idle_seconds: float = Field(
        default=300.0, gt=0, le=86400, description="Idle upstream sessions are closed after this long."
    )
    upstream_max_sessions: int = Field(
        default=64, ge=1, le=4096, description="Ceiling on concurrently held upstream sessions."
    )
    tool_cache_ttl_seconds: float = Field(
        default=300.0, ge=0, le=86400, description="How long discovered tool metadata stays fresh."
    )
    discovery_concurrency: int = Field(
        default=8, ge=1, le=64, description="Integrations discovered in parallel at startup."
    )

    # -- lifecycle -----------------------------------------------------------

    allow_parallel_updates: bool = Field(
        default=False, description="Update different integrations concurrently (arch §30). Sequential by default."
    )
    rollback_on_failure: bool = Field(default=True, description="Automatic rollback default (arch §59).")
    lock_timeout_seconds: float = Field(
        default=900.0, gt=0, le=7200, description="Lifetime of an integration lifecycle lock."
    )
    backup_retention: int = Field(default=10, ge=1, le=200, description="Rollback points kept per integration.")
    version_retention: int = Field(default=5, ge=1, le=100, description="Installed versions kept per integration.")
    require_signed_artifacts: bool = Field(
        default=False, description="Refuse artifacts without verifiable provenance (arch §55)."
    )
    allow_mutable_image_tags: bool | None = Field(
        default=None, description="Permit untagged-by-digest images. Forced off in production."
    )

    # -- registry ------------------------------------------------------------

    registry_url: str = Field(
        default="https://registry.modelcontextprotocol.io", description="Official MCP Registry (arch §7)."
    )
    registry_timeout_seconds: float = Field(default=15.0, gt=0, le=120, description="Registry request budget.")
    registry_cache_ttl_seconds: float = Field(default=900.0, ge=0, le=86400, description="Registry cache lifetime.")

    # -- paths ---------------------------------------------------------------

    config_dir: Path = Field(default=_REPO_ROOT / "config", description="Directory holding the YAML contract.")
    runtime_dir: Path = Field(default=_REPO_ROOT / "runtime", description="Mutable state root.")

    # -- observability -------------------------------------------------------

    metrics_enabled: bool = Field(default=True, description="Serve Prometheus metrics at `/metrics`.")

    # ------------------------------------------------------------------ paths

    @cached_property
    def catalog_path(self) -> Path:
        """`config/integrations.yaml` — desired state."""
        return self.config_dir / "integrations.yaml"

    @cached_property
    def lock_path(self) -> Path:
        """`config/integrations.lock.yaml` — resolved state."""
        return self.config_dir / "integrations.lock.yaml"

    @cached_property
    def policies_path(self) -> Path:
        """`config/policies.yaml` — authorization rules."""
        return self.config_dir / "policies.yaml"

    @cached_property
    def manifests_dir(self) -> Path:
        """Directory of per-integration manifests (arch §32, §50)."""
        return self.config_dir / "manifests"

    @cached_property
    def integrations_dir(self) -> Path:
        """Installed integration versions live here (arch §15)."""
        return self.runtime_dir / "integrations"

    @cached_property
    def backups_dir(self) -> Path:
        """Rollback points (arch §19)."""
        return self.runtime_dir / "backups"

    @cached_property
    def cache_dir(self) -> Path:
        """Scratch space for staging and downloads."""
        return self.runtime_dir / "cache"

    @cached_property
    def logs_dir(self) -> Path:
        """Per-integration log files (`mcp-hub logs <name>`)."""
        return self.runtime_dir / "logs"

    # ------------------------------------------------------------ derived flags

    @property
    def is_production(self) -> bool:
        """Whether production hardening applies."""
        return self.env in ("production", "staging")

    @property
    def json_logs(self) -> bool:
        """Whether to emit JSON log lines."""
        return self.log_json if self.log_json is not None else self.env != "development"

    @property
    def mutable_image_tags_allowed(self) -> bool:
        """Whether an image may be pulled by tag rather than digest (arch §55)."""
        if self.allow_mutable_image_tags is not None:
            return self.allow_mutable_image_tags and not self.is_production
        return not self.is_production

    @property
    def request_state_keys(self) -> tuple[bytes, ...]:
        """Key ring sealing multi-round-trip request state.

        The MCP SDK treats a client-echoed `requestState` as attacker-controlled
        and seals it. Deriving from `auth_secret` keeps a pending confirmation
        valid across hub replicas; without a configured secret the SDK's
        ephemeral per-process key is used instead, which is safe but pins a
        confirmation to the replica that issued it.
        """
        raw = self.auth_secret.get_secret_value()
        if not raw:
            return ()
        from hashlib import blake2b

        return (blake2b(raw.encode("utf-8"), digest_size=32, person=b"mcp-hub-rs").digest(),)

    # ------------------------------------------------------------- validation

    @model_validator(mode="after")
    def _production_requires_real_secrets(self) -> Self:
        """Fail fast rather than run production on development defaults."""
        if not self.is_production:
            return self
        problems: list[str] = []
        if not self.auth_secret.get_secret_value():
            problems.append("MCP_HUB_AUTH_SECRET must be set")
        if self.database_url.startswith("sqlite"):
            problems.append("MCP_HUB_DATABASE_URL must point at PostgreSQL (arch §27)")
        if not self.redis_url:
            problems.append("MCP_HUB_REDIS_URL must be set so locks and rate limits are shared (arch §28)")
        if not self.auth_required:
            problems.append("MCP_HUB_AUTH_REQUIRED must not be disabled")
        if "encrypted_database" in self.secret_providers and not self.secret_encryption_key.get_secret_value():
            problems.append("MCP_HUB_SECRET_ENCRYPTION_KEY must be set for the encrypted_database provider")
        if problems:
            raise ValueError(f"Refusing to start in {self.env}: " + "; ".join(problems) + ".")
        return self

    @model_validator(mode="after")
    def _mcp_path_shape(self) -> Self:
        if not self.mcp_path.startswith("/") or self.mcp_path.rstrip("/") == "":
            raise ValueError("MCP_HUB_MCP_PATH must be an absolute path such as '/mcp'.")
        object.__setattr__(self, "mcp_path", self.mcp_path.rstrip("/"))
        return self

    @model_validator(mode="after")
    def _known_secret_providers(self) -> Self:
        known = {"environment", "encrypted_database", "vault"}
        unknown = sorted(set(self.secret_providers) - known)
        if unknown:
            raise ValueError(f"Unknown secret provider(s) {unknown}; expected any of {sorted(known)}.")
        if not self.secret_providers:
            raise ValueError("At least one secret provider must be configured.")
        return self


# Annotated alias documenting intent at injection sites.
SettingsDep = Annotated[Settings, "process settings"]


def load_settings(**overrides: object) -> Settings:
    """Build `Settings` from the environment, with explicit overrides on top.

    Tests pass overrides directly instead of mutating `os.environ`, which keeps
    them independent of execution order.
    """
    return Settings(**overrides)  # type: ignore[arg-type]
