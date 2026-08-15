"""The declarative contract: catalog, manifests, lock file, and policies.

These models *are* the file format. Every YAML document the hub reads is parsed
into one of them, and every document it writes is rendered from one — arch §38
forbids editing configuration by string substitution, and the only way to
guarantee that is to never have a code path that can.

Two rules run through the whole module:

* `extra="forbid"` everywhere. A typo in `integrations.yaml` must fail loudly at
  load, not silently disable the setting the operator thought they set.
* No secrets. Source, transport, and *references* to credentials live here;
  credential material never does (arch §21). `SecretRef` names a secret, and the
  secret manager resolves it at use time.

The source union is discriminated on `type`, so adding a source kind is a new
model plus a registry entry — the core never grows an `if source == ...`
(arch §49).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.core.clock import utcnow
from app.core.domain import (
    ExposureMode,
    RiskLevel,
    SourceType,
    TransportKind,
    TrustTier,
    UpdatePolicy,
)
from app.core.ids import is_valid_namespace

__all__ = [
    "AuthSpec",
    "BuiltinSource",
    "Catalog",
    "CatalogEntry",
    "DockerSource",
    "GitSource",
    "IntegrationManifest",
    "LockEntry",
    "LockFile",
    "NpmSource",
    "PolicyDocument",
    "PolicyRule",
    "PythonSource",
    "RateLimitSpec",
    "RemoteSource",
    "RuntimeSpec",
    "SecretRef",
    "SourceSpec",
    "ToolPolicy",
]

_INTEGRATION_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")


class _Strict(BaseModel):
    """Base for every configuration model: reject unknown keys, freeze values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


# --------------------------------------------------------------------------- secrets


class SecretRef(_Strict):
    """A pointer to a credential, never the credential itself.

    Written in YAML either as a bare string (`env:BRAVE_API_KEY`) or as a
    mapping. `per_user` is the switch behind arch §20: when true the hub looks
    the secret up under the calling principal and refuses to fall back to a
    shared value, so one user's token can never serve another's request.
    """

    name: str = Field(min_length=1, description="Logical secret name, e.g. BRAVE_API_KEY.")
    provider: str | None = Field(
        default=None,
        description="Force a specific provider (`environment`, `database`, `vault`). "
        "Default consults providers in configured order.",
    )
    per_user: bool = Field(
        default=False,
        description="Resolve per calling principal. No shared-value fallback.",
    )
    required: bool = Field(default=True, description="Whether absence blocks the integration.")

    @model_validator(mode="before")
    @classmethod
    def _accept_shorthand(cls, value: object) -> object:
        """Allow `secret: env:BRAVE_API_KEY` and `secret: BRAVE_API_KEY`."""
        if isinstance(value, str):
            provider, separator, name = value.partition(":")
            if separator:
                return {"provider": provider, "name": name}
            return {"name": value}
        return value


# --------------------------------------------------------------------------- sources


class _SourceBase(_Strict):
    """Fields common to every source kind."""

    transport: TransportKind = Field(
        default=TransportKind.STDIO,
        description="How the hub speaks MCP to this upstream once it is running.",
    )
    startup_timeout_seconds: float = Field(
        default=30.0, gt=0, le=600, description="Budget for reaching a usable MCP session."
    )
    request_timeout_seconds: float = Field(
        default=60.0, gt=0, le=3600, description="Per-request budget for tool calls."
    )

    @property
    def origin(self) -> str:
        """Where this integration's server comes from, in one line.

        Each source kind pins itself with a different field — an endpoint, a
        clone URL, a package, an image — so asking the model is the only way to
        render a source without a chain of guesses about which field is set.
        `mcp-hub show` and the admin API both read this.
        """
        return "in-process"


class RemoteSource(_SourceBase):
    """A provider-hosted MCP service. Nothing is installed locally (arch §8).

    The hub proxies these rather than cloning them — arch §6.1/§6.2 are explicit
    that Atlassian's and Figma's servers are managed remote services.
    """

    type: Literal[SourceType.REMOTE] = SourceType.REMOTE
    endpoint: HttpUrl = Field(description="Streamable HTTP MCP endpoint.")
    transport: Literal[TransportKind.STREAMABLE_HTTP] = TransportKind.STREAMABLE_HTTP
    verify_tls: bool = Field(default=True, description="Never disable outside a test fixture.")
    allowed_hosts: tuple[str, ...] = Field(
        default=(),
        description="Extra hosts this integration may be redirected to. The endpoint host "
        "is always allowed; everything else is refused (arch §31, SSRF).",
    )
    max_redirects: int = Field(default=0, ge=0, le=5, description="Redirect budget. Zero is safest.")
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Static non-secret headers. Credentials come from `auth`, not here.",
    )

    @property
    def origin(self) -> str:
        """The provider's endpoint. Nothing is installed for a remote source."""
        return str(self.endpoint)

    @model_validator(mode="after")
    def _require_https(self) -> Self:
        """HTTPS is mandatory except on loopback, which tests and dev need."""
        host = self.endpoint.host or ""
        if self.endpoint.scheme != "https" and host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(f"Remote endpoints must use HTTPS (got {self.endpoint.scheme}://{host}).")
        return self

    @model_validator(mode="after")
    def _reject_secret_headers(self) -> Self:
        """Static headers are committed to Git; a credential must not be."""
        from app.core.redaction import looks_sensitive

        offenders = sorted(key for key in self.headers if looks_sensitive(key))
        if offenders:
            raise ValueError(
                f"Header(s) {offenders} look like credentials. Use `auth:` with a secret "
                "reference instead — arch §21 forbids secrets in YAML."
            )
        return self


class GitSource(_SourceBase):
    """A repository the hub clones, builds, and runs (arch §8, §33).

    Exactly one of `branch`, `tag`, or `commit` pins the desired state. A branch
    is resolved to a commit at install and recorded in the lock file, so the
    deployment stays reproducible even though the branch moves.
    """

    type: Literal[SourceType.GIT] = SourceType.GIT
    repository: str = Field(min_length=1, description="Clone URL.")
    branch: str | None = Field(default=None, description="Track this branch's head.")
    tag: str | None = Field(default=None, description="Pin to this tag.")
    commit: str | None = Field(default=None, description="Pin to this exact commit.")
    subdirectory: str | None = Field(default=None, description="Server root inside the repo.")
    command: tuple[str, ...] = Field(
        default=(), description="Argv to launch the server. Inferred from the project layout when empty."
    )
    build_command: tuple[str, ...] = Field(
        default=(), description="Argv to build before first launch. Inferred when empty."
    )

    @model_validator(mode="after")
    def _exactly_one_ref(self) -> Self:
        refs = (("branch", self.branch), ("tag", self.tag), ("commit", self.commit))
        chosen = [name for name, value in refs if value]
        if len(chosen) > 1:
            raise ValueError(f"Specify only one of branch/tag/commit (got {chosen}).")
        return self

    @model_validator(mode="after")
    def _safe_repository_url(self) -> Self:
        if not re.match(r"^(https://|git@|file://)", self.repository):
            raise ValueError(
                "Repository must be an https://, git@, or file:// URL. "
                "Other schemes (ssh:// with a port, git://) are refused as unverifiable."
            )
        return self

    @property
    def origin(self) -> str:
        """Clone URL and the ref that pins it."""
        location = f"{self.repository}@{self.desired_ref}"
        return f"{location} ({self.subdirectory})" if self.subdirectory else location

    @property
    def desired_ref(self) -> str:
        """The git ref the update manager resolves against."""
        return self.commit or self.tag or self.branch or "HEAD"


class NpmSource(_SourceBase):
    """An npm package run in an isolated Node environment (arch §8)."""

    type: Literal[SourceType.NPM] = SourceType.NPM
    package: str = Field(min_length=1, description="Package name, optionally scoped.")
    version: str | None = Field(default=None, description="Exact version. Resolved from the registry when unset.")
    registry: HttpUrl | None = Field(default=None, description="Alternate registry.")
    binary: str | None = Field(default=None, description="Bin name to execute; defaults to the package's own.")
    args: tuple[str, ...] = Field(default=(), description="Arguments appended to the launch command.")

    @property
    def origin(self) -> str:
        """Package name and requested version."""
        return f"{self.package}@{self.version}" if self.version else self.package


class PythonSource(_SourceBase):
    """A PyPI distribution run in an isolated Python environment (arch §8)."""

    type: Literal[SourceType.PYTHON] = SourceType.PYTHON
    package: str = Field(min_length=1, description="Distribution name.")
    version: str | None = Field(default=None, description="Exact version. Resolved from the index when unset.")
    index_url: HttpUrl | None = Field(default=None, description="Alternate package index.")
    module: str | None = Field(default=None, description="`python -m <module>` entry point.")
    binary: str | None = Field(default=None, description="Console script to execute instead of a module.")
    args: tuple[str, ...] = Field(default=(), description="Arguments appended to the launch command.")

    @property
    def origin(self) -> str:
        """Distribution name and requested version."""
        return f"{self.package}=={self.version}" if self.version else self.package

    @model_validator(mode="after")
    def _needs_entry_point(self) -> Self:
        if not self.module and not self.binary:
            raise ValueError("Python sources need either `module` or `binary` to know what to run.")
        return self


class DockerSource(_SourceBase):
    """A container image run under a constrained profile (arch §8, §31)."""

    type: Literal[SourceType.DOCKER] = SourceType.DOCKER
    image: str = Field(min_length=1, description="Image reference without a tag.")
    tag: str = Field(default="latest", description="Mutable tag. Resolved to a digest at install.")
    digest: str | None = Field(default=None, description="`sha256:...` pin. Required in production (arch §55).")
    args: tuple[str, ...] = Field(default=(), description="Command arguments passed to the container.")

    @model_validator(mode="after")
    def _digest_shape(self) -> Self:
        if self.digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.digest):
            raise ValueError("Digest must look like 'sha256:' followed by 64 hex characters.")
        return self

    @property
    def origin(self) -> str:
        """The pull reference, digest-pinned when one is configured."""
        return self.reference

    @property
    def reference(self) -> str:
        """The pull reference, preferring the digest when one is pinned."""
        return f"{self.image}@{self.digest}" if self.digest else f"{self.image}:{self.tag}"


class BuiltinSource(_SourceBase):
    """A server implemented inside the hub (arch §8)."""

    type: Literal[SourceType.BUILTIN] = SourceType.BUILTIN
    transport: Literal[TransportKind.IN_MEMORY] = TransportKind.IN_MEMORY
    entrypoint: str = Field(
        min_length=1,
        description="Import path `module:factory` returning an `mcp.server.lowlevel.Server`.",
    )

    @property
    def origin(self) -> str:
        """The import path of the in-process server factory."""
        return self.entrypoint

    @model_validator(mode="after")
    def _entrypoint_shape(self) -> Self:
        module, separator, attribute = self.entrypoint.partition(":")
        if not separator or not module or not attribute:
            raise ValueError("Builtin entrypoint must be 'package.module:factory'.")
        if not module.startswith("app."):
            raise ValueError("Builtin entrypoints must live under `app.` — arch §37 forbids loading arbitrary code.")
        return self


SourceSpec = Annotated[
    RemoteSource | GitSource | NpmSource | PythonSource | DockerSource | BuiltinSource,
    Field(discriminator="type"),
]
"""Every source kind, discriminated on `type` so YAML stays declarative."""


# --------------------------------------------------------------------------- runtime & auth


class RuntimeSpec(_Strict):
    """The sandbox an untrusted local server runs inside (arch §31).

    Defaults are the restrictive ones: read-only root, no privileges, bounded
    CPU and memory, network off. An integration that needs more has to say so in
    its manifest, which makes the grant reviewable in a diff.
    """

    isolation: Literal["container", "subprocess"] = Field(
        default="container",
        description="Containers are preferred for third-party code (arch §31).",
    )
    read_only_root: bool = Field(default=True, description="Mount the container root filesystem read-only.")
    network: Literal["none", "outbound", "host"] = Field(
        default="outbound", description="`host` is refused for community-tier integrations."
    )
    cpu_limit: float = Field(default=1.0, gt=0, le=64, description="CPU cores.")
    memory_limit_mb: int = Field(default=512, ge=64, le=65536, description="Memory ceiling in MiB.")
    pids_limit: int = Field(default=256, ge=16, le=8192, description="Process/thread ceiling.")
    execution_timeout_seconds: float = Field(default=300.0, gt=0, le=3600, description="Wall-clock cap per process.")
    run_as_non_root: bool = Field(default=True, description="Never run third-party code as root.")
    allowed_env: tuple[str, ...] = Field(
        default=(),
        description="Environment variable allowlist. Anything not listed is stripped "
        "before launch, so a server cannot read the hub's own configuration.",
    )
    writable_paths: tuple[str, ...] = Field(
        default=(), description="Paths made writable inside the sandbox, relative to its work directory."
    )
    mounts: tuple[str, ...] = Field(
        default=(), description="Host paths exposed read-only, as `host:container` pairs."
    )


class AuthSpec(_Strict):
    """How the hub authenticates *to* an upstream (arch §20)."""

    type: Literal["none", "oauth2", "bearer", "api_key", "basic", "passthrough"] = Field(
        default="none", description="Credential scheme the upstream expects."
    )
    secret: SecretRef | None = Field(default=None, description="Where the credential comes from.")
    header: str = Field(default="Authorization", description="Header carrying the credential.")
    value_template: str = Field(
        default="Bearer {secret}",
        description="How the header value is built. `{secret}` is substituted at call time.",
    )
    query_parameter: str | None = Field(
        default=None, description="Send the credential as this query parameter instead of a header."
    )
    scopes: tuple[str, ...] = Field(default=(), description="OAuth scopes requested during authorization.")
    authorization_url: HttpUrl | None = Field(default=None, description="OAuth 2.1 authorization endpoint.")
    token_url: HttpUrl | None = Field(default=None, description="OAuth 2.1 token endpoint.")
    client_id_secret: SecretRef | None = Field(default=None, description="OAuth client id, held as a secret.")
    client_secret: SecretRef | None = Field(default=None, description="OAuth client secret.")
    env_var: str | None = Field(
        default=None,
        description="For local servers: inject the credential as this environment variable "
        "instead of an HTTP header (how `BRAVE_API_KEY` reaches its server).",
    )

    @model_validator(mode="after")
    def _credential_present(self) -> Self:
        if self.type in ("bearer", "api_key", "basic") and self.secret is None:
            raise ValueError(f"auth.type '{self.type}' requires a `secret` reference.")
        if self.type == "oauth2" and not (self.token_url or self.secret):
            raise ValueError("OAuth integrations need either a `token_url` or a pre-issued `secret`.")
        if self.value_template and "{secret}" not in self.value_template and self.type != "none":
            raise ValueError("auth.value_template must contain the '{secret}' placeholder.")
        return self

    @property
    def is_per_user(self) -> bool:
        """Whether each principal supplies their own credential (arch §20)."""
        return self.secret is not None and self.secret.per_user


# --------------------------------------------------------------------------- manifest


class IntegrationManifest(_Strict):
    """One integration's full definition (arch §32).

    Dropping a manifest file into the manifest directory is enough to register
    an integration (arch §50) — nothing in the core needs to learn its name.
    """

    id: str = Field(min_length=1, max_length=64, description="Stable identifier, e.g. `jira`.")
    name: str = Field(default="", description="Human-readable label for operator output.")
    description: str = Field(default="", description="One line on what this integration provides.")
    namespace: str = Field(default="", description="Tool prefix. Defaults to `id` with '-' mapped to '_'.")
    source: SourceSpec = Field(description="Where the MCP server comes from.")
    trust: TrustTier = Field(default=TrustTier.COMMUNITY, description="Provenance tier (arch §46).")
    auth: AuthSpec = Field(default_factory=AuthSpec, description="How to authenticate upstream.")
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec, description="Sandbox profile for local sources.")
    capabilities: tuple[str, ...] = Field(default=(), description="Free-form capability tags for discovery.")
    risk_level: RiskLevel = Field(
        default=RiskLevel.WRITE, description="Default risk for tools with no explicit classification."
    )
    tool_risk: dict[str, RiskLevel] = Field(
        default_factory=dict,
        description="Per-tool risk overrides. An explicit entry always beats the name heuristic (arch §52).",
    )
    confirmation_required: tuple[str, ...] = Field(
        default=(),
        description="Tool names or `create`/`update`/`delete` verb prefixes that always "
        "need human confirmation (arch §32).",
    )
    update_policy: UpdatePolicy = Field(default=UpdatePolicy.MANUAL, description="Who triggers updates (arch §34).")
    secrets: tuple[SecretRef, ...] = Field(
        default=(), description="Additional secrets this server needs, beyond `auth`."
    )
    homepage: HttpUrl | None = Field(default=None, description="Documentation link, for operator output.")

    @model_validator(mode="after")
    def _defaults_and_shape(self) -> Self:
        if not _INTEGRATION_ID.match(self.id):
            raise ValueError(
                f"Integration id {self.id!r} must be 1-64 characters of lowercase letters, "
                "digits, '_' or '-', starting and ending alphanumeric."
            )
        namespace = self.namespace or self.id.replace("-", "_")
        if not is_valid_namespace(namespace):
            raise ValueError(f"Namespace {namespace!r} is not a valid tool namespace.")
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "name", self.name or self.id)
        return self

    @model_validator(mode="after")
    def _sandbox_matches_trust(self) -> Self:
        """Community code never gets host networking or a root filesystem."""
        if self.trust is TrustTier.COMMUNITY and self.source.type is not SourceType.REMOTE:
            if self.runtime.network == "host":
                raise ValueError(f"Integration {self.id!r} is community-tier and may not use host networking.")
            if not self.runtime.run_as_non_root:
                raise ValueError(f"Integration {self.id!r} is community-tier and must run as a non-root user.")
        return self

    @property
    def source_type(self) -> SourceType:
        """Shorthand for `source.type`, which callers reach for constantly."""
        return self.source.type


# --------------------------------------------------------------------------- catalog


class CatalogEntry(_Strict):
    """Desired state for one integration in `config/integrations.yaml`.

    Two shapes are valid, both from arch §5 and §38. A short entry carries only
    `enabled: true` and leans on a manifest for the definition; a long entry
    inlines the source and defines the integration on its own. Merging happens
    in `Catalog.resolve`, so both reach the rest of the hub identically.
    """

    enabled: bool = Field(default=True, description="Whether this integration routes traffic.")
    source_type: SourceType | None = Field(default=None, description="Inline source kind (short form).")
    transport: TransportKind | None = Field(default=None, description="Inline transport override.")
    endpoint: HttpUrl | None = Field(default=None, description="Inline remote endpoint.")
    repository: str | None = Field(default=None, description="Inline git repository.")
    branch: str | None = Field(default=None, description="Inline git branch.")
    tag: str | None = Field(default=None, description="Inline git tag.")
    commit: str | None = Field(default=None, description="Inline git commit pin.")
    package: str | None = Field(default=None, description="Inline npm/python package.")
    version: str | None = Field(default=None, description="Inline package version.")
    image: str | None = Field(default=None, description="Inline docker image.")
    digest: str | None = Field(default=None, description="Inline docker digest pin.")
    namespace: str | None = Field(default=None, description="Override the manifest's namespace.")
    update_policy: UpdatePolicy | None = Field(default=None, description="Override the manifest's update policy.")
    trust: TrustTier | None = Field(default=None, description="Override the manifest's trust tier.")

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_bool(cls, value: object) -> object:
        """Allow `jira: true` as shorthand for `jira: {enabled: true}`."""
        if isinstance(value, bool):
            return {"enabled": value}
        return value

    def to_inline_manifest(self, integration_id: str) -> IntegrationManifest | None:
        """Build a manifest from inline fields, or `None` if this is a short entry.

        Raises:
            ValueError: The inline fields do not describe a usable source.
        """
        if self.source_type is None:
            return None
        source = self._build_source()
        return IntegrationManifest.model_validate(
            {
                "id": integration_id,
                "namespace": self.namespace or integration_id.replace("-", "_"),
                "source": source,
                "trust": self.trust or _default_trust(self.source_type),
                **({"update_policy": self.update_policy} if self.update_policy else {}),
            }
        )

    def _build_source(self) -> dict[str, object]:
        match self.source_type:
            case SourceType.REMOTE:
                if self.endpoint is None:
                    raise ValueError("source_type 'remote' requires an `endpoint`.")
                return {"type": "remote", "endpoint": str(self.endpoint)}
            case SourceType.GIT:
                if not self.repository:
                    raise ValueError("source_type 'git' requires a `repository`.")
                pins = {k: v for k, v in (("branch", self.branch), ("tag", self.tag), ("commit", self.commit)) if v}
                if not pins:
                    pins = {"branch": "main"}
                return {"type": "git", "repository": self.repository, **pins}
            case SourceType.NPM | SourceType.PYTHON:
                if not self.package:
                    raise ValueError(f"source_type '{self.source_type}' requires a `package`.")
                extra = {"version": self.version} if self.version else {}
                if self.source_type is SourceType.PYTHON:
                    return {"type": "python", "package": self.package, "module": self.package, **extra}
                return {"type": "npm", "package": self.package, **extra}
            case SourceType.DOCKER:
                if not self.image:
                    raise ValueError("source_type 'docker' requires an `image`.")
                extra = {"digest": self.digest} if self.digest else {}
                return {"type": "docker", "image": self.image, "tag": self.version or "latest", **extra}
            case SourceType.BUILTIN:
                raise ValueError("Builtin integrations must be defined by a manifest, not inline.")
            case _:  # pragma: no cover - exhaustive over the enum
                raise ValueError(f"Unsupported source_type {self.source_type!r}.")


def _default_trust(source_type: SourceType) -> TrustTier:
    """Trust tier assumed for an inline entry that does not state one."""
    if source_type is SourceType.REMOTE:
        return TrustTier.REMOTE_OFFICIAL
    if source_type is SourceType.BUILTIN:
        return TrustTier.BUILTIN
    return TrustTier.COMMUNITY


class Catalog(_Strict):
    """`config/integrations.yaml` — the operator's desired state (arch §62)."""

    version: int = Field(default=1, ge=1, description="File format version.")
    tool_exposure_mode: ExposureMode = Field(
        default=ExposureMode.SELECTIVE, description="How many tool schemas agents see (arch §13)."
    )
    integrations: dict[str, CatalogEntry] = Field(
        default_factory=dict, description="Desired state, keyed by integration id."
    )

    @model_validator(mode="after")
    def _valid_ids(self) -> Self:
        bad = sorted(key for key in self.integrations if not _INTEGRATION_ID.match(key))
        if bad:
            raise ValueError(f"Invalid integration id(s) in catalog: {bad}.")
        return self


# --------------------------------------------------------------------------- lock file


class LockEntry(_Strict):
    """The resolved state of one integration (arch §33, §62).

    Whatever identifies an exact artifact for this source kind is recorded here:
    a commit for git, a version for a package, a digest for an image, a
    negotiated protocol version for a remote service. `installed_path` points at
    the immutable version directory the `current` pointer resolves to.
    """

    source_type: SourceType = Field(description="Which resolution fields are meaningful.")
    resolved_commit: str | None = Field(default=None, description="Exact git commit.")
    resolved_version: str | None = Field(default=None, description="Exact package or release version.")
    resolved_digest: str | None = Field(default=None, description="Exact image digest.")
    repository: str | None = Field(default=None, description="Repository the commit belongs to.")
    branch: str | None = Field(default=None, description="Branch tracked at resolution time.")
    endpoint: str | None = Field(default=None, description="Remote endpoint in effect.")
    protocol_version: str | None = Field(default=None, description="MCP protocol version negotiated upstream.")
    server_version: str | None = Field(default=None, description="Version the upstream reported at initialize.")
    installed_path: str | None = Field(default=None, description="Immutable version directory.")
    tool_digest: str | None = Field(
        default=None,
        description="Digest of the discovered tool set. A change across an update is "
        "surfaced for review rather than applied silently (arch §15).",
    )
    tool_count: int = Field(default=0, ge=0, description="Tools discovered at resolution time.")
    installed_at: datetime = Field(default_factory=utcnow, description="When this version was first promoted.")
    updated_at: datetime = Field(default_factory=utcnow, description="When this entry last changed.")

    @property
    def version_id(self) -> str:
        """Short label identifying this resolution, for plans and CLI output."""
        for candidate in (self.resolved_commit, self.resolved_version, self.resolved_digest):
            if candidate:
                return candidate[:16] if len(candidate) > 16 else candidate
        return self.protocol_version or "remote"


class LockFile(_Strict):
    """`config/integrations.lock.yaml` — resolved state (arch §62)."""

    version: int = Field(default=1, ge=1, description="File format version.")
    generated_at: datetime = Field(default_factory=utcnow, description="Last write time.")
    integrations: dict[str, LockEntry] = Field(default_factory=dict, description="Resolved state by id.")


# --------------------------------------------------------------------------- policies


class RateLimitSpec(_Strict):
    """A token-bucket limit (arch §23)."""

    requests: int = Field(gt=0, description="Requests allowed per window.")
    window_seconds: float = Field(default=60.0, gt=0, le=86400, description="Window length.")
    burst: int | None = Field(default=None, gt=0, description="Bucket capacity. Defaults to `requests`.")
    scope: Literal["principal", "integration", "global"] = Field(
        default="principal", description="What the limit counts against."
    )

    @property
    def capacity(self) -> int:
        """Bucket size."""
        return self.burst or self.requests

    @property
    def refill_per_second(self) -> float:
        """Token replenishment rate."""
        return self.requests / self.window_seconds


class ToolPolicy(_Strict):
    """Per-tool rules layered on top of the integration's rules."""

    decision: Literal["allow", "deny", "confirm"] | None = Field(
        default=None, description="Force an outcome for this tool."
    )
    rate_limit: RateLimitSpec | None = Field(default=None, description="Limit specific to this tool.")
    allowed_arguments: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description="Argument allowlists: a call is denied if a named argument's "
        "value is outside its list (arch §23).",
    )
    denied_arguments: dict[str, tuple[str, ...]] = Field(
        default_factory=dict, description="Argument values that always deny."
    )
    max_argument_bytes: int | None = Field(
        default=None, gt=0, description="Reject oversized argument payloads before they reach an upstream."
    )
    allowed_scopes: tuple[str, ...] = Field(default=(), description="Caller must hold one of these scopes.")


class PolicyRule(_Strict):
    """One integration's authorization rules (arch §22).

    `allow` is an allowlist *when non-empty*: anything unlisted is denied. That
    is what makes `SELECTIVE` exposure meaningful — the default posture is
    closed, and an operator opens tools deliberately.
    """

    allow: tuple[str, ...] = Field(
        default=(), description="Permitted tool names. Empty means 'no allowlist' (fall through to defaults)."
    )
    deny: tuple[str, ...] = Field(default=(), description="Always refused. Beats `allow`.")
    require_confirmation: tuple[str, ...] = Field(
        default=(), description="Permitted only after a human accepts (arch §53)."
    )
    rate_limit: RateLimitSpec | None = Field(default=None, description="Default limit for this integration.")
    allowed_scopes: tuple[str, ...] = Field(default=(), description="Scopes required for any tool here.")
    allowed_principals: tuple[str, ...] = Field(
        default=(), description="Restrict to these principal subjects. Empty means any authenticated caller."
    )
    tools: dict[str, ToolPolicy] = Field(default_factory=dict, description="Per-tool overrides.")
    confirm_risk_at_or_above: RiskLevel | None = Field(
        default=RiskLevel.DESTRUCTIVE,
        description="Risk level at which confirmation becomes mandatory regardless of lists.",
    )
    deny_risk_at_or_above: RiskLevel | None = Field(default=None, description="Risk level that is refused outright.")


class PolicyDocument(_Strict):
    """`config/policies.yaml` (arch §22)."""

    version: int = Field(default=1, ge=1, description="File format version.")
    default: PolicyRule = Field(
        default_factory=PolicyRule,
        description="Applied to integrations with no rule of their own, and as the "
        "base every integration rule layers over.",
    )
    policies: dict[str, PolicyRule] = Field(default_factory=dict, description="Rules by integration id.")
    global_rate_limit: RateLimitSpec | None = Field(
        default=None, description="Ceiling applied to every tool call regardless of integration."
    )
    require_authentication: bool = Field(
        default=True,
        description="Refuse unauthenticated tool calls. Disable only for a closed development loop.",
    )
