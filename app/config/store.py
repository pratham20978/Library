"""Reading and writing the YAML contract, safely (arch §38).

Every configuration file is parsed into a Pydantic model and re-rendered from
one. There is no code path that edits YAML as text, because that is how a
lock file loses a field or a catalog gains a duplicate key during a concurrent
update.

Writes are atomic and serialised:

* the document is rendered, written to a sibling temp file, `fsync`ed, then
  `os.replace`d onto the target — a reader either sees the whole old file or
  the whole new one, never a half-written one, even if the process dies mid-write;
* an advisory lock file serialises writers within and across processes, so the
  CLI and the API cannot interleave read-modify-write cycles and lose an edit.

Comments in hand-edited YAML do not survive a rewrite. That is a deliberate
trade: a machine-owned file with guaranteed integrity beats a prettier one that
can be corrupted.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from app.config.models import Catalog, IntegrationManifest, LockFile, PolicyDocument
from app.core.errors import InvalidConfiguration
from app.core.logging import get_logger

__all__ = ["ConfigStore", "dump_yaml", "read_model", "write_model"]

log = get_logger(__name__)

_LOCK_SUFFIX = ".lock"
_LOCK_TIMEOUT_SECONDS = 30.0


def dump_yaml(model: BaseModel) -> str:
    """Render a model as deterministic YAML.

    `mode="json"` so enums, URLs, and datetimes become primitives PyYAML can
    represent; key order is the model's declaration order, not alphabetical, so
    diffs stay readable.
    """
    payload = model.model_dump(mode="json", exclude_none=True, by_alias=False)
    return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False, allow_unicode=True, width=100)


def read_model[ModelT: BaseModel](path: Path, model: type[ModelT], *, default: ModelT | None = None) -> ModelT:
    """Parse `path` into `model`.

    Args:
        path: File to read.
        model: Target model type.
        default: Returned when the file is absent. Without one, absence is an error.

    Raises:
        InvalidConfiguration: The file is missing with no default, is not a YAML
            mapping, or fails validation. The message names the offending field.
    """
    if not path.exists():
        if default is not None:
            return default
        raise InvalidConfiguration(f"Configuration file not found: {path}", path=str(path))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InvalidConfiguration(f"{path.name} is not valid YAML: {exc}", path=str(path)) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise InvalidConfiguration(
            f"{path.name} must contain a YAML mapping at the top level, found {type(raw).__name__}.",
            path=str(path),
        )
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise InvalidConfiguration(f"{path.name} is invalid:\n{_format_errors(exc)}", path=str(path)) from exc


def write_model(path: Path, model: BaseModel) -> None:
    """Atomically replace `path` with the rendering of `model`.

    The temp file is created in the destination directory so `os.replace` stays
    a same-filesystem rename, which is the part that makes this atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = dump_yaml(model)
    # Not a `with` block: the file must outlive the handle so it can be renamed
    # onto the target, and `delete=False` means we own cleanup on failure.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Persist the rename itself, not just the file contents."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - not all platforms allow this
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def _format_errors(exc: ValidationError) -> str:
    """Render validation failures as operator-readable lines."""
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


class _FileLock:
    """Advisory exclusive lock guarding one configuration file.

    POSIX `flock` on a sidecar `.lock` file. Cross-process, released by the OS
    if the holder dies, and re-entrant within a process via a depth counter so
    a nested `mutate` does not deadlock against itself.
    """

    def __init__(self, target: Path, *, timeout: float = _LOCK_TIMEOUT_SECONDS) -> None:
        self._path = target.with_name(target.name + _LOCK_SUFFIX)
        self._timeout = timeout
        self._fd: int | None = None
        self._depth = 0

    def __enter__(self) -> None:
        if self._depth > 0:
            self._depth += 1
            return
        import fcntl
        import time

        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise InvalidConfiguration(
                        f"Timed out after {self._timeout:g}s waiting for the lock on {self._path.name}. "
                        "Another process is writing configuration.",
                        path=str(self._path),
                    ) from None
                time.sleep(0.05)
        self._fd = fd
        self._depth = 1

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._depth -= 1
        if self._depth > 0 or self._fd is None:
            return
        import fcntl

        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


class ConfigStore:
    """Typed access to the hub's configuration files.

    One instance per process. Reads go straight to disk so an operator's manual
    edit is picked up by the next `reconcile` without a restart; writes go
    through `update_catalog`/`update_lock`, which hold the file lock across the
    whole read-modify-write rather than just the write.
    """

    def __init__(
        self,
        *,
        catalog_path: Path,
        lock_path: Path,
        policies_path: Path,
        manifests_dir: Path,
    ) -> None:
        self.catalog_path = catalog_path
        self.lock_path = lock_path
        self.policies_path = policies_path
        self.manifests_dir = manifests_dir
        self._catalog_lock = _FileLock(catalog_path)
        self._lockfile_lock = _FileLock(lock_path)

    @classmethod
    def from_settings(cls, settings: Any) -> ConfigStore:
        """Build a store from `Settings` without importing it (avoids a cycle)."""
        return cls(
            catalog_path=settings.catalog_path,
            lock_path=settings.lock_path,
            policies_path=settings.policies_path,
            manifests_dir=settings.manifests_dir,
        )

    # ------------------------------------------------------------------ reads

    def load_catalog(self) -> Catalog:
        """Read desired state. An absent file means an empty catalog."""
        return read_model(self.catalog_path, Catalog, default=Catalog())

    def load_lock(self) -> LockFile:
        """Read resolved state. An absent file means nothing is installed."""
        return read_model(self.lock_path, LockFile, default=LockFile())

    def load_policies(self) -> PolicyDocument:
        """Read authorization rules. An absent file means defaults only."""
        return read_model(self.policies_path, PolicyDocument, default=PolicyDocument())

    def load_manifests(self) -> dict[str, IntegrationManifest]:
        """Load every manifest in the manifest directory (arch §50).

        A manifest whose `id` disagrees with its filename is rejected: the
        filename is what an operator greps for, and a mismatch makes the catalog
        silently reference a different integration than it appears to.

        Raises:
            InvalidConfiguration: A manifest is invalid or its id is duplicated.
        """
        manifests: dict[str, IntegrationManifest] = {}
        if not self.manifests_dir.exists():
            return manifests
        for path in sorted(self.manifests_dir.glob("*.y*ml")):
            manifest = read_model(path, IntegrationManifest)
            if manifest.id != path.stem:
                raise InvalidConfiguration(
                    f"Manifest {path.name} declares id {manifest.id!r}; expected {path.stem!r} "
                    "to match the filename.",
                    path=str(path),
                )
            if manifest.id in manifests:
                raise InvalidConfiguration(f"Duplicate manifest id {manifest.id!r}.", path=str(path))
            manifests[manifest.id] = manifest
        return manifests

    def load_manifest(self, integration_id: str) -> IntegrationManifest | None:
        """Load one manifest by id, or `None` when there is no file for it."""
        for suffix in (".yaml", ".yml"):
            path = self.manifests_dir / f"{integration_id}{suffix}"
            if path.exists():
                return read_model(path, IntegrationManifest)
        return None

    # ----------------------------------------------------------------- writes

    def update_catalog(self, mutator: Callable[[Catalog], Catalog]) -> Catalog:
        """Read-modify-write the catalog under an exclusive lock.

        `mutator` receives the catalog as it is on disk *right now* — read inside
        the lock, so two concurrent enable/disable operations cannot both build on
        the same stale copy and have one silently overwrite the other. Returning
        an unchanged catalog skips the write entirely, which keeps a no-op
        `reconcile` from churning the file's mtime.
        """
        with self._catalog_lock:
            current = self.load_catalog()
            updated = mutator(current)
            if updated == current:
                return current
            write_model(self.catalog_path, updated)
            log.info("catalog.written", path=str(self.catalog_path), integrations=len(updated.integrations))
            return updated

    def update_lock(self, mutator: Callable[[LockFile], LockFile]) -> LockFile:
        """Read-modify-write the lock file under an exclusive lock.

        `generated_at` is stamped here rather than by callers, so every write
        carries an accurate timestamp and no caller can forget one.
        """
        with self._lockfile_lock:
            current = self.load_lock()
            updated = mutator(current)
            if updated.integrations == current.integrations and updated.version == current.version:
                return current
            from app.core.clock import utcnow

            updated = updated.model_copy(update={"generated_at": utcnow()})
            write_model(self.lock_path, updated)
            log.info("lockfile.written", path=str(self.lock_path), integrations=len(updated.integrations))
            return updated

    def write_manifest(self, manifest: IntegrationManifest) -> Path:
        """Persist a manifest, e.g. after a registry install (arch §56)."""
        path = self.manifests_dir / f"{manifest.id}.yaml"
        write_model(path, manifest)
        log.info("manifest.written", integration=manifest.id, path=str(path))
        return path

    def remove_manifest(self, integration_id: str) -> bool:
        """Delete a manifest. Returns whether a file was actually removed."""
        removed = False
        for suffix in (".yaml", ".yml"):
            path = self.manifests_dir / f"{integration_id}{suffix}"
            if path.exists():
                path.unlink()
                removed = True
        return removed
