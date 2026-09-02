"""Transactional SQLite persistence for normalized usage intervals."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .errors import ConfigurationError, StorageError
from .models import SyncResult, UsageInterval

SCHEMA_VERSION = 1

_CREATE_TABLE = """
CREATE TABLE usage_intervals (
    account_id TEXT NOT NULL CHECK (account_id <> ''),
    service_point_id TEXT NOT NULL CHECK (service_point_id <> ''),
    meter_id TEXT CHECK (meter_id IS NULL OR meter_id <> ''),
    channel TEXT NOT NULL CHECK (channel <> ''),
    interval_start_utc TEXT NOT NULL,
    interval_end_utc TEXT NOT NULL,
    consumption_kwh TEXT NOT NULL,
    quality TEXT,
    source_updated_at_utc TEXT,
    fetched_at_utc TEXT NOT NULL
)
"""

_CREATE_IDENTITY_INDEX = """
CREATE UNIQUE INDEX usage_intervals_identity
ON usage_intervals (
    account_id,
    service_point_id,
    COALESCE(meter_id, ''),
    channel,
    interval_start_utc,
    interval_end_utc
)
"""

_CREATE_INCOMING = """
CREATE TEMP TABLE incoming_usage (
    account_id TEXT NOT NULL,
    service_point_id TEXT NOT NULL,
    meter_id TEXT,
    meter_key TEXT NOT NULL,
    channel TEXT NOT NULL,
    interval_start_utc TEXT NOT NULL,
    interval_end_utc TEXT NOT NULL,
    consumption_kwh TEXT NOT NULL,
    quality TEXT,
    source_updated_at_utc TEXT,
    fetched_at_utc TEXT NOT NULL,
    PRIMARY KEY (
        account_id,
        service_point_id,
        meter_key,
        channel,
        interval_start_utc,
        interval_end_utc
    )
) WITHOUT ROWID
"""

_INSERT_INCOMING = """
INSERT INTO incoming_usage (
    account_id,
    service_point_id,
    meter_id,
    meter_key,
    channel,
    interval_start_utc,
    interval_end_utc,
    consumption_kwh,
    quality,
    source_updated_at_utc,
    fetched_at_utc
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SAME_IDENTITY = """
u.account_id = i.account_id
AND u.service_point_id = i.service_point_id
AND u.meter_id IS i.meter_id
AND u.channel = i.channel
AND u.interval_start_utc = i.interval_start_utc
AND u.interval_end_utc = i.interval_end_utc
"""

_COUNT_INSERTED = f"""
SELECT COUNT(*)
FROM incoming_usage AS i
WHERE NOT EXISTS (
    SELECT 1 FROM usage_intervals AS u WHERE {_SAME_IDENTITY}
)
"""

_COUNT_UPDATED = f"""
SELECT COUNT(*)
FROM incoming_usage AS i
JOIN usage_intervals AS u ON {_SAME_IDENTITY}
WHERE u.consumption_kwh IS NOT i.consumption_kwh
   OR u.quality IS NOT i.quality
   OR u.source_updated_at_utc IS NOT i.source_updated_at_utc
"""

_MERGE_INCOMING = """
INSERT INTO usage_intervals (
    account_id,
    service_point_id,
    meter_id,
    channel,
    interval_start_utc,
    interval_end_utc,
    consumption_kwh,
    quality,
    source_updated_at_utc,
    fetched_at_utc
)
SELECT
    account_id,
    service_point_id,
    meter_id,
    channel,
    interval_start_utc,
    interval_end_utc,
    consumption_kwh,
    quality,
    source_updated_at_utc,
    fetched_at_utc
FROM incoming_usage
WHERE 1
ON CONFLICT DO UPDATE SET
    consumption_kwh = excluded.consumption_kwh,
    quality = excluded.quality,
    source_updated_at_utc = excluded.source_updated_at_utc,
    fetched_at_utc = excluded.fetched_at_utc
"""


def _canonical_datetime(value: datetime, *, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ConfigurationError(f"{field} must be a timezone-aware datetime")
    try:
        utc_value = value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ConfigurationError(f"{field}={value!r} is invalid: {exc}") from exc
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _validated_path(db_path: Path) -> Path:
    if not isinstance(db_path, Path):
        raise ConfigurationError("db_path must be a pathlib.Path")
    if str(db_path) == ":memory:":
        raise ConfigurationError("db_path must identify an on-disk SQLite file")

    try:
        if db_path.is_symlink():
            raise ConfigurationError("db_path must not be a symbolic link")
        if db_path.exists():
            mode = db_path.stat().st_mode
            if not stat.S_ISREG(mode):
                raise ConfigurationError("db_path must identify a regular file")
    except OSError as exc:
        raise ConfigurationError(
            f"Cannot inspect database path {db_path}: {exc}"
        ) from exc
    return db_path


def _prepare_database_file(db_path: Path) -> None:
    try:
        db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(db_path, flags, 0o600)
        except FileExistsError:
            return
        os.close(descriptor)
    except OSError as exc:
        raise StorageError(f"Cannot create SQLite file {db_path}: {exc}") from exc


def _initialize_schema(connection: sqlite3.Connection) -> None:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    if version_row is None:
        raise StorageError("SQLite schema version could not be read")
    version = version_row[0]
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise StorageError("SQLite database schema version is unsupported")

    connection.execute(_CREATE_TABLE)
    connection.execute(_CREATE_IDENTITY_INDEX)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _deduplicate(
    intervals: Iterable[UsageInterval],
) -> tuple[UsageInterval, ...]:
    unique: dict[
        tuple[str, str, str | None, str, datetime, datetime], UsageInterval
    ] = {}
    try:
        for interval in intervals:
            if not isinstance(interval, UsageInterval):
                raise ConfigurationError(
                    "intervals must contain only UsageInterval instances"
                )
            previous = unique.get(interval.record_identity)
            if previous is not None and previous != interval:
                raise StorageError("usage batch contains a conflicting duplicate")
            unique[interval.record_identity] = interval
    except TypeError as exc:
        raise ConfigurationError(f"intervals is not iterable: {exc}") from exc
    return tuple(unique.values())


def _incoming_row(
    interval: UsageInterval, fetched_at_utc: str
) -> tuple[str | None, ...]:
    return (
        interval.account_id,
        interval.service_point_id,
        interval.meter_id,
        interval.meter_id or "",
        interval.channel,
        _canonical_datetime(interval.interval_start, field="interval_start"),
        _canonical_datetime(interval.interval_end, field="interval_end"),
        _canonical_decimal(interval.consumption_kwh),
        interval.quality,
        (
            _canonical_datetime(interval.source_updated_at, field="source_updated_at")
            if interval.source_updated_at is not None
            else None
        ),
        fetched_at_utc,
    )


def upsert_usage_intervals(
    db_path: Path,
    intervals: Iterable[UsageInterval],
    *,
    fetched_at_utc: datetime,
) -> SyncResult:
    """Atomically create the schema and upsert one normalized usage batch.

    Outcome counts describe unique provider records. An identical duplicate in the input
    is collapsed; a duplicate identity with different values rejects the full batch.
    ``fetched_at_utc`` is refreshed for unchanged rows without counting them as updated.
    """

    path = _validated_path(db_path)
    fetched_at = _canonical_datetime(fetched_at_utc, field="fetched_at_utc")
    records = _deduplicate(intervals)
    incoming_rows = tuple(_incoming_row(record, fetched_at) for record in records)
    _prepare_database_file(path)

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        _initialize_schema(connection)
        connection.execute(_CREATE_INCOMING)
        connection.executemany(_INSERT_INCOMING, incoming_rows)

        inserted_row = connection.execute(_COUNT_INSERTED).fetchone()
        updated_row = connection.execute(_COUNT_UPDATED).fetchone()
        if inserted_row is None or updated_row is None:
            raise StorageError("SQLite persistence outcomes could not be determined")
        inserted = int(inserted_row[0])
        updated = int(updated_row[0])
        unchanged = len(records) - inserted - updated

        connection.execute(_MERGE_INCOMING)
        connection.commit()
        return SyncResult(
            inserted=inserted,
            updated=updated,
            unchanged=unchanged,
        )
    except sqlite3.Error:
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()
