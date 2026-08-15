"""Rollback points (arch §19, §39).

Arch §39 requires a backup before any update, removal, or configuration
migration, and arch §19 says what a rollback must restore: the version, the lock
entry, the configuration, and the tool metadata.

What a backup deliberately does *not* contain is credentials. Arch §19 and §21
both forbid it, and the reason is worth stating: a backup directory is copied,
archived, and mounted far more casually than a secret store, so a credential
inside one outlives every control placed on the original. Secrets stay where the
secret manager put them, and a rollback re-resolves them rather than restoring
them.

The code itself is never copied. Versions already live in immutable directories
under `runtime/integrations/<id>/versions/<ref>/`, so rolling back is repointing
`current` — a rename, not a restore. The backup only holds the metadata needed to
know *which* version to point at and what the world looked like when it served.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.models import LockEntry
from app.core.clock import utcnow
from app.core.errors import RollbackFailed
from app.core.logging import get_logger

__all__ = ["BackupManager", "RollbackPointRecord"]

log = get_logger(__name__)

_MANIFEST_NAME = "rollback.json"
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True, slots=True)
class RollbackPointRecord:
    """A restorable snapshot of one integration's state."""

    integration_id: str
    version_id: str
    path: Path
    created_at: datetime
    reason: str
    lock_entry: dict[str, Any] | None
    tools: list[str]
    """Qualified tool names that were live, so a rollback can report the delta."""

    @property
    def label(self) -> str:
        """Directory name, which doubles as the operator-facing identifier."""
        return self.path.name

    def summary(self) -> dict[str, Any]:
        """Compact form for `mcp-hub rollback --list` and the REST API."""
        return {
            "id": self.label,
            "integration": self.integration_id,
            "version": self.version_id,
            "created_at": self.created_at.isoformat(),
            "reason": self.reason,
            "tools": len(self.tools),
        }


class BackupManager:
    """Creates, lists, and prunes rollback points."""

    def __init__(self, backups_dir: Path, *, retention: int = 10) -> None:
        self._root = backups_dir
        self._retention = retention

    def _dir_for(self, integration_id: str) -> Path:
        return self._root / integration_id

    def create(
        self,
        integration_id: str,
        *,
        version_id: str,
        reason: str = "update",
        lock_entry: LockEntry | None = None,
        tools: list[str] | None = None,
        created_by: str | None = None,
    ) -> RollbackPointRecord:
        """Record a rollback point before a mutating operation (arch §39).

        Raises:
            RollbackFailed: The backup directory could not be written. Failing
                here aborts the operation on purpose — arch §15 requires a
                rollback point to exist *before* anything changes, and proceeding
                without one would leave no way back.
        """
        stamp = utcnow()
        target = self._dir_for(integration_id) / stamp.strftime(_TIMESTAMP_FORMAT)
        suffix = 1
        while target.exists():
            target = target.with_name(f"{stamp.strftime(_TIMESTAMP_FORMAT)}-{suffix}")
            suffix += 1

        payload = {
            "integration": integration_id,
            "version": version_id,
            "created_at": stamp.isoformat(),
            "created_by": created_by,
            "reason": reason,
            "lock_entry": lock_entry.model_dump(mode="json") if lock_entry else None,
            "tools": sorted(tools or []),
            # Stated in the artifact itself, so anyone who finds a backup knows
            # it is not a place to look for credentials.
            "note": "Contains configuration and metadata only. Credentials are never backed up.",
        }
        try:
            target.mkdir(parents=True)
            (target / _MANIFEST_NAME).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            raise RollbackFailed(
                f"Could not create a rollback point for {integration_id!r}: {exc}. Refusing to continue without one.",
                integration=integration_id,
            ) from exc

        log.info(
            "backup.created",
            integration=integration_id,
            version=version_id,
            reason=reason,
            path=str(target),
        )
        self.prune(integration_id)
        return RollbackPointRecord(
            integration_id=integration_id,
            version_id=version_id,
            path=target,
            created_at=stamp,
            reason=reason,
            lock_entry=payload["lock_entry"],  # type: ignore[arg-type]
            tools=list(payload["tools"]),  # type: ignore[arg-type]
        )

    def list(self, integration_id: str) -> list[RollbackPointRecord]:
        """Rollback points for one integration, newest first."""
        directory = self._dir_for(integration_id)
        if not directory.exists():
            return []
        records: list[RollbackPointRecord] = []
        for child in sorted(directory.iterdir(), reverse=True):
            record = self._read(integration_id, child)
            if record is not None:
                records.append(record)
        return records

    def latest(self, integration_id: str) -> RollbackPointRecord | None:
        """The most recent rollback point, which is what a bare `rollback` uses."""
        records = self.list(integration_id)
        return records[0] if records else None

    def get(self, integration_id: str, label: str) -> RollbackPointRecord:
        """One rollback point by its directory label.

        Raises:
            RollbackFailed: No such rollback point.
        """
        record = self._read(integration_id, self._dir_for(integration_id) / label)
        if record is None:
            available = [item.label for item in self.list(integration_id)]
            raise RollbackFailed(
                f"No rollback point {label!r} for {integration_id!r}.",
                integration=integration_id,
                available=available,
            )
        return record

    def _read(self, integration_id: str, path: Path) -> RollbackPointRecord | None:
        manifest = path / _MANIFEST_NAME
        if not manifest.is_file():
            return None
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("backup.unreadable", integration=integration_id, path=str(path), error=str(exc))
            return None
        try:
            created_at = datetime.fromisoformat(str(payload["created_at"]))
        except (KeyError, ValueError):
            created_at = utcnow()
        return RollbackPointRecord(
            integration_id=integration_id,
            version_id=str(payload.get("version", "")),
            path=path,
            created_at=created_at,
            reason=str(payload.get("reason", "unknown")),
            lock_entry=payload.get("lock_entry"),
            tools=list(payload.get("tools") or []),
        )

    def prune(self, integration_id: str) -> int:
        """Drop rollback points beyond the retention limit. Returns how many."""
        records = self.list(integration_id)
        excess = records[self._retention :]
        for record in excess:
            shutil.rmtree(record.path, ignore_errors=True)
        if excess:
            log.info("backup.pruned", integration=integration_id, removed=len(excess))
        return len(excess)

    def purge(self, integration_id: str) -> int:
        """Remove every rollback point for an integration, on full removal."""
        directory = self._dir_for(integration_id)
        if not directory.exists():
            return 0
        count = len(self.list(integration_id))
        shutil.rmtree(directory, ignore_errors=True)
        log.info("backup.purged", integration=integration_id, removed=count)
        return count

    def restore_lock_entry(self, record: RollbackPointRecord) -> LockEntry | None:
        """Rebuild the lock entry a rollback point captured (arch §19)."""
        if record.lock_entry is None:
            return None
        try:
            return LockEntry.model_validate(record.lock_entry)
        except Exception as exc:  # noqa: BLE001 - a corrupt backup must not crash a rollback
            log.error("backup.lock_entry_invalid", integration=record.integration_id, error=str(exc))
            return None
