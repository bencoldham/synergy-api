"""Home Assistant-ready hourly cumulative energy statistics."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .errors import UsageValidationError
from .models import UsageInterval

_IMPORT_CHANNELS = frozenset({"OFF_PEAK", "PEAK", "SUPER_OFFPEAK"})
_HALF_HOUR = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class HourlyStatistic:
    """One cumulative grid-import statistic at the end of an hourly period."""

    service_point_id: str
    start: datetime
    sum_kwh: Decimal


def build_hourly_import_statistics(
    intervals: Iterable[UsageInterval],
    *,
    since: datetime | None = None,
) -> tuple[HourlyStatistic, ...]:
    """Aggregate complete half-hour import records into cumulative hourly sums."""

    if since is not None:
        if since.tzinfo is None or since.utcoffset() is None:
            raise UsageValidationError("statistics since must be timezone-aware")
        since = since.astimezone(UTC)

    slot_totals: dict[tuple[str, datetime], Decimal] = defaultdict(Decimal)
    seen: set[tuple[str, datetime, str]] = set()
    for interval in intervals:
        if not isinstance(interval, UsageInterval):
            raise UsageValidationError(
                "statistics input must contain only UsageInterval instances"
            )
        if interval.channel not in _IMPORT_CHANNELS:
            continue
        if (
            interval.interval_end - interval.interval_start != _HALF_HOUR
            or interval.interval_start.second != 0
            or interval.interval_start.microsecond != 0
            or interval.interval_start.minute not in (0, 30)
        ):
            raise UsageValidationError(
                "grid-import statistics require aligned 30-minute intervals"
            )
        if interval.consumption_kwh < 0:
            raise UsageValidationError("grid-import quantities must not be negative")
        identity = (
            interval.service_point_id,
            interval.interval_start,
            interval.channel,
        )
        if identity in seen:
            raise UsageValidationError(
                "grid-import statistics contain a duplicate channel interval"
            )
        seen.add(identity)
        slot_totals[(interval.service_point_id, interval.interval_start)] += (
            interval.consumption_kwh
        )

    hourly_slots: dict[tuple[str, datetime], dict[datetime, Decimal]] = defaultdict(dict)
    for (service_point_id, slot_start), quantity in slot_totals.items():
        hour_start = slot_start.replace(minute=0)
        hourly_slots[(service_point_id, hour_start)][slot_start] = quantity

    complete_hours: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)
    for (service_point_id, hour_start), slots in hourly_slots.items():
        expected = {hour_start, hour_start + _HALF_HOUR}
        if set(slots) != expected:
            continue
        complete_hours[service_point_id].append(
            (hour_start, sum(slots.values(), start=Decimal()))
        )

    results: list[HourlyStatistic] = []
    for service_point_id in sorted(complete_hours):
        cumulative = Decimal()
        for hour_start, quantity in sorted(complete_hours[service_point_id]):
            cumulative += quantity
            if since is None or hour_start >= since:
                results.append(
                    HourlyStatistic(
                        service_point_id=service_point_id,
                        start=hour_start,
                        sum_kwh=cumulative,
                    )
                )
    return tuple(results)
