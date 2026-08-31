from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
from pathlib import Path
import sqlite3
import stat
import threading
from types import SimpleNamespace

import pytest

from turtle_runtime import maintenance
from turtle_runtime.maintenance import (
    BackupRetentionItem,
    DAILY_BACKUPS,
    GIB,
    LOG_GENERATIONS,
    LOG_ROTATE_BYTES,
    MaintenanceError,
    WEEKLY_BACKUPS,
    backup_sqlite,
    check_disk_space,
    classify_disk_space,
    finish_backup_retention_removal,
    prune_backup_retention,
    retention_tombstone_path,
    rotate_logs,
    sha256_manifest_path,
)


def _create_database(path: Path, *, wal: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    if wal:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.execute("CREATE TABLE samples(value TEXT NOT NULL)")
    connection.execute("INSERT INTO samples VALUES ('snapshot-row')")
    connection.commit()
    return connection


def _retention_item(path: Path, created_at: datetime) -> BackupRetentionItem:
    path.write_bytes(b"backup")
    sha256_manifest_path(path).write_text("digest\n", encoding="ascii")
    return BackupRetentionItem(path, created_at)


def test_sqlite_backup_captures_live_wal_with_online_api_and_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "live.sqlite3"
    destination = tmp_path / "backups" / "snapshot.sqlite3"
    destination.parent.mkdir()
    writer = _create_database(source, wal=True)
    assert source.with_name(f"{source.name}-wal").exists()

    try:
        result = backup_sqlite(source, destination)
    finally:
        writer.close()

    with sqlite3.connect(destination) as clone:
        assert clone.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert clone.execute("SELECT value FROM samples").fetchall() == [
            ("snapshot-row",)
        ]
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    assert result.sha256 == digest
    assert result.manifest == sha256_manifest_path(destination)
    assert result.manifest.read_text(encoding="ascii") == f"{digest}\n"
    assert result.size_bytes == destination.stat().st_size
    if maintenance._POSIX:
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.manifest.stat().st_mode) == 0o600


def test_sqlite_backup_quarantines_incomplete_destination_before_recreating(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    _create_database(source).close()
    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"keep-me")

    result = backup_sqlite(source, destination)

    quarantined = list(tmp_path.glob(f".{destination.name}.quarantine-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"keep-me"
    assert result.destination == destination
    assert result.manifest.exists()
    with sqlite3.connect(destination) as clone:
        assert clone.execute("PRAGMA quick_check").fetchall() == [("ok",)]


def test_sqlite_backup_rejects_bad_source_and_quarantines_orphan_manifest(
    tmp_path: Path,
) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not sqlite")
    destination = tmp_path / "corrupt-backup.sqlite3"
    with pytest.raises(MaintenanceError):
        backup_sqlite(corrupt, destination)
    assert not destination.exists()

    source = tmp_path / "source.sqlite3"
    _create_database(source).close()
    manifest = sha256_manifest_path(destination)
    manifest.write_text("existing", encoding="ascii")
    backup_sqlite(source, destination)
    quarantined = list(tmp_path.glob(f".{manifest.name}.quarantine-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="ascii") == "existing"
    assert destination.exists()
    assert manifest.read_text(encoding="ascii").endswith("\n")


def test_sqlite_backup_resumes_complete_verified_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _create_database(source).close()
    destination = tmp_path / "snapshot.sqlite3"
    first = backup_sqlite(source, destination)

    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO samples VALUES ('later-row')")
        connection.commit()
    second = backup_sqlite(source, destination)

    assert second.sha256 == first.sha256
    assert second.size_bytes == first.size_bytes
    assert not list(tmp_path.glob(".*.quarantine-*"))
    with sqlite3.connect(destination) as clone:
        assert clone.execute("SELECT value FROM samples ORDER BY rowid").fetchall() == [
            ("snapshot-row",)
        ]


def test_sqlite_backup_explicit_replace_quarantines_valid_stale_pair(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    _create_database(source).close()
    destination = tmp_path / "snapshot.sqlite3"
    first = backup_sqlite(source, destination)
    stale_database = destination.read_bytes()
    stale_manifest = first.manifest.read_bytes()

    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO samples VALUES ('current-row')")
        connection.commit()
    second = backup_sqlite(source, destination, replace_existing=True)

    quarantined_database = list(
        tmp_path.glob(f".{destination.name}.quarantine-*")
    )
    quarantined_manifest = list(
        tmp_path.glob(f".{first.manifest.name}.quarantine-*")
    )
    assert len(quarantined_database) == len(quarantined_manifest) == 1
    assert quarantined_database[0].read_bytes() == stale_database
    assert quarantined_manifest[0].read_bytes() == stale_manifest
    assert second.sha256 != first.sha256
    with sqlite3.connect(destination) as clone:
        assert clone.execute("SELECT value FROM samples ORDER BY rowid").fetchall() == [
            ("snapshot-row",),
            ("current-row",),
        ]


def test_sqlite_backup_resume_still_requires_healthy_source(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _create_database(source).close()
    destination = tmp_path / "snapshot.sqlite3"
    first = backup_sqlite(source, destination)
    original_database = destination.read_bytes()
    original_manifest = first.manifest.read_bytes()
    source.write_bytes(b"corrupt source")

    with pytest.raises(MaintenanceError, match="sqlite_source_quick_check_failed"):
        backup_sqlite(source, destination)

    assert destination.read_bytes() == original_database
    assert first.manifest.read_bytes() == original_manifest
    assert not list(tmp_path.glob(".*.quarantine-*"))


def test_sqlite_backup_quarantines_invalid_pair_and_recreates(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _create_database(source).close()
    destination = tmp_path / "snapshot.sqlite3"
    first = backup_sqlite(source, destination)
    first.manifest.write_text(f"{'0' * 64}\n", encoding="ascii")

    second = backup_sqlite(source, destination)

    quarantined_database = list(
        tmp_path.glob(f".{destination.name}.quarantine-*")
    )
    quarantined_manifest = list(
        tmp_path.glob(f".{first.manifest.name}.quarantine-*")
    )
    assert len(quarantined_database) == len(quarantined_manifest) == 1
    assert quarantined_manifest[0].read_text(encoding="ascii") == f"{'0' * 64}\n"
    assert second.sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert second.manifest.read_text(encoding="ascii") == f"{second.sha256}\n"


def test_sqlite_backup_fails_closed_on_existing_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _create_database(source).close()
    target = tmp_path / "unrelated.sqlite3"
    target.write_bytes(b"keep-me")
    destination = tmp_path / "snapshot.sqlite3"
    try:
        destination.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(MaintenanceError, match="sqlite_existing_backup_symlink"):
        backup_sqlite(source, destination)

    assert destination.is_symlink()
    assert target.read_bytes() == b"keep-me"
    assert not sha256_manifest_path(destination).exists()


def test_sqlite_backup_rejects_auxiliary_file_and_cleans_up_on_posix_mode_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auxiliary = tmp_path / "source.sqlite3-wal"
    _create_database(auxiliary).close()
    with pytest.raises(MaintenanceError, match="sqlite_auxiliary_file_rejected"):
        backup_sqlite(auxiliary, tmp_path / "aux-backup.sqlite3")

    source = tmp_path / "source.sqlite3"
    _create_database(source).close()
    destination = tmp_path / "private-backup.sqlite3"
    monkeypatch.setattr(maintenance, "_POSIX", True)
    monkeypatch.setattr(
        maintenance.os,
        "chmod",
        lambda _path, _mode: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(MaintenanceError, match="private_mode_failed"):
        backup_sqlite(source, destination)
    assert not destination.exists()
    assert not sha256_manifest_path(destination).exists()


def test_default_retention_keeps_35_daily_plus_6_nonoverlapping_weekly(
    tmp_path: Path,
) -> None:
    newest = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)
    items = [
        _retention_item(tmp_path / f"backup-{offset:02}.sqlite3", newest - timedelta(days=offset))
        for offset in range(80)
    ]

    result = prune_backup_retention(items)

    assert len(result.daily) == DAILY_BACKUPS == 35
    assert len(result.weekly) == WEEKLY_BACKUPS == 6
    assert len(result.kept) == len(set(result.kept)) == 41
    daily_weeks = {
        items[int(path.stem.rsplit("-", 1)[1])].created_at.date().isocalendar()[:2]
        for path in result.daily
    }
    weekly_weeks = {
        items[int(path.stem.rsplit("-", 1)[1])].created_at.date().isocalendar()[:2]
        for path in result.weekly
    }
    assert daily_weeks.isdisjoint(weekly_weeks)
    assert len(weekly_weeks) == 6
    for path in result.kept:
        assert path.exists()
        assert sha256_manifest_path(path).exists()
    for path in result.removed:
        assert not path.exists()
        assert not sha256_manifest_path(path).exists()


def test_retention_uses_one_daily_and_one_from_an_older_week(tmp_path: Path) -> None:
    latest = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)
    newer = _retention_item(tmp_path / "newer.sqlite3", latest)
    same_day = _retention_item(tmp_path / "same-day.sqlite3", latest - timedelta(hours=1))
    older_week = _retention_item(tmp_path / "older.sqlite3", latest - timedelta(days=8))

    result = prune_backup_retention(
        [same_day, older_week, newer],
        daily_keep=1,
        weekly_keep=1,
    )

    assert result.daily == (newer.path,)
    assert result.weekly == (older_week.path,)
    assert result.removed == (same_day.path,)


def test_retention_validates_every_explicit_artifact_before_removing(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    valid = _retention_item(tmp_path / "valid.sqlite3", now)
    invalid_path = tmp_path / "missing-manifest.sqlite3"
    invalid_path.write_bytes(b"backup")

    with pytest.raises(MaintenanceError, match="backup_retention_manifest_invalid"):
        prune_backup_retention(
            [valid, BackupRetentionItem(invalid_path, now - timedelta(days=1))],
            daily_keep=0,
            weekly_keep=0,
        )

    assert valid.path.exists()
    assert sha256_manifest_path(valid.path).exists()


def test_retention_tombstone_durably_recovers_a_crash_without_false_corruption(
    tmp_path: Path,
) -> None:
    database = tmp_path / "planner-2026-08-01.sqlite3"
    _retention_item(database, datetime(2026, 8, 1, tzinfo=timezone.utc))
    tombstone = retention_tombstone_path(database)
    tombstone.write_text("retention-delete-v1\n", encoding="ascii")
    database.unlink()

    assert finish_backup_retention_removal(database) is True
    assert not database.exists()
    assert not sha256_manifest_path(database).exists()
    assert tombstone.read_text(encoding="ascii") == "retention-delete-v1\n"
    assert finish_backup_retention_removal(database) is True

    # Restoring an older backup directory must not resurrect a pair whose
    # authorization may be newer than a restored planner-database snapshot.
    database.write_bytes(b"restored database")
    sha256_manifest_path(database).write_text(f"{'0' * 64}\n", encoding="ascii")
    assert finish_backup_retention_removal(database) is True
    assert not database.exists()
    assert not sha256_manifest_path(database).exists()
    assert tombstone.exists()


def test_retention_recovers_an_interrupted_tombstone_publish(tmp_path: Path) -> None:
    database = tmp_path / "planner-2026-08-01.sqlite3"
    item = _retention_item(database, datetime(2026, 8, 1, tzinfo=timezone.utc))
    temporary = maintenance._retention_tombstone_temporary_path(database)
    temporary.write_text("partial", encoding="ascii")

    result = prune_backup_retention(
        [item],
        daily_keep=0,
        weekly_keep=0,
    )

    assert result.removed == (database,)
    assert not temporary.exists()
    assert not database.exists()
    assert not sha256_manifest_path(database).exists()
    assert (
        retention_tombstone_path(database).read_text(encoding="ascii")
        == "retention-delete-v1\n"
    )


def test_retention_authorizes_each_pair_before_deletion(tmp_path: Path) -> None:
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    keep = _retention_item(tmp_path / "keep.sqlite3", now)
    remove = _retention_item(
        tmp_path / "remove.sqlite3", now - timedelta(days=1)
    )
    authorized: list[Path] = []

    result = prune_backup_retention(
        [keep, remove],
        daily_keep=1,
        weekly_keep=0,
        authorize_remove=authorized.append,
    )

    assert result.removed == (remove.path,)
    assert authorized == [remove.path]
    assert (
        retention_tombstone_path(remove.path).read_text(encoding="ascii")
        == "retention-delete-v1\n"
    )


@pytest.mark.parametrize(
    ("total", "free", "level"),
    [
        (100 * GIB, 20 * GIB, "ok"),
        (100 * GIB, 19 * GIB, "warning"),
        (100 * GIB, 10 * GIB, "warning"),
        (100 * GIB, 9 * GIB, "critical"),
        (40 * GIB, 5 * GIB, "warning"),
        (40 * GIB, 4 * GIB, "critical"),
    ],
)
def test_disk_thresholds_use_strict_ratio_or_gib_limits(
    total: int,
    free: int,
    level: str,
) -> None:
    assert classify_disk_space(total, free).level == level


def test_disk_check_uses_the_explicit_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []

    def fake_usage(path: Path):
        calls.append(path)
        return SimpleNamespace(total=100 * GIB, used=81 * GIB, free=19 * GIB)

    monkeypatch.setattr(maintenance.shutil, "disk_usage", fake_usage)

    assert check_disk_space(tmp_path).level == "warning"
    assert calls == [tmp_path]
    with pytest.raises(MaintenanceError, match="disk_path_invalid"):
        check_disk_space(Path("relative"))


def test_log_rotation_touches_only_explicit_paths_and_compresses_at_limit(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected.log"
    selected.write_bytes(b"selected-data")
    unlisted = tmp_path / "unlisted.log"
    unlisted.write_bytes(b"unlisted-data")
    small = tmp_path / "small.log"
    small.write_bytes(b"small")
    missing = tmp_path / "missing.log"

    result = rotate_logs([selected, small, missing], max_bytes=len(b"selected-data"))

    assert result.rotated == (selected,)
    assert result.skipped == (small,)
    assert result.missing == (missing,)
    assert selected.read_bytes() == b""
    assert gzip.decompress((tmp_path / "selected.log.1.gz").read_bytes()) == b"selected-data"
    assert unlisted.read_bytes() == b"unlisted-data"


def test_log_rotation_never_truncates_bytes_appended_during_compression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"before")
    source_stage = tmp_path / ".service.log.rotation.source"
    real_gzip = maintenance.gzip.GzipFile

    def mutating_gzip(*args, **kwargs):
        with source_stage.open("ab") as handle:
            handle.write(b"-during")
        return real_gzip(*args, **kwargs)

    monkeypatch.setattr(maintenance, "_matching_standard_descriptors", lambda _path: ())
    monkeypatch.setattr(maintenance.gzip, "GzipFile", mutating_gzip)

    with pytest.raises(MaintenanceError, match="log_rotation_busy"):
        rotate_logs([log], max_bytes=1)

    assert log.read_bytes() == b"before-during"
    assert not source_stage.exists()
    assert not (tmp_path / "service.log.1.gz").exists()
    assert not (tmp_path / ".service.log.rotation.tmp.gz").exists()


def test_log_rotation_keeps_exactly_30_compressed_generations(tmp_path: Path) -> None:
    assert LOG_ROTATE_BYTES == 10 * 1024**2
    assert LOG_GENERATIONS == 30
    log = tmp_path / "service.log"
    log.write_bytes(b"current")
    for generation in range(1, LOG_GENERATIONS + 1):
        (tmp_path / f"service.log.{generation}.gz").write_bytes(
            gzip.compress(f"old-{generation}".encode())
        )

    rotate_logs([log], max_bytes=1)

    generations = sorted(tmp_path.glob("service.log.*.gz"))
    assert len(generations) == LOG_GENERATIONS
    assert gzip.decompress((tmp_path / "service.log.1.gz").read_bytes()) == b"current"
    assert gzip.decompress((tmp_path / "service.log.2.gz").read_bytes()) == b"old-1"
    assert gzip.decompress((tmp_path / "service.log.30.gz").read_bytes()) == b"old-29"
    assert not (tmp_path / "service.log.31.gz").exists()


def test_log_rotation_validates_full_path_set_before_mutation(tmp_path: Path) -> None:
    valid = tmp_path / "valid.log"
    valid.write_bytes(b"rotate-me")
    invalid = tmp_path / "directory.log"
    invalid.mkdir()

    with pytest.raises(MaintenanceError, match="log_path_invalid"):
        rotate_logs([valid, invalid], max_bytes=1)

    assert valid.read_bytes() == b"rotate-me"
    assert not (tmp_path / "valid.log.1.gz").exists()


def test_log_rotation_rejects_relative_and_temporary_collision(tmp_path: Path) -> None:
    with pytest.raises(MaintenanceError, match="log_path_invalid"):
        rotate_logs([Path("relative.log")], max_bytes=1)

    log = tmp_path / "service.log"
    log.write_bytes(b"rotate-me")
    (tmp_path / ".service.log.rotation.tmp.gz").write_bytes(b"collision")
    with pytest.raises(MaintenanceError, match="log_rotation_temporary_exists"):
        rotate_logs([log], max_bytes=1)
    assert log.read_bytes() == b"rotate-me"


@pytest.mark.parametrize("generations", [0, 10_001, True])
def test_log_rotation_rejects_unbounded_or_non_integer_generations(
    tmp_path: Path,
    generations: object,
) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"unchanged")

    with pytest.raises(MaintenanceError, match="log_rotation_invalid"):
        rotate_logs([log], max_bytes=1, generations=generations)  # type: ignore[arg-type]

    assert log.read_bytes() == b"unchanged"


def test_log_rotation_recovers_crash_after_source_detach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"detached-bytes")
    real_open = maintenance.os.open

    with monkeypatch.context() as crash:
        def crash_before_replacement(path, flags, mode=0o777):
            if Path(path) == log and flags & maintenance.os.O_EXCL:
                raise SystemExit("simulated crash")
            return real_open(path, flags, mode)

        crash.setattr(maintenance.os, "open", crash_before_replacement)
        with pytest.raises(SystemExit, match="simulated crash"):
            rotate_logs([log], max_bytes=1)

    source = tmp_path / ".service.log.rotation.source"
    assert not log.exists()
    assert source.read_bytes() == b"detached-bytes"

    result = rotate_logs([log], max_bytes=1024)

    assert result.skipped == (log,)
    assert log.read_bytes() == b"detached-bytes"
    assert not source.exists()


@pytest.mark.parametrize("crash_phase", ["stage", "publish"])
def test_log_rotation_resumes_transaction_without_losing_or_duplicating_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: str,
) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"current")
    (tmp_path / "service.log.1.gz").write_bytes(gzip.compress(b"old-1"))
    (tmp_path / "service.log.2.gz").write_bytes(gzip.compress(b"old-2"))
    real_replace = maintenance.os.replace

    with monkeypatch.context() as crash:
        def crash_after_one_move(source, destination):
            real_replace(source, destination)
            destination_name = Path(destination).name
            if crash_phase == "stage" and destination_name.endswith(
                "rotation.generation.1.gz"
            ):
                raise SystemExit("simulated crash")
            if crash_phase == "publish" and destination_name == "service.log.3.gz":
                raise SystemExit("simulated crash")

        crash.setattr(maintenance.os, "replace", crash_after_one_move)
        with pytest.raises(SystemExit, match="simulated crash"):
            rotate_logs([log], max_bytes=1, generations=3)

    rotate_logs([log], max_bytes=1024, generations=3)

    payloads = [
        gzip.decompress((tmp_path / f"service.log.{generation}.gz").read_bytes())
        for generation in range(1, 4)
    ]
    assert log.read_bytes() == b""
    assert payloads == [b"current", b"old-1", b"old-2"]
    assert len(payloads) == len(set(payloads))
    assert not tuple(tmp_path.glob(".service.log.rotation.*"))


def test_log_rotation_recovers_after_generation_publish_before_state_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"published-before-crash")

    with monkeypatch.context() as crash:
        def crash_before_state_cleanup(path: Path) -> None:
            raise SystemExit("simulated crash")

        crash.setattr(
            maintenance,
            "_clear_log_rotation_state",
            crash_before_state_cleanup,
        )
        with pytest.raises(SystemExit, match="simulated crash"):
            rotate_logs([log], max_bytes=1)

    assert not (tmp_path / ".service.log.rotation.source").exists()
    assert gzip.decompress((tmp_path / "service.log.1.gz").read_bytes()) == (
        b"published-before-crash"
    )
    assert (tmp_path / ".service.log.rotation.state").exists()

    rotate_logs([log], max_bytes=1024)

    assert gzip.decompress((tmp_path / "service.log.1.gz").read_bytes()) == (
        b"published-before-crash"
    )
    assert not tuple(tmp_path.glob(".service.log.rotation.*"))


def test_legacy_cutover_with_identical_previous_generation_is_not_misclassified(
    tmp_path: Path,
) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"")
    (tmp_path / ".service.log.rotation.source").write_bytes(b"same-payload")
    (tmp_path / "service.log.1.gz").write_bytes(gzip.compress(b"same-payload"))
    (tmp_path / "service.log.2.gz").write_bytes(gzip.compress(b"older"))

    rotate_logs([log], max_bytes=1024, generations=3)

    payloads = [
        gzip.decompress((tmp_path / f"service.log.{generation}.gz").read_bytes())
        for generation in range(1, 4)
    ]
    assert payloads == [b"same-payload", b"same-payload", b"older"]
    assert not tuple(tmp_path.glob(".service.log.rotation.*"))


def test_log_rotation_lock_prevents_concurrent_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"single-owner-bytes")
    compression_started = threading.Event()
    release_compression = threading.Event()
    first_error: list[BaseException] = []
    real_compress = maintenance._compress_log_source

    def slow_first_compression(source: Path, temporary: Path) -> None:
        compression_started.set()
        assert release_compression.wait(timeout=5)
        real_compress(source, temporary)

    monkeypatch.setattr(maintenance, "_compress_log_source", slow_first_compression)

    def first_rotation() -> None:
        try:
            rotate_logs([log], max_bytes=1)
        except BaseException as exc:  # pragma: no cover - surfaced below
            first_error.append(exc)

    worker = threading.Thread(target=first_rotation)
    worker.start()
    assert compression_started.wait(timeout=5)
    try:
        with pytest.raises(MaintenanceError, match="log_rotation_locked"):
            rotate_logs([log], max_bytes=1)
    finally:
        release_compression.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert first_error == []
    assert gzip.decompress((tmp_path / "service.log.1.gz").read_bytes()) == (
        b"single-owner-bytes"
    )


def test_log_rotation_retains_and_reconciles_bytes_appended_after_finalize_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"before")
    source = tmp_path / ".service.log.rotation.source"
    real_matches = maintenance._gzip_matches_source
    calls = 0

    def append_after_final_match(compressed: Path, source_path: Path) -> bool:
        nonlocal calls
        calls += 1
        matches = real_matches(compressed, source_path)
        if calls == 3:
            with source_path.open("ab") as handle:
                handle.write(b"-late")
        return matches

    with monkeypatch.context() as race:
        race.setattr(
            maintenance,
            "_gzip_matches_source",
            append_after_final_match,
        )
        rotate_logs([log], max_bytes=1)

    raw = tmp_path / ".service.log.generation.1.raw"
    assert not source.exists()
    assert raw.read_bytes() == b"before-late"
    assert gzip.decompress((tmp_path / "service.log.1.gz").read_bytes()) == b"before"
    rotate_logs([log], max_bytes=1024)
    assert gzip.decompress((tmp_path / "service.log.1.gz").read_bytes()) == (
        b"before-late"
    )
    assert raw.read_bytes() == b"before-late"


def test_log_rotation_discards_truncated_unpublished_state_temp(tmp_path: Path) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"unchanged")
    state_temporary = tmp_path / ".service.log.rotation.state.tmp"
    state_temporary.write_bytes(b"log-rot")

    result = rotate_logs([log], max_bytes=1024)

    assert result.skipped == (log,)
    assert log.read_bytes() == b"unchanged"
    assert not state_temporary.exists()


def test_log_rotation_uses_durable_state_when_new_state_temp_is_truncated(
    tmp_path: Path,
) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"active-after-cutover")
    source = tmp_path / ".service.log.rotation.source"
    source.write_bytes(b"detached-before-crash")
    maintenance._set_log_rotation_phase(log, "compress", 3)
    (tmp_path / ".service.log.rotation.state.tmp").write_bytes(b"")

    rotate_logs([log], max_bytes=1024, generations=3)

    assert log.read_bytes() == b"active-after-cutover"
    assert gzip.decompress((tmp_path / "service.log.1.gz").read_bytes()) == (
        b"detached-before-crash"
    )
    assert (tmp_path / ".service.log.generation.1.raw").read_bytes() == (
        b"detached-before-crash"
    )
    assert not (tmp_path / ".service.log.rotation.state.tmp").exists()


def test_log_rotation_shifts_raw_recovery_companions_with_gzip_generations(
    tmp_path: Path,
) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"first")
    rotate_logs([log], max_bytes=1, generations=3)
    log.write_bytes(b"second")

    rotate_logs([log], max_bytes=1, generations=3)

    assert gzip.decompress((tmp_path / "service.log.1.gz").read_bytes()) == b"second"
    assert gzip.decompress((tmp_path / "service.log.2.gz").read_bytes()) == b"first"
    assert (tmp_path / ".service.log.generation.1.raw").read_bytes() == b"second"
    assert (tmp_path / ".service.log.generation.2.raw").read_bytes() == b"first"


def test_log_rotation_rejects_silent_retention_window_shrink(tmp_path: Path) -> None:
    log = tmp_path / "service.log"
    log.write_bytes(b"first")
    rotate_logs([log], max_bytes=1, generations=3)
    log.write_bytes(b"second")
    rotate_logs([log], max_bytes=1, generations=3)

    with pytest.raises(MaintenanceError, match="log_rotation_generation_mismatch"):
        rotate_logs([log], max_bytes=1, generations=1)

    assert gzip.decompress((tmp_path / "service.log.2.gz").read_bytes()) == b"first"
    assert (tmp_path / ".service.log.generation.2.raw").read_bytes() == b"first"


def test_log_rotation_rejects_cross_path_reserved_namespace_before_mutation(
    tmp_path: Path,
) -> None:
    first = tmp_path / "service.log"
    second = tmp_path / ".service.log.generation.1.raw"
    first.write_bytes(b"first-active")
    second.write_bytes(b"second-active")

    with pytest.raises(MaintenanceError, match="log_path_namespace_collision"):
        rotate_logs([first, second], max_bytes=1, generations=1)

    assert first.read_bytes() == b"first-active"
    assert second.read_bytes() == b"second-active"
    assert not (tmp_path / "service.log.1.gz").exists()
    assert not (tmp_path / ".service.log.maintenance.lock").exists()


def test_log_rotation_validates_all_journals_before_recovering_any_path(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.log"
    second = tmp_path / "second.log"
    first.write_bytes(b"first-active")
    second.write_bytes(b"second-active")
    (tmp_path / ".second.log.rotation.state.tmp").write_bytes(b"truncated")
    (tmp_path / ".second.log.rotation.generation.1.gz").write_bytes(
        gzip.compress(b"staged")
    )

    with pytest.raises(MaintenanceError, match="log_rotation_state_invalid"):
        rotate_logs([first, second], max_bytes=1, generations=1)

    assert first.read_bytes() == b"first-active"
    assert second.read_bytes() == b"second-active"
    assert not (tmp_path / "first.log.1.gz").exists()
