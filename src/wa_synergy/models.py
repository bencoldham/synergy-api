"""Immutable public models and normalized usage invariants."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

from .errors import ConfigurationError, UsageValidationError


def _query_date(value: date | str, *, field: str) -> date:
    if isinstance(value, datetime):
        raise ConfigurationError(f"UsageQuery {field} must be a calendar date")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ConfigurationError(
            f"UsageQuery {field} must be a date or YYYY-MM-DD string"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationError(
            f"UsageQuery {field}={value!r} is not YYYY-MM-DD: {exc}"
        ) from exc
    if parsed.isoformat() != value:
        raise ConfigurationError(
            f"UsageQuery {field}={value!r} is not canonical YYYY-MM-DD"
        )
    return parsed


def _identifier_filter(value: Iterable[str], *, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise ConfigurationError(
            f"UsageQuery {field} must be a collection of identifiers"
        )
    try:
        identifiers = tuple(value)
    except TypeError as exc:
        raise ConfigurationError(
            f"UsageQuery {field} is not an iterable of identifiers: {exc}"
        ) from exc
    for identifier in identifiers:
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier != identifier.strip()
        ):
            raise ConfigurationError(
                f"UsageQuery {field} must contain non-empty string identifiers"
            )
    if len(set(identifiers)) != len(identifiers):
        raise ConfigurationError(f"UsageQuery {field} must not contain duplicates")
    return identifiers


def _required_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise UsageValidationError(f"UsageInterval {field} must be a non-empty string")


def _utc_datetime(value: datetime, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise UsageValidationError(f"UsageInterval {field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class UsageQuery:
    """A portal-local calendar-date range with exclusive ``end`` semantics.

    ``start`` is included and ``end`` is excluded. Account and service-point filters
    are optional; empty tuples mean every accessible value must be discovered rather
    than selecting an arbitrary first result.
    """

    start: date | str
    end: date | str
    account_ids: tuple[str, ...] = ()
    service_point_ids: tuple[str, ...] = ()
    interval_type: str = "DAILY"

    def __post_init__(self) -> None:
        start = _query_date(self.start, field="start")
        end = _query_date(self.end, field="end")
        if end <= start:
            raise ConfigurationError("UsageQuery end must be later than start")
        if not isinstance(self.interval_type, str) or self.interval_type.upper() not in (
            "DAILY",
            "MONTH",
        ):
            raise ConfigurationError(
                f"UsageQuery interval_type must be 'DAILY' or 'MONTH', got {self.interval_type!r}"
            )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "interval_type", self.interval_type.upper())
        object.__setattr__(
            self,
            "account_ids",
            _identifier_filter(self.account_ids, field="account_ids"),
        )
        object.__setattr__(
            self,
            "service_point_ids",
            _identifier_filter(self.service_point_ids, field="service_point_ids"),
        )

@dataclass(frozen=True, slots=True)
class UsageInterval:
    """One normalized channel quantity over a discrete interval.

    Rows have canonical kWh units and UTC boundaries. ``meter_id`` remains optional
    because the observed provider response aggregates rows across its device list and
    does not establish per-row meter attribution. ``channel`` preserves the observed
    tariff/solar channel, so future provider fields cannot be silently merged into an
    existing quantity.

    ``record_identity`` is the stable persistence key unless later provider discovery
    establishes a durable record identifier.
    """

    UNIT: ClassVar[str] = "kWh"
    DURATION: ClassVar[timedelta] = timedelta(minutes=30)

    account_id: str
    service_point_id: str
    meter_id: str | None
    channel: str
    interval_start: datetime
    interval_end: datetime
    consumption_kwh: Decimal
    quality: str | None = None
    source_updated_at: datetime | None = None

    def __post_init__(self) -> None:
        _required_identifier(self.account_id, field="account_id")
        _required_identifier(self.service_point_id, field="service_point_id")
        if self.meter_id is not None:
            _required_identifier(self.meter_id, field="meter_id")
        _required_identifier(self.channel, field="channel")

        interval_start = _utc_datetime(self.interval_start, field="interval_start")
        interval_end = _utc_datetime(self.interval_end, field="interval_end")
        if interval_end <= interval_start:
            raise UsageValidationError(
                "UsageInterval interval_end must be after interval_start"
            )
        object.__setattr__(self, "interval_start", interval_start)
        object.__setattr__(self, "interval_end", interval_end)

        if (
            not isinstance(self.consumption_kwh, Decimal)
            or not self.consumption_kwh.is_finite()
        ):
            raise UsageValidationError(
                "UsageInterval consumption_kwh must be a finite Decimal"
            )
        if self.quality is not None and (
            not isinstance(self.quality, str) or not self.quality
        ):
            raise UsageValidationError(
                "UsageInterval quality must be a non-empty string or None"
            )
        if self.source_updated_at is not None:
            object.__setattr__(
                self,
                "source_updated_at",
                _utc_datetime(self.source_updated_at, field="source_updated_at"),
            )

    @property
    def record_identity(self) -> tuple[str, str, str | None, str, datetime, datetime]:
        """Return the documented composite identity for idempotent persistence."""

        return (
            self.account_id,
            self.service_point_id,
            self.meter_id,
            self.channel,
            self.interval_start,
            self.interval_end,
        )


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Counts produced by one complete transactional synchronization."""

    inserted: int
    updated: int
    unchanged: int

    def __post_init__(self) -> None:
        for field in ("inserted", "updated", "unchanged"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"SyncResult {field} must be a non-negative integer")

    @property
    def total(self) -> int:
        """Return the number of normalized records considered by the sync."""

        return self.inserted + self.updated + self.unchanged
