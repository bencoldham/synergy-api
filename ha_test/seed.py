"""Seed deterministic interval data for the Home Assistant stack."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from wa_synergy import UsageInterval
from wa_synergy.storage import upsert_usage_intervals

_DB_PATH = Path(os.environ.get("WA_SYNERGY_DB_PATH", "/data/synergy.sqlite3"))
_START = datetime(2026, 9, 1, tzinfo=UTC)
_SERVICE_POINT_ID = "001HA000000000TEST"
_CHANNEL_VALUES = {
    "OFF_PEAK": Decimal("0.100"),
    "PEAK": Decimal("0.200"),
    "SUPER_OFFPEAK": Decimal("0.050"),
    "VAL_SOLAR": Decimal("9.999"),
}

intervals = tuple(
    UsageInterval(
        account_id=_SERVICE_POINT_ID,
        service_point_id=_SERVICE_POINT_ID,
        meter_id=None,
        channel=channel,
        interval_start=_START + timedelta(minutes=offset),
        interval_end=_START + timedelta(minutes=offset + 30),
        consumption_kwh=quantity,
        quality="Billed",
    )
    for offset in (0, 30, 60, 90)
    for channel, quantity in _CHANNEL_VALUES.items()
)

upsert_usage_intervals(_DB_PATH, intervals, fetched_at_utc=_START)
