"""GST-inclusive Synergy import tariffs effective 1 July 2026.

A unit is one kWh. K1 is consumption-tiered, so time alone cannot price it.
These rates describe the published plans, not a customer's billing history.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

CONF_PLAN = "plan"
DEFAULT_PLAN = "not_set"
EFFECTIVE_FROM = "2026-07-01"
TIME_ZONE = "Australia/Perth"
_PERTH = ZoneInfo(TIME_ZONE)
_BASE_URL = "https://www.synergy.net.au/Your-home/Energy-plans/"


@dataclass(frozen=True, slots=True)
class RatePeriod:
    """An inclusive start, exclusive end Perth-hour band, priced in AUD/kWh."""

    start_hour: int
    end_hour: int
    name: str
    price: float


@dataclass(frozen=True, slots=True)
class UsageTier:
    """A published daily consumption band, priced in AUD/kWh."""

    name: str
    lower_kwh: int
    upper_kwh: int | None
    price: float


@dataclass(frozen=True, slots=True)
class TariffPlan:
    """Published import rates and separate fixed supply charge in AUD/day."""

    name: str
    source_url: str
    supply_charge: float
    periods: tuple[RatePeriod, ...] = ()
    tiers: tuple[UsageTier, ...] = ()


PLANS = {
    "home_a1": TariffPlan(
        name="Home Plan (A1)",
        source_url=f"{_BASE_URL}Home-Plan-A1",
        supply_charge=1.192419,
        periods=(RatePeriod(0, 24, "flat", 0.332621),),
    ),
    "midday_saver": TariffPlan(
        name="Midday Saver",
        source_url=f"{_BASE_URL}Midday-Saver",
        supply_charge=1.327806,
        periods=(
            RatePeriod(0, 9, "off_peak", 0.243431),
            RatePeriod(9, 15, "super_off_peak", 0.088520),
            RatePeriod(15, 21, "peak", 0.553253),
            RatePeriod(21, 24, "off_peak", 0.243431),
        ),
    ),
    "home_business_k1": TariffPlan(
        name="Home Business Plan (K1)",
        source_url=f"{_BASE_URL}Home-Business-Plan-K1-Resi",
        supply_charge=2.104115,
        tiers=(
            UsageTier("first_20", 0, 20, 0.347481),
            UsageTier("20_to_1650", 20, 1650, 0.327455),
            UsageTier("above_1650", 1650, None, 0.369194),
        ),
    ),
    "electric_vehicle": TariffPlan(
        name="Electric Vehicle Add On",
        source_url=f"{_BASE_URL}Electric-Vehicle-Add-On",
        supply_charge=1.327806,
        periods=(
            RatePeriod(0, 6, "overnight", 0.199172),
            RatePeriod(6, 9, "off_peak", 0.243431),
            RatePeriod(9, 15, "super_off_peak", 0.088520),
            RatePeriod(15, 21, "peak", 0.553253),
            RatePeriod(21, 23, "off_peak", 0.243431),
            RatePeriod(23, 24, "overnight", 0.199172),
        ),
    ),
}

PLAN_OPTIONS = {
    DEFAULT_PLAN: "Not configured",
    **{plan_id: plan.name for plan_id, plan in PLANS.items()},
}


def _rate_period(plan_id: str, hour: int) -> RatePeriod | None:
    if plan_id == DEFAULT_PLAN:
        return None
    return next(
        (
            period
            for period in PLANS[plan_id].periods
            if period.start_hour <= hour < period.end_hour
        ),
        None,
    )


def _perth_hour(when: datetime) -> int:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("tariff timestamps must be timezone-aware")
    return when.astimezone(_PERTH).hour


def price_at(plan_id: str, when: datetime) -> float | None:
    """Return the clock-determined import price, never guess a K1 usage tier."""
    period = _rate_period(plan_id, _perth_hour(when))
    return period.price if period is not None else None


def period_at(plan_id: str, when: datetime) -> str | None:
    """Return the current Perth time band, if the plan has one."""
    period = _rate_period(plan_id, _perth_hour(when))
    return period.name if period is not None else None


def hourly_prices(plan_id: str) -> list[dict[str, int | float | str | None]]:
    """Return a recurring 24-hour Perth schedule, not historical prices."""
    schedule = []
    for hour in range(24):
        period = _rate_period(plan_id, hour)
        schedule.append(
            {
                "hour": hour,
                "price": period.price if period is not None else None,
                "period": period.name if period is not None else None,
            }
        )
    return schedule


def calculate_historical_cost(
    plan_id: str,
    points: Iterable[tuple[datetime, Decimal]],
) -> Decimal | None:
    """Backfill cumulative usage cost using the selected plan's tariff rates.

    `points` must yield `(start, sum_kwh)` in chronological order.
    Returns None if the plan is unconfigured or has no hourly price (e.g. K1).
    """
    if plan_id not in PLANS:
        return None
    total_cost = Decimal(0)
    previous_sum = Decimal(0)
    for start, cumulative in points:
        price = price_at(plan_id, start)
        if price is None:
            return None
        delta_kwh = cumulative - previous_sum
        if delta_kwh > 0:
            total_cost += delta_kwh * Decimal(str(price))
        previous_sum = cumulative
    return total_cost
