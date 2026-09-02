"""Explicit fetch-and-persist orchestration."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

from .client import SynergyClient
from .errors import ConfigurationError
from .models import SyncResult, UsageQuery
from .storage import _validated_path, get_stored_days, upsert_usage_intervals


def find_missing_date_ranges(
    start: date,
    end: date,
    stored_days: set[date],
) -> list[tuple[date, date]]:
    """Return contiguous [start, end) ranges of calendar dates not in stored_days."""
    missing_days: list[date] = []
    curr = start
    while curr < end:
        if curr not in stored_days:
            missing_days.append(curr)
        curr += timedelta(days=1)

    if not missing_days:
        return []

    ranges: list[tuple[date, date]] = []
    range_start = missing_days[0]
    prev = missing_days[0]

    for day in missing_days[1:]:
        if day == prev + timedelta(days=1):
            prev = day
        else:
            ranges.append((range_start, prev + timedelta(days=1)))
            range_start = day
            prev = day
    ranges.append((range_start, prev + timedelta(days=1)))
    return ranges


def sync_usage_to_db(
    *,
    client: SynergyClient,
    query: UsageQuery,
    db_path: Path,
    table_name: str | None = None,
    force: bool = False,
) -> SyncResult:
    """Fetch missing normalized usage ranges, then atomically upsert into SQLite.

    If the database already contains complete usage records for requested days,
    Synergy API calls for those days are skipped to prevent API spam.
    """

    if not isinstance(client, SynergyClient):
        raise ConfigurationError("sync_usage_to_db requires a SynergyClient")
    if not isinstance(query, UsageQuery):
        raise ConfigurationError("sync_usage_to_db requires a UsageQuery")
    path = _validated_path(db_path)

    effective_table = (
        table_name
        if table_name is not None
        else ("daily_usage_intervals" if query.interval_type == "DAILY" else "usage_intervals")
    )

    start_date = cast(date, query.start)
    end_date = cast(date, query.end)

    if force:
        missing_ranges = [(start_date, end_date)]
    else:
        stored_days = get_stored_days(
            path,
            table_name=effective_table,
            interval_type=query.interval_type,
            service_point_ids=query.service_point_ids,
        )
        missing_ranges = find_missing_date_ranges(start_date, end_date, stored_days)

    if not missing_ranges:
        return SyncResult(inserted=0, updated=0, unchanged=0)

    total_inserted = 0
    total_updated = 0
    total_unchanged = 0

    for sub_start, sub_end in missing_ranges:
        sub_query = UsageQuery(
            start=sub_start,
            end=sub_end,
            account_ids=query.account_ids,
            service_point_ids=query.service_point_ids,
            interval_type=query.interval_type,
        )
        intervals = client.get_usage(sub_query)
        fetched_at_utc = datetime.now(UTC)
        result = upsert_usage_intervals(
            path,
            intervals,
            fetched_at_utc=fetched_at_utc,
            table_name=effective_table,
        )
        total_inserted += result.inserted
        total_updated += result.updated
        total_unchanged += result.unchanged

    return SyncResult(
        inserted=total_inserted,
        updated=total_updated,
        unchanged=total_unchanged,
    )


def sync_daily_usage_to_db(
    *,
    client: SynergyClient,
    query: UsageQuery,
    db_path: Path,
    table_name: str = "daily_usage_intervals",
    force: bool = False,
) -> SyncResult:
    """Fetch daily usage (hourly/half-hourly intervals) and persist to SQLite without spamming."""
    if not isinstance(query, UsageQuery):
        raise ConfigurationError("sync_daily_usage_to_db requires a UsageQuery")
    if query.interval_type != "DAILY":
        query = UsageQuery(
            start=query.start,
            end=query.end,
            account_ids=query.account_ids,
            service_point_ids=query.service_point_ids,
            interval_type="DAILY",
        )
    return sync_usage_to_db(
        client=client,
        query=query,
        db_path=db_path,
        table_name=table_name,
        force=force,
    )
