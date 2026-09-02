"""Validation and normalization for the captured Synergy usage response."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import NoReturn, cast
from zoneinfo import ZoneInfo

from .errors import UsageValidationError
from .models import UsageInterval, UsageQuery

_PERTH = ZoneInfo("Australia/Perth")
_PROVIDER_UNIT = "KWH"
_IMPORT_CHANNELS = frozenset({"OFF_PEAK", "PEAK", "SUPER_OFFPEAK"})
_ROW_FIELDS = frozenset({"BillingStatus", "VAL_DAY", "VAL_SOLAR", "VAL_TIME"})
_DECIMAL_STRING = re.compile(r"[+-]?\d+(?:\.\d{1,3})?\Z")
_POSSIBLE_CHANNEL = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_DEVICE_ID = re.compile(r"[A-Za-z0-9]{18}\Z")


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("non-finite JSON constant")


def _loads_decimal(document: object, *, state: str) -> object:
    if not isinstance(document, str):
        raise UsageValidationError(f"{state} must be a JSON string")
    try:
        return json.loads(
            document,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        raise UsageValidationError(f"{state} is not valid JSON") from None


def _object(value: object, *, state: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UsageValidationError(f"{state} must be an object")
    return cast(dict[str, object], value)


def _required_object(
    value: dict[str, object], key: str, *, state: str
) -> dict[str, object]:
    if key not in value:
        raise UsageValidationError(f"{state} is missing {key}")
    return _object(value[key], state=f"{state} {key}")


def _required_string(value: dict[str, object], key: str, *, state: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise UsageValidationError(f"{state} {key} must be a non-empty string")
    return candidate


def _decode_usage_response(response_text: str) -> dict[str, object]:
    """Decode the two-layer Aura response without exposing it publicly."""

    envelope = _object(
        _loads_decimal(response_text, state="Aura response"),
        state="Aura response",
    )
    actions = envelope.get("actions")
    if not isinstance(actions, list) or len(actions) != 1:
        raise UsageValidationError("Aura response actions must contain one action")
    action = _object(actions[0], state="Aura action")
    if action.get("state") != "SUCCESS":
        raise UsageValidationError("Aura action state is not successful")
    return_value = _required_object(action, "returnValue", state="Aura action")
    nested_document = return_value.get("returnValue")
    decoded = _object(
        _loads_decimal(nested_document, state="Aura nested response"),
        state="Aura nested response",
    )
    return _required_object(decoded, "IPResult", state="Aura nested response")


def _validate_context(
    *, account_id: str, service_point_id: str, query: UsageQuery
) -> None:
    for value, field in (
        (account_id, "account_id"),
        (service_point_id, "service_point_id"),
    ):
        if not isinstance(value, str) or not value or value != value.strip():
            raise UsageValidationError(
                f"Normalization {field} must be a non-empty string"
            )
    if not isinstance(query, UsageQuery):
        raise UsageValidationError("Normalization query must be a UsageQuery")
    if query.account_ids and account_id not in query.account_ids:
        raise UsageValidationError("Normalization account_id is outside the query")
    if query.service_point_ids and service_point_id not in query.service_point_ids:
        raise UsageValidationError(
            "Normalization service_point_id is outside the query"
        )


def _validate_provider_unit(provider_unit: str) -> None:
    if provider_unit != _PROVIDER_UNIT:
        raise UsageValidationError("Provider usage unit is not the captured KWH unit")


def _parse_day(value: object) -> date:
    if not isinstance(value, str):
        raise UsageValidationError("Usage row VAL_DAY must be a YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise UsageValidationError(
            "Usage row VAL_DAY must be a valid YYYY-MM-DD date"
        ) from None
    if parsed.isoformat() != value:
        raise UsageValidationError("Usage row VAL_DAY must be a valid YYYY-MM-DD date")
    return parsed


def _parse_time(value: object) -> time:
    if not isinstance(value, str) or len(value) != 4 or not value.isascii():
        raise UsageValidationError("Usage row VAL_TIME must use HHMM format")
    if not value.isdigit():
        raise UsageValidationError("Usage row VAL_TIME must use HHMM format")
    hour = int(value[:2])
    minute = int(value[2:])
    if hour > 23 or minute not in (0, 30):
        raise UsageValidationError(
            "Usage row VAL_TIME must identify a 30-minute boundary"
        )
    return time(hour=hour, minute=minute)


def _parse_quantity(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str) or _DECIMAL_STRING.fullmatch(value) is None:
        raise UsageValidationError(
            f"Usage row {field} must be a decimal string with at most three places"
        )
    quantity = Decimal(value)
    if not quantity.is_finite():
        raise UsageValidationError(f"Usage row {field} must be finite")
    return quantity


def _looks_like_unknown_channel(field: str, value: object) -> bool:
    if _POSSIBLE_CHANNEL.fullmatch(field) is None:
        return False
    if isinstance(value, Decimal):
        return True
    return isinstance(value, str) and _DECIMAL_STRING.fullmatch(value) is not None


def _validate_devices(chart_data: dict[str, object]) -> None:
    devices = chart_data.get("Devices")
    if not isinstance(devices, list):
        raise UsageValidationError("ChartData Devices must be a list")
    if any(
        not isinstance(device, str) or _DEVICE_ID.fullmatch(device) is None
        for device in devices
    ):
        raise UsageValidationError(
            "ChartData Devices must contain 18-character identifiers"
        )
    if len(set(devices)) != len(devices):
        raise UsageValidationError("ChartData Devices must not contain duplicates")


def _validate_chart(ip_result: dict[str, object]) -> dict[str, object]:
    if ip_result.get("ChartType") != "INTERVAL_DATA":
        raise UsageValidationError("IPResult ChartType is not INTERVAL_DATA")
    if ip_result.get("IntervalType") != "DAILY":
        raise UsageValidationError("IPResult IntervalType is not DAILY")
    chart_data = _required_object(ip_result, "ChartData", state="IPResult")
    if chart_data.get("errorCode") != "INVOKE-200" or chart_data.get("error") != "OK":
        raise UsageValidationError("ChartData does not contain a successful result")
    _validate_devices(chart_data)
    return chart_data


def _normalize_row(
    row: dict[str, object],
    *,
    account_id: str,
    service_point_id: str,
    query: UsageQuery,
) -> tuple[UsageInterval, UsageInterval]:
    missing = _ROW_FIELDS - row.keys()
    if missing:
        raise UsageValidationError("Usage row is missing a required field")

    import_channels = _IMPORT_CHANNELS & row.keys()
    if len(import_channels) != 1:
        raise UsageValidationError(
            "Usage row must contain exactly one captured import channel"
        )
    for field in row.keys() - _ROW_FIELDS - _IMPORT_CHANNELS:
        if _looks_like_unknown_channel(field, row[field]):
            raise UsageValidationError("Usage row contains an unknown quantity channel")

    billing_status = _required_string(row, "BillingStatus", state="Usage row")
    day = _parse_day(row["VAL_DAY"])
    start_date = cast(date, query.start)
    end_date = cast(date, query.end)
    if not start_date <= day < end_date:
        raise UsageValidationError("Usage row is outside the requested date range")
    row_time = _parse_time(row["VAL_TIME"])
    local_start = datetime.combine(day, row_time, tzinfo=_PERTH)
    interval_start = local_start.astimezone(UTC)
    interval_end = (local_start + timedelta(minutes=30)).astimezone(UTC)

    import_channel = next(iter(import_channels))
    return (
        UsageInterval(
            account_id=account_id,
            service_point_id=service_point_id,
            meter_id=None,
            channel=import_channel,
            interval_start=interval_start,
            interval_end=interval_end,
            consumption_kwh=_parse_quantity(row[import_channel], field=import_channel),
            quality=billing_status,
            source_updated_at=None,
        ),
        UsageInterval(
            account_id=account_id,
            service_point_id=service_point_id,
            meter_id=None,
            channel="VAL_SOLAR",
            interval_start=interval_start,
            interval_end=interval_end,
            consumption_kwh=_parse_quantity(row["VAL_SOLAR"], field="VAL_SOLAR"),
            quality=billing_status,
            source_updated_at=None,
        ),
    )


def _interval_sort_key(
    interval: UsageInterval,
) -> tuple[str, str, tuple[bool, str], datetime, str, datetime]:
    meter = (interval.meter_id is not None, interval.meter_id or "")
    return (
        interval.account_id,
        interval.service_point_id,
        meter,
        interval.interval_start,
        interval.channel,
        interval.interval_end,
    )


def merge_usage_intervals(
    intervals: Iterable[UsageInterval],
) -> tuple[UsageInterval, ...]:
    """Collapse exact duplicates, reject conflicts, and sort deterministically."""

    records: dict[
        tuple[str, str, str | None, str, datetime, datetime], UsageInterval
    ] = {}
    for interval in intervals:
        if not isinstance(interval, UsageInterval):
            raise UsageValidationError("Normalized usage contains an invalid record")
        existing = records.get(interval.record_identity)
        if existing is not None and existing != interval:
            raise UsageValidationError(
                "Normalized usage contains a conflicting duplicate"
            )
        records[interval.record_identity] = interval
    return tuple(sorted(records.values(), key=_interval_sort_key))


def normalize_usage_response(
    response_text: str,
    *,
    account_id: str,
    service_point_id: str,
    query: UsageQuery,
    provider_unit: str = _PROVIDER_UNIT,
) -> tuple[UsageInterval, ...]:
    """Validate one complete Aura usage response and return normalized records."""

    _validate_context(
        account_id=account_id,
        service_point_id=service_point_id,
        query=query,
    )
    _validate_provider_unit(provider_unit)
    chart_data = _validate_chart(_decode_usage_response(response_text))
    response = chart_data.get("Response")
    if not isinstance(response, list):
        raise UsageValidationError("ChartData Response must be a list")

    seen_rows: dict[tuple[date, time], dict[str, object]] = {}
    intervals: list[UsageInterval] = []
    for value in response:
        row = _object(value, state="Usage row")
        day = _parse_day(row.get("VAL_DAY"))
        row_time = _parse_time(row.get("VAL_TIME"))
        row_identity = (day, row_time)
        existing = seen_rows.get(row_identity)
        if existing is not None:
            if existing != row:
                raise UsageValidationError(
                    "Usage response contains conflicting duplicate rows"
                )
            continue
        seen_rows[row_identity] = row
        intervals.extend(
            _normalize_row(
                row,
                account_id=account_id,
                service_point_id=service_point_id,
                query=query,
            )
        )
    return merge_usage_intervals(intervals)
