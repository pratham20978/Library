"""Merging desired state, definitions, and resolved state into one view.

Three files describe an integration from different angles: a manifest defines
*what it is*, `integrations.yaml` says *whether we want it and how*, and
`integrations.lock.yaml` records *what is actually installed*. Everything above
this module works with the merged `ResolvedIntegration` instead of juggling all
three, which is what keeps `if integration == "jira"` from ever being tempting
(arch §49).

Precedence, highest first:

1. `integrations.yaml` overrides — an operator's explicit intent wins;
2. the manifest — the integration's own definition;
3. an inline definition in `integrations.yaml` — for integrations with no manifest.

An entry that neither has a manifest nor inlines a source is an error, not a
silent skip: it almost always means a misspelled id, and quietly ignoring it
would leave an operator staring at a tool that never appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from app.config.models import (
    Catalog,
    CatalogEntry,
    IntegrationManifest,
    LockEntry,
    LockFile,
    PolicyDocument,
    PolicyRule,
)
from app.config.store import ConfigStore
from app.core.domain import ExposureMode, HealthStatus, SourceType, TrustTier, UpdatePolicy
from app.core.errors import IntegrationNotFound, InvalidConfiguration
from app.core.logging import get_logger

__all__ = ["ResolvedCatalog", "ResolvedIntegration"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedIntegration:
    """One integration, as every other layer sees it."""

    manifest: IntegrationManifest
    """The merged definition: manifest fields with catalog overrides applied."""

    enabled: bool
    """Whether the operator wants this integration serving traffic."""

    lock: LockEntry | None
    """Resolved state, or `None` when nothing has been installed yet."""

    policy: PolicyRule
    """The authorization rule governing this integration's tools."""

    @property
    def id(self) -> str:
        """Stable integration identifier."""
        return self.manifest.id

    @property
    def namespace(self) -> str:
        """Prefix this integration's tools appear under (arch §11)."""
        return self.manifest.namespace

    @property
    def source_type(self) -> SourceType:
        """Which adapter handles this integration."""
        return self.manifest.source.type

    @property
    def trust(self) -> TrustTier:
        """Provenance tier (arch §46)."""
        return self.manifest.trust

    @property
    def update_policy(self) -> UpdatePolicy:
        """Who triggers updates for this integration (arch §34)."""
        return self.manifest.update_policy

    @property
    def is_installed(self) -> bool:
        """Whether a resolved version exists.

        Remote and builtin integrations need no local artifact, so they count as
        installed as soon as the lock file records them.
        """
        return self.lock is not None

    @property
    def needs_install(self) -> bool:
        """Whether enabling this integration requires fetching an artifact first."""
        return self.manifest.source.type.is_local and self.lock is None

    def baseline_status(self) -> HealthStatus:
        """Health before any probe — what can be told from configuration alone."""
        if not self.enabled:
            return HealthStatus.DISABLED
        if self.needs_install:
            return HealthStatus.UPDATE_REQUIRED
        return HealthStatus.HEALTHY


@dataclass(frozen=True, slots=True)
class ResolvedCatalog:
    """Every configured integration, merged and indexed.

    Immutable. Reload from disk to pick up a change rather than mutating, so a
    request in flight always sees one consistent view of configuration.
    """

    integrations: dict[str, ResolvedIntegration]
    """Merged integrations by id."""

    exposure_mode: ExposureMode
    """How many tool schemas agents see (arch §13)."""

    policies: PolicyDocument
    """The whole policy document, for global rules the per-integration rule omits."""

    @classmethod
    def load(cls, store: ConfigStore, *, exposure_override: ExposureMode | None = None) -> Self:
        """Read all three files and merge them.

        Args:
            store: Where the files live.
            exposure_override: Settings-level override beating the catalog's mode.

        Raises:
            InvalidConfiguration: A catalog entry has no definition, two
                integrations claim the same namespace, or a file is malformed.
        """
        catalog = store.load_catalog()
        manifests = store.load_manifests()
        lock = store.load_lock()
        policies = store.load_policies()
        return cls.merge(
            catalog=catalog,
            manifests=manifests,
            lock=lock,
            policies=policies,
            exposure_override=exposure_override,
        )

    @classmethod
    def merge(
        cls,
        *,
        catalog: Catalog,
        manifests: dict[str, IntegrationManifest],
        lock: LockFile,
        policies: PolicyDocument,
        exposure_override: ExposureMode | None = None,
    ) -> Self:
        """Merge already-loaded documents. Pure, so it is directly testable."""
        resolved: dict[str, ResolvedIntegration] = {}

        # A manifest with no catalog entry is defined but not requested: it is
        # visible to `mcp-hub list` and installable, but disabled by default.
        ids = sorted(set(catalog.integrations) | set(manifests))
        for integration_id in ids:
            entry = catalog.integrations.get(integration_id)
            manifest = _merge_manifest(integration_id, entry, manifests.get(integration_id))
            rule = _resolve_policy(policies, integration_id, manifest.namespace)
            resolved[integration_id] = ResolvedIntegration(
                manifest=manifest,
                enabled=entry.enabled if entry is not None else False,
                lock=lock.integrations.get(integration_id),
                policy=rule,
            )

        _reject_namespace_collisions(resolved)
        _reject_orphan_policies(policies, resolved)
        return cls(
            integrations=resolved,
            exposure_mode=exposure_override or catalog.tool_exposure_mode,
            policies=policies,
        )

    # ------------------------------------------------------------------ views

    def get(self, integration_id: str) -> ResolvedIntegration:
        """Look up one integration.

        Raises:
            IntegrationNotFound: No integration is configured under that id.
        """
        try:
            return self.integrations[integration_id]
        except KeyError:
            raise IntegrationNotFound(
                f"No integration named {integration_id!r} is configured.",
                integration=integration_id,
                known=sorted(self.integrations),
            ) from None

    def enabled(self) -> list[ResolvedIntegration]:
        """Every integration the operator wants serving, in id order."""
        return [item for _, item in sorted(self.integrations.items()) if item.enabled]

    def all(self) -> list[ResolvedIntegration]:
        """Every configured integration, in id order."""
        return [item for _, item in sorted(self.integrations.items())]

    def by_namespace(self, namespace: str) -> ResolvedIntegration | None:
        """Reverse the namespace mapping, for routing a qualified tool name."""
        for item in self.integrations.values():
            if item.namespace == namespace:
                return item
        return None

    def __contains__(self, integration_id: object) -> bool:
        return integration_id in self.integrations

    def __len__(self) -> int:
        return len(self.integrations)


# --------------------------------------------------------------------------- merging


def _merge_manifest(
    integration_id: str,
    entry: CatalogEntry | None,
    manifest: IntegrationManifest | None,
) -> IntegrationManifest:
    """Combine a catalog entry with a manifest into the effective definition."""
    if manifest is None:
        if entry is None:  # pragma: no cover - ids come from the union of both
            raise InvalidConfiguration(f"Integration {integration_id!r} has neither a catalog entry nor a manifest.")
        inline = _inline_or_fail(integration_id, entry)
        return inline
    if entry is None:
        return manifest
    return _apply_overrides(manifest, entry)


def _inline_or_fail(integration_id: str, entry: CatalogEntry) -> IntegrationManifest:
    """Build a manifest from inline catalog fields, or explain what is missing."""
    try:
        inline = entry.to_inline_manifest(integration_id)
    except ValueError as exc:
        raise InvalidConfiguration(
            f"Integration {integration_id!r} is defined inline in integrations.yaml but is incomplete: {exc}",
            integration=integration_id,
        ) from exc
    if inline is None:
        raise InvalidConfiguration(
            f"Integration {integration_id!r} is listed in integrations.yaml but has no definition. "
            f"Add config/manifests/{integration_id}.yaml, or give the entry a `source_type` "
            "and its source fields.",
            integration=integration_id,
        )
    return inline


def _apply_overrides(manifest: IntegrationManifest, entry: CatalogEntry) -> IntegrationManifest:
    """Layer a catalog entry's explicit overrides onto a manifest.

    Only fields the operator actually set are applied. Source-shaped fields are
    ignored here — changing a manifest's source kind from the catalog would make
    the manifest a lie, so that requires editing the manifest itself.
    """
    overrides: dict[str, object] = {}
    if entry.namespace is not None:
        overrides["namespace"] = entry.namespace
    if entry.update_policy is not None:
        overrides["update_policy"] = entry.update_policy
    if entry.trust is not None:
        overrides["trust"] = entry.trust
    if not overrides:
        return manifest

    source_fields = {"source_type", "endpoint", "repository", "branch", "tag", "commit", "package", "image"}
    conflicting = sorted(
        field for field in source_fields if getattr(entry, field, None) is not None and field != "source_type"
    )
    if conflicting and entry.source_type is not None and entry.source_type is not manifest.source.type:
        raise InvalidConfiguration(
            f"Integration {manifest.id!r} has a manifest declaring source {manifest.source.type.value!r} "
            f"but integrations.yaml inlines {entry.source_type.value!r}. Edit the manifest instead.",
            integration=manifest.id,
        )
    # Re-validate rather than `model_copy`: an override can violate an invariant
    # (a community integration granted host networking, an illegal namespace).
    return IntegrationManifest.model_validate({**manifest.model_dump(mode="json"), **overrides})


def _resolve_policy(policies: PolicyDocument, integration_id: str, namespace: str) -> PolicyRule:
    """Layer an integration's rule over the document default.

    A rule may be keyed by integration id or by namespace — an operator reading
    `brave_search.web_search` in a log reaches for `brave_search`, while the
    catalog calls it `brave-search`, and neither should be a silent miss. The id
    wins when both exist.

    Absent list fields inherit the default; present ones replace it. Confirmation
    thresholds combine by taking whichever is stricter, so a per-integration rule
    can tighten the default but never quietly loosen it.
    """
    rule = policies.policies.get(integration_id) or policies.policies.get(namespace)
    default = policies.default
    if rule is None:
        return default

    merged = rule.model_dump(mode="json")
    for field in ("allow", "deny", "require_confirmation", "allowed_scopes", "allowed_principals"):
        if not merged.get(field):
            merged[field] = list(getattr(default, field))
    if merged.get("rate_limit") is None and default.rate_limit is not None:
        merged["rate_limit"] = default.rate_limit.model_dump(mode="json")
    merged["confirm_risk_at_or_above"] = _stricter(rule.confirm_risk_at_or_above, default.confirm_risk_at_or_above)
    merged["deny_risk_at_or_above"] = _stricter(rule.deny_risk_at_or_above, default.deny_risk_at_or_above)
    return PolicyRule.model_validate(merged)


def _stricter(left: object, right: object) -> object:
    """The lower of two risk thresholds — a lower threshold catches more calls."""
    from app.core.domain import RiskLevel

    if left is None:
        return right
    if right is None:
        return left
    assert isinstance(left, RiskLevel) and isinstance(right, RiskLevel)
    return left if left.rank <= right.rank else right


def _reject_orphan_policies(policies: PolicyDocument, resolved: dict[str, ResolvedIntegration]) -> None:
    """A policy for an integration that does not exist is almost always a typo.

    Silently ignoring it is the dangerous outcome: an operator believes a tool is
    denied while the default rule is quietly permitting it.
    """
    claimed = {item.id for item in resolved.values()} | {item.namespace for item in resolved.values()}
    orphans = sorted(set(policies.policies) - claimed)
    if orphans:
        raise InvalidConfiguration(
            f"policies.yaml defines rules for unknown integration(s) {orphans}. "
            "Every policy key must match an integration id or namespace.",
            orphans=orphans,
            known=sorted(claimed),
        )


def _reject_namespace_collisions(resolved: dict[str, ResolvedIntegration]) -> None:
    """Two integrations sharing a namespace would make routing ambiguous (arch §11)."""
    seen: dict[str, str] = {}
    for integration_id, item in sorted(resolved.items()):
        owner = seen.get(item.namespace)
        if owner is not None:
            raise InvalidConfiguration(
                f"Integrations {owner!r} and {integration_id!r} both use namespace "
                f"{item.namespace!r}. Namespaces must be unique so tool names stay unambiguous.",
                namespace=item.namespace,
            )
        seen[item.namespace] = integration_id
