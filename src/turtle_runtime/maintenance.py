from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import sys
from typing import Callable, Iterable, Iterator
import unicodedata


MIB = 1024**2
GIB = 1024**3
LOG_ROTATE_BYTES = 10 * MIB
LOG_GENERATIONS = 30
DAILY_BACKUPS = 35
WEEKLY_BACKUPS = 6
_POSIX = os.name == "posix"


class MaintenanceError(RuntimeError):
    """Fail-closed maintenance error with no database or log content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SQLiteBackupResult:
    source: Path
    destination: Path
    manifest: Path
    sha256: str
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True)
class BackupRetentionItem:
    path: Path
    created_at: datetime


@dataclass(frozen=True)
class BackupRetentionResult:
    daily: tuple[Path, ...]
    weekly: tuple[Path, ...]
    removed: tuple[Path, ...]

    @property
    def kept(self) -> tuple[Path, ...]:
        return self.daily + self.weekly


@dataclass(frozen=True)
class DiskSpaceStatus:
    level: str
    total_bytes: int
    free_bytes: int
    free_fraction: float


@dataclass(frozen=True)
class LogRotationResult:
    rotated: tuple[Path, ...]
    skipped: tuple[Path, ...]
    missing: tuple[Path, ...]


def sha256_manifest_path(database: str | Path) -> Path:
    path = Path(database)
    return path.with_name(f"{path.name}.sha256")


def retention_tombstone_path(database: str | Path) -> Path:
    path = Path(database)
    return path.with_name(f"{path.name}.retention-delete")


def _retention_tombstone_temporary_path(database: str | Path) -> Path:
    path = Path(database)
    return path.with_name(f".{path.name}.retention-delete.tmp")


def backup_sqlite(
    source: str | Path,
    destination: str | Path,
    *,
    replace_existing: bool = False,
) -> SQLiteBackupResult:
    """Create, resume, or explicitly replace one checked SQLite snapshot."""

    if type(replace_existing) is not bool:
        raise TypeError("replace_existing must be a boolean")
    source_path = _existing_explicit_file(source, "sqlite_source_invalid")
    destination_path = Path(destination)
    if not destination_path.is_absolute():
        raise MaintenanceError("sqlite_destination_invalid")
    _explicit_parent(destination_path, "sqlite_destination_invalid")
    if source_path.name.endswith(("-wal", "-shm")):
        raise MaintenanceError("sqlite_auxiliary_file_rejected")
    if source_path.resolve() == destination_path.resolve(strict=False):
        raise MaintenanceError("sqlite_destination_invalid")
    _quick_check_path(source_path, "sqlite_source_quick_check_failed")
    if _backup_artifact_state(retention_tombstone_path(destination_path)) != "missing":
        raise MaintenanceError("sqlite_destination_retired")
    manifest_path = sha256_manifest_path(destination_path)
    if replace_existing:
        destination_state = _backup_artifact_state(destination_path)
        manifest_state = _backup_artifact_state(manifest_path)
        _quarantine_backup_artifacts(
            path
            for path, state in (
                (destination_path, destination_state),
                (manifest_path, manifest_state),
            )
            if state == "file"
        )
    else:
        resumed = _resume_or_quarantine_backup(
            source=source_path,
            destination=destination_path,
            manifest=manifest_path,
        )
        if resumed is not None:
            return resumed
    _new_explicit_file(destination_path, "sqlite_destination_invalid")
    _new_explicit_file(manifest_path, "sqlite_manifest_exists")

    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    destination_created = False
    manifest_created = False
    try:
        source_connection = sqlite3.connect(
            f"{source_path.as_uri()}?mode=ro",
            uri=True,
            timeout=30,
        )
        _quick_check(source_connection, "sqlite_source_quick_check_failed")

        descriptor = os.open(
            destination_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(descriptor)
        destination_created = True
        destination_connection = sqlite3.connect(destination_path, timeout=30)
        source_connection.backup(destination_connection)
        _quick_check(destination_connection, "sqlite_backup_quick_check_failed")
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None

        _private_mode(destination_path)
        _fsync_file(destination_path)
        digest = _sha256(destination_path)
        descriptor = os.open(
            manifest_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        manifest_created = True
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write(f"{digest}\n")
            handle.flush()
            os.fsync(handle.fileno())
        _private_mode(manifest_path)
        _fsync_directory(destination_path.parent)
        return SQLiteBackupResult(
            source=source_path,
            destination=destination_path,
            manifest=manifest_path,
            sha256=digest,
            size_bytes=destination_path.stat().st_size,
            created_at=datetime.now(timezone.utc),
        )
    except MaintenanceError:
        _close_quietly(destination_connection)
        _close_quietly(source_connection)
        _remove_created(manifest_path, manifest_created)
        _remove_created(destination_path, destination_created)
        raise
    except (OSError, sqlite3.Error):
        _close_quietly(destination_connection)
        _close_quietly(source_connection)
        _remove_created(manifest_path, manifest_created)
        _remove_created(destination_path, destination_created)
        raise MaintenanceError("sqlite_backup_failed") from None


def quarantine_invalid_sqlite_backup(destination: str | Path) -> bool:
    """Validate an old dated snapshot and quarantine it without rewriting history."""

    destination_path = Path(destination)
    if not destination_path.is_absolute():
        raise MaintenanceError("sqlite_destination_invalid")
    manifest_path = sha256_manifest_path(destination_path)
    destination_state = _backup_artifact_state(destination_path)
    manifest_state = _backup_artifact_state(manifest_path)
    if destination_state == manifest_state == "missing":
        return False
    resumed = _resume_or_quarantine_backup(
        source=destination_path,
        destination=destination_path,
        manifest=manifest_path,
    )
    return resumed is None


def finish_backup_retention_removal(destination: str | Path) -> bool:
    """Enforce a durable backup-pair deletion intent after a crash or restore.

    The tombstone is an append-only filesystem ledger entry, not a temporary
    work file.  Keeping it after the pair is gone makes the deletion intent
    independent of a later restore of an older planner-database snapshot.
    """

    destination_path = Path(destination)
    if not destination_path.is_absolute():
        raise MaintenanceError("backup_retention_path_invalid")
    tombstone = retention_tombstone_path(destination_path)
    state = _backup_artifact_state(tombstone)
    if state == "missing":
        return False
    try:
        if tombstone.read_text(encoding="ascii") != "retention-delete-v1\n":
            raise MaintenanceError("backup_retention_tombstone_invalid")
        _private_mode(tombstone)
        removed = False
        for path in (destination_path, sha256_manifest_path(destination_path)):
            artifact_state = _backup_artifact_state(path)
            if artifact_state == "file":
                path.unlink()
                removed = True
        if removed:
            _fsync_directory(destination_path.parent)
    except MaintenanceError:
        raise
    except (OSError, UnicodeError):
        raise MaintenanceError("backup_retention_remove_failed") from None
    return True


def _resume_or_quarantine_backup(
    *,
    source: Path,
    destination: Path,
    manifest: Path,
) -> SQLiteBackupResult | None:
    destination_state = _backup_artifact_state(destination)
    manifest_state = _backup_artifact_state(manifest)
    if destination_state == manifest_state == "missing":
        return None

    if destination_state == manifest_state == "file":
        try:
            expected_digest = _read_sha256_manifest(manifest)
            actual_digest = _sha256(destination)
            if expected_digest != actual_digest:
                raise MaintenanceError("sqlite_existing_backup_invalid")
            _quick_check_path(destination, "sqlite_existing_backup_invalid")
        except MaintenanceError:
            pass
        else:
            _private_mode(destination)
            _private_mode(manifest)
            try:
                created_at = datetime.fromtimestamp(
                    destination.stat().st_mtime,
                    tz=timezone.utc,
                )
                size_bytes = destination.stat().st_size
            except OSError:
                raise MaintenanceError("sqlite_existing_backup_invalid") from None
            return SQLiteBackupResult(
                source=source,
                destination=destination,
                manifest=manifest,
                sha256=actual_digest,
                size_bytes=size_bytes,
                created_at=created_at,
            )

    _quarantine_backup_artifacts(
        path
        for path, state in (
            (destination, destination_state),
            (manifest, manifest_state),
        )
        if state == "file"
    )
    return None


def _backup_artifact_state(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError:
        raise MaintenanceError("sqlite_existing_backup_invalid") from None
    if stat.S_ISLNK(mode):
        raise MaintenanceError("sqlite_existing_backup_symlink")
    if not stat.S_ISREG(mode):
        raise MaintenanceError("sqlite_existing_backup_invalid")
    return "file"


def _read_sha256_manifest(path: Path) -> str:
    try:
        raw = path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise MaintenanceError("sqlite_existing_backup_invalid") from None
    digest = raw.removesuffix("\n")
    if (
        raw != f"{digest}\n"
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise MaintenanceError("sqlite_existing_backup_invalid")
    return digest


def _quarantine_backup_artifacts(paths: Iterable[Path]) -> None:
    artifacts = tuple(paths)
    if not artifacts:
        return
    token = secrets.token_hex(12)
    targets = tuple(
        path.with_name(f".{path.name}.quarantine-{token}") for path in artifacts
    )
    for path in artifacts:
        _existing_explicit_file(path, "sqlite_existing_backup_invalid")
        _private_mode(path)
    for target in targets:
        _new_explicit_file(target, "sqlite_backup_quarantine_failed")

    moved: list[tuple[Path, Path]] = []
    try:
        for source, target in zip(artifacts, targets, strict=True):
            os.rename(source, target)
            moved.append((source, target))
        _fsync_directory(artifacts[0].parent)
    except (MaintenanceError, OSError):
        for source, target in reversed(moved):
            try:
                if not source.exists() and not source.is_symlink():
                    os.rename(target, source)
            except OSError:
                pass
        raise MaintenanceError("sqlite_backup_quarantine_failed") from None


def prune_backup_retention(
    backups: Iterable[BackupRetentionItem],
    *,
    daily_keep: int = DAILY_BACKUPS,
    weekly_keep: int = WEEKLY_BACKUPS,
    authorize_remove: Callable[[Path], None] | None = None,
) -> BackupRetentionResult:
    """Keep unique daily files, then weekly files from weeks not held daily."""

    if daily_keep < 0 or weekly_keep < 0:
        raise MaintenanceError("backup_retention_invalid")
    normalized: list[BackupRetentionItem] = []
    seen_paths: set[Path] = set()
    for item in backups:
        path = _existing_explicit_file(item.path, "backup_retention_path_invalid")
        if (
            path in seen_paths
            or item.created_at.tzinfo is None
            or item.created_at.utcoffset() is None
        ):
            raise MaintenanceError("backup_retention_invalid")
        manifest = _existing_explicit_file(
            sha256_manifest_path(path),
            "backup_retention_manifest_invalid",
        )
        if manifest == path:
            raise MaintenanceError("backup_retention_manifest_invalid")
        seen_paths.add(path)
        normalized.append(BackupRetentionItem(path, item.created_at))

    ordered = sorted(
        normalized,
        key=lambda item: (item.created_at.astimezone(timezone.utc), str(item.path)),
        reverse=True,
    )
    daily: list[BackupRetentionItem] = []
    daily_dates = set()
    for item in ordered:
        backup_date = item.created_at.date()
        if backup_date not in daily_dates and len(daily) < daily_keep:
            daily.append(item)
            daily_dates.add(backup_date)

    daily_paths = {item.path for item in daily}
    daily_weeks = {item.created_at.date().isocalendar()[:2] for item in daily}
    weekly: list[BackupRetentionItem] = []
    weekly_weeks = set()
    for item in ordered:
        week = item.created_at.date().isocalendar()[:2]
        if (
            item.path in daily_paths
            or week in daily_weeks
            or week in weekly_weeks
            or len(weekly) >= weekly_keep
        ):
            continue
        weekly.append(item)
        weekly_weeks.add(week)

    kept_paths = daily_paths | {item.path for item in weekly}
    removed = tuple(item.path for item in ordered if item.path not in kept_paths)
    for path in removed:
        tombstone = retention_tombstone_path(path)
        temporary_tombstone = _retention_tombstone_temporary_path(path)
        try:
            if authorize_remove is not None:
                authorize_remove(path)
            temporary_state = _backup_artifact_state(temporary_tombstone)
            if temporary_state == "file":
                temporary_tombstone.unlink()
            _new_explicit_file(tombstone, "backup_retention_tombstone_exists")
            descriptor = os.open(
                temporary_tombstone,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
                handle.write("retention-delete-v1\n")
                handle.flush()
                os.fsync(handle.fileno())
            _private_mode(temporary_tombstone)
            os.replace(temporary_tombstone, tombstone)
            _fsync_directory(path.parent)
            finish_backup_retention_removal(path)
        except MaintenanceError:
            raise
        except OSError:
            raise MaintenanceError("backup_retention_remove_failed") from None
    return BackupRetentionResult(
        daily=tuple(item.path for item in daily),
        weekly=tuple(item.path for item in weekly),
        removed=removed,
    )


def classify_disk_space(total_bytes: int, free_bytes: int) -> DiskSpaceStatus:
    if total_bytes <= 0 or free_bytes < 0 or free_bytes > total_bytes:
        raise MaintenanceError("disk_usage_invalid")
    free_fraction = free_bytes / total_bytes
    if free_fraction < 0.10 or free_bytes < 5 * GIB:
        level = "critical"
    elif free_fraction < 0.20 or free_bytes < 10 * GIB:
        level = "warning"
    else:
        level = "ok"
    return DiskSpaceStatus(level, total_bytes, free_bytes, free_fraction)


def check_disk_space(path: str | Path) -> DiskSpaceStatus:
    target = Path(path)
    if not target.is_absolute():
        raise MaintenanceError("disk_path_invalid")
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        raise MaintenanceError("disk_usage_failed") from None
    return classify_disk_space(usage.total, usage.free)


def rotate_logs(
    paths: Iterable[str | Path],
    *,
    max_bytes: int = LOG_ROTATE_BYTES,
    generations: int = LOG_GENERATIONS,
) -> LogRotationResult:
    """Compress only the exact log paths supplied by the caller."""

    if (
        type(max_bytes) is not int
        or max_bytes < 1
        or type(generations) is not int
        or generations < 1
        or generations > 10_000
    ):
        raise MaintenanceError("log_rotation_invalid")
    seen: set[Path] = set()
    requested: list[Path] = []
    for value in paths:
        path = Path(value)
        if not path.is_absolute() or path in seen:
            raise MaintenanceError("log_path_invalid")
        seen.add(path)
        _explicit_parent(path, "log_path_invalid")
        requested.append(path)

    _validate_log_namespace_disjoint(requested)
    with ExitStack() as locks:
        for path in sorted(requested, key=os.fspath):
            locks.enter_context(_log_rotation_lock(path))
        return _rotate_logs_locked(
            requested,
            max_bytes=max_bytes,
            generations=generations,
        )


def _rotate_logs_locked(
    requested: list[Path],
    *,
    max_bytes: int,
    generations: int,
) -> LogRotationResult:
    for path in requested:
        _validate_log_rotation_artifacts(path, generations)
        for generation in range(1, generations + 1):
            candidate = _log_generation(path, generation)
            if candidate.exists() or candidate.is_symlink():
                _existing_explicit_file(candidate, "log_generation_invalid")

    # Recover every interrupted exact-path rotation only after the complete input
    # set has passed validation. This preserves the all-or-nothing validation
    # guarantee for callers that supply more than one log.
    for path in requested:
        try:
            _recover_log_rotation(path, generations)
        except MaintenanceError:
            raise
        except (EOFError, OSError):
            raise MaintenanceError("log_rotation_failed") from None
    for path in requested:
        _reconcile_raw_log_generations(path)
        _require_log_generation_bound(path, generations)

    logs: list[Path] = []
    missing: list[Path] = []
    for path in requested:
        if not path.exists() and not path.is_symlink():
            missing.append(path)
            continue
        logs.append(_existing_explicit_file(path, "log_path_invalid"))

    rotated: list[Path] = []
    skipped: list[Path] = []
    for path in logs:
        if path.stat().st_size < max_bytes:
            skipped.append(path)
            continue
        _rotate_log(path, generations)
        rotated.append(path)
    return LogRotationResult(tuple(rotated), tuple(skipped), tuple(missing))


def _validate_log_namespace_disjoint(paths: list[Path]) -> None:
    occupied: dict[tuple[str, str], int] = {}
    for owner, path in enumerate(paths):
        try:
            parent = str(path.parent.resolve(strict=True))
        except OSError:
            raise MaintenanceError("log_path_invalid") from None
        parent_key = unicodedata.normalize("NFC", parent).casefold()
        for name in _reserved_log_names(path.name):
            name_key = unicodedata.normalize("NFC", name).casefold()
            key = (parent_key, name_key)
            previous = occupied.setdefault(key, owner)
            if previous != owner:
                raise MaintenanceError("log_path_namespace_collision")


def _reserved_log_names(name: str) -> Iterator[str]:
    yield name
    yield f".{name}.rotation.tmp.gz"
    yield f".{name}.rotation.source"
    yield f".{name}.rotation.state"
    yield f".{name}.rotation.state.tmp"
    yield f".{name}.maintenance.lock"
    for generation in range(1, 10_001):
        yield f"{name}.{generation}.gz"
        yield f".{name}.rotation.generation.{generation}.gz"
        yield f".{name}.generation.{generation}.raw"
        yield f".{name}.rotation.raw.{generation}.raw"
        yield f".{name}.raw-reconcile.{generation}.tmp.gz"


@contextmanager
def _log_rotation_lock(path: Path) -> Iterator[None]:
    lock_path = _log_lock(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    locked = False
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise MaintenanceError("log_rotation_lock_invalid")
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                if _POSIX:
                    raise MaintenanceError("log_rotation_lock_invalid") from None
        elif _POSIX:
            raise MaintenanceError("log_rotation_lock_invalid")
        lock_stat = os.fstat(descriptor)
        visible_lock_stat = lock_path.lstat()
        if (
            not stat.S_ISREG(visible_lock_stat.st_mode)
            or visible_lock_stat.st_nlink != 1
            or (visible_lock_stat.st_dev, visible_lock_stat.st_ino)
            != (lock_stat.st_dev, lock_stat.st_ino)
        ):
            raise MaintenanceError("log_rotation_lock_invalid")
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MaintenanceError("log_rotation_locked") from None
        elif os.name == "nt":
            import msvcrt

            if lock_stat.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise MaintenanceError("log_rotation_locked") from None
        locked = True
        yield
    except MaintenanceError:
        raise
    except OSError:
        raise MaintenanceError("log_rotation_lock_failed") from None
    finally:
        if descriptor is not None:
            if locked:
                try:
                    if os.name == "posix":
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    elif os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            os.close(descriptor)


def _rotate_log(path: Path, generations: int) -> None:
    source_temporary = _log_source_temporary(path)
    standard_fds: tuple[int, ...] = ()
    try:
        standard_fds = _matching_standard_descriptors(path)
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream.fileno() in standard_fds:
                    stream.flush()
            except (AttributeError, OSError, ValueError):
                continue
        _set_log_rotation_phase(path, "cutover", generations)
        os.replace(path, source_temporary)
        replacement_fd = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            for descriptor_number in standard_fds:
                os.dup2(replacement_fd, descriptor_number)
        finally:
            os.close(replacement_fd)
        _private_mode(path)
        _fsync_directory(path.parent)
        _set_log_rotation_phase(path, "compress", generations)
        _resume_log_rotation(path, "compress", generations)
    except MaintenanceError:
        if _log_rotation_phase_or_none(path) == "compress":
            _rollback_busy_log_rotation(path, standard_fds)
        raise
    except OSError:
        raise MaintenanceError("log_rotation_failed") from None


def _validate_log_rotation_artifacts(path: Path, generations: int) -> None:
    if path.exists() or path.is_symlink():
        _existing_log_artifact(path, "log_path_invalid")
    for candidate in (
        _log_temporary(path),
        _log_source_temporary(path),
        _log_state(path),
        _log_state_temporary(path),
    ):
        if candidate.exists() or candidate.is_symlink():
            _existing_log_artifact(candidate, "log_rotation_temporary_exists")
    durable_state: tuple[str, int] | None = None
    state_path = _log_state(path)
    if state_path.exists() or state_path.is_symlink():
        durable_state = _parse_log_rotation_state(state_path)
    temporary_state: tuple[str, int] | None = None
    state_temporary = _log_state_temporary(path)
    if state_temporary.exists() or state_temporary.is_symlink():
        try:
            temporary_state = _parse_log_rotation_state(state_temporary)
        except MaintenanceError:
            # A killed state write may leave an incomplete temp. The prior
            # durable state, when present, remains authoritative; otherwise no
            # cutover could have started because state publish precedes rename.
            temporary_state = None
    transaction_generations = (
        durable_state[1]
        if durable_state is not None
        else temporary_state[1]
        if temporary_state is not None
        else generations
    )
    stage_indices = set(_log_generation_stage_indices(path)) | set(
        _log_raw_generation_stage_indices(path)
    )
    for generation in _log_generation_stage_indices(path):
        staged = _log_generation_stage(path, generation)
        _existing_log_artifact(staged, "log_rotation_temporary_exists")
    for generation in _log_raw_generation_stage_indices(path):
        staged = _log_raw_generation_stage(path, generation)
        _existing_log_artifact(staged, "log_rotation_temporary_exists")
    if stage_indices and durable_state is None and temporary_state is None:
        # A journal-less stage cannot be interpreted without risking a second
        # generation shift.
        raise MaintenanceError("log_rotation_state_invalid")
    if any(generation > transaction_generations for generation in stage_indices):
        raise MaintenanceError("log_rotation_state_invalid")
    for generation in _log_raw_generation_indices(path):
        _existing_log_artifact(
            _log_raw_generation(path, generation), "log_generation_invalid"
        )
    for generation in _log_raw_reconcile_temporary_indices(path):
        _existing_log_artifact(
            _log_raw_reconcile_temporary(path, generation),
            "log_rotation_temporary_exists",
        )
    _validate_log_rotation_transaction(path, transaction_generations)


def _recover_log_rotation(path: Path, requested_generations: int) -> None:
    state = _read_log_rotation_state(path)
    source = _log_source_temporary(path)
    temporary = _log_temporary(path)
    stage_indices = tuple(
        sorted(
            set(_log_generation_stage_indices(path))
            | set(_log_raw_generation_stage_indices(path))
        )
    )
    staged = bool(stage_indices)

    if state is not None:
        phase, generations = state
        if any(generation > generations for generation in stage_indices):
            raise MaintenanceError("log_rotation_state_invalid")
        _validate_log_rotation_transaction(path, generations)
        if phase == "cutover":
            if not source.exists() and not source.is_symlink():
                _clear_log_rotation_state(path)
                return
            _existing_explicit_file(source, "log_rotation_state_invalid")
            if not path.exists() and not path.is_symlink():
                os.replace(source, path)
                _fsync_directory(path.parent)
                _clear_log_rotation_state(path)
                return
            _existing_explicit_file(path, "log_path_invalid")
            _private_mode(path)
            _rebind_standard_descriptors(source, path)
            _set_log_rotation_phase(path, "compress", generations)
            phase = "compress"
        _resume_log_rotation(path, phase, generations)
        return
    if staged:
        raise MaintenanceError("log_rotation_state_invalid")
    if not source.exists():
        if temporary.exists() or temporary.is_symlink():
            raise MaintenanceError("log_rotation_temporary_exists")
        return
    _existing_explicit_file(source, "log_rotation_temporary_exists")
    if not path.exists() and not path.is_symlink():
        if temporary.exists() or temporary.is_symlink():
            raise MaintenanceError("log_rotation_state_invalid")
        os.replace(source, path)
        _fsync_directory(path.parent)
        return
    _existing_explicit_file(path, "log_path_invalid")
    _rebind_standard_descriptors(source, path)
    if temporary.exists() or temporary.is_symlink():
        _existing_explicit_file(temporary, "log_rotation_temporary_exists")
        temporary.unlink()
        _fsync_directory(path.parent)
    _set_log_rotation_phase(path, "compress", requested_generations)
    _resume_log_rotation(path, "compress", requested_generations)


def _resume_log_rotation(path: Path, phase: str, generations: int) -> None:
    source = _log_source_temporary(path)
    temporary = _log_temporary(path)
    if phase != "finalize":
        _existing_explicit_file(path, "log_path_invalid")
        _existing_explicit_file(source, "log_rotation_state_invalid")

    if phase == "compress":
        _compress_log_source(source, temporary)
        _set_log_rotation_phase(path, "stage", generations)
        phase = "stage"

    if phase == "stage":
        _existing_explicit_file(temporary, "log_rotation_state_invalid")
        if not _gzip_matches_source(temporary, source):
            raise MaintenanceError("log_rotation_compression_invalid")
        for generation in range(1, generations + 1):
            for current, staged in (
                (
                    _log_generation(path, generation),
                    _log_generation_stage(path, generation),
                ),
                (
                    _log_raw_generation(path, generation),
                    _log_raw_generation_stage(path, generation),
                ),
            ):
                if current.exists() or current.is_symlink():
                    _existing_explicit_file(current, "log_generation_invalid")
                    if staged.exists() or staged.is_symlink():
                        raise MaintenanceError("log_rotation_state_invalid")
                    os.replace(current, staged)
                elif staged.exists() or staged.is_symlink():
                    _existing_explicit_file(staged, "log_rotation_state_invalid")
        _fsync_directory(path.parent)
        _set_log_rotation_phase(path, "publish", generations)
        phase = "publish"

    if phase == "publish":
        _publish_log_generations(path, generations)
        _fsync_directory(path.parent)
        _set_log_rotation_phase(path, "finalize", generations)
        phase = "finalize"

    if phase == "finalize":
        _existing_explicit_file(path, "log_path_invalid")
        first = _existing_explicit_file(
            _log_generation(path, 1), "log_rotation_state_invalid"
        )
        raw_first = _log_raw_generation(path, 1)
        if source.exists() or source.is_symlink():
            _existing_explicit_file(source, "log_rotation_state_invalid")
            if raw_first.exists() or raw_first.is_symlink():
                raise MaintenanceError("log_rotation_state_invalid")
            if _POSIX:
                try:
                    os.chmod(source, 0o400)
                except OSError:
                    raise MaintenanceError("log_rotation_source_seal_failed") from None
            os.replace(source, raw_first)
            _fsync_directory(path.parent)
        elif raw_first.exists() or raw_first.is_symlink():
            _existing_explicit_file(raw_first, "log_rotation_state_invalid")
        else:
            raise MaintenanceError("log_rotation_state_invalid")
        _reconcile_raw_log_generation(path, 1)
        _validate_gzip(first)
        _clear_log_rotation_state(path)


def _compress_log_source(source: Path, temporary: Path) -> None:
    if temporary.exists() or temporary.is_symlink():
        _existing_explicit_file(temporary, "log_rotation_state_invalid")
        temporary.unlink()
        _fsync_directory(temporary.parent)
    source_stat = source.stat()
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with source.open("rb") as source_handle, os.fdopen(
            descriptor, "wb"
        ) as raw_output:
            descriptor = -1
            with gzip.GzipFile(
                fileobj=raw_output, mode="wb", filename="", mtime=0
            ) as output:
                while chunk := source_handle.read(1024 * 1024):
                    output.write(chunk)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        _private_mode(temporary)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    final_source_stat = source.stat()
    if (
        final_source_stat.st_dev != source_stat.st_dev
        or final_source_stat.st_ino != source_stat.st_ino
        or final_source_stat.st_size != source_stat.st_size
        or final_source_stat.st_mtime_ns != source_stat.st_mtime_ns
    ):
        temporary.unlink(missing_ok=True)
        _fsync_directory(temporary.parent)
        raise MaintenanceError("log_rotation_busy")
    if not _gzip_matches_source(temporary, source):
        raise MaintenanceError("log_rotation_compression_invalid")


def _reconcile_raw_log_generations(path: Path) -> None:
    raw_indices = set(_log_raw_generation_indices(path))
    temporary_indices = set(_log_raw_reconcile_temporary_indices(path))
    if temporary_indices - raw_indices:
        raise MaintenanceError("log_rotation_state_invalid")
    for generation in sorted(raw_indices | temporary_indices):
        _reconcile_raw_log_generation(path, generation)


def _require_log_generation_bound(path: Path, generations: int) -> None:
    indices = (
        set(_log_generation_indices(path))
        | set(_log_raw_generation_indices(path))
        | set(_log_raw_reconcile_temporary_indices(path))
    )
    if any(generation > generations for generation in indices):
        raise MaintenanceError("log_rotation_generation_mismatch")


def _reconcile_raw_log_generation(path: Path, generation: int) -> None:
    raw = _existing_explicit_file(
        _log_raw_generation(path, generation), "log_generation_invalid"
    )
    compressed = _log_generation(path, generation)
    temporary = _log_raw_reconcile_temporary(path, generation)
    if temporary.exists() or temporary.is_symlink():
        _existing_explicit_file(temporary, "log_rotation_state_invalid")
        if _gzip_matches_source(temporary, raw):
            os.replace(temporary, compressed)
            _fsync_directory(path.parent)
        else:
            temporary.unlink()
            _fsync_directory(path.parent)
    if compressed.exists() or compressed.is_symlink():
        _existing_explicit_file(compressed, "log_generation_invalid")
        if _gzip_matches_source(compressed, raw):
            return
    _compress_log_source(raw, temporary)
    os.replace(temporary, compressed)
    _fsync_directory(path.parent)


def _publish_log_generations(path: Path, generations: int) -> None:
    temporary = _log_temporary(path)
    first = _log_generation(path, 1)
    for generation in range(generations - 1, 0, -1):
        for staged, destination in (
            (
                _log_generation_stage(path, generation),
                _log_generation(path, generation + 1),
            ),
            (
                _log_raw_generation_stage(path, generation),
                _log_raw_generation(path, generation + 1),
            ),
        ):
            if staged.exists() or staged.is_symlink():
                _existing_explicit_file(staged, "log_rotation_state_invalid")
                if destination.exists() or destination.is_symlink():
                    raise MaintenanceError("log_rotation_state_invalid")
                os.replace(staged, destination)
            elif destination.exists() or destination.is_symlink():
                _existing_explicit_file(destination, "log_generation_invalid")
    for oldest in (
        _log_generation_stage(path, generations),
        _log_raw_generation_stage(path, generations),
    ):
        if oldest.exists() or oldest.is_symlink():
            _existing_explicit_file(oldest, "log_rotation_state_invalid")
            oldest.unlink()
    if temporary.exists() or temporary.is_symlink():
        _existing_explicit_file(temporary, "log_rotation_state_invalid")
        if first.exists() or first.is_symlink():
            raise MaintenanceError("log_rotation_state_invalid")
        os.replace(temporary, first)
    elif first.exists() or first.is_symlink():
        _existing_explicit_file(first, "log_generation_invalid")
    else:
        raise MaintenanceError("log_rotation_state_invalid")


def _rollback_busy_log_rotation(path: Path, standard_fds: tuple[int, ...]) -> None:
    source = _log_source_temporary(path)
    if standard_fds or not source.exists() or not path.exists():
        return
    try:
        if path.stat().st_size != 0:
            return
        _log_temporary(path).unlink(missing_ok=True)
        path.unlink()
        os.replace(source, path)
        _clear_log_rotation_state(path)
    except (MaintenanceError, OSError):
        return


def _validate_log_rotation_transaction(path: Path, generations: int) -> None:
    for generation in range(1, generations + 1):
        for candidate, code in (
            (_log_generation(path, generation), "log_generation_invalid"),
            (_log_generation_stage(path, generation), "log_rotation_state_invalid"),
            (_log_raw_generation(path, generation), "log_generation_invalid"),
            (
                _log_raw_generation_stage(path, generation),
                "log_rotation_state_invalid",
            ),
            (
                _log_raw_reconcile_temporary(path, generation),
                "log_rotation_state_invalid",
            ),
        ):
            if candidate.exists() or candidate.is_symlink():
                _existing_log_artifact(candidate, code)


def _rebind_standard_descriptors(source: Path, active: Path) -> None:
    descriptors = _matching_standard_descriptors(source)
    if not descriptors:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.fileno() in descriptors:
                stream.flush()
        except (AttributeError, OSError, ValueError):
            continue
    replacement = os.open(active, os.O_WRONLY | os.O_APPEND)
    try:
        for descriptor in descriptors:
            os.dup2(replacement, descriptor)
    finally:
        os.close(replacement)


def _set_log_rotation_phase(path: Path, phase: str, generations: int) -> None:
    if phase not in {"cutover", "compress", "stage", "publish", "finalize"}:
        raise MaintenanceError("log_rotation_state_invalid")
    if generations < 1 or generations > 10_000:
        raise MaintenanceError("log_rotation_state_invalid")
    state = _log_state(path)
    temporary = _log_state_temporary(path)
    if temporary.exists() or temporary.is_symlink():
        _existing_explicit_file(temporary, "log_rotation_state_invalid")
        temporary.unlink()
        _fsync_directory(path.parent)
    payload = f"log-rotation-v1 {phase} {generations}\n".encode("ascii")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _private_mode(temporary)
        os.replace(temporary, state)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_log_rotation_state(path: Path) -> tuple[str, int] | None:
    state = _log_state(path)
    temporary = _log_state_temporary(path)
    durable_state: tuple[str, int] | None = None
    if state.exists() or state.is_symlink():
        _existing_explicit_file(state, "log_rotation_state_invalid")
        durable_state = _parse_log_rotation_state(state)
    if temporary.exists() or temporary.is_symlink():
        _existing_explicit_file(temporary, "log_rotation_state_invalid")
        try:
            temporary_state = _parse_log_rotation_state(temporary)
        except MaintenanceError:
            temporary.unlink()
            _fsync_directory(path.parent)
            return durable_state
        if durable_state is not None:
            temporary.unlink()
        else:
            os.replace(temporary, state)
        _fsync_directory(path.parent)
        return durable_state if durable_state is not None else temporary_state
    return durable_state


def _parse_log_rotation_state(path: Path) -> tuple[str, int]:
    try:
        payload = path.read_bytes()
        if len(payload) > 128:
            raise ValueError
        version, phase, raw_generations = payload.decode("ascii").strip().split(" ")
        generations = int(raw_generations)
    except (OSError, UnicodeError, ValueError):
        raise MaintenanceError("log_rotation_state_invalid") from None
    if (
        version != "log-rotation-v1"
        or phase not in {"cutover", "compress", "stage", "publish", "finalize"}
        or generations < 1
        or generations > 10_000
    ):
        raise MaintenanceError("log_rotation_state_invalid")
    return phase, generations


def _log_rotation_phase_or_none(path: Path) -> str | None:
    state = _read_log_rotation_state(path)
    return None if state is None else state[0]


def _clear_log_rotation_state(path: Path) -> None:
    removed = False
    for candidate in (_log_state_temporary(path), _log_state(path)):
        if candidate.exists() or candidate.is_symlink():
            _existing_explicit_file(candidate, "log_rotation_state_invalid")
            candidate.unlink()
            removed = True
    if removed:
        _fsync_directory(path.parent)


def _gzip_matches_source(compressed: Path, source: Path) -> bool:
    try:
        with gzip.open(compressed, "rb") as compressed_handle, source.open("rb") as source_handle:
            while True:
                compressed_chunk = compressed_handle.read(1024 * 1024)
                source_chunk = source_handle.read(1024 * 1024)
                if compressed_chunk != source_chunk:
                    return False
                if not compressed_chunk:
                    return True
    except (EOFError, OSError):
        return False


def _validate_gzip(path: Path) -> None:
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(1024 * 1024):
                pass
    except (EOFError, OSError):
        raise MaintenanceError("log_rotation_compression_invalid") from None


def _matching_standard_descriptors(path: Path) -> tuple[int, ...]:
    try:
        target = path.stat()
    except OSError:
        return ()
    matches = []
    for descriptor in (1, 2):
        try:
            current = os.fstat(descriptor)
        except OSError:
            continue
        if (current.st_dev, current.st_ino) == (target.st_dev, target.st_ino):
            matches.append(descriptor)
    return tuple(matches)


def _quick_check(connection: sqlite3.Connection, code: str) -> None:
    try:
        rows = tuple(row[0] for row in connection.execute("PRAGMA quick_check"))
    except sqlite3.Error:
        raise MaintenanceError(code) from None
    if rows != ("ok",):
        raise MaintenanceError(code)


def _quick_check_path(path: Path, code: str) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=30,
        )
        _quick_check(connection, code)
    except MaintenanceError:
        raise
    except sqlite3.Error:
        raise MaintenanceError(code) from None
    finally:
        _close_quietly(connection)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise MaintenanceError("sqlite_backup_hash_failed") from None
    return digest.hexdigest()


def _existing_explicit_file(value: str | Path, code: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise MaintenanceError(code)
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise MaintenanceError(code) from None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise MaintenanceError(code)
    return path


def _existing_log_artifact(value: str | Path, code: str) -> Path:
    path = _existing_explicit_file(value, code)
    try:
        if path.lstat().st_nlink != 1:
            raise MaintenanceError(code)
    except OSError:
        raise MaintenanceError(code) from None
    return path


def _new_explicit_file(value: str | Path, code: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise MaintenanceError(code)
    _explicit_parent(path, code)
    return path


def _explicit_parent(path: Path, code: str) -> None:
    try:
        mode = path.parent.lstat().st_mode
    except OSError:
        raise MaintenanceError(code) from None
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise MaintenanceError(code)


def _private_mode(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        if _POSIX:
            raise MaintenanceError("private_mode_failed") from None
        return
    if _POSIX and mode != 0o600:
        raise MaintenanceError("private_mode_failed")


def _fsync_file(path: Path) -> None:
    try:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
    except OSError:
        raise MaintenanceError("fsync_failed") from None


def _fsync_directory(path: Path) -> None:
    if not _POSIX:
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError:
        raise MaintenanceError("fsync_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _close_quietly(connection: sqlite3.Connection | None) -> None:
    if connection is not None:
        try:
            connection.close()
        except sqlite3.Error:
            pass


def _remove_created(path: Path, created: bool) -> None:
    if created:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _log_generation(path: Path, generation: int) -> Path:
    return path.with_name(f"{path.name}.{generation}.gz")


def _log_temporary(path: Path) -> Path:
    return path.with_name(f".{path.name}.rotation.tmp.gz")


def _log_source_temporary(path: Path) -> Path:
    return path.with_name(f".{path.name}.rotation.source")


def _log_state(path: Path) -> Path:
    return path.with_name(f".{path.name}.rotation.state")


def _log_state_temporary(path: Path) -> Path:
    return path.with_name(f".{path.name}.rotation.state.tmp")


def _log_generation_stage(path: Path, generation: int) -> Path:
    return path.with_name(f".{path.name}.rotation.generation.{generation}.gz")


def _log_raw_generation(path: Path, generation: int) -> Path:
    return path.with_name(f".{path.name}.generation.{generation}.raw")


def _log_raw_generation_stage(path: Path, generation: int) -> Path:
    return path.with_name(f".{path.name}.rotation.raw.{generation}.raw")


def _log_raw_reconcile_temporary(path: Path, generation: int) -> Path:
    return path.with_name(f".{path.name}.raw-reconcile.{generation}.tmp.gz")


def _log_lock(path: Path) -> Path:
    return path.with_name(f".{path.name}.maintenance.lock")


def _log_generation_stage_indices(path: Path) -> tuple[int, ...]:
    return _numbered_log_artifact_indices(
        path,
        prefix=f".{path.name}.rotation.generation.",
        suffix=".gz",
    )


def _log_generation_indices(path: Path) -> tuple[int, ...]:
    return _numbered_log_artifact_indices(
        path,
        prefix=f"{path.name}.",
        suffix=".gz",
    )


def _log_raw_generation_indices(path: Path) -> tuple[int, ...]:
    return _numbered_log_artifact_indices(
        path,
        prefix=f".{path.name}.generation.",
        suffix=".raw",
    )


def _log_raw_generation_stage_indices(path: Path) -> tuple[int, ...]:
    return _numbered_log_artifact_indices(
        path,
        prefix=f".{path.name}.rotation.raw.",
        suffix=".raw",
    )


def _log_raw_reconcile_temporary_indices(path: Path) -> tuple[int, ...]:
    return _numbered_log_artifact_indices(
        path,
        prefix=f".{path.name}.raw-reconcile.",
        suffix=".tmp.gz",
    )


def _numbered_log_artifact_indices(
    path: Path,
    *,
    prefix: str,
    suffix: str,
) -> tuple[int, ...]:
    indices: list[int] = []
    try:
        entries = tuple(path.parent.iterdir())
    except OSError:
        raise MaintenanceError("log_rotation_state_invalid") from None
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        raw_index = entry.name[len(prefix) :]
        if not raw_index.endswith(suffix):
            raise MaintenanceError("log_rotation_state_invalid")
        raw_index = raw_index[: -len(suffix)]
        if not raw_index.isascii() or not raw_index.isdecimal():
            raise MaintenanceError("log_rotation_state_invalid")
        index = int(raw_index)
        if index < 1 or index > 10_000 or index in indices:
            raise MaintenanceError("log_rotation_state_invalid")
        indices.append(index)
    return tuple(sorted(indices))
