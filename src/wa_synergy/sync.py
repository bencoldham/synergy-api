"""Explicit fetch-and-persist orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .client import SynergyClient
from .errors import ConfigurationError
from .models import SyncResult, UsageQuery
from .storage import _validated_path, upsert_usage_intervals


def sync_usage_to_db(
    *,
    client: SynergyClient,
    query: UsageQuery,
    db_path: Path,
) -> SyncResult:
    """Fetch a complete normalized range, then atomically upsert it into SQLite."""

    if not isinstance(client, SynergyClient):
        raise ConfigurationError("sync_usage_to_db requires a SynergyClient")
    if not isinstance(query, UsageQuery):
        raise ConfigurationError("sync_usage_to_db requires a UsageQuery")
    path = _validated_path(db_path)

    intervals = client.get_usage(query)
    fetched_at_utc = datetime.now(UTC)
    return upsert_usage_intervals(
        path,
        intervals,
        fetched_at_utc=fetched_at_utc,
    )
