"""Async client for the WA Synergy companion service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession


class SynergyServiceError(Exception):
    """Base companion-service error."""


class CannotConnect(SynergyServiceError):
    """The companion service could not be reached."""


class InvalidAuth(SynergyServiceError):
    """The companion service rejected its API token."""


class InvalidResponse(SynergyServiceError):
    """The companion service returned an invalid response."""


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """Safe companion-service status."""

    instance_id: str
    ready: bool
    syncing: bool
    last_success: datetime | None
    last_error: str | None
    data_through: datetime | None
    service_points: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StatisticPoint:
    """One cumulative hourly energy statistic."""

    service_point_id: str
    start: datetime
    sum_kwh: Decimal


@dataclass(frozen=True, slots=True)
class StatisticsSnapshot:
    """Status and statistics returned by one service request."""

    status: ServiceStatus
    statistics: tuple[StatisticPoint, ...]


class SynergyServiceClient:
    """Call the authenticated local companion service."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        api_token: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.strip().rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_token.strip()}"}

    async def _request(self, path: str) -> Any:
        try:
            async with asyncio.timeout(30):
                response = await self._session.get(
                    f"{self._base_url}{path}", headers=self._headers
                )
                return await self._decode_response(response)
        except InvalidAuth:
            raise
        except (TimeoutError, ClientError) as exc:
            raise CannotConnect("cannot connect to the WA Synergy service") from exc

    async def _decode_response(self, response: ClientResponse) -> Any:
        if response.status == 401:
            await response.read()
            raise InvalidAuth("the WA Synergy service rejected the API token")
        if response.status >= 400:
            await response.read()
            raise CannotConnect(
                f"WA Synergy service returned HTTP {response.status}"
            )
        try:
            return await response.json(content_type="application/json")
        except (ClientError, ValueError) as exc:
            raise InvalidResponse("WA Synergy service returned invalid JSON") from exc

    async def async_get_status(self) -> ServiceStatus:
        """Fetch and validate service status."""

        return _status(await self._request("/v1/status"))

    async def async_get_statistics(
        self, since: datetime | None
    ) -> StatisticsSnapshot:
        """Fetch and validate cumulative hourly statistics."""

        suffix = f"?since={since.isoformat()}" if since is not None else ""
        payload = await self._request(f"/v1/statistics{suffix}")
        if not isinstance(payload, dict):
            raise InvalidResponse("statistics response must be an object")
        status = _status(payload.get("status"))
        raw_statistics = payload.get("statistics")
        if not isinstance(raw_statistics, list):
            raise InvalidResponse("statistics must be a list")

        statistics: list[StatisticPoint] = []
        identities: set[tuple[str, datetime]] = set()
        for value in raw_statistics:
            if not isinstance(value, dict) or value.get("stream") != "grid_import":
                raise InvalidResponse("statistics contain an invalid stream")
            service_point_id = value.get("service_point_id")
            if not isinstance(service_point_id, str) or not service_point_id:
                raise InvalidResponse("statistic service point is invalid")
            start = _datetime(value.get("start"), field="statistic start")
            raw_sum = value.get("sum_kwh")
            if not isinstance(raw_sum, str):
                raise InvalidResponse("statistic sum is invalid")
            try:
                sum_kwh = Decimal(raw_sum)
            except InvalidOperation as exc:
                raise InvalidResponse("statistic sum is invalid") from exc
            if not sum_kwh.is_finite() or sum_kwh < 0:
                raise InvalidResponse("statistic sum is invalid")
            identity = (service_point_id, start)
            if identity in identities:
                raise InvalidResponse("statistics contain a duplicate hour")
            identities.add(identity)
            statistics.append(
                StatisticPoint(
                    service_point_id=service_point_id,
                    start=start,
                    sum_kwh=sum_kwh,
                )
            )
        return StatisticsSnapshot(status=status, statistics=tuple(statistics))


def _datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidResponse(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidResponse(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidResponse(f"{field} is invalid")
    return parsed


def _optional_datetime(value: object, *, field: str) -> datetime | None:
    return None if value is None else _datetime(value, field=field)


def _status(value: object) -> ServiceStatus:
    if not isinstance(value, dict):
        raise InvalidResponse("status response must be an object")
    instance_id = value.get("instance_id")
    ready = value.get("ready")
    syncing = value.get("syncing")
    service_points = value.get("service_points")
    last_error = value.get("last_error")
    if not isinstance(instance_id, str) or not instance_id:
        raise InvalidResponse("service instance ID is invalid")
    if not isinstance(ready, bool) or not isinstance(syncing, bool):
        raise InvalidResponse("service readiness state is invalid")
    if not isinstance(service_points, list) or not all(
        isinstance(item, str) and item for item in service_points
    ):
        raise InvalidResponse("service point list is invalid")
    if last_error is not None and not isinstance(last_error, str):
        raise InvalidResponse("service error state is invalid")
    return ServiceStatus(
        instance_id=instance_id,
        ready=ready,
        syncing=syncing,
        last_success=_optional_datetime(
            value.get("last_success"), field="last successful sync"
        ),
        last_error=last_error,
        data_through=_optional_datetime(value.get("data_through"), field="data through"),
        service_points=tuple(service_points),
    )
