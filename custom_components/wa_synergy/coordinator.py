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
from .tariffs import CONF_PLAN, DEFAULT_PLAN, PLANS, historical_cost_points

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
        self._plan_id = config_entry.options.get(
            CONF_PLAN, config_entry.data.get(CONF_PLAN, DEFAULT_PLAN)
        )

    async def _async_update_data(self) -> StatisticsSnapshot:
        since = None
        plan = PLANS.get(self._plan_id)
        # Cost sums need the full prefix, including after provider corrections.
        if not self._initial_import and not (plan and plan.periods):
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
            _import_statistics(self.hass, snapshot.statistics, self._plan_id)
        except HomeAssistantError as exc:
            raise UpdateFailed(
                f"could not import WA Synergy statistics: {exc}"
            ) from exc
        self._initial_import = False
        return snapshot


def _import_statistics(
    hass: HomeAssistant,
    points: tuple[StatisticPoint, ...],
    plan_id: str,
) -> None:
    grouped: dict[str, list[StatisticPoint]] = defaultdict(list)
    for point in points:
        grouped[point.service_point_id].append(point)

    for service_point_id, service_points in grouped.items():
        statistic_id = f"{DOMAIN}:{service_point_id.casefold()}_grid_import"
        service_points.sort(key=lambda point: point.start)
        statistics: list[StatisticData] = [
            {
                "start": point.start,
                "sum": float(point.sum_kwh),
            }
            for point in service_points
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
        costs: list[StatisticData] = [
            {"start": start, "sum": float(total)}
            for start, total in historical_cost_points(
                plan_id,
                ((point.start, point.sum_kwh) for point in service_points),
            )
        ]
        if costs:
            async_add_external_statistics(
                hass,
                {
                    "source": DOMAIN,
                    "statistic_id": f"{statistic_id}_cost",
                    "name": f"Synergy {service_point_id} grid import cost",
                    "unit_of_measurement": "AUD",
                    "unit_class": None,
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                },
                costs,
            )
