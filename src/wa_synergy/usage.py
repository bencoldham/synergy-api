"""Validation and normalization for the captured Synergy usage response."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, NoReturn, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from .errors import (
    AuthenticationContractError,
    AuthorizationError,
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
_AURA_EXECUTE_QUERY = {"aura.ApexAction.execute": "1"}
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded;charset=UTF-8"
_ACTION_DESCRIPTOR = "aura://ApexActionController/ACTION$execute"
_USAGE_CONTROLLER = "vlocity_cmt.BusinessProcessDisplayController"
_USAGE_METHOD = "GenericInvoke2NoCont"
_INTEGRATION_PROCEDURE_SERVICE = "vlocity_cmt.IntegrationProcedureService"
_CHART_PROCEDURE = "MyAccount_ChartData"
_DISCOVERY_CONTROLLER = "CommunityServiceController"
_DISCOVERY_METHOD = "getLinkedServices"

_LOGIN_PATH = "/s/login"


class _AuthenticationLost(Exception):
    """Known direct-Aura signal that requires one credential remint."""


def _is_login_redirect(response: httpx.Response) -> bool:
    if not 300 <= response.status_code < 400:
        return False
    location = response.headers.get("location")
    if not location:
        return False
    return cast(str, urlsplit(location).path).rstrip("/") == _LOGIN_PATH


def _is_login_html(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != "text/html":
        return False
    document = response.text.casefold()
    return (
        "/s/login/" in document
        and "email" in document
        and "password" in document
        and "log in" in document
    )


def _is_invalid_session_payload(response_text: str, *, controller: str) -> bool:
    controller_name = controller.rpartition(".")[2]
    try:
        envelope = json.loads(response_text)
        if not isinstance(envelope, dict):
            return False
        actions = envelope.get("actions")
        if not isinstance(actions, list) or len(actions) != 1:
            return False
        action = actions[0]
        if not isinstance(action, dict) or action.get("state") != "SUCCESS":
            return False
        return_value = action.get("returnValue")
        if not isinstance(return_value, dict):
            return False
        nested_document = return_value.get("returnValue")
        if not isinstance(nested_document, str):
            return False
        nested = json.loads(nested_document)
        if not isinstance(nested, dict):
            return False
        ip_result = nested.get("IPResult")
        if not isinstance(ip_result, dict) or set(ip_result) != {"success", "error"}:
            return False
        if ip_result.get("success") not in {False, "false"}:
            return False
        error = ip_result.get("error")
        if not isinstance(error, str):
            return False
        escaped_class = re.escape(controller_name)
        return (
            re.fullmatch(
                rf"You do not have access to the Apex class named "
                rf"['\"]?(?:vlocity_cmt\.)?{escaped_class}['\"]?\.?",
                error.strip(),
            )
            is not None
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


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


@dataclass(frozen=True, slots=True)
class _LinkedService:
    account_id: str
    service_point_id: str


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
) -> str:
    action = {
        "id": "1;a",
        "descriptor": _ACTION_DESCRIPTOR,
        "params": {
            "namespace": "",
            "classname": controller,
            "method": method,
            "params": parameters,
            "cacheable": False,
            "isContinuation": False,
        },
    }
    return json.dumps({"actions": [action]}, separators=(",", ":"))


def _usage_action_message(*, service_point_id: str, query: UsageQuery) -> str:
    start = cast(date, query.start).strftime("%Y%m%d")
    inclusive_end = (cast(date, query.end) - timedelta(days=1)).strftime("%Y%m%d")
    procedure_input = {
        "IntervalType": "DAILY",
        "ChartType": "INTERVAL_DATA",
        "ServiceId": service_point_id,
        "StartDate": start,
        "EndDate": inclusive_end,
        "PeriodStartDate": start,
        "PeriodEndDate": inclusive_end,
        "Device": [],
    }
    return _apex_action_message(
        controller=_USAGE_CONTROLLER,
        method=_USAGE_METHOD,
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
) -> str:
    _require_closed_authentication(authentication)
    try:
        response = client.post(
            _AURA_PATH,
            params=_AURA_EXECUTE_QUERY,
            data={
                "message": message,
                "aura.context": authentication.aura_context,
                "aura.token": authentication.aura_token,
            },
        )
    except httpx.HTTPError:
        raise UsageFetchError(f"Synergy {state} request failed") from None

    if response.status_code == 401 or _is_login_redirect(response):
        raise _AuthenticationLost
    invalid_session = _is_invalid_session_payload(
        response.text,
        controller=controller,
    )
    if response.status_code == 403 and invalid_session:
        raise _AuthenticationLost
    if response.status_code != 200:
        raise UsageFetchError(
            f"Synergy {state} request failed with HTTP {response.status_code}"
        )
    if _is_login_html(response):
        raise _AuthenticationLost
    content_type = response.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != "application/json":
        raise UsageValidationError(
            f"Synergy {state} response is not an Aura JSON document"
        )
    try:
        json.loads(response.text)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise _AuthenticationLost from None
    if invalid_session:
        raise _AuthenticationLost
    return response.text


def _decode_discovery_response(response_text: str) -> tuple[_LinkedService, ...]:
    envelope = _object(
        _loads_decimal(response_text, state="Aura discovery response"),
        state="Aura discovery response",
    )
    actions = envelope.get("actions")
    if not isinstance(actions, list) or len(actions) != 1:
        raise UsageValidationError(
            "Aura discovery response actions must contain one action"
        )
    action = _object(actions[0], state="Aura discovery action")
    if action.get("state") != "SUCCESS":
        raise UsageValidationError("Aura discovery action state is not successful")
    wrapper = _required_object(action, "returnValue", state="Aura discovery action")
    result = _required_object(
        wrapper,
        "returnValue",
        state="Aura discovery result wrapper",
    )
    active_services = result.get("ActiveServices")
    if not isinstance(active_services, list):
        raise UsageValidationError("Aura discovery ActiveServices must be a list")

    services: dict[str, _LinkedService] = {}
    for value in active_services:
        service = _object(value, state="Aura discovery service")
        service_point_id = _required_string(
            service,
            "Id",
            state="Aura discovery service",
        )
        if service.get("value") != service_point_id:
            raise UsageValidationError("Aura discovery service Id and value must match")
        linked = _LinkedService(
            account_id=_required_string(
                service,
                "AccountNumber",
                state="Aura discovery service",
            ),
            service_point_id=service_point_id,
        )
        existing = services.get(service_point_id)
        if existing is not None and existing != linked:
            raise UsageValidationError("Aura discovery contains a conflicting service")
        services[service_point_id] = linked
    return tuple(
        sorted(
            services.values(),
            key=lambda service: (service.account_id, service.service_point_id),
        )
    )


def discover_services(
    client: httpx.Client,
    authentication: _AuthenticationResult,
) -> tuple[_LinkedService, ...]:
    """Discover every active linked service through the direct Aura endpoint."""

    response_text = _post_aura(
        client,
        authentication,
        message=_apex_action_message(
            controller=_DISCOVERY_CONTROLLER,
            method=_DISCOVERY_METHOD,
            parameters={},
        ),
        state="service discovery",
        controller=_DISCOVERY_CONTROLLER,
    )
    return _decode_discovery_response(response_text)


def _services_for_query(
    client: httpx.Client,
    authentication: _AuthenticationResult,
    query: UsageQuery,
) -> tuple[_LinkedService, ...]:
    if len(query.account_ids) == 1 and query.service_point_ids:
        account_id = query.account_ids[0]
        return tuple(
            _LinkedService(account_id, service_point_id)
            for service_point_id in query.service_point_ids
        )

    discovered = discover_services(client, authentication)
    selected = tuple(
        service
        for service in discovered
        if (not query.account_ids or service.account_id in query.account_ids)
        and (
            not query.service_point_ids
            or service.service_point_id in query.service_point_ids
        )
    )
    selected_accounts = {service.account_id for service in selected}
    selected_service_points = {service.service_point_id for service in selected}
    if (
        set(query.account_ids) - selected_accounts
        or set(query.service_point_ids) - selected_service_points
    ):
        raise AuthorizationError(
            "Synergy discovery did not return every requested account and service"
        )
    return selected


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

    intervals: list[UsageInterval] = []
    for service in _services_for_query(client, authentication, query):
        response_text = _post_aura(
            client,
            authentication,
            message=_usage_action_message(
                service_point_id=service.service_point_id,
                query=query,
            ),
            state="usage",
            controller=_USAGE_CONTROLLER,
        )
        intervals.extend(
            normalize_usage_response(
                response_text,
                account_id=service.account_id,
                service_point_id=service.service_point_id,
                query=query,
            )
        )
    return merge_usage_intervals(intervals)
