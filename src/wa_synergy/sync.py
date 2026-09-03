"""Explicit fetch-and-persist orchestration."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

from .client import SynergyClient
from .errors import ConfigurationError
from .models import SyncResult, UsageQuery
from .storage import _validated_path, get_stored_days, upsert_usage_intervals


def _missing_date_ranges(
    start: date,
    end: date,
    stored_days: set[date],
) -> list[tuple[date, date]]:
    """Return contiguous [start, end) ranges of calendar dates not in stored_days."""
    ranges: list[tuple[date, date]] = []
    range_start: date | None = None
    current = start
    while current < end:
        if current not in stored_days:
            if range_start is None:
                range_start = current
        elif range_start is not None:
            ranges.append((range_start, current))
            range_start = None
        current += timedelta(days=1)

    if range_start is not None:
        ranges.append((range_start, end))
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
        else (
            "daily_usage_intervals"
            if query.interval_type == "DAILY"
            else "usage_intervals"
        )
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
        missing_ranges = _missing_date_ranges(start_date, end_date, stored_days)

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
