"""Validation and normalization for the captured Synergy usage response."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, NoReturn, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from .errors import (
    AuthenticationContractError,
    UsageFetchError,
    UsageValidationError,
)
from .models import UsageInterval, UsageQuery

if TYPE_CHECKING:
    from .auth import _AuthenticationResult

_PERTH = ZoneInfo("Australia/Perth")
_PROVIDER_UNIT = "KWH"
_IMPORT_CHANNELS = frozenset({"OFF_PEAK", "PEAK", "SUPER_OFFPEAK"})
_ROW_FIELDS = frozenset({"BillingStatus", "VAL_DAY", "VAL_SOLAR", "VAL_TIME"})
_DECIMAL_STRING = re.compile(r"[+-]?\d+(?:\.\d{1,3})?\Z")
_POSSIBLE_CHANNEL = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_DEVICE_ID = re.compile(r"[A-Za-z0-9]{18}\Z")
_PORTAL_ORIGIN = "https://my.synergy.net.au"
_AURA_PATH = "/s/sfsites/aura"
_AURA_EXECUTE_QUERY = {"r": "51", "aura.ApexAction.execute": "1"}
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded;charset=UTF-8"
_ACTION_DESCRIPTOR = "aura://ApexActionController/ACTION$execute"
_USAGE_NAMESPACE = "vlocity_cmt"
_USAGE_CONTROLLER = "BusinessProcessDisplayController"
_USAGE_METHOD = "GenericInvoke2NoCont"
_INTEGRATION_PROCEDURE_SERVICE = "vlocity_cmt.IntegrationProcedureService"
_CHART_PROCEDURE = "MyAccount_ChartData"


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
    except (json.JSONDecodeError, ValueError) as exc:
        raise UsageValidationError(f"{state} is not valid JSON: {exc}") from exc


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
        raise UsageValidationError(
            f"Aura response expected one action, got {type(actions).__name__}: "
            f"{actions!r}"
        )
    action = _object(actions[0], state="Aura action")
    if action.get("state") != "SUCCESS":
        raise UsageValidationError(
            f"Aura usage action failed with state {action.get('state')!r}: {action!r}"
        )
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
        raise UsageValidationError(
            f"Provider usage unit is {provider_unit!r}; expected {_PROVIDER_UNIT!r}"
        )


def _parse_day(value: object) -> date:
    if not isinstance(value, str):
        raise UsageValidationError("Usage row VAL_DAY must be a YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise UsageValidationError(
            f"Usage row VAL_DAY is not a valid YYYY-MM-DD date: {value!r}: {exc}"
        ) from exc
    if parsed.isoformat() != value:
        raise UsageValidationError(
            f"Usage row VAL_DAY is not canonical YYYY-MM-DD: {value!r}"
        )
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
        raise UsageValidationError(
            f"IPResult ChartType is {ip_result.get('ChartType')!r}; "
            f"expected 'INTERVAL_DATA'"
        )
    if ip_result.get("IntervalType") != "WEEK":
        raise UsageValidationError(
            f"IPResult IntervalType is {ip_result.get('IntervalType')!r}; "
            f"expected 'WEEK'"
        )
    chart_data = _required_object(ip_result, "ChartData", state="IPResult")
    if chart_data.get("errorCode") != "INVOKE-200" or chart_data.get("error") != "OK":
        raise UsageValidationError(
            f"ChartData failed: errorCode={chart_data.get('errorCode')!r}, "
            f"error={chart_data.get('error')!r}"
        )
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


def _require_closed_authentication(authentication: _AuthenticationResult) -> None:
    if (
        not getattr(authentication, "browser_closed", False)
        or not getattr(authentication, "sid", None)
        or not getattr(authentication, "aura_token", None)
        or not getattr(authentication, "aura_context", None)
    ):
        raise AuthenticationContractError(
            "Direct HTTP requests require closed-browser authentication material"
        )


def create_http_client(
    authentication: _AuthenticationResult,
    *,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Create a memory-only direct client containing only the captured ``sid`` cookie."""

    _require_closed_authentication(authentication)
    hostname = urlsplit(_PORTAL_ORIGIN).hostname
    if hostname is None:
        raise AuthenticationContractError(
            "Direct HTTP portal origin does not contain a hostname"
        )
    client = httpx.Client(
        base_url=_PORTAL_ORIGIN,
        follow_redirects=False,
        transport=transport,
        trust_env=False,
    )
    client.headers.clear()
    client.headers["Content-Type"] = _FORM_CONTENT_TYPE
    client.cookies.set(
        "sid",
        authentication.sid,
        domain=hostname,
        path="/",
    )
    return client


def _apex_action_message(
    *,
    controller: str,
    method: str,
    parameters: dict[str, object],
    namespace: str = "",
) -> str:
    action = {
        "id": "1;a",
        "descriptor": _ACTION_DESCRIPTOR,
        "callingDescriptor": "UNKNOWN",
        "params": {
            "namespace": namespace,
            "classname": controller,
            "method": method,
            "params": parameters,
            "cacheable": False,
            "isContinuation": False,
        },
    }
    return json.dumps({"actions": [action]}, separators=(",", ":"))


def _usage_action_message(*, service_point_id: str, query: UsageQuery) -> str:
    start_date = cast(date, query.start)
    inclusive_end_date = cast(date, query.end) - timedelta(days=1)
    procedure_input = {
        "StartDate": start_date.strftime("%Y%m%d"),
        "EndDate": inclusive_end_date.strftime("%Y%m%d"),
        "ServiceId": service_point_id,
        "IntervalType": "WEEK",
        "ChartType": "INTERVAL_DATA",
        "Interval": "",
        "Daily": "X",
        "Monthly": "",
        "DisplayOptionValue": "Weekly",
        "Device": [],
        "PeriodStartDate": start_date.isoformat(),
        "PeriodEndDate": inclusive_end_date.isoformat(),
        "PreviousMeters": [],
        "UnbilledStartDate": start_date.isoformat(),
        "UnbilledEndDate": inclusive_end_date.isoformat(),
        "AmiMeterCount": 1,
        "OtherStartDate": None,
        "OtherEndDate": None,
    }
    return _apex_action_message(
        controller=_USAGE_CONTROLLER,
        method=_USAGE_METHOD,
        namespace=_USAGE_NAMESPACE,
        parameters={
            "input": json.dumps(procedure_input, separators=(",", ":")),
            "options": "{}",
            "sClassName": _INTEGRATION_PROCEDURE_SERVICE,
            "sMethodName": _CHART_PROCEDURE,
        },
    )


def _post_aura(
    client: httpx.Client,
    authentication: _AuthenticationResult,
    *,
    message: str,
    state: str,
    controller: str,
    method: str,
) -> str:
    _require_closed_authentication(authentication)
    response = client.post(
        _AURA_PATH,
        params=_AURA_EXECUTE_QUERY,
        headers={
            "X-SFDC-LDS-Endpoints": (
                f"ApexActionController.execute:{controller}.{method}"
            )
        },
        data={
            "message": message,
            "aura.context": authentication.aura_context,
            "aura.token": authentication.aura_token,
        },
    )

    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != "application/json":
        raise UsageValidationError(
            f"Synergy {state} response has content type {content_type!r}, "
            f"expected application/json; body: {response.text!r}"
        )
    try:
        json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise UsageFetchError(
            f"Synergy {state} response was not valid JSON: {exc}; "
            f"body: {response.text!r}"
        ) from exc
    return response.text


def fetch_usage(
    client: httpx.Client,
    authentication: _AuthenticationResult,
    query: UsageQuery,
) -> tuple[UsageInterval, ...]:
    """Fetch and normalize one complete range using direct HTTP only."""

    if not isinstance(client, httpx.Client):
        raise UsageFetchError("Direct usage retrieval requires an httpx client")
    if not isinstance(query, UsageQuery):
        raise UsageValidationError("Direct usage retrieval requires a UsageQuery")
    _require_closed_authentication(authentication)

    service_id = authentication.service_id
    response_text = _post_aura(
        client,
        authentication,
        message=_usage_action_message(
            service_point_id=service_id,
            query=query,
        ),
        state="usage",
        controller=_USAGE_CONTROLLER,
        method=_USAGE_METHOD,
    )
    intervals = normalize_usage_response(
        response_text,
        account_id=service_id,
        service_point_id=service_id,
        query=query,
    )
    if not intervals:
        raise UsageValidationError(
            f"Synergy returned no usage intervals for service {service_id!r} "
            f"from {query.start} to {query.end}; response: {response_text}"
        )
    return intervals
