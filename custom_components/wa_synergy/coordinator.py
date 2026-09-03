"""Data coordinator and recorder import for WA Synergy."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime

from homeassistant.components.recorder.models import StatisticData, StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.unit_conversion import EnergyConverter

from .api import (
    CannotConnect,
    InvalidAuth,
    InvalidResponse,
    StatisticPoint,
    StatisticsSnapshot,
    SynergyServiceClient,
)
from .const import CORRECTION_WINDOW, DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class SynergyCoordinator(DataUpdateCoordinator[StatisticsSnapshot]):
    """Fetch service statistics and import them into the recorder."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: SynergyServiceClient,
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            always_update=True,
            config_entry=config_entry,
        )
        self._client = client
        self._initial_import = True

    async def _async_update_data(self) -> StatisticsSnapshot:
        since = None
        if not self._initial_import:
            since = datetime.now(UTC) - CORRECTION_WINDOW
        try:
            snapshot = await self._client.async_get_statistics(since)
        except InvalidAuth as exc:
            raise ConfigEntryAuthFailed(
                "WA Synergy companion-service token was rejected"
            ) from exc
        except (CannotConnect, InvalidResponse) as exc:
            raise UpdateFailed(str(exc)) from exc

        if not snapshot.status.ready and not snapshot.statistics:
            raise UpdateFailed("WA Synergy service has not completed its first sync")

        try:
            _import_statistics(self.hass, snapshot.statistics)
        except HomeAssistantError as exc:
            raise UpdateFailed(f"could not import WA Synergy statistics: {exc}") from exc
        self._initial_import = False
        return snapshot


def _import_statistics(
    hass: HomeAssistant,
    points: tuple[StatisticPoint, ...],
) -> None:
    grouped: dict[str, list[StatisticPoint]] = defaultdict(list)
    for point in points:
        grouped[point.service_point_id].append(point)

    for service_point_id, service_points in grouped.items():
        statistic_id = f"{DOMAIN}:{service_point_id.casefold()}_grid_import"
        statistics: list[StatisticData] = [
            {
                "start": point.start,
                "sum": float(point.sum_kwh),
            }
            for point in sorted(service_points, key=lambda value: value.start)
        ]
        async_add_external_statistics(
            hass,
            {
                "source": DOMAIN,
                "statistic_id": statistic_id,
                "name": f"Synergy {service_point_id} grid import",
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                "unit_class": EnergyConverter.UNIT_CLASS,
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
            },
            statistics,
        )
