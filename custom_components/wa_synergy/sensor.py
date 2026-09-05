"""Diagnostic sensors for WA Synergy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ServiceStatus
from .const import DOMAIN
from .coordinator import SynergyCoordinator

if TYPE_CHECKING:
    from . import SynergyRuntimeData


class SynergyStatusSensor(CoordinatorEntity[SynergyCoordinator], SensorEntity):
    """A timestamp reported by the companion service."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SynergyCoordinator,
        entry: ConfigEntry[SynergyRuntimeData],
        key: str,
        value: Callable[[ServiceStatus], datetime | None],
    ) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._value = value
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Synergy",
            name="WA Synergy",
        )

    @property
    def native_value(self) -> datetime | None:
        """Return the current timestamp."""

        return self._value(self.coordinator.data.status)


class SynergyGridImportSensor(
    CoordinatorEntity[SynergyCoordinator],
    SensorEntity,
):
    """Cumulative grid-import energy for one Synergy service point."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3
    _attr_translation_key = "grid_import"

    def __init__(
        self,
        coordinator: SynergyCoordinator,
        entry: ConfigEntry[SynergyRuntimeData],
        service_point_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._service_point_id = service_point_id
        self._attr_unique_id = (
            f"{entry.unique_id}_{service_point_id.casefold()}_grid_import"
        )
        self._attr_translation_placeholders = {
            "service_point_id": service_point_id,
        }
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Synergy",
            name="WA Synergy",
        )

    @property
    def native_value(self) -> Decimal | None:
        """Return the latest cumulative grid-import energy."""

        latest = max(
            (
                point
                for point in self.coordinator.data.statistics
                if point.service_point_id == self._service_point_id
            ),
            key=lambda point: point.start,
            default=None,
        )
        return latest.sum_kwh if latest is not None else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[SynergyRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up WA Synergy status and grid-import sensors."""

    coordinator = entry.runtime_data.coordinator
    status_sensors = (
        SynergyStatusSensor(
            coordinator,
            entry,
            "data_through",
            lambda status: status.data_through,
        ),
        SynergyStatusSensor(
            coordinator,
            entry,
            "last_success",
            lambda status: status.last_success,
        ),
    )
    energy_sensors = tuple(
        SynergyGridImportSensor(coordinator, entry, service_point_id)
        for service_point_id in coordinator.data.status.service_points
    )
    async_add_entities((*status_sensors, *energy_sensors))
