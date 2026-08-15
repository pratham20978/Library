"""The adapter registry (arch §49, §50).

Arch §49 states the rule this module exists to enforce: the core resolves an
adapter by source type, it never branches on an integration's name.

    adapter = adapter_registry.get(source_type)     # good
    if integration == "jira": ...                   # never

Adding a source kind is therefore a new `IntegrationAdapter` subclass plus one
`register` call. Nothing in the gateway, the update manager, or the CLI changes,
because none of them know what source kinds exist.
"""

from __future__ import annotations

from app.core.domain import SourceType
from app.core.errors import ValidationFailed
from app.integrations.adapters.builtin import BuiltinAdapter
from app.integrations.adapters.docker import DockerAdapter
from app.integrations.adapters.git import GitAdapter
from app.integrations.adapters.local import LocalAdapter
from app.integrations.adapters.npm import NpmAdapter
from app.integrations.adapters.python import PythonAdapter
from app.integrations.adapters.remote import RemoteAdapter
from app.integrations.base import AdapterContext, IntegrationAdapter

__all__ = [
    "AdapterRegistry",
    "BuiltinAdapter",
    "DockerAdapter",
    "GitAdapter",
    "LocalAdapter",
    "NpmAdapter",
    "PythonAdapter",
    "RemoteAdapter",
    "adapter_registry",
]


class AdapterRegistry:
    """Maps a source type to the adapter class that handles it."""

    def __init__(self) -> None:
        self._adapters: dict[SourceType, type[IntegrationAdapter]] = {}

    def register(self, adapter: type[IntegrationAdapter]) -> type[IntegrationAdapter]:
        """Register `adapter` for its declared source type.

        Usable as a decorator so an out-of-tree adapter registers itself at import.

        Raises:
            ValidationFailed: The class declares no `source_type`, or that type is
                already claimed — a silent overwrite would make which adapter runs
                depend on import order.
        """
        source_type = getattr(adapter, "source_type", None)
        if source_type is None:
            raise ValidationFailed(f"{adapter.__name__} does not declare a `source_type`.")
        existing = self._adapters.get(source_type)
        if existing is not None and existing is not adapter:
            raise ValidationFailed(
                f"Source type {source_type.value!r} is already handled by {existing.__name__}; "
                f"{adapter.__name__} cannot also claim it."
            )
        self._adapters[source_type] = adapter
        return adapter

    def get(self, source_type: SourceType) -> type[IntegrationAdapter]:
        """The adapter class for `source_type`.

        Raises:
            ValidationFailed: No adapter is registered for that source type.
        """
        try:
            return self._adapters[source_type]
        except KeyError:
            raise ValidationFailed(
                f"No adapter is registered for source type {source_type.value!r}.",
                known=sorted(item.value for item in self._adapters),
            ) from None

    def create(self, context: AdapterContext) -> IntegrationAdapter:
        """Build the adapter instance for one integration."""
        return self.get(context.manifest.source.type)(context)

    def supported(self) -> tuple[SourceType, ...]:
        """Every source type this hub can handle."""
        return tuple(sorted(self._adapters, key=lambda item: item.value))

    def __contains__(self, source_type: object) -> bool:
        return source_type in self._adapters


adapter_registry = AdapterRegistry()
"""The process-wide registry. Extend it by calling `register` at import time."""

for _adapter in (RemoteAdapter, GitAdapter, NpmAdapter, PythonAdapter, DockerAdapter, BuiltinAdapter):
    adapter_registry.register(_adapter)
