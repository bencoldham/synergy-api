"""Home Assistant-ready hourly statistics and complete-day consumption summaries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import groupby
from zoneinfo import ZoneInfo

from .errors import UsageValidationError
from .models import UsageInterval

_IMPORT_CHANNELS = frozenset({"OFF_PEAK", "PEAK", "SUPER_OFFPEAK"})
_HALF_HOUR = timedelta(minutes=30)
_PERTH = ZoneInfo("Australia/Perth")
_COMPLETE_DAY_HOURS = (1 << 24) - 1


@dataclass(frozen=True, slots=True)
class HourlyStatistic:
    """One cumulative grid-import statistic at the end of an hourly period."""

    service_point_id: str
    start: datetime
    sum_kwh: Decimal


@dataclass(frozen=True, slots=True)
class ConsumptionPeriod:
    """Inclusive Perth dates with unknown energy when any day is incomplete."""

    start_date: date
    end_date: date
    import_kwh: Decimal | None


@dataclass(frozen=True, slots=True)
class ConsumptionSummary:
    """Full-history import total and complete-day snapshots for one service."""

    service_point_id: str
    total_import_kwh: Decimal
    latest_day: ConsumptionPeriod | None
    last_7_days: ConsumptionPeriod | None
    month_to_date: ConsumptionPeriod | None


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

    hourly_slots: dict[tuple[str, datetime], dict[datetime, Decimal]] = defaultdict(
        dict
    )
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


def _consumption_period(
    complete_days: dict[date, Decimal], start_date: date, end_date: date
) -> ConsumptionPeriod:
    total = Decimal()
    current = start_date
    while current <= end_date:
        quantity = complete_days.get(current)
        if quantity is None:
            return ConsumptionPeriod(start_date, end_date, None)
        total += quantity
        current += timedelta(days=1)
    return ConsumptionPeriod(start_date, end_date, total)


def build_consumption_summaries(
    statistics: Iterable[HourlyStatistic], *, today: date
) -> tuple[ConsumptionSummary, ...]:
    """Summarize unfiltered builder output using the supplied current Perth date.

    Input must contain the full cumulative history, ordered by service and hour
    as returned by ``build_hourly_import_statistics``.
    """

    summaries: list[ConsumptionSummary] = []
    month_start = today.replace(day=1)
    for service_point_id, hours in groupby(
        statistics, key=lambda statistic: statistic.service_point_id
    ):
        days: dict[date, tuple[Decimal, int]] = {}
        cumulative = Decimal()
        for statistic in hours:
            quantity = statistic.sum_kwh - cumulative
            cumulative = statistic.sum_kwh
            local_start = statistic.start.astimezone(_PERTH)
            day = local_start.date()
            if day >= today:
                continue
            total, hour_slots = days.get(day, (Decimal(), 0))
            days[day] = (total + quantity, hour_slots | (1 << local_start.hour))

        complete_days = {
            day: total
            for day, (total, hour_slots) in days.items()
            if hour_slots == _COMPLETE_DAY_HOURS
        }
        latest_date = max(complete_days, default=None)
        latest_day = None
        last_7_days = None
        month_to_date = None
        if latest_date is not None:
            latest_day = ConsumptionPeriod(
                latest_date, latest_date, complete_days[latest_date]
            )
            last_7_days = _consumption_period(
                complete_days, latest_date - timedelta(days=6), latest_date
            )
            if latest_date >= month_start:
                month_to_date = _consumption_period(
                    complete_days, month_start, latest_date
                )
        summaries.append(
            ConsumptionSummary(
                service_point_id=service_point_id,
                total_import_kwh=cumulative,
                latest_day=latest_day,
                last_7_days=last_7_days,
                month_to_date=month_to_date,
            )
        )
    return tuple(summaries)
